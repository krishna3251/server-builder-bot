"""Offline regression tests. No Discord token or external services are used."""
import asyncio
import copy
import importlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands

import config
from cogs.server_builder import TEMPLATES, BuilderQuickstartView, CustomBuildConfirmView, PresetPickerView, ServerBuilderCog
from cogs.ticket_system import AutoDeleteManager, TicketControlView, TicketSystem, ticket_transaction
from services import economy_service as economy
from services import json_builder as builder
from services.error_service import SafeView, respond_error
from services import ai_service


class SchemaTests(unittest.TestCase):
    def test_all_presets_remain_valid(self):
        for schema in TEMPLATES.values():
            builder._validate_schema(schema)

    def test_malformed_nested_data_is_rejected(self):
        cases = [
            {'categories': [{'name': 'Private', 'permission_overwrites': [42]}]},
            {'categories': [{'name': 'Private', 'permission_overwrites': [{'role': 'Missing', 'deny': ['view_channel']}]}]},
            {'roles': [{'name': 'Admin', 'permissions': ['invented_permission']}]},
            {'roles': [{'name': 'Admin', 'permissions': ['ADMINISTRATOR']}], 'auto_assign': 'Admin'},
            {'roles': [{'name': 'Mod', 'permissions': ['manage_messages']}], 'auto_assign': 'Mod'},
            {'auto_assign': 'Missing'},
            {'roles': [{'name': 'Bad', 'color': '#1000000'}]},
            {'categories': [{'name': 'Voice', 'channels': [{'name': 'vc', 'type': 'voice', 'bitrate': 'bad'}]}]},
            {'categories': [{'name': 'Chat', 'channels': [{'name': 'chat', 'threads': [None]}]}]},
            {'categories': [{'name': 'Chat', 'channels': [{'name': 'chat', 'slowmode': True}]}]},
            {'categories': [{'name': 'Chat', 'channels': [{'name': 'chat', 'topic': 'x' * 1025}]}]},
            {'categories': [{'name': 'Chat', 'channels': [{'name': 'chat', 'threads': [{'name': 'thread', 'auto_archive': 12}]}]}]},
        ]
        for schema in cases:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                builder._validate_schema(schema)

    def test_safe_auto_assignment_is_allowed(self):
        builder._validate_schema({'roles': [{'name': 'Member', 'permissions': ['read_messages']}], 'auto_assign': 'Member'})


class BuildTests(unittest.IsolatedAsyncioTestCase):
    def guild(self):
        guild = MagicMock()
        guild.id = 1
        guild.name = 'Example'
        guild.me.guild_permissions = discord.Permissions.all()
        guild.channels = []
        guild.roles = []
        return guild

    async def test_busy_build_cannot_delete_resources(self):
        guild = self.guild()
        channel = SimpleNamespace(id=99, delete=AsyncMock())
        guild.channels = [channel]
        async with builder.build_session(guild.id):
            with self.assertRaises(RuntimeError):
                await builder.build_server(guild, {}, clean_existing=True, safe_channel_id=99)
        channel.delete.assert_not_awaited()

    async def test_guilds_can_build_independently(self):
        async with builder.build_session(1):
            async with builder.build_session(2):
                self.assertEqual(len(builder._BUILD_LOCKS), 2)
        self.assertFalse(builder._BUILD_LOCKS)

    async def test_bad_schema_cannot_trigger_cleanup(self):
        guild = self.guild()
        channel = SimpleNamespace(id=99, delete=AsyncMock())
        guild.channels = [channel]
        with self.assertRaises(ValueError):
            await builder.build_server(guild, {'roles': [42]}, clean_existing=True, safe_channel_id=99)
        channel.delete.assert_not_awaited()

    async def test_missing_permissions_cannot_trigger_cleanup(self):
        guild = self.guild()
        guild.me.guild_permissions = discord.Permissions.none()
        channel = SimpleNamespace(id=99, delete=AsyncMock())
        guild.channels = [channel]
        with self.assertRaises(ValueError):
            await builder.build_server(guild, {}, clean_existing=True, safe_channel_id=99)
        channel.delete.assert_not_awaited()

    async def test_channel_overwrites_keep_category_privacy(self):
        guild = self.guild()
        guild.create_category = AsyncMock(return_value=MagicMock())
        guild.create_text_channel = AsyncMock(return_value=MagicMock(id=50))
        schema = {'roles': [{'name': 'Member'}], 'categories': [{
            'name': 'Private', 'permission_overwrites': [{'role': '@everyone', 'deny': ['read_messages']}],
            'channels': [{'name': 'chat', 'permission_overwrites': [
                {'role': 'Member', 'allow': ['send_messages']},
                {'role': '@everyone', 'allow': ['send_messages']},
            ]}],
        }]}
        guild.create_role = AsyncMock(return_value=MagicMock())
        await builder.build_server(guild, schema)
        overwrites = guild.create_text_channel.call_args.kwargs['overwrites']
        self.assertFalse(overwrites[guild.default_role].view_channel)
        self.assertEqual(len(overwrites), 2)

    async def test_partial_creation_rolls_back(self):
        guild = self.guild()
        role = MagicMock()
        role.delete = AsyncMock()
        guild.create_role = AsyncMock(return_value=role)
        guild.create_category = AsyncMock(side_effect=RuntimeError('API unavailable'))
        with self.assertRaises(RuntimeError):
            await builder.build_server(guild, {'roles': [{'name': 'Member'}], 'categories': [{'name': 'Chat'}]})
        role.delete.assert_awaited_once()
        self.assertFalse(builder._BUILD_LOCKS)


class EconomyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(config, WALLETS_FILE=str(Path(self.temp.name) / 'wallets.json'), SHOP_FILE=str(Path(self.temp.name) / 'shop.json'))
        self.paths.start()
        economy._wallets = {}
        economy._loaded = False
        economy._wallet_lock = asyncio.Lock()

    async def asyncTearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    async def test_concurrent_claims_award_once(self):
        results = await asyncio.gather(*(economy.claim_reward(1, 2, 'daily', 200) for _ in range(20)))
        self.assertEqual(sum(result[0] for result in results), 1)
        self.assertEqual(await economy.get_balance(1, 2), 200)
        saved = json.loads(Path(config.WALLETS_FILE).read_text())
        self.assertEqual(saved['1_2']['balance'], 200)

    async def test_work_and_daily_have_separate_cooldowns(self):
        self.assertTrue((await economy.claim_reward(1, 2, 'daily', 200))[0])
        self.assertTrue((await economy.claim_reward(1, 2, 'work', 50))[0])
        self.assertEqual(await economy.get_balance(1, 2), 250)

    async def test_concurrent_transfers_cannot_overspend(self):
        await economy.add_balance(1, 2, 100)
        results = await asyncio.gather(*(economy.transfer(1, 2, 3, 80) for _ in range(10)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(await economy.get_balance(1, 2), 20)
        self.assertEqual(await economy.get_balance(1, 3), 80)

    async def test_invalid_transfers_do_not_change_balances(self):
        await economy.add_balance(1, 2, 100)
        self.assertFalse(await economy.transfer(1, 2, 3, -10))
        self.assertFalse(await economy.transfer(1, 2, 2, 20))
        self.assertEqual(await economy.get_balance(1, 2), 100)

    async def test_failed_save_restores_memory(self):
        await economy.add_balance(1, 2, 100)
        with patch.object(economy, '_save_wallets', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await economy.claim_reward(1, 2, 'daily', 200)
        self.assertEqual(await economy.get_balance(1, 2), 100)
        self.assertIsNone(await economy.get_last_daily(1, 2))

    async def test_corrupt_wallet_file_is_not_overwritten(self):
        Path(config.WALLETS_FILE).write_text('{broken')
        with self.assertRaises(RuntimeError):
            await economy.add_balance(1, 2, 200)
        self.assertEqual(Path(config.WALLETS_FILE).read_text(), '{broken')


class UITests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, *, admin=True):
        interaction = MagicMock()
        interaction.user.id = 1
        interaction.user.guild_permissions.administrator = admin
        interaction.guild_id = 1
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_preview_ownership_permissions_and_double_submit(self):
        cog = SimpleNamespace(_execute_custom_build=AsyncMock())
        view = CustomBuildConfirmView(cog, TEMPLATES['gaming'], owner_id=1, command_channel_id=10)
        interaction = self.interaction(admin=False)
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.guild_permissions.administrator = True
        interaction.user.id = 2
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id = 1
        with patch('cogs.server_builder.preflight_build'):
            await view.confirm.callback(interaction)
            await view.confirm.callback(interaction)
        cog._execute_custom_build.assert_awaited_once()
        self.assertTrue(view.is_finished())

    async def test_large_preview_pages_fit_embed_limits(self):
        schema = {'roles': [{'name': str(i) + 'x' * 90, 'permissions': ['send_messages']} for i in range(40)], 'categories': [
            {'name': str(i) + 'x' * 90, 'channels': [{'name': 'x' * 100} for _ in range(5)]} for i in range(20)
        ]}
        cog = ServerBuilderCog(MagicMock())
        view = CustomBuildConfirmView(cog, schema, owner_id=1, command_channel_id=10)
        for section in ('overview', 'channels', 'roles'):
            view.section = section
            for page in range(view.detail_page_count):
                view.detail_page = page
                embed = view.review_embed()
                self.assertLessEqual(len(embed), 6000)
                for field in embed.fields:
                    self.assertLessEqual(len(field.value), 1024)
        view.stop()

    async def test_errors_after_defer_use_private_followup(self):
        interaction = self.interaction()
        interaction.response.is_done.return_value = True
        with self.assertLogs('bot.errors', level='ERROR'):
            await respond_error(interaction, RuntimeError('private detail'), context='Test')
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertNotIn('private detail', kwargs['embed'].description)

    async def test_persistent_ticket_view_is_not_globally_claimed(self):
        first, second = TicketControlView(), TicketControlView()
        first.claim_ticket.disabled = True
        self.assertFalse(second.claim_ticket.disabled)
        self.assertTrue(second.is_persistent())
        first.stop(); second.stop()

    async def test_all_cogs_register_offline(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        await bot._async_setup_hook()
        with patch.object(config, 'MONGO_URI', 'mongodb://localhost:27017'):
            for extension in ('ai', 'analytics', 'economy', 'leveling', 'moderation', 'server_builder', 'ticket_system', 'utility'):
                await bot.load_extension('cogs.' + extension)
        self.assertEqual(len(bot.extensions), 8)
        self.assertIsNotNone(bot.tree.get_command('diagnostics'))
        await bot.close()


class AIIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_generation_ignores_personal_chat_history(self):
        memory = {'2': {'persona': 'sarcastic', 'history': [{'role': 'user', 'content': 'private chat'}]}}
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={'choices': [{'message': {'content': '{"roles": []}'}}]})
        request_context = MagicMock()
        request_context.__aenter__ = AsyncMock(return_value=response)
        request_context.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.post.return_value = request_context
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(config, 'OPENROUTER_API_KEY', 'test-placeholder'), patch.object(ai_service, '_memory', memory), patch.object(ai_service, '_save_memory', new_callable=AsyncMock) as save, patch.object(ai_service.aiohttp, 'ClientSession', return_value=session_context):
            reply = await ai_service.get_ai_response('Generate a study server', 2, use_history=False)
            messages = session.post.call_args.kwargs['json']['messages']
            self.assertEqual(len(messages), 2)
            self.assertNotIn('private chat', str(messages))
            self.assertEqual(reply, '{"roles": []}')
            save.assert_not_awaited()
            self.assertEqual(memory['2']['history'][0]['content'], 'private chat')


class TicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_creation_handles_duplicate_clicks_once(self):
        cog = TicketSystem.__new__(TicketSystem)
        cog.ticket_locks = {}
        cog.ticket_cooldowns = {}
        state = {'ticket_category_id': 9, 'open_tickets': {}, 'ticket_counter': 0}
        async def get_guild(_):
            await asyncio.sleep(0)
            return copy.deepcopy(state)
        async def save_guild(_, data):
            state.update(copy.deepcopy(data))
        cog.config_manager = SimpleNamespace(get_guild=get_guild, save_guild=save_guild)
        cog.notify_staff = AsyncMock()
        category = MagicMock(spec=discord.CategoryChannel)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.send = AsyncMock(return_value=SimpleNamespace(id=20))
        channel.delete = AsyncMock()
        guild = MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.name = 'Test server'
        guild.get_channel.side_effect = lambda cid: category if cid == 9 else channel if cid == 10 else None
        guild.get_role.return_value = None
        guild.create_text_channel = AsyncMock(return_value=channel)
        user = MagicMock(spec=discord.Member)
        user.id = 2
        user.name = 'tester'
        def interaction():
            return SimpleNamespace(guild=guild, user=user,
                response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()))
        await asyncio.gather(cog.create_ticket(interaction(), 'Tech'), cog.create_ticket(interaction(), 'Tech'))
        guild.create_text_channel.assert_awaited_once()
        self.assertEqual(state['open_tickets'], {'2': 10})
        self.assertEqual(state['ticket_counter'], 1)

    async def test_actual_creation_removes_orphan_on_database_failure(self):
        cog = TicketSystem.__new__(TicketSystem)
        cog.ticket_locks = {}
        cog.ticket_cooldowns = {}
        cog.config_manager = SimpleNamespace(
            get_guild=AsyncMock(return_value={'ticket_category_id': 9}),
            save_guild=AsyncMock(side_effect=RuntimeError('database unavailable')),
        )
        category = MagicMock(spec=discord.CategoryChannel)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.send = AsyncMock(return_value=SimpleNamespace(id=20))
        channel.delete = AsyncMock()
        guild = MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.get_channel.return_value = category
        guild.get_role.return_value = None
        guild.create_text_channel = AsyncMock(return_value=channel)
        user = MagicMock(spec=discord.Member)
        user.id = 2; user.name = 'tester'
        interaction = SimpleNamespace(guild=guild, user=user,
            response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        with self.assertRaises(RuntimeError):
            await cog.create_ticket(interaction, 'Tech')
        channel.delete.assert_awaited_once()

    async def test_guild_transactions_serialize_updates(self):
        class Store:
            def __init__(self):
                self.ticket_locks = {}; self.active = 0; self.peak = 0
            @ticket_transaction
            async def update(self, interaction):
                self.active += 1
                self.peak = max(self.peak, self.active)
                await asyncio.sleep(0)
                self.active -= 1
        store = Store()
        def interaction():
            return SimpleNamespace(guild=SimpleNamespace(id=1), response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()))
        await asyncio.gather(*(store.update(interaction()) for _ in range(10)))
        self.assertEqual(store.peak, 1)

    async def test_restores_persisted_deletion_deadline(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        guild = MagicMock(); guild.get_channel.return_value = channel
        cog = TicketSystem.__new__(TicketSystem)
        cog.bot = SimpleNamespace(wait_until_ready=AsyncMock(), get_guild=lambda _: guild)
        doc = {'_id': '1', 'pending_deletions': {'10': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}}
        async def cursor():
            yield doc
        cog.config_manager = SimpleNamespace(col=SimpleNamespace(find=lambda _: cursor()))
        cog._schedule_deletion = MagicMock()
        await cog._restore_deletions()
        cog._schedule_deletion.assert_called_once()
        restored_channel, delay = cog._schedule_deletion.call_args.args
        self.assertIs(restored_channel, channel)
        self.assertGreater(delay, 50)
        self.assertLessEqual(delay, 60)

    async def test_deletion_cancellation_keeps_persisted_deadline(self):
        manager = AutoDeleteManager(SimpleNamespace(delete_key=AsyncMock()))
        channel = SimpleNamespace(id=10, guild=SimpleNamespace(id=1), send=AsyncMock(), delete=AsyncMock())
        task = asyncio.create_task(manager.schedule_delete(channel, 60, None, b'', 'ticket'))
        manager.pending_deletes[10] = task
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        manager.config_manager.delete_key.assert_not_awaited()
        self.assertNotIn(10, manager.pending_deletes)


if __name__ == '__main__':
    unittest.main()

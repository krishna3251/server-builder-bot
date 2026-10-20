"""Command-level offline checks; fake interactions never contact Discord."""
import asyncio
import ast
import copy
import inspect
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
from cogs.json_tools import JsonTools, JsonReferenceView, plan_file
from cogs.utility import UtilityCog
from cogs.moderation import ClearWarningsConfirmView
from services.template_service import example_plan, parse_plan, read_plan_attachment, generate_plan
from services.ticket_store import TicketStore
from services.reminder_service import ReminderService
from services import ai_service, economy_service, level_service
from utils.helpers import parse_duration
from utils.interactions import respond, ResultView
from utils.polls import PollView


class FakeInteraction:
    def __init__(self, *, guild=True):
        self.extras = {}
        self.guild_id = 10 if guild else None
        self.guild = MagicMock(spec=discord.Guild) if guild else None
        if self.guild:
            self.guild.id = 10
        self.user = MagicMock(spec=discord.Member)
        self.user.id = 1
        self.command = None
        self.response = SimpleNamespace(is_done=lambda: self.response.type is not None, type=None)
        async def defer(**kwargs): self.response.type = discord.InteractionResponseType.deferred_channel_message
        self.response.defer = AsyncMock(side_effect=defer)
        self.response.send_message = AsyncMock()
        self.response.edit_message = AsyncMock()
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock()
        self.original_response = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))


class FormatTests(unittest.TestCase):
    def test_examples_are_valid_and_independent(self):
        for kind in ('minimal', 'creator', 'gaming', 'community', 'study', 'business'):
            schema = example_plan(kind)
            self.assertEqual(parse_plan(json.dumps(schema)), schema)
            schema['roles'].clear()
            self.assertTrue(example_plan(kind)['roles'])

    def test_strict_json_reports_location_and_rejects_surrounding_text(self):
        for raw in ('hello {"roles": []}', '{"roles": [],}', '[]', '{}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_plan(raw)
        self.assertEqual(parse_plan('```json\n{"roles":[{"name":"Member"}]}\n```')['roles'][0]['name'], 'Member')

    def test_duration_parser_rejects_negative_zero_junk_and_overflow(self):
        for raw in ('-1h', '0s', 'later 2h', '1h junk', '999999999999999999999999d'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_duration(raw)
        self.assertEqual(parse_duration('1d 2h30m').total_seconds(), 95400)

    def test_large_xp_is_computed_without_linear_scan(self):
        xp = 9_000_000_000_000
        level = level_service.level_from_xp(xp)
        self.assertLessEqual(level_service.xp_for_level(level), xp)
        self.assertGreater(level_service.xp_for_level(level + 1), xp)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_commands_serialize_with_valid_annotations(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        await bot._async_setup_hook()
        with patch.object(config, 'MONGO_URI', ''):
            for path in Path('cogs').glob('*.py'):
                if not path.name.startswith('_'):
                    await bot.load_extension('cogs.' + path.stem)
        self.assertEqual(len(bot.extensions), 9)
        all_commands = list(bot.tree.walk_commands())
        for command in all_commands:
            with self.subTest(command=command.qualified_name):
                command.to_dict(bot.tree)
                if isinstance(command, discord.app_commands.Command):
                    self.assertTrue(getattr(command.callback, '__acknowledged__', False), command.qualified_name)
        self.assertGreaterEqual(len(all_commands), 54)
        self.assertIsNotNone(bot.tree.get_command('json_example'))
        self.assertIsNotNone(bot.tree.get_command('json_validate'))
        await bot.close()

    async def test_balance_acknowledges_before_storage_then_finishes_original(self):
        from cogs.economy import EconomyCog
        interaction = FakeInteraction()
        async def read(*args):
            interaction.response.defer.assert_awaited_once()
            return 200
        with patch.object(economy_service, 'get_balance', side_effect=read):
            await EconomyCog.balance.callback(EconomyCog(None), interaction)
        interaction.edit_original_response.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()

    async def test_server_json_always_exports_valid_reference(self):
        from cogs.server_builder import ServerBuilderCog
        interaction = FakeInteraction()
        await ServerBuilderCog.server_json.callback(ServerBuilderCog(None), interaction)
        arguments = interaction.edit_original_response.call_args.kwargs
        self.assertEqual(type(arguments['view']).__name__, 'JsonReferenceView')
        uploaded = arguments['attachments'][0]
        self.assertTrue(parse_plan(uploaded.fp.getvalue().decode())['roles'])
        uploaded.close()
        arguments['view'].stop()

    async def test_json_validation_never_builds(self):
        interaction = FakeInteraction()
        with patch('cogs.server_builder.build_server', new_callable=AsyncMock) as build:
            await JsonTools.json_validate.callback(JsonTools(), interaction, json_text=json.dumps(example_plan()))
            build.assert_not_awaited()
        view = interaction.edit_original_response.call_args.kwargs['view']
        self.assertEqual(type(view).__name__, 'JsonReferenceView')
        view.stop()

    async def test_bad_json_gives_actionable_private_response(self):
        interaction = FakeInteraction()
        await JsonTools.json_validate.callback(JsonTools(), interaction, json_text='{"roles":,}')
        self.assertIn('line 1', interaction.edit_original_response.call_args.kwargs['embed'].description)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_wrong_file_is_rejected_before_download(self):
        attachment = SimpleNamespace(filename='template.txt', size=10, read=AsyncMock())
        with self.assertRaises(ValueError): await read_plan_attachment(attachment)
        attachment.read.assert_not_awaited()

    async def test_reference_panel_rejects_other_user(self):
        view = JsonReferenceView(example_plan(), owner_id=99)
        interaction = FakeInteraction()
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()
        view.stop()

    async def test_warning_cancel_button_has_working_embed(self):
        view = ClearWarningsConfirmView(guild_id=10, member=MagicMock(), owner_id=1)
        interaction = FakeInteraction()
        await view.cancel.callback(interaction)
        self.assertIn('cancelled', interaction.response.edit_message.call_args.kwargs['embed'].title)
        self.assertTrue(view.is_finished())

    async def test_refresh_rechecks_permissions(self):
        from cogs.economy import EconomyCog
        interaction = FakeInteraction()
        interaction.command = SimpleNamespace(name='balance', qualified_name='balance', checks=[AsyncMock(side_effect=discord.app_commands.MissingPermissions(['manage_guild']))])
        with patch.object(economy_service, 'get_balance', new_callable=AsyncMock, return_value=0) as read:
            await EconomyCog.balance.callback(EconomyCog(None), interaction)
            view = interaction.edit_original_response.call_args.kwargs['view']
            with self.assertRaises(discord.app_commands.MissingPermissions):
                await view.refresh.callback(FakeInteraction())
            self.assertEqual(read.await_count, 1)
            view.stop()

    async def test_poll_changes_vote_without_duplicate_count(self):
        view = PollView('Best game?', ['A', 'B'], owner_id=1)
        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        select._values = ['0']
        await select.callback(FakeInteraction())
        select._values = ['1']
        await select.callback(FakeInteraction())
        self.assertEqual(view.votes, {1: 1})
        self.assertIn('1 voters', view.card().footer.text)
        view.stop()

    async def test_poll_close_requires_owner_or_moderator(self):
        view = PollView('Question', ['A', 'B'], owner_id=99)
        interaction = FakeInteraction()
        interaction.user.guild_permissions.manage_messages = False
        await view.close_poll.callback(interaction)
        self.assertFalse(view.closed)
        view.stop()

    async def test_remind_returns_after_persisting_instead_of_sleeping(self):
        cog = UtilityCog(None)
        cog.reminders_service = SimpleNamespace(add=AsyncMock(return_value='reference'))
        interaction = FakeInteraction()
        await asyncio.wait_for(cog.remind.callback(cog, interaction, '1d', 'Do the thing'), 1)
        cog.reminders_service.add.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()

    async def test_diagnostics_handles_not_ready_latency(self):
        bot = SimpleNamespace(is_ready=lambda: False, latency=float('nan'), extensions={}, extension_errors={})
        interaction = FakeInteraction()
        await UtilityCog.diagnostics.callback(UtilityCog(bot), interaction)
        self.assertIn('N/A', interaction.edit_original_response.call_args.kwargs['embed'].fields[0].value)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = patch.multiple(config, DATA_DIR=self.directory.name, MONGO_URI='')
        self.settings.start()

    async def asyncTearDown(self):
        self.settings.stop(); self.directory.cleanup()

    async def test_ticket_local_mode_survives_new_store_and_dotted_deletion(self):
        store = TicketStore()
        await store.save_guild(10, {'pending_deletions': {'12': 'deadline'}, 'open_tickets': {'1': 42}})
        await store.delete_key(10, 'pending_deletions.12')
        again = TicketStore()
        data = await again.get_guild(10)
        self.assertEqual(data['open_tickets'], {'1': 42})
        self.assertEqual(data['pending_deletions'], {})
        self.assertIsNone(again.client)

    async def test_parallel_guild_ticket_saves_are_not_lost(self):
        store = TicketStore()
        await asyncio.gather(*(store.save_guild(i, {'ticket_counter': i}) for i in range(10)))
        again = TicketStore()
        for i in range(10): self.assertEqual((await again.get_guild(i))['ticket_counter'], i)

    async def test_reminder_survives_restart_and_only_owner_can_cancel(self):
        service = ReminderService(None)
        reference = await service.add(1, datetime.now(timezone.utc) + timedelta(days=1), 'hello')
        again = ReminderService(None)
        self.assertIn(reference, await again.list_for(1))
        self.assertFalse(await again.cancel(2, reference))
        self.assertTrue(await again.cancel(1, reference))
        self.assertFalse(await ReminderService(None).list_for(1))

    async def test_due_reminder_is_delivered_once_and_removed(self):
        user = SimpleNamespace(send=AsyncMock())
        service = ReminderService(SimpleNamespace(get_user=lambda _: user))
        await service.add(1, datetime.now(timezone.utc) - timedelta(seconds=1), 'hello')
        await service.deliver_due(); await service.deliver_due()
        user.send.assert_awaited_once()
        self.assertFalse(await service.list_for(1))


class AIWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_ai_plan_gets_one_repair(self):
        with patch.object(ai_service, 'get_ai_response', new_callable=AsyncMock, side_effect=['{"roles":,}', json.dumps(example_plan())]) as model:
            result = await generate_plan('Gaming club', 1)
            self.assertTrue(result['roles'])
            self.assertEqual(model.await_count, 2)
            self.assertIn('invalid', model.call_args.args[0])

    async def test_repeated_bad_ai_plan_never_becomes_buildable(self):
        with patch.object(ai_service, 'get_ai_response', new_callable=AsyncMock, return_value='not JSON') as model:
            with self.assertRaises(ValueError): await generate_plan('Gaming club', 1)
            self.assertEqual(model.await_count, 2)

    async def test_memory_is_scoped_and_old_unscoped_history_is_not_reused(self):
        self.assertNotEqual(ai_service._user_key(1, 'server-a'), ai_service._user_key(1, 'server-b'))
        self.assertNotEqual(ai_service._user_key(1), ai_service._user_key(1, 'server-a'))




class AdditionalFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_setup_does_not_require_unused_manage_threads(self):
        from cogs.ticket_system import TicketSystem
        cog = TicketSystem.__new__(TicketSystem)
        cog.ticket_locks = {}
        cog.bot = SimpleNamespace(add_view=MagicMock())
        cog.config_manager = SimpleNamespace(get_guild=AsyncMock(return_value={}), save_guild=AsyncMock())
        interaction = FakeInteraction()
        interaction.guild.me.guild_permissions = discord.Permissions(manage_channels=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)
        interaction.guild.icon = None
        interaction.guild.name = 'Example'
        interaction.channel = MagicMock(spec=discord.TextChannel)
        interaction.channel.id = 30
        interaction.channel.send = AsyncMock(return_value=SimpleNamespace(id=31, channel=SimpleNamespace(mention='#tickets'), jump_url='https://discord.com/channels/10/30/31'))
        await TicketSystem.setup_tickets.callback(cog, interaction, 'Help desk', 'Choose a category', SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3), SimpleNamespace(id=4))
        interaction.channel.send.assert_awaited_once()
        cog.config_manager.save_guild.assert_awaited_once()

    async def test_xp_boost_purchase_has_real_multiplier(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(config, WALLETS_FILE=str(Path(directory)/'wallet.json'), SHOP_FILE=str(Path(directory)/'shop.json')):
            economy_service._wallets = {}; economy_service._loaded = False; economy_service._wallet_lock = asyncio.Lock()
            await economy_service.add_balance(1, 1, 6000)
            ok, _ = await economy_service.buy_item(1, 1, 'XP Boost')
            self.assertTrue(ok)
            self.assertEqual(await economy_service.get_xp_multiplier(1, 1), 2)
            self.assertEqual(await economy_service.get_balance(1, 1), 1000)
            economy_service._wallets['1_1']['boost_until'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            self.assertEqual(await economy_service.get_xp_multiplier(1, 1), 1)

    async def test_failed_xp_save_restores_previous_value(self):
        with patch.object(level_service, '_loaded', True), patch.object(level_service, '_xp_data', {'1_1': {'xp': 100, 'level': 1}}), patch.object(level_service, '_xp_lock', asyncio.Lock()), patch.object(level_service, '_save', new_callable=AsyncMock, side_effect=OSError('full')):
            with self.assertRaises(OSError): await level_service.set_xp(1, 1, 999)
            self.assertEqual((await level_service.get_stats(1, 1))['xp'], 100)

    async def test_corrupt_local_ticket_store_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(config, DATA_DIR=directory, MONGO_URI=''):
            path = Path(directory)/'tickets'/'guild_configs.json'
            path.parent.mkdir(); path.write_text('{broken')
            with self.assertRaises(ValueError): await TicketStore().save_guild(10, {'open_tickets': {}})
            self.assertEqual(path.read_text(), '{broken')

    async def test_unauthorized_developer_sync_does_not_touch_tree(self):
        bot = SimpleNamespace(is_owner=AsyncMock(return_value=False), tree=SimpleNamespace(sync=AsyncMock()))
        interaction = FakeInteraction()
        with patch.object(config, 'DEV_IDS', []):
            await UtilityCog.sync_commands.callback(UtilityCog(bot), interaction)
        bot.tree.sync.assert_not_awaited()
        self.assertIn('Developer', interaction.edit_original_response.call_args.kwargs['embed'].title)


class ChatPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_command_is_private_and_passes_server_scope(self):
        from cogs.ai import AICog
        interaction = FakeInteraction()
        with patch('cogs.ai._rate_limited', return_value=False), patch.object(ai_service, 'get_ai_response', new_callable=AsyncMock, return_value='A helpful answer') as model:
            await AICog.ai_chat.callback(AICog(None), interaction, 'Hello')
        model.assert_awaited_once_with('Hello', 1, scope='10')
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.edit_original_response.assert_awaited_once()
        self.assertEqual(interaction.edit_original_response.call_args.kwargs['allowed_mentions'].to_dict()['parse'], [])


if __name__ == '__main__': unittest.main()

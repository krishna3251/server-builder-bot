"""Security regressions through service and real command entry points; no network."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.security import SecurityCog, SecurityConsole
from cogs.utility import CommandHelpView
from services.quarantine_service import QuarantineService, DENIED, overwrite_dict
from services.butler_store import ButlerStore
from services.moderation_notices import notify_action
from utils.interactions import respond
from utils.modern_ui import ModernResultView
from test_command_workflows import FakeInteraction


class Role:
    def __init__(self, identifier, position, **perms):
        self.id, self.position = identifier, position
        self.permissions = discord.Permissions(**perms)
        self.managed = False
    def is_default(self):
        return self.id == 10
    def __ge__(self, other):
        return self.position >= other.position
    def __gt__(self, other):
        return self.position > other.position


class Channel:
    def __init__(self, identifier, guild, overwrites=None):
        self.id, self.guild = identifier, guild
        self.overwrites = dict(overwrites or {})
        self.permissions_synced = False
        self.category_id = None
        self.permissions_for = Mock(return_value=discord.Permissions(view_channel=True, manage_channels=True, manage_roles=True))
        self.mention = f'<#{identifier}>'
        self.set_permissions = AsyncMock(side_effect=self.set_overwrite)
    async def set_overwrite(self, target, *, overwrite, reason):
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = copy.copy(overwrite)
    def overwrites_for(self, target):
        return copy.copy(self.overwrites.get(target, discord.PermissionOverwrite()))


class SecurityFixture:
    async def asyncSetUp(self):
        global SecurityCog, SecurityConsole
        from cogs.security import SecurityCog, SecurityConsole
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = QuarantineService(Path(self.directory.name) / 'quarantine.json')
        self.everyone = Role(10, 0, view_channel=True, send_messages=True)
        self.regular = Role(20, 2, view_channel=True, send_messages=True)
        self.moderator = Role(30, 4, moderate_members=True, manage_roles=True)
        self.bot_role = Role(40, 10, manage_roles=True, manage_channels=True)
        self.roles = {r.id: r for r in (self.everyone, self.regular, self.moderator, self.bot_role)}
        self.guild = SimpleNamespace(id=10, name='Guild', owner_id=1, default_role=self.everyone,
                                     roles=list(self.roles.values()), unavailable=False)
        self.actor = Mock(spec=discord.Member, id=1, guild=self.guild, top_role=self.moderator)
        self.target = Mock(id=2, bot=False, guild=self.guild, voice=None)
        self.target.roles = [self.everyone, self.regular]
        self.target.top_role = self.regular
        self.guild.owner = self.actor
        self.guild.me = Mock(id=3, top_role=self.bot_role, guild_permissions=discord.Permissions(manage_roles=True, manage_channels=True, move_members=True))
        self.members = {2: self.target}
        self.guild.get_member = self.members.get
        self.guild.get_role = self.roles.get
        self.channel = Channel(50, self.guild, {self.target: discord.PermissionOverwrite(view_channel=True, attach_files=True)})
        self.channels = {50: self.channel}
        self.guild.get_channel = self.channels.get
        self.guild.channels = list(self.channels.values())
        async def create_role(**kwargs):
            role = Role(60, 1)
            self.roles[60] = role
            return role
        async def create_channel(*args, **kwargs):
            channel = Channel(70 + len(self.channels), self.guild, kwargs.get('overwrites'))
            self.channels[channel.id] = channel
            self.guild.channels = list(self.channels.values())
            return channel
        self.guild.create_role = AsyncMock(side_effect=create_role)
        self.guild.create_category = AsyncMock(side_effect=create_channel)
        self.guild.create_text_channel = AsyncMock(side_effect=create_channel)
        self.guild.create_voice_channel = AsyncMock(side_effect=create_channel)
        async def remove(*roles, **kwargs):
            self.target.roles = [r for r in self.target.roles if r not in roles]
        async def add(*roles, **kwargs):
            self.target.roles += [r for r in roles if r not in self.target.roles]
        self.target.remove_roles = AsyncMock(side_effect=remove)
        self.target.add_roles = AsyncMock(side_effect=add)
        self.target.move_to = AsyncMock()


class QuarantineTests(SecurityFixture, unittest.IsolatedAsyncioTestCase):
    async def test_setup_is_private_idempotent_and_preserves_existing_overwrites(self):
        original = overwrite_dict(self.channel.overwrites_for(self.target))
        settings = await self.service.setup(self.guild)
        self.assertEqual(original, overwrite_dict(self.channel.overwrites_for(self.target)))
        for key in ('category', 'room'):
            self.assertFalse(self.channels[settings[key]].overwrites_for(self.everyone).view_channel)
        self.assertFalse(self.channel.overwrites_for(self.roles[60]).view_channel)
        await self.service.setup(self.guild)
        self.guild.create_role.assert_awaited_once()
        self.guild.create_category.assert_awaited_once()
        self.guild.create_text_channel.assert_awaited_once()

    async def test_quarantine_and_release_round_trip_survives_restart_and_staff_edit(self):
        settings = await self.service.setup(self.guild)
        record = await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.assertEqual(record['phase'], 'active')
        self.assertNotIn(self.regular, self.target.roles)
        self.assertFalse(self.channel.overwrites_for(self.target).view_channel)
        room = self.channels[settings['room']]
        self.assertTrue(room.overwrites_for(self.target).send_messages)
        # Another staff edit must survive release.
        edited = self.channel.overwrites_for(self.target)
        edited.attach_files = False
        self.channel.overwrites[self.target] = edited
        restarted = QuarantineService(self.service.path)
        self.assertEqual(await restarted.release(self.guild, self.actor, self.target), [])
        restored = self.channel.overwrites_for(self.target)
        self.assertTrue(restored.view_channel)
        self.assertFalse(restored.attach_files)
        self.assertNotIn(self.roles[60], self.target.roles)
        self.assertIn(self.regular, self.target.roles)
        self.assertEqual((await restarted.load())['10']['members'], {})

    async def test_partial_application_and_release_retain_recovery_journal(self):
        await self.service.setup(self.guild)
        denied = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')
        self.channel.set_permissions.side_effect = denied
        record = await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.assertEqual(record['phase'], 'partial')
        self.assertEqual(record['failed_channels'], [50])
        self.assertEqual(await self.service.release(self.guild, self.actor, self.target), [50])
        self.assertIn('2', (await self.service.load())['10']['members'])
        self.channel.set_permissions.side_effect = self.channel.set_overwrite
        await self.service.release(self.guild, self.actor, self.target)
        self.assertEqual((await self.service.load())['10']['members'], {})

    async def test_restore_rejects_role_permission_escalation_before_channel_changes(self):
        await self.service.setup(self.guild)
        await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.channel.set_permissions.reset_mock()
        self.regular.permissions = discord.Permissions(administrator=True)
        with self.assertRaisesRegex(ValueError, 'gained permissions'):
            await self.service.release(self.guild, self.actor, self.target)
        self.channel.set_permissions.assert_not_awaited()
        self.assertFalse(self.channel.overwrites_for(self.target).view_channel)

    async def test_hidden_channels_are_partial_and_never_snapshotted(self):
        await self.service.setup(self.guild)
        self.channel.permissions_for.return_value = discord.Permissions.none()
        self.channel.set_permissions.reset_mock()
        record = await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.assertEqual(record['phase'], 'partial')
        self.assertIn(50, record['failed_channels'])
        self.assertNotIn('50', record['overwrites'])
        self.channel.set_permissions.assert_not_awaited()

    async def test_owner_and_uneditable_privileged_roles_are_rejected(self):
        await self.service.setup(self.guild)
        self.target.id = 1
        with self.assertRaises(ValueError):
            await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.target.id = 2
        self.regular.managed = True
        self.regular.permissions = discord.Permissions(administrator=True)
        with self.assertRaisesRegex(ValueError, 'uneditable'):
            await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.target.remove_roles.assert_not_awaited()

    async def test_new_channel_gets_member_override_to_defeat_role_allow(self):
        await self.service.setup(self.guild)
        await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        new = Channel(100, self.guild, {self.regular: discord.PermissionOverwrite(view_channel=True)})
        self.channels[new.id] = new
        await self.service.channel_changed(new)
        self.assertFalse(new.overwrites_for(self.target).view_channel)
        self.assertIn('100', (await self.service.load())['10']['members']['2']['overwrites'])

    async def test_private_room_rejects_new_nonstaff_visibility_grants(self):
        settings = await self.service.setup(self.guild)
        room = self.channels[settings['room']]
        room.overwrites[self.regular] = discord.PermissionOverwrite(view_channel=True)
        await self.service.channel_changed(room)
        self.assertFalse(room.overwrites_for(self.regular).view_channel)

    async def test_rejoin_keeps_quarantine(self):
        await self.service.setup(self.guild)
        await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        self.target.roles = [self.everyone, self.regular]
        await self.service.rejoin(self.target)
        self.assertNotIn(self.regular, self.target.roles)
        self.assertIn(self.roles[60], self.target.roles)


class CommandSecurityTests(SecurityFixture, unittest.IsolatedAsyncioTestCase):
    async def test_create_channel_real_callback_defers_before_creation_and_private_acl(self):
        cog = SecurityCog(Mock())
        cog.quarantine_service = self.service
        interaction = FakeInteraction()
        interaction.guild = self.guild
        interaction.user.id = 1
        self.guild.me.guild_permissions = discord.Permissions(manage_channels=True, manage_roles=True)
        await SecurityCog.create_channel.callback(cog, interaction, 'staff', 'text', None, True)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.guild.create_text_channel.assert_awaited_once()
        kwargs = self.guild.create_text_channel.await_args.kwargs
        self.assertFalse(kwargs['overwrites'][self.everyone].view_channel)
        self.assertTrue(kwargs['overwrites'][interaction.user].view_channel)
        self.assertEqual(kwargs['name'], 'staff')

    async def test_created_channel_releases_seeded_member_restrictions(self):
        await self.service.setup(self.guild)
        await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        cog = SecurityCog(Mock())
        cog.quarantine_service = self.service
        interaction = FakeInteraction()
        interaction.guild = self.guild
        await SecurityCog.create_channel.callback(cog, interaction, 'new-chat')
        channel = self.channels[max(self.channels)]
        self.assertFalse(channel.overwrites_for(self.target).view_channel)
        await self.service.release(self.guild, self.actor, self.target)
        self.assertIsNone(channel.overwrites_for(self.target).view_channel)

    async def test_no_channel_created_when_permissions_are_missing(self):
        cog = SecurityCog(Mock())
        cog.quarantine_service = self.service
        interaction = FakeInteraction()
        interaction.guild = self.guild
        self.guild.me.guild_permissions = discord.Permissions.none()
        await SecurityCog.create_channel.callback(cog, interaction, 'chat')
        self.guild.create_text_channel.assert_not_awaited()

    async def test_quarantine_command_and_sudo_alias_use_same_service_and_private_receipt(self):
        cog = SecurityCog(Mock())
        cog.quarantine_service = SimpleNamespace(quarantine=AsyncMock(return_value={'phase': 'active'}))
        with patch('cogs.security.notify_action', new_callable=AsyncMock) as notice:
            for command in (SecurityCog.quarantine, SecurityCog.sudo_ban):
                interaction = FakeInteraction()
                interaction.id = 123
                await command.callback(cog, interaction, self.target, 'review')
                interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            self.assertEqual(cog.quarantine_service.quarantine.await_count, 2)
            self.assertEqual(notice.await_count, 2)

    async def test_profile_description_is_dynamic_and_credits_username(self):
        bot = SimpleNamespace(guilds=[1, 2], application_info=AsyncMock(), change_presence=AsyncMock())
        bot.application_info.return_value.description = ''
        bot.application_info.return_value.edit = AsyncMock()
        cog = SecurityCog(bot)
        await cog.refresh_profile()
        description = bot.application_info.return_value.edit.await_args.kwargs['description']
        self.assertIn('Created by luffyanna', description)
        self.assertIn('2 servers', description)
        self.assertNotIn('quarantin', description.lower())
        bot.change_presence.assert_awaited_once()

    async def test_v2_views_serialize_and_controls_reject_other_users(self):
        guide = CommandHelpView(1)
        console = SecurityConsole(Mock(), 1, 'Ready')
        for view in (guide, console):
            payload = view.to_components()
            self.assertEqual(payload[0]['type'], 17)
            self.assertLess(view.content_length(), 4000)
            interaction = FakeInteraction()
            interaction.user.id = 9
            self.assertFalse(await view.interaction_check(interaction))
            view.stop()

    async def test_standard_result_uses_components_v2_and_refresh_keeps_context(self):
        from cogs.economy import EconomyCog
        interaction = FakeInteraction()
        interaction.command = SimpleNamespace(name='balance', qualified_name='balance', checks=[])
        with patch('services.economy_service.get_balance', new_callable=AsyncMock, return_value=200):
            await EconomyCog.balance.callback(EconomyCog(None), interaction)
            args = interaction.edit_original_response.call_args.kwargs
            self.assertNotIn('embed', args)
            self.assertIsInstance(args['view'], ModernResultView)
            refresh = FakeInteraction()
            await args['view'].refresh.callback(refresh)
            updated = refresh.edit_original_response.call_args.kwargs['view']
            self.assertIsInstance(updated, ModernResultView)
            self.assertIsNotNone(updated.refresh_callback)
            updated.stop()
            args['view'].stop()


class ModerationReceiptTests(SecurityFixture, unittest.IsolatedAsyncioTestCase):
    async def test_kick_ban_and_timeout_notify_only_after_success(self):
        from cogs.moderation import ModerationCog
        self.target.guild_permissions = discord.Permissions.none()
        self.target.kick = AsyncMock()
        self.target.timeout = AsyncMock()
        self.target.display_name = 'Member'
        self.target.send = AsyncMock()
        self.guild.ban = AsyncMock()
        self.guild.me.guild_permissions = discord.Permissions.all()
        for command, args, operation, expected in (
            (ModerationCog.kick, (), self.target.kick, 'kick'),
            (ModerationCog.ban, (), self.guild.ban, 'ban'),
            (ModerationCog.mute, ('10m',), self.target.timeout, 'timeout'),
        ):
            interaction = FakeInteraction()
            interaction.guild = self.guild
            interaction.user = self.actor
            interaction.id = 234
            async def notification(*args):
                self.assertGreater(operation.await_count, 0)
                self.assertEqual(args[4], expected)
            with patch('cogs.moderation.notify_action', new_callable=AsyncMock, side_effect=notification) as notice, \
                 patch('cogs.moderation._mod_log', new_callable=AsyncMock):
                await command.callback(ModerationCog(Mock()), interaction, self.target, *args)
                notice.assert_awaited_once()
            operation.reset_mock()
            operation.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')
            with patch('cogs.moderation.notify_action', new_callable=AsyncMock) as notice:
                with self.assertRaises(discord.Forbidden):
                    await command.callback(ModerationCog(Mock()), interaction, self.target, *args)
                notice.assert_not_awaited()
            operation.side_effect = None


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_outbox_is_durable_deduplicated_and_in_daily_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'butler.sqlite3'
            store = ButlerStore(path, clock=lambda: 100)
            await store.moderation(123, 10, 1, 2, 'timeout', 'review', {1, 3})
            await store.moderation(123, 10, 1, 2, 'timeout', 'review', {1, 3})
            restarted = ButlerStore(path, clock=lambda: 100)
            jobs = await restarted.due()
            self.assertEqual(len(jobs), 2)
            self.assertEqual({item['recipient'] for item in jobs}, {1, 3})
            report = await restarted.report(10, 0, 101)
            self.assertEqual(report['counts']['moderation'], 1)
            self.assertEqual(report['moderation'][0]['action'], 'timeout')

    async def test_notice_queues_for_bot_owner_and_server_owner(self):
        service = SimpleNamespace(enabled=lambda _: True, resolve_application_owner=AsyncMock(return_value=1),
                                  store=SimpleNamespace(moderation=AsyncMock()))
        bot = SimpleNamespace(get_cog=lambda _: SimpleNamespace(service=service))
        await notify_action(bot, SimpleNamespace(id=10, name='Guild', owner_id=3),
                            SimpleNamespace(id=4), SimpleNamespace(id=5), 'ban', 'review', 123)
        self.assertEqual(service.store.moderation.await_args.args[-1], {1, 3})

    async def test_moderation_delivery_uses_application_owner_even_with_report_override(self):
        from services.butler_service import ButlerService, ButlerSettings
        with tempfile.TemporaryDirectory() as directory:
            users = {identifier: SimpleNamespace(send=AsyncMock()) for identifier in (1, 3, 99)}
            bot = SimpleNamespace(application_info=AsyncMock(return_value=SimpleNamespace(team=None, owner=SimpleNamespace(id=1))),
                                  get_user=users.get, fetch_user=AsyncMock())
            service = ButlerService(bot, Path(directory) / 'butler.sqlite3',
                                    ButlerSettings(owner_id=99), clock=lambda: 100)
            bot.get_cog = lambda _: SimpleNamespace(service=service)
            service.guilds.add(10)
            await notify_action(bot, SimpleNamespace(id=10, name='Guild', owner_id=3),
                                SimpleNamespace(id=4), SimpleNamespace(id=5), 'timeout', 'review', 321)
            await service.deliver()
            users[1].send.assert_awaited_once()
            users[3].send.assert_awaited_once()
            users[99].send.assert_not_awaited()

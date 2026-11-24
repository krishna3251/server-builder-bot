"""Offline resource recovery, migration and confirmation regressions."""
import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from cogs._quarantine_ui import QuarantineConfirmation
from services.automatic_moderation import coordinator
from services.json_builder import build_server
from services.quarantine_resources import QuarantineResources
from services.quarantine_service import QuarantineService
from test_command_workflows import FakeInteraction
from test_security import Role, SecurityFixture


class ResourceTests(SecurityFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.resources = QuarantineResources(self.service)
        self.original = await self.service.setup(self.guild)

    def add_role(self, identifier):
        role = self.roles[identifier] = Role(identifier, 1)
        self.guild.roles = list(self.roles.values())
        return role

    async def test_repeat_setup_avoids_redundant_overwrite_writes(self):
        for channel in self.channels.values():
            channel.set_permissions.reset_mock()
        await self.service.setup(self.guild)
        self.guild.create_role.assert_awaited_once()
        self.guild.create_text_channel.assert_awaited_once()
        for channel in self.channels.values():
            channel.set_permissions.assert_not_awaited()

    async def test_sync_reports_missing_room_and_repair_recreates_it_once(self):
        identifier = self.original['room']
        del self.channels[identifier]
        self.guild.channels.remove(next(channel for channel in self.guild.channels if channel.id == identifier))
        self.assertIn('room', (await self.resources.discovery(self.guild))['missing'])
        with self.assertRaisesRegex(ValueError, 'missing'):
            await self.service.setup(self.guild, create=False)
        self.guild.create_text_channel.assert_awaited_once()
        await self.service.setup(self.guild)
        await self.service.setup(self.guild)
        self.assertEqual(self.guild.create_text_channel.await_count, 2)

    async def prepare_migration(self):
        marker = self.roles[self.original['role']]
        await self.service.quarantine(self.guild, self.actor, self.target, 'review')
        replacement = self.add_role(61)
        self.guild.create_role.side_effect = None
        self.guild.create_role.return_value = replacement
        marker.permissions = discord.Permissions(send_messages=True)
        await self.service.setup(self.guild)
        return marker, replacement

    async def test_replacement_is_verified_before_previous_marker_is_removed(self):
        marker, replacement = await self.prepare_migration()
        original_remove = self.target.remove_roles.side_effect

        async def remove(*roles, **kwargs):
            self.assertIn(replacement, self.target.roles)
            self.assertFalse(self.channel.overwrites_for(self.target).view_channel)
            await original_remove(*roles, **kwargs)

        self.target.remove_roles.side_effect = remove
        self.assertEqual(await self.resources.migrate(self.guild), (['2'], {}))
        self.assertNotIn(marker, self.target.roles)
        self.assertIn(replacement, self.target.roles)

    async def test_migration_failure_has_restart_journal_and_preserves_old_role(self):
        marker, replacement = await self.prepare_migration()
        original_add = self.target.add_roles.side_effect
        self.target.add_roles.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'Denied')
        migrated, failed = await self.resources.migrate(self.guild)
        self.assertEqual(migrated, [])
        self.assertEqual(set(failed), {'2'})
        self.assertIn(marker, self.target.roles)
        self.assertNotIn(replacement, self.target.roles)
        self.assertFalse(self.channel.overwrites_for(self.target).view_channel)
        restarted = QuarantineService(self.service.path)
        self.assertEqual((await restarted.load())['10']['members']['2']['replacement_marker'], replacement.id)
        self.target.add_roles.side_effect = original_add
        self.assertEqual(await QuarantineResources(restarted).migrate(self.guild), (['2'], {}))

    async def test_cleanup_revalidates_membership_and_preserves_uncertain_roles(self):
        old = self.add_role(61)
        uncertain = self.add_role(62)
        uncertain.name = 'Quarantine (staff)'
        self.service.settings(self.guild)['managed'][str(old.id)] = 'role'
        preview = await self.resources.cleanup_plan(self.guild)
        self.assertEqual(set(preview['candidates']), {old.id})
        self.assertIn(uncertain.id, (await self.resources.discovery(self.guild))['uncertain'])
        self.target.roles.append(old)
        later = await self.resources.cleanup_plan(self.guild)
        self.assertEqual(later['skipped'][old.id], 'assigned to 2')
        with self.assertRaisesRegex(ValueError, 'changed'):
            await self.resources.cleanup(self.guild, self.actor, preview)
        old.delete.assert_not_awaited()
        self.target.roles.remove(old)
        deleted, snapshot = await self.resources.cleanup(self.guild, self.actor, preview)
        self.assertEqual(deleted, [old.id])
        saved = json.loads(self.service.path.with_name(snapshot).read_text())
        self.assertIn(str(old.id), saved['candidates'])
        uncertain.delete.assert_not_awaited()

    async def test_builder_rejects_cleanup_in_a_configured_quarantine_guild(self):
        with self.assertRaisesRegex(ValueError, 'Destructive rebuild'):
            await build_server(self.guild, {}, clean_existing=True, quarantine=self.service)

    async def test_confirmation_is_private_actor_bound_and_single_use(self):
        interaction = FakeInteraction()
        interaction.guild.owner_id = 1
        cog = Mock(run_quarantine_operation=AsyncMock(return_value=('Done', None)))
        view = QuarantineConfirmation(interaction, cog, 'cleanup', {'candidates': {61: {}}})
        stranger = FakeInteraction()
        stranger.user.id = 42
        self.assertFalse(await view.interaction_check(stranger))
        self.assertTrue(await view.interaction_check(interaction))
        await view.confirm.callback(interaction)
        self.assertFalse(await view.interaction_check(interaction))
        cog.run_quarantine_operation.assert_awaited_once()


class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_detectors_only_call_discord_once(self):
        bot = NS()
        message = NS(id=42, guild=NS(id=10), channel=NS(id=11),
                     author=NS(id=17), delete=AsyncMock())
        results = await asyncio.gather(
            coordinator(bot).remove(message, 'Butler', 'Repeat ping'),
            coordinator(bot).remove(message, 'Local AutoMod', 'Link'),
        )
        self.assertEqual(set(results), {'Deleted', 'Already handled by another listener'})
        message.delete.assert_awaited_once()

"""Regression tests for the automatic no-write channel."""
from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.soft_banned_diddy import (
    SoftBannedDiddy, STAFF_REPLY, is_admin, private_log_channel,
)


def role(identifier, **perms):
    item = SimpleNamespace(id=identifier, permissions=discord.Permissions(**perms))
    item.is_default = lambda: identifier == 1
    item.managed = False
    return item


class DiddyValidationTests(unittest.TestCase):
    def test_owner_and_admin_are_exempt_but_not_regular_member(self):
        guild = SimpleNamespace(owner_id=11)
        owner = SimpleNamespace(id=11, guild=guild, guild_permissions=discord.Permissions.none())
        admin = SimpleNamespace(id=12, guild=guild, guild_permissions=discord.Permissions(administrator=True))
        ordinary = SimpleNamespace(id=13, guild=guild, guild_permissions=discord.Permissions.none())
        self.assertTrue(is_admin(owner))
        self.assertTrue(is_admin(admin))
        self.assertFalse(is_admin(ordinary))

    def test_logging_channel_must_not_be_accessible_to_regular_roles(self):
        everyone = role(1)
        moderator = role(2, moderate_members=True)
        regular = role(3)
        guild = SimpleNamespace(owner_id=11, me=SimpleNamespace(id=20),
                                roles=[everyone, moderator, regular], default_role=everyone)
        channel = Mock(spec=discord.TextChannel)
        channel.guild = guild
        channel.overwrites = {}
        def access(item):
            return discord.Permissions(view_channel=item.id == moderator.id)
        channel.permissions_for.side_effect = access
        self.assertTrue(private_log_channel(channel, moderator))
        channel.permissions_for.side_effect = lambda item: discord.Permissions(view_channel=item.id in (2, 3))
        self.assertFalse(private_log_channel(channel, moderator))


class DiddyModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.get_cog.return_value = None
        self.cog = SoftBannedDiddy(self.bot)
        self.cog._loaded = True
        self.cog._settings = {'10': {
            'enabled': True, 'channel_id': 22, 'log_channel_id': 23, 'mod_role_id': 4
        }}
        self.cog._log_action = AsyncMock()
        self.bot_member = SimpleNamespace(id=30)
        self.bot_member.top_role = role(5)
        self.guild = SimpleNamespace(id=10, owner_id=11, me=self.bot_member)
        self.channel = SimpleNamespace(id=22, send=AsyncMock())
        self.channel.permissions_for = Mock(return_value=discord.Permissions(manage_messages=True))

    def message(self, *, identifier=42, owner=False, admin=False):
        member = Mock(spec=discord.Member)
        member.id = 11 if owner else identifier
        member.bot = False
        member.guild = self.guild
        member.guild_permissions = discord.Permissions(administrator=admin)
        message = SimpleNamespace(id=333, author=member, guild=self.guild,
                                  channel=self.channel, delete=AsyncMock())
        return message

    async def test_owner_is_not_deleted_or_quarantined(self):
        message = self.message(owner=True)
        await self.cog._process(message)
        message.delete.assert_not_awaited()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.args[0], STAFF_REPLY)
        self.cog._log_action.assert_awaited_once()

    async def test_admin_is_not_deleted_or_quarantined(self):
        message = self.message(admin=True)
        await self.cog._process(message)
        message.delete.assert_not_awaited()
        self.channel.send.assert_awaited_once()

    async def test_regular_message_is_deleted_then_quarantined(self):
        security = SimpleNamespace(quarantine_service=SimpleNamespace(
            quarantine=AsyncMock(return_value={'phase': 'active'})
        ))
        self.bot.get_cog.return_value = security
        message = self.message()
        await self.cog._process(message)
        message.delete.assert_awaited_once_with()
        security.quarantine_service.quarantine.assert_awaited_once()
        self.assertEqual(security.quarantine_service.quarantine.await_args.args[:3],
                         (self.guild, self.bot_member, message.author))
        self.assertIn((10, 42), self.cog._recent_quarantines)
        self.cog._log_action.assert_awaited_once()
        self.assertIn('Quarantined', self.cog._log_action.await_args.args[-1])

    async def test_partial_message_delete_does_not_pass_unsupported_reason(self):
        # discord.py Message/PartialMessage.delete() cannot take a reason kwarg.
        async def delete_like_discord_py():
            return None

        security = SimpleNamespace(quarantine_service=SimpleNamespace(
            quarantine=AsyncMock(return_value={'phase': 'active'})
        ))
        self.bot.get_cog.return_value = security
        message = self.message(identifier=44)
        message.delete = AsyncMock(side_effect=delete_like_discord_py)

        await self.cog._process(message)

        message.delete.assert_awaited_once_with()
        security.quarantine_service.quarantine.assert_awaited_once()
        self.assertIn((10, 44), self.cog._recent_quarantines)
        self.assertIn("Quarantined", self.cog._log_action.await_args.args[-1])

    async def test_partial_quarantine_is_logged_for_review(self):
        security = SimpleNamespace(quarantine_service=SimpleNamespace(
            quarantine=AsyncMock(return_value={'phase': 'partial'})
        ))
        self.bot.get_cog.return_value = security
        await self.cog._process(self.message())
        self.assertNotIn((10, 42), self.cog._recent_quarantines)
        self.assertIn('PARTIAL', self.cog._log_action.await_args.args[-1])

    async def test_cannot_delete_without_manage_messages(self):
        self.channel.permissions_for.return_value = discord.Permissions.none()
        message = self.message()
        await self.cog._process(message)
        message.delete.assert_not_awaited()
        self.cog._log_action.assert_awaited_once()
        self.assertIn('ERROR', self.cog._log_action.await_args.args[-1])

    async def test_saved_enabled_trap_works_after_new_cog_instance(self):
        """A process restart must not require a repeat setup command."""
        security = SimpleNamespace(quarantine_service=SimpleNamespace(
            quarantine=AsyncMock(return_value={'phase': 'active'})
        ))
        self.bot.get_cog.return_value = security
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "soft_banned_diddy.json"
            path.write_text(json.dumps(self.cog._settings), encoding="utf-8")
            restarted = SoftBannedDiddy(self.bot)
            restarted.path = path
            restarted._log_action = AsyncMock()
            message = self.message(identifier=86)
            await restarted._process(message)
            self.assertTrue(restarted._loaded)
            self.assertTrue(restarted._cfg(10)["enabled"])
            message.delete.assert_awaited_once_with()
            security.quarantine_service.quarantine.assert_awaited_once()
            self.assertIn((10, 86), restarted._recent_quarantines)

    async def test_restart_diagnostics_identify_lost_settings_file(self):
        self.bot.guilds = [self.guild]
        with tempfile.TemporaryDirectory() as directory:
            restarted = SoftBannedDiddy(self.bot)
            restarted.path = Path(directory) / "not-present.json"
            with self.assertLogs("cogs.soft_banned_diddy", level="WARNING") as log_records:
                await restarted.on_ready()
            self.assertTrue(any(
                "No trap settings file" in message for message in log_records.output
            ))

    async def test_restart_audit_respects_explicit_disabled_flag(self):
        self.bot.guilds = [self.guild]
        with tempfile.TemporaryDirectory() as directory:
            restarted = SoftBannedDiddy(self.bot)
            restarted.path = Path(directory) / "soft_banned_diddy.json"
            restarted.path.write_text(json.dumps({'10': {'enabled': False}}), encoding="utf-8")
            restarted._quarantine_ready = AsyncMock()
            with self.assertLogs("cogs.soft_banned_diddy", level="INFO") as log_records:
                await restarted.on_ready()
            restarted._quarantine_ready.assert_not_awaited()
            self.assertFalse(restarted._cfg(10)["enabled"])
            self.assertTrue(any(
                "intentionally disabled" in message for message in log_records.output
            ))

    async def test_repeat_message_does_not_trigger_second_role_change(self):
        security = SimpleNamespace(quarantine_service=SimpleNamespace(
            quarantine=AsyncMock(return_value={'phase': 'active'})
        ))
        self.bot.get_cog.return_value = security
        await self.cog._process(self.message())
        await self.cog._process(self.message())
        self.assertEqual(security.quarantine_service.quarantine.await_count, 1)
        self.assertEqual(self.cog._log_action.await_count, 2)


if __name__ == '__main__':
    unittest.main()

"""Offline regressions for per-rule AutoMod exceptions and timeout lookup."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from cogs.automod import AutoModCog
from services.automod_service import AutoModService, AutoModSettingsError, recent_timeouts
from test_command_workflows import FakeInteraction


class AutoModTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.interaction = FakeInteraction()
        self.actor = self.interaction.user
        self.actor.guild_permissions = discord.Permissions(manage_guild=True, moderate_members=True)
        self.guild = self.interaction.guild
        self.guild.me.guild_permissions = discord.Permissions(manage_guild=True, moderate_members=True)
        self.http = SimpleNamespace(edit_auto_moderation_rule=AsyncMock(side_effect=self.save))
        self.state = SimpleNamespace(http=self.http)
        self.data = {
            "id": "999", "guild_id": "10", "name": "GIF links", "creator_id": "8", "enabled": True,
            "event_type": 1, "trigger_type": 1, "trigger_metadata": {"keyword_filter": ["gif"]},
            "actions": [{"type": 3, "metadata": {"duration_seconds": 60}}],
            "exempt_roles": ["15"], "exempt_channels": ["21"],
        }
        self.guild.fetch_automod_rule = AsyncMock(side_effect=lambda _: self.rule())
        self.guild.fetch_automod_rules = AsyncMock(side_effect=lambda: [self.rule()])
        self.service = AutoModService()

    def rule(self):
        return discord.AutoModRule(data=self.data, guild=self.guild, state=self.state)

    async def save(self, guild_id, rule_id, *, reason, **changes):
        await asyncio.sleep(0)
        self.assertEqual((guild_id, rule_id), (10, 999))
        for field, ids in changes.items():
            self.data[field] = list(map(str, ids))
        return self.data.copy()

    async def test_tagged_role_and_channel_save_to_the_native_timeout_rule(self):
        await AutoModCog.exception.callback(
            AutoModCog(), self.interaction, rule="999",
            role=SimpleNamespace(id=17), channel=SimpleNamespace(id=23),
        )
        self.assertEqual(self.data["exempt_roles"], ["15", "17"])
        self.assertEqual(self.data["exempt_channels"], ["21", "23"])
        self.assertEqual(self.data["actions"][0]["metadata"]["duration_seconds"], 60)
        self.assertEqual(set(self.http.edit_auto_moderation_rule.call_args.kwargs), {
            "exempt_roles", "exempt_channels", "reason"
        })
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_concurrent_panels_keep_other_changes_and_removal_is_idempotent(self):
        await asyncio.gather(*(
            self.service.change(self.guild, self.actor, 999, role_ids={role}, channel_ids=set())
            for role in (17, 18)
        ))
        self.assertEqual(self.data["exempt_roles"], ["15", "17", "18"])
        for expected in (True, False):
            _, changed = await self.service.change(
                self.guild, self.actor, 999, role_ids={17}, channel_ids=set(), remove=True,
            )
            self.assertEqual(changed, expected)
        self.assertEqual(self.data["exempt_channels"], ["21"])
        self.assertNotIn("exempt_channels", self.http.edit_auto_moderation_rule.call_args.kwargs)
        self.assertEqual(self.http.edit_auto_moderation_rule.await_count, 3)

    async def test_rule_limits_and_timeout_permissions_prevent_writes(self):
        with self.assertRaisesRegex(AutoModSettingsError, "specific role"):
            await self.service.change(self.guild, self.actor, 999, role_ids={10}, channel_ids=set())
        with self.assertRaisesRegex(AutoModSettingsError, "20 exempt roles"):
            await self.service.change(self.guild, self.actor, 999, role_ids=set(range(101, 121)), channel_ids={23})
        self.actor.guild_permissions.moderate_members = False
        with self.assertRaisesRegex(AutoModSettingsError, "Moderate Members"):
            await self.service.change(self.guild, self.actor, 999, role_ids={17}, channel_ids=set())
        self.http.edit_auto_moderation_rule.assert_not_awaited()

    async def test_private_panel_renders_and_uses_native_role_and_channel_pickers(self):
        await AutoModCog.exception.callback(AutoModCog(), self.interaction)
        view = self.interaction.edit_original_response.call_args.kwargs["view"]
        self.addCleanup(view.stop)
        self.assertEqual(view.to_components()[0]["type"], 17)
        self.assertEqual(view.selected_id, 999)
        controls = list(view.walk_children())
        role = next(item for item in controls if isinstance(item, discord.ui.RoleSelect))
        channel = next(item for item in controls if isinstance(item, discord.ui.ChannelSelect))
        role._values = [SimpleNamespace(id=17)]
        channel._values = [SimpleNamespace(id=23)]
        for picker in (role, channel):
            event = FakeInteraction()
            event.guild = self.guild
            event.user = self.actor
            self.assertTrue(await view.interaction_check(event))
            await picker.callback(event)
        self.assertEqual(self.data["exempt_roles"], ["15", "17"])
        self.assertEqual(self.data["exempt_channels"], ["21", "23"])
        await view.toggle_mode(FakeInteraction())
        old_menu = FakeInteraction()
        await role.callback(old_menu)
        old_menu.response.send_message.assert_awaited_once()
        self.assertEqual(self.http.edit_auto_moderation_rule.await_count, 2)
        self.actor.guild_permissions.manage_guild = False
        self.assertFalse(await view.interaction_check(self.interaction))
        self.interaction.response.send_message.assert_awaited_once()


class TimeoutAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_native_and_bot_timeouts_but_skips_other_members_and_unmute(self):
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        native = discord.AuditLogAction.automod_timeout_member
        update = discord.AuditLogAction.member_update

        def entry(identifier, member, action, until=None):
            return SimpleNamespace(
                id=identifier, target=discord.Object(id=member), action=action,
                created_at=date, after=SimpleNamespace(timed_out_until=until),
            )

        entries = {
            native: [entry(15, 2, native), entry(13, 1, native)],
            update: [entry(17, 1, update), entry(16, 1, update, date + timedelta(hours=1))],
        }

        async def audit_logs(*, action, limit):
            for item in entries[action]:
                yield item

        result = await recent_timeouts(SimpleNamespace(audit_logs=audit_logs), 1)
        self.assertEqual([item.id for item in result], [16, 13])

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from services.spam_guard import PrivilegedSpamGuard


class GuardFixture:
    def setUp(self):
        self.guard = PrivilegedSpamGuard()
        self.owner = Mock(spec=discord.Member, id=1, bot=False)
        self.admin = Mock(spec=discord.Member, id=2, bot=False)
        self.admin.guild_permissions = discord.Permissions(administrator=True)
        self.guild = SimpleNamespace(id=10, owner_id=1, owner=self.owner)
        self.channel = Mock(spec=discord.TextChannel)
        self.sequence = 0

    def message(self, text="same", author=None, channel=None, ping=False):
        self.sequence += 1
        return SimpleNamespace(id=self.sequence, author=author or self.admin, guild=self.guild,
                               channel=channel or self.channel, type=discord.MessageType.default,
                               content=text, mention_everyone=ping, webhook_id=None, delete=AsyncMock())


class DetectionTests(GuardFixture, unittest.TestCase):
    def test_cross_channel_copies_select_only_matching_messages_and_continue(self):
        first = self.message("CLAIM   now", self.owner)
        other = self.message("an unrelated announcement", self.owner)
        second = self.message("Claim now", self.owner, Mock(spec=discord.Thread))
        third = self.message("claim now", self.owner)
        for at, message in enumerate([first, other, second]):
            self.assertEqual(self.guard.observe(message, at), ([], False))
        self.assertEqual(self.guard.observe(third, 3), ([first, second, third], True))
        self.assertEqual(self.guard.observe(third, 4), ([], False))
        fourth = self.message("claim now", self.owner)
        self.assertEqual(self.guard.observe(fourth, 5), ([fourth], False))

    def test_varying_flood_and_recovery(self):
        messages = [self.message(str(i)) for i in range(8)]
        for i, msg in enumerate(messages[:-1]):
            self.assertEqual(self.guard.observe(msg, i), ([], False))
        self.assertEqual(self.guard.observe(messages[-1], 7), (messages, True))
        self.assertEqual(self.guard.observe(self.message("back"), 18), ([], False))

    def test_only_real_mass_mentions_count(self):
        messages = [self.message(str(i), ping=True) for i in range(3)]
        for at, msg in [(0, messages[0]), (3, self.message("@everyone")), (7, messages[1])]:
            self.assertEqual(self.guard.observe(msg, at), ([], False))
        self.assertEqual(self.guard.observe(messages[-1], 14), (messages, True))

    def test_expiry_and_account_guild_isolation(self):
        for at in (0, 1):
            self.guard.observe(self.message(), at)
        self.assertEqual(self.guard.observe(self.message(author=self.owner), 2), ([], False))
        different_guild = self.message()
        different_guild.guild = SimpleNamespace(id=11, owner_id=1)
        self.assertEqual(self.guard.observe(different_guild, 2), ([], False))
        self.assertEqual(self.guard.observe(self.message(), 30), ([], False))
        self.guard.observe(self.message(), 93)
        self.assertEqual(list(self.guard.windows), [(10, 2)])

    def test_empty_messages_ignored_as_copies_and_ineligible_sources_excluded(self):
        for i in range(3):
            self.assertEqual(self.guard.observe(self.message(""), i), ([], False))
        normal = Mock(spec=discord.Member, id=3, bot=False)
        normal.guild_permissions = discord.Permissions.none()
        for field, value in (("author", normal), ("webhook_id", 99), ("guild", None),
                             ("author", Mock(spec=discord.Member, id=1, bot=True)),
                             ("type", discord.MessageType.pins_add)):
            for i in range(8):
                msg = self.message()
                setattr(msg, field, value)
                self.assertEqual(self.guard.observe(msg, 4), ([], False))


class ListenerTests(GuardFixture, unittest.IsolatedAsyncioTestCase):
    async def test_channel_failure_does_not_stop_other_cleanup_or_alerts(self):
        from cogs.moderation import ModerationCog

        cog = ModerationCog(Mock())
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
        self.channel.delete_messages.side_effect = forbidden
        thread = Mock(spec=discord.Thread)
        messages = [self.message(), self.message(), self.message(channel=thread)]
        with patch("cogs.moderation._dm_user", new_callable=AsyncMock) as dm, \
             patch("cogs.moderation._mod_log", new_callable=AsyncMock) as modlog:
            for msg in messages:
                await cog.on_message(msg)
            self.channel.delete_messages.assert_awaited_once()
            messages[-1].delete.assert_awaited_once()
            details = modlog.await_args.args[1].description
            self.assertIn("Removal unconfirmed: 2", details)
            self.assertEqual([call.args[0] for call in dm.await_args_list], [self.admin, self.owner])
            fourth = self.message(channel=thread)
            await cog.on_message(fourth)
            fourth.delete.assert_awaited_once()
            modlog.assert_awaited_once()

    async def test_failed_bulk_falls_back_and_owner_gets_one_dm(self):
        from cogs.moderation import ModerationCog

        cog = ModerationCog(Mock())
        error = discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "gone")
        self.channel.delete_messages.side_effect = error
        messages = [self.message(author=self.owner) for _ in range(3)]
        messages[0].delete.side_effect = error
        with patch("cogs.moderation._dm_user", new_callable=AsyncMock) as dm, \
             patch("cogs.moderation._mod_log", new_callable=AsyncMock) as modlog:
            for msg in messages:
                await cog.on_message(msg)
            for msg in messages:
                msg.delete.assert_awaited_once()
            dm.assert_awaited_once()
            self.assertIn("Removal unconfirmed: 0", modlog.await_args.args[1].description)

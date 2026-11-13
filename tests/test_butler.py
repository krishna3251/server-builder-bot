"""Butler policy, privacy, timing, audit review and delivery; no live Discord."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord

from cogs.butler import ButlerCog
from services.butler_service import ButlerService, ButlerSettings, day_bounds
from services.butler_store import ButlerStore

UTC = timezone.utc


class ButlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
        self.owner = NS(id=42, send=AsyncMock())
        self.member_dm = NS(id=1, send=AsyncMock())
        self.bot = NS(
            user=NS(id=999),
            get_user=Mock(
                side_effect=lambda uid: self.owner if uid == 42 else self.member_dm
            ),
            fetch_user=AsyncMock(return_value=self.owner),
            application_info=AsyncMock(return_value=NS(team=None, owner=NS(id=42))),
            is_owner=AsyncMock(side_effect=lambda user: user.id == 42),
            is_ready=Mock(return_value=True),
        )
        self.settings = ButlerSettings(owner_id=42, timezone_name="UTC")
        self.service = ButlerService(
            self.bot,
            Path(self.tmp.name) / "butler.sqlite3",
            self.settings,
            clock=lambda: self.now,
        )
        self.store = self.service.store
        self.guild = NS(
            id=10,
            name="Test Server",
            owner_id=2,
            members=[],
            voice_channels=[],
            stage_channels=[],
            get_member=Mock(return_value=None),
            me=NS(guild_permissions=discord.Permissions.none()),
            unavailable=False,
        )
        self.bot.guilds = [self.guild]
        await self.store.begin(10, "Test Server", [])
        self.service.guilds.add(10)

    async def data(self, day=None, guild=10):
        day = day or self.service.today()
        start, end = day_bounds(day, self.service.zone)
        return await self.store.report(guild, start, end)

    def message(self, identifier, uid=1, *, actual=True, bot=False):
        return NS(
            id=identifier,
            guild=self.guild,
            author=NS(
                id=uid,
                display_name="Viewer",
                bot=bot,
                guild_permissions=NS(administrator=True),
            ),
            mention_everyone=actual,
            content="@everyone",
            webhook_id=None,
            delete=AsyncMock(),
        )

    async def test_invalid_scope_configuration_does_not_enable_all_servers(self):
        with patch.dict(
            "os.environ", {"BUTLER_GUILD_IDS": "invalid", "BUTLER_ENABLED": " false "}
        ):
            settings = ButlerSettings.from_env()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.guild_ids, frozenset({0}))

    async def test_invalid_timezone_falls_back_to_labeled_utc(self):
        with patch.dict("os.environ", {"BUTLER_TIMEZONE": "../invalid"}):
            settings = ButlerSettings.from_env()
        self.assertEqual(settings.timezone_name, "UTC")

    async def test_team_application_reports_to_team_owner(self):
        self.service.owner_id = 0
        self.bot.application_info.return_value = NS(
            team=NS(owner_id=42), owner=NS(id=8)
        )
        await self.store.enqueue_report(10, str(self.service.today()), "initial")
        await self.service.deliver()
        self.owner.send.assert_awaited_once()
        self.member_dm.send.assert_not_awaited()

    async def test_ready_worker_automatically_sends_today_once_and_shuts_down(self):
        cog = ButlerCog(self.bot)
        cog.service = self.service
        self.bot.wait_until_ready = AsyncMock()
        sent = asyncio.Event()

        async def delivered(*args, **kwargs):
            sent.set()

        self.owner.send.side_effect = delivered
        await cog.cog_load()
        try:
            await asyncio.wait_for(sent.wait(), 2)
            await cog.on_ready()
            self.owner.send.assert_awaited_once()
        finally:
            await cog.cog_unload()
        self.assertTrue(cog.worker.done())
        self.assertFalse(cog.ready.is_set())

    async def test_first_ping_allowed_second_removed_and_notice_once(self):
        first, second, third = self.message(1), self.message(2), self.message(3)
        await self.service.mention(first)
        await self.service.mention(second)
        await self.service.mention(third)
        first.delete.assert_not_awaited()
        second.delete.assert_awaited_once_with()
        third.delete.assert_awaited_once_with()
        jobs = await self.store.due()
        self.assertEqual(sorted(j["kind"] for j in jobs), ["incident", "warning"])
        data = await self.data()
        self.assertEqual(data["counts"]["ping_violation"], 2)

    async def test_everyone_here_share_the_window_across_channels(self):
        first, second = self.message(1), self.message(2)
        second.content = "@here"
        await self.service.mention(first)
        await self.service.mention(second)
        second.delete.assert_awaited_once()

    async def test_administrators_server_owner_and_bot_owner_are_not_exempt(self):
        for uid in (1, 2, 42):
            await self.service.mention(self.message(uid * 10, uid))
            second = self.message(uid * 10 + 1, uid)
            await self.service.mention(second)
            second.delete.assert_awaited_once()

    async def test_rolling_three_hours_and_restart_preserve_policy(self):
        await self.service.mention(self.message(1))
        self.now += 10799
        self.service.store = ButlerStore(self.store.path, clock=lambda: self.now)
        second = self.message(2)
        await self.service.mention(second)
        second.delete.assert_awaited_once()
        self.now += 10800
        third = self.message(3)
        await self.service.mention(third)
        third.delete.assert_not_awaited()

    async def test_exact_window_boundary_allows_a_new_ping(self):
        await self.service.mention(self.message(1))
        self.now += 10800
        message = self.message(2)
        await self.service.mention(message)
        message.delete.assert_not_awaited()

    async def test_duplicate_delivery_or_edit_does_not_count_same_message_twice(self):
        message = self.message(1)
        await self.service.mention(message)
        await self.service.mention(message)
        message.content = "@everyone @here and a correction"
        await self.service.mention(message)
        message.delete.assert_not_awaited()
        second = self.message(2)
        await self.service.mention(second)
        second.delete.assert_awaited_once()

    async def test_users_and_guilds_are_isolated(self):
        await self.service.mention(self.message(1))
        other = self.message(2, uid=2)
        await self.service.mention(other)
        other.delete.assert_not_awaited()
        self.guild.id = 11
        third = self.message(3)
        await self.service.mention(third)
        third.delete.assert_not_awaited()

    async def test_plain_literals_dms_and_own_messages_are_ignored(self):
        for message in (self.message(1, actual=False), self.message(2, uid=999)):
            await self.service.mention(message)
            message.delete.assert_not_awaited()
        message = self.message(3)
        message.guild = None
        await self.service.mention(message)
        self.assertEqual(await self.store.due(), [])

    async def test_deletion_failure_is_reported_truthfully(self):
        await self.service.mention(self.message(1))
        message = self.message(2)
        message.delete.side_effect = discord.Forbidden(
            NS(status=403, reason="Forbidden"), "Missing permissions"
        )
        await self.service.mention(message)
        details = json.loads((await self.data())["incidents"][0]["details"])
        self.assertFalse(details["deleted"])
        await self.service.deliver()
        self.assertIn("could not remove", self.member_dm.send.call_args.args[0])
        self.assertIn("missing Manage Messages", self.owner.send.call_args.args[0])

    async def test_other_bots_are_enforced_without_trying_to_dm_them(self):
        await self.service.mention(self.message(1, bot=True))
        message = self.message(2, bot=True)
        await self.service.mention(message)
        message.delete.assert_awaited_once()
        self.assertEqual([j["kind"] for j in await self.store.due()], ["incident"])

    async def test_dm_failure_does_not_post_publicly_or_retry_blocked_user(self):
        await self.service.mention(self.message(1))
        await self.service.mention(self.message(2))
        self.member_dm.send.side_effect = discord.Forbidden(
            NS(status=403, reason="Forbidden"), "DM closed"
        )
        await self.service.deliver()
        await self.service.deliver()
        self.member_dm.send.assert_awaited_once()
        self.owner.send.assert_awaited_once()
        self.assertEqual((await self.data())["delivery"]["failed"], 1)

    async def test_transient_delivery_is_bounded_to_three_attempts(self):
        await self.store.enqueue_report(10, str(self.service.today()), "initial")
        self.owner.send.side_effect = RuntimeError("synthetic")
        for _ in range(4):
            await self.service.deliver()
            self.now += 2000
        self.assertEqual(self.owner.send.await_count, 3)
        self.assertEqual((await self.data())["delivery"]["failed"], 1)

    async def test_stale_warnings_expire_instead_of_sending_after_outage(self):
        await self.service.mention(self.message(1))
        await self.service.mention(self.message(2))
        self.now += 10801
        self.assertEqual(await self.store.due(), [])

    async def test_restart_initial_report_dedup_and_bot_owner_delivery(self):
        await self.service.begin(self.guild)
        await self.service.begin(self.guild)
        self.service.owner_id = 0
        await self.service.deliver()
        self.owner.send.assert_awaited_once()
        self.member_dm.send.assert_not_awaited()
        self.bot.application_info.assert_awaited_once()
        kwargs = self.owner.send.call_args.kwargs
        self.assertIn("today's first ledger", kwargs["embed"].title)
        self.assertIn("before tracking", kwargs["file"].fp.getvalue().decode())
        self.assertEqual(
            kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        self.assertEqual(await self.store.due(), [])

    async def test_daily_report_due_time_and_first_day_no_fake_previous_day(self):
        await self.service.schedule()
        self.assertEqual(await self.store.due(), [])
        self.now = datetime(2026, 11, 11, 0, 4, tzinfo=UTC).timestamp()
        await self.service.schedule()
        self.assertEqual(await self.store.due(), [])
        self.now += 60
        await self.service.schedule()
        await self.service.schedule()
        jobs = await self.store.due()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0]["payload"])["day"], "2026-11-10")

    async def test_indian_timezone_day_is_not_the_utc_day(self):
        zone = ZoneInfo("Asia/Kolkata")
        date = datetime(2026, 11, 11).date()
        start, end = day_bounds(date, zone)
        self.assertEqual(
            datetime.fromtimestamp(start, UTC).isoformat(), "2026-11-10T18:30:00+00:00"
        )
        self.assertEqual(end - start, 86400)

    async def test_voice_snapshot_matches_real_discord_guild_interface(self):
        guild = Mock(spec_set=discord.Guild)
        guild.voice_channels = [NS(id=100, voice_states={1: NS()})]
        guild.stage_channels = [NS(id=200, voice_states={2: NS()})]
        guild.get_member.side_effect = lambda uid: (
            NS(display_name="Cached") if uid == 1 else None
        )
        self.assertEqual(
            self.service.voices(guild), [(1, "Cached", 100), (2, "2", 200)]
        )

    async def test_voice_joins_moves_and_mute_updates(self):
        await self.store.voice_change(10, 1, "User", None, 100)
        self.now += 60
        await self.store.voice_change(10, 1, "User", 100, 100)
        self.now += 60
        await self.store.voice_change(10, 1, "User", 100, 200)
        self.now += 60
        await self.store.voice_change(10, 1, "User", 200, None)
        row = (await self.data())["voices"][0]
        self.assertEqual(row["joins"], 1)
        self.assertEqual(row["moves"], 1)
        self.assertEqual(row["seconds"], 180)

    async def test_voice_time_is_clipped_across_midnight(self):
        self.now = datetime(2026, 11, 10, 23, 59, tzinfo=UTC).timestamp()
        await self.store.voice_change(10, 1, "User", None, 100)
        self.now += 120
        await self.store.voice_change(10, 1, "User", 100, None)
        yesterday = await self.data(datetime(2026, 11, 10).date())
        today = await self.data(datetime(2026, 11, 11).date())
        self.assertEqual(yesterday["voices"][0]["seconds"], 60)
        self.assertEqual(yesterday["voices"][0]["joins"], 1)
        self.assertEqual(today["voices"][0]["seconds"], 60)
        self.assertEqual(today["voices"][0]["joins"], 0)

    async def test_voice_restart_uses_last_heartbeat_not_downtime(self):
        await self.store.voice_change(10, 1, "User", None, 100)
        self.now += 60
        await self.store.heartbeat(10, [(1, "User", 100)])
        self.now += 3600
        await self.store.begin(10, "Test Server", [(1, "User", 100)])
        self.now += 60
        row = (await self.data())["voices"][0]
        self.assertEqual(row["seconds"], 120)
        self.assertEqual(row["joins"], 1)

    async def test_startup_voice_presence_does_not_invent_a_join(self):
        await self.store.begin(10, "Test Server", [(1, "User", 100)])
        self.now += 120
        row = (await self.data())["voices"][0]
        self.assertEqual(row["joins"], 0)
        self.assertEqual(row["seconds"], 120)
        self.assertTrue(row["active"])

    async def test_disconnect_pause_excludes_offline_voice_time(self):
        await self.store.voice_change(10, 1, "User", None, 100)
        self.now += 60
        await self.store.pause()
        self.now += 600
        await self.store.begin(10, "Test Server", [(1, "User", 100)])
        self.now += 60
        self.assertEqual((await self.data())["voices"][0]["seconds"], 120)

    async def test_member_import_dedup_leave_and_rejoin(self):
        stamp = self.now - 100
        await self.store.import_joins(10, [(1, "User", stamp)])
        await self.store.member(10, 1, "User", "join", stamp)
        await self.store.member(10, 1, "User", "leave")
        await self.store.member(10, 1, "User", "leave")
        self.now += 1
        await self.store.member(10, 1, "User", "join", self.now)
        await self.store.member(10, 1, "User", "leave")
        data = await self.data()
        self.assertEqual(data["counts"]["join"], 2)
        self.assertEqual(data["counts"]["leave"], 2)

    def audit_entry(self, identifier, *, action="role_update", actor=55, flags=True):
        return NS(
            guild=self.guild,
            id=identifier,
            user=None,
            user_id=actor,
            action=NS(name=action),
            target=NS(id=70),
            created_at=datetime.fromtimestamp(self.now, UTC),
            before=NS(permissions=discord.Permissions.none()),
            after=NS(permissions=discord.Permissions(administrator=flags)),
        )

    async def test_audit_uses_actor_id_without_cache_and_is_idempotent(self):
        entry = self.audit_entry(100)
        await self.service.audit(entry)
        await self.service.audit(entry)
        data = await self.data()
        self.assertEqual(data["counts"]["audit"], 1)
        self.assertEqual(data["audits"][0]["user"], 55)
        self.assertIn(
            "administrator", json.loads(data["audit_details"][0]["details"])["flags"][0]
        )
        self.assertEqual([j["kind"] for j in await self.store.due()], ["audit_alert"])

    async def test_audit_backfill_does_not_flood_owner_with_historical_alerts(self):
        self.guild.me.guild_permissions = discord.Permissions(view_audit_log=True)

        async def entries(**kwargs):
            for i in range(4):
                yield self.audit_entry(100 + i)

        self.guild.audit_logs = entries
        await self.service.collect_audit(self.guild, self.now - 3600, initial=True)
        self.assertEqual((await self.data())["counts"]["audit"], 4)
        self.assertEqual(await self.store.due(), [])

    async def test_malformed_audit_entry_does_not_prevent_first_report(self):
        self.guild.me.guild_permissions = discord.Permissions(view_audit_log=True)

        async def entries(**kwargs):
            yield NS(guild=self.guild, action=None)
            yield self.audit_entry(100)

        self.guild.audit_logs = entries
        await self.service.begin(self.guild)
        self.assertIn(
            "malformed entries skipped", (await self.data())["guild"]["audit_status"]
        )
        self.assertEqual([j["kind"] for j in await self.store.due()], ["initial"])

    async def test_periodic_audit_backfill_advances_cursor_and_alerts_new_entries(self):
        self.guild.me.guild_permissions = discord.Permissions(view_audit_log=True)
        calls = []

        async def entries(**kwargs):
            calls.append(kwargs)
            yield self.audit_entry(100 + len(calls))

        self.guild.audit_logs = entries
        await self.service.collect_audit(self.guild, self.now - 3600, initial=True)
        self.now += 900
        await self.service.collect_audit(self.guild, self.now - 4500)
        self.assertGreater(calls[1]["after"].timestamp(), calls[0]["after"].timestamp())
        self.assertEqual([j["kind"] for j in await self.store.due()], ["audit_alert"])

    async def test_audit_missing_permission_is_explicit(self):
        await self.service.collect_audit(self.guild, self.now - 3600)
        self.assertIn(
            "permission missing", (await self.data())["guild"]["audit_status"]
        )

    async def test_audit_deletion_burst_flags_for_review_without_automatic_punishment(
        self,
    ):
        for i in range(4):
            await self.service.audit(
                self.audit_entry(100 + i, action="channel_delete", flags=False)
            )
        jobs = await self.store.due()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0]["payload"])["recent_count"], 3)
        await self.service.deliver()
        self.assertIn("may be legitimate", self.owner.send.call_args.args[0])

    async def test_no_message_contents_or_audit_reasons_are_stored(self):
        first = self.message(1)
        first.content = "@everyone SYNTHETIC_PRIVATE_BODY"
        second = self.message(2)
        second.content = "@everyone SYNTHETIC_PRIVATE_BODY"
        await self.service.mention(first)
        await self.service.mention(second)
        entry = self.audit_entry(100)
        entry.reason = "SYNTHETIC_PRIVATE_REASON"
        await self.service.audit(entry)
        payload = json.dumps(await self.data()) + json.dumps(await self.store.due())
        self.assertNotIn("SYNTHETIC_PRIVATE_BODY", payload)
        self.assertNotIn("SYNTHETIC_PRIVATE_REASON", payload)

    async def test_actual_voice_event_promotes_a_snapshot_without_losing_join_count(
        self,
    ):
        await self.store.heartbeat(10, [(1, "User", 100)])
        await self.store.voice_change(10, 1, "User", None, 100)
        await self.store.voice_change(10, 1, "User", None, 100)
        self.assertEqual((await self.data())["voices"][0]["joins"], 1)

    async def test_parallel_mentions_and_delivery_do_not_duplicate_notices(self):
        first, second = self.message(1), self.message(2)
        await asyncio.gather(self.service.mention(first), self.service.mention(second))
        self.assertEqual(first.delete.await_count + second.delete.await_count, 1)
        await asyncio.gather(self.service.deliver(), self.service.deliver())
        self.member_dm.send.assert_awaited_once()
        self.owner.send.assert_awaited_once()

    async def test_bounded_outbox_prioritizes_reports_without_evicting_on_duplicate(
        self,
    ):
        now = self.now

        def fill(conn):
            for i in range(1000):
                self.store._enqueue(conn, f"warning:{i}", 10, 1, "warning", {}, now)

        await self.store.run(fill)
        self.assertTrue(
            await self.store.enqueue_report(10, str(self.service.today()), "initial")
        )
        self.assertFalse(
            await self.store.enqueue_report(10, str(self.service.today()), "initial")
        )
        data = await self.data()
        self.assertEqual(data["delivery"]["pending"], 1000)
        self.assertEqual(data["delivery"]["failed"], 1)

    async def test_retention_removes_old_event_data(self):
        await self.store.member(10, 1, "User", "join", self.now)
        self.now += 31 * 86400
        data = await self.data(datetime(2026, 11, 10).date())
        self.assertEqual(data["counts"], {})

    async def test_disabled_or_unselected_guild_is_ignored(self):
        self.service.settings = replace(self.settings, guild_ids=frozenset({11}))
        await self.service.mention(self.message(1))
        await self.service.mention(self.message(2))
        self.assertEqual((await self.data())["counts"], {})

    async def test_report_details_cannot_cross_guild(self):
        await self.store.member(10, 1, "PRIVATE_GUILD_USER", "join", self.now)
        await self.store.begin(11, "Other Server", [])
        embed, text = await self.service.report(11, self.service.today())
        self.assertNotIn("PRIVATE_GUILD_USER", text)

    async def test_raw_departure_is_recorded_without_member_cache(self):
        cog = ButlerCog(self.bot)
        cog.service = self.service
        cog.ready.set()
        await cog.on_raw_member_remove(NS(guild_id=10, user=NS(id=1)))
        self.assertEqual((await self.data())["counts"]["leave"], 1)

    def interaction(self, uid, admin=False):
        response = NS(
            is_done=Mock(return_value=False),
            defer=AsyncMock(),
            send_message=AsyncMock(),
        )

        async def defer(**kwargs):
            response.is_done.return_value = True
            response.type = discord.InteractionResponseType.deferred_channel_message

        response.defer.side_effect = defer
        return NS(
            user=NS(id=uid, guild_permissions=NS(administrator=admin)),
            guild=self.guild,
            guild_id=10,
            response=response,
            edit_original_response=AsyncMock(),
            followup=NS(send=AsyncMock()),
            extras={},
            command=None,
        )

    async def test_only_bot_owner_can_open_private_report_even_via_callback(self):
        cog = ButlerCog(self.bot)
        cog.service = self.service
        cog.ready.set()
        event = self.interaction(8, admin=True)
        await ButlerCog.report.callback(cog, event)
        self.assertIn(
            "Only the bot owner",
            event.edit_original_response.call_args.kwargs["content"],
        )
        event = self.interaction(42)
        await ButlerCog.report.callback(cog, event)
        self.assertIsNotNone(
            event.edit_original_response.call_args.kwargs["attachments"]
        )
        event.response.defer.assert_awaited_once()

    async def test_report_invalid_dates_and_retention_are_validated(self):
        cog = ButlerCog(self.bot)
        cog.service = self.service
        cog.ready.set()
        for date in ("bad", "1900-01-01", "2099-01-01"):
            event = self.interaction(42)
            await ButlerCog.report.callback(cog, event, date=date)
            self.assertNotIn(
                "attachments", event.edit_original_response.call_args.kwargs
            )

    async def test_sqlite_worker_does_not_release_lock_while_cancelled_write_runs(self):
        started, finish = threading.Event(), threading.Event()

        def work(conn):
            started.set()
            finish.wait(2)

        task = asyncio.create_task(self.store.run(work))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertTrue(self.store.lock.locked())
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.store.lock.locked())

"""Server butler: metadata collection, conservative audit review, private reports."""

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta, timezone
import io
import json
import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from services.butler_store import ButlerStore

log = logging.getLogger(__name__)
UTC = timezone.utc


def integer_env(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class ButlerSettings:
    enabled: bool = True
    owner_id: int = 0
    timezone_name: str = "Asia/Kolkata"
    report_hour: int = 0
    report_minute: int = 5
    retention_days: int = 30
    guild_ids: frozenset = frozenset()

    @classmethod
    def from_env(cls):
        zone = os.getenv("BUTLER_TIMEZONE", "Asia/Kolkata").strip()
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning(
                "Butler timezone unavailable; using UTC. Install tzdata for named timezones on Windows."
            )
            zone = "UTC"
        scope = os.getenv("BUTLER_GUILD_IDS", "").strip()
        ids = frozenset(int(x.strip()) for x in scope.split(",") if x.strip().isdigit())
        if scope and not ids:
            log.warning(
                "BUTLER_GUILD_IDS has no valid IDs; no servers will be collected"
            )
            ids = frozenset({0})
        return cls(
            os.getenv("BUTLER_ENABLED", "true").strip().lower()
            in {"1", "true", "on", "yes"},
            integer_env("BUTLER_OWNER_ID", 0, 0, 2**64 - 1),
            zone,
            integer_env("BUTLER_REPORT_HOUR", 0, 0, 23),
            integer_env("BUTLER_REPORT_MINUTE", 5, 0, 59),
            integer_env("BUTLER_RETENTION_DAYS", 30, 7, 90),
            ids,
        )


def day_bounds(day, zone):
    start = datetime.combine(day, day_time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), day_time(), tzinfo=zone)
    return start.timestamp(), end.timestamp()


def duration(seconds):
    total = max(0, int(seconds))
    return f"{total//3600}h {(total%3600)//60}m {total%60}s"


class ButlerService:
    def __init__(self, bot, path, settings=None, *, clock=None):
        import time

        self.bot = bot
        self.settings = settings or ButlerSettings.from_env()
        self.clock = clock or time.time
        self.zone = ZoneInfo(self.settings.timezone_name)
        self.store = ButlerStore(
            path, retention_days=self.settings.retention_days, clock=self.clock
        )
        self.owner_id = self.settings.owner_id
        self.guilds = set()
        self.delivery_lock = asyncio.Lock()
        self.audit_lock = asyncio.Lock()
        self.last_error = None
        self.audit_cursor = {}
        self.audit_capped = set()

    def enabled(self, guild):
        return self.settings.enabled and (
            not self.settings.guild_ids or guild in self.settings.guild_ids
        )

    def today(self):
        return datetime.fromtimestamp(self.clock(), self.zone).date()

    async def is_owner(self, user):
        if self.settings.owner_id and user.id == self.settings.owner_id:
            return True
        return await self.bot.is_owner(user)

    async def resolve_owner(self):
        if not self.owner_id:
            app = await self.bot.application_info()
            self.owner_id = app.team.owner_id if app.team else app.owner.id
        return self.owner_id

    @staticmethod
    def voices(guild):
        items = []
        # Guild has no public voice_states property. Each vocal channel exposes
        # uncached member IDs as well as cached occupants (discord.py 1.3+).
        for channel in (*guild.voice_channels, *guild.stage_channels):
            for user in channel.voice_states:
                member = guild.get_member(user)
                items.append(
                    (
                        user,
                        str(member.display_name) if member else str(user),
                        channel.id,
                    )
                )
        return items

    async def begin(self, guild):
        if not self.enabled(guild.id):
            return
        await self.store.begin(guild.id, guild.name, self.voices(guild))
        self.guilds.add(guild.id)
        start, end = day_bounds(self.today(), self.zone)
        # A join timestamp establishes that this currently present member joined
        # today, but says nothing about members who already left or past voice time.
        joins = []
        for member in guild.members:
            stamp = member.joined_at.timestamp() if member.joined_at else None
            if stamp is not None and start <= stamp < end:
                joins.append((member.id, member.display_name, stamp))
                if len(joins) >= 2000:
                    break
        await self.store.import_joins(guild.id, joins)
        await self.collect_audit(guild, start, initial=True)
        await self.store.enqueue_report(guild.id, str(self.today()), "initial")

    async def mention(self, message):
        guild = message.guild
        if (
            guild is None
            or not self.enabled(guild.id)
            or message.author.id == getattr(self.bot.user, "id", None)
        ):
            return
        if not message.mention_everyone:
            return
        # Discord notifies once per message, even if the token is repeated.
        # Its parsed flag also distinguishes real mentions from plain literals.
        count = 1
        decision = await self.store.ping(
            guild.id, message.author.id, message.author.display_name, message.id, count
        )
        if decision is None:
            return
        deleted = False
        reason = "deleted"
        try:
            await message.delete()
            deleted = True
        except discord.NotFound:
            deleted = True
            reason = "already deleted"
        except discord.Forbidden:
            reason = "missing Manage Messages permission"
        except discord.HTTPException:
            reason = "Discord deletion failed"
        await self.store.complete_ping(
            guild.id,
            message.author.id,
            message.author.display_name,
            guild.name,
            decision,
            deleted,
            reason,
            dm_user=not message.author.bot and not message.webhook_id,
        )

    @staticmethod
    def audit_flags(entry):
        flags = []
        action = entry.action.name
        before = getattr(entry, "before", None)
        after = getattr(entry, "after", None)
        for field in ("permissions", "allow"):
            previous = getattr(before, field, None)
            current = getattr(after, field, None)
            if current is None:
                continue
            for permission in (
                "administrator",
                "mention_everyone",
                "manage_roles",
                "manage_guild",
                "manage_webhooks",
                "ban_members",
                "kick_members",
            ):
                if getattr(current, permission, False) and (
                    previous is not None
                    and not getattr(previous, permission, False)
                    or action == "role_create"
                ):
                    flags.append(f"{field}: {permission} enabled")
        if action in {"webhook_create", "bot_add"}:
            flags.append("New integration; review if unexpected")
        return flags

    async def audit(self, entry, *, notify=True):
        if not self.enabled(entry.guild.id):
            return
        user = getattr(entry, "user", None)
        user_id = getattr(entry, "user_id", None) or getattr(user, "id", 0) or 0
        target = getattr(getattr(entry, "target", None), "id", 0) or 0
        return await self.store.audit(
            entry.guild.id,
            entry.id,
            user_id,
            str(user) if user else str(user_id),
            entry.action.name,
            target,
            entry.created_at.timestamp(),
            self.audit_flags(entry),
            notify=notify,
        )

    async def collect_audit(self, guild, since, *, initial=False):
        if not guild.me or not guild.me.guild_permissions.view_audit_log:
            await self.store.audit_status(
                guild.id, "unavailable: View Audit Log permission missing"
            )
            return
        async with self.audit_lock:
            since = max(since, self.audit_cursor.get(guild.id, since))
            count = 0
            skipped = 0
            try:
                async for entry in guild.audit_logs(
                    limit=500,
                    after=datetime.fromtimestamp(since, UTC),
                    oldest_first=False,
                ):
                    count += 1
                    try:
                        await self.audit(entry, notify=not initial)
                    except (AttributeError, ValueError, TypeError):
                        skipped += 1
                        log.warning("Skipped malformed Discord audit metadata")
                if count == 500:
                    self.audit_capped.add((guild.id, self.today()))
                self.audit_cursor[guild.id] = self.clock() - 300
                status = (
                    "Backfill capped at 500 entries; additional history may be missing"
                    if (guild.id, self.today()) in self.audit_capped
                    else "available (gateway + bounded REST backfill)"
                )
                if skipped:
                    status += f"; {skipped} malformed entries skipped"
            except discord.Forbidden:
                status = "unavailable: audit-log access denied"
            except discord.HTTPException:
                status = "partial: audit-log API request failed"
            await self.store.audit_status(guild.id, status)

    async def schedule(self):
        now = datetime.fromtimestamp(self.clock(), self.zone)
        due = now.replace(
            hour=self.settings.report_hour,
            minute=self.settings.report_minute,
            second=0,
            microsecond=0,
        )
        guilds = await self.store.guilds()
        for guild in guilds:
            if not self.enabled(guild["id"]) or guild["id"] not in self.guilds:
                continue
            for age in range(1, min(7, self.settings.retention_days) + 1):
                day = now.date() - timedelta(days=age)
                start, end = day_bounds(day, self.zone)
                if end <= guild["first_seen"] or age == 1 and now < due:
                    continue
                await self.store.enqueue_report(guild["id"], str(day), "daily")

    async def report(self, guild, day):
        start, end = day_bounds(day, self.zone)
        data = await self.store.report(guild, start, end)
        return self.render(data, day, start, end)

    def render(self, data, day, start, end):
        guild = data["guild"]
        counts = data["counts"]
        title = f"Your server's daily ledger — {day}"
        description = (
            f"At your service. Here is the recorded activity for **{discord.utils.escape_markdown(guild['name'])[:100]}**.\n"
            f"Joins: **{counts.get('join',0)}** · Departures: **{counts.get('leave',0)}** · Repeat-ping messages: **{counts.get('ping_violation',0)}**\n"
            f"Audit entries: **{counts.get('audit',0)}** · Observed voice time: **{duration(data['voice_total_seconds'])}**\n"
            f"Timezone: {self.settings.timezone_name}. Observed online coverage: {duration(data['coverage_seconds'])}.\n"
            "The attached ledger names participants and shows per-person voice counts/time. History before tracking or during downtime may be incomplete."
        )
        embed = discord.Embed(title=title, description=description, color=0x5865F2)
        embed.set_footer(
            text="Private bot-owner report • Metadata only • No message bodies or voice audio"
        )

        def stamp(value):
            return datetime.fromtimestamp(value, self.zone).isoformat(
                timespec="seconds"
            )

        lines = [
            title,
            f"Server: {guild['name']} ({guild['id']})",
            f"Timezone: {self.settings.timezone_name}",
            f"Period: {stamp(start)} to {stamp(end)} (end exclusive)",
            f"Generated: {stamp(self.clock())}",
            f"Tracking began: {stamp(guild['first_seen'])}",
            f"Online coverage observed in this day: {duration(data['coverage_seconds'])}",
            "Coverage: departures and voice time before tracking/during disconnection are unknown, not zero.",
            "Current-member join timestamps are imported from cache on startup (at most 2,000); those members may already have left again after import.",
            "Voice time means presence in a voice/stage channel, including mute/AFK; it does not measure speaking, audio or attention.",
            "Startup voice occupants have unknown join time; count 0 new joins until an actual join event is seen.",
            f"Audit coverage: {guild['audit_status']}",
            "Audit summaries are observations, not proof of malicious intent. Reasons, message contents and tokens are not retained.",
            f"Delivery queue status: {data['delivery']}",
            "",
            f"JOIN/LEAVE EVENTS — joins {counts.get('join',0)}, departures {counts.get('leave',0)} (first 1,000 details)",
        ]
        for row in data["members"]:
            source = json.loads(row["details"]).get("source", "gateway")
            lines.append(
                f"{stamp(row['at'])} | {row['kind']} | {json.dumps(row['name'],ensure_ascii=False)} ({row['user']}) | {source}"
            )
        lines += [
            "",
            "VOICE PER PERSON — top 500 by observed time; joins are server voice connections, moves are separate",
        ]
        for row in data["voices"]:
            lines.append(
                f"{json.dumps(row['name'],ensure_ascii=False)} ({row['user']}) | joins {row['joins']} | channel moves {row['moves']} | {duration(row['seconds'])}"
                + (" | currently connected" if row["active"] else "")
            )
        lines += ["", "REPEAT-PING INCIDENTS — first 200 details"]
        for row in data["incidents"]:
            detail = json.loads(row["details"])
            lines.append(
                f"{stamp(row['at'])} | {json.dumps(row['name'],ensure_ascii=False)} ({row['user']}) | message {row['target']} | deletion: {detail.get('delete_result',detail.get('deleted'))} | window mentions {detail.get('mentions_in_window')}"
            )
        lines += ["", "AUDIT COUNTS BY ACTOR/ACTION — top 200 combinations"]
        for row in data["audits"]:
            lines.append(
                f"{json.dumps(row['name'],ensure_ascii=False)} ({row['user']}) | {row['action']} | {row['n']}"
            )
        lines += ["", "RECENT AUDIT DETAILS — at most 200"]
        for row in data["audit_details"]:
            lines.append(
                f"{stamp(row['at'])} | {json.dumps(row['name'],ensure_ascii=False)} ({row['user']}) | {row['action']} | target {row['target']} | review: {', '.join(json.loads(row['details']).get('flags',[])) or 'no configured flag'}"
            )
        text = "\n".join(lines)
        if len(text.encode()) > 256000:
            text = (
                text.encode()[:255000].decode("utf-8", errors="ignore")
                + "\n[Detail export size limit reached. Aggregate counts may exceed listed details.]"
            )
        return embed, text

    async def deliver(self, *, limit=10):
        if self.delivery_lock.locked():
            return
        async with self.delivery_lock:
            jobs = await self.store.due(limit)
            for job in jobs:
                if not self.enabled(job["guild"]):
                    await self.store.failed(job["id"], permanent=True)
                    continue
                if job["guild"] not in self.guilds:
                    getter = getattr(self.bot, "get_guild", lambda identifier: None)
                    if getter(job["guild"]) is None:
                        await self.store.failed(job["id"], permanent=True)
                    else:
                        await self.store.postpone(job["id"])
                    continue
                payload = json.loads(job["payload"])
                try:
                    recipient = job["recipient"] or await self.resolve_owner()
                    user = self.bot.get_user(recipient) or await self.bot.fetch_user(
                        recipient
                    )
                    if job["kind"] in {"initial", "daily"}:
                        day = datetime.strptime(payload["day"], "%Y-%m-%d").date()
                        embed, text = await self.report(job["guild"], day)
                        if job["kind"] == "initial":
                            embed.title = (
                                f"At your service — today's first ledger ({day})"
                            )
                        await user.send(
                            embed=embed,
                            file=discord.File(
                                io.BytesIO(text.encode()),
                                filename=f"server-{job['guild']}-{day}.txt",
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        if job["kind"] == "warning":
                            text = (
                                f"A small housekeeping request from {payload['server']}: please keep @everyone/@here to once every three hours. "
                                + (
                                    "I removed the repeat-mention message. "
                                    if payload["deleted"]
                                    else "I could not remove it automatically; please tidy it up. "
                                )
                                + "Thank you for helping keep the server calm. This courtesy applies equally to members, administrators and the server owner."
                            )
                        elif job["kind"] == "incident":
                            text = f"Housekeeping report • Server {job['guild']}: {payload['name']} ({payload['user']}) repeated a mass mention. Deletion: {payload['delete_result']}. Further incidents will be counted in the daily ledger; I will avoid repeated DMs during this three-hour window."
                        else:
                            text = f"Audit review requested • Observed {datetime.fromtimestamp(job['created'],self.zone).isoformat(timespec='minutes')} • Server {job['guild']}: actor {payload['name']} ({payload['user']}), action {payload['action']}, target {payload['target']}. Recent same-action entries: {payload['recent_count']}. {'; '.join(payload['flags']) or 'Elevated activity in a ten-minute window'}. This may be legitimate administration; please review before taking action."
                        await user.send(
                            text[:1900], allowed_mentions=discord.AllowedMentions.none()
                        )
                    await self.store.delivered(job["id"])
                except (discord.Forbidden, discord.NotFound):
                    await self.store.failed(job["id"], permanent=True)
                    self.last_error = (
                        "A private delivery was blocked; check owner ID/DM settings."
                    )
                except Exception as exc:
                    await self.store.failed(job["id"])
                    self.last_error = f"Private delivery failed: {type(exc).__name__}"
                    log.warning(
                        "Butler private delivery failed: %s", type(exc).__name__
                    )
                await asyncio.sleep(0.25)

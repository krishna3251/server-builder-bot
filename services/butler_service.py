"""Server butler: metadata collection, conservative audit review, private reports."""

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta, timezone
import io
import json
import logging
import os
from collections import OrderedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from aiohttp import ClientConnectionError

from services.butler_store import ButlerStore
from services.automatic_moderation import (coordinator, env_enabled, is_trusted_channel,
    log_automatic_removal, private_mod_log, trap_owns, trusted_channel_ids)

log = logging.getLogger(__name__)
OWNER_DELIVERIES = frozenset({"initial", "daily", "audit_alert", "incident"})
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
    anti_ping_delete: bool = True
    trusted_channels: frozenset[int] = frozenset()
    mode: str | None = None

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
        from config import configured_id
        ids = frozenset(value for item in scope.split(",") if (value := configured_id(item, "BUTLER_GUILD_IDS")))
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
            env_enabled("BUTLER_ANTI_PING_DELETE"),
            trusted_channel_ids(),
            os.getenv("BUTLER_ANTI_PING_MODE", "").strip().lower() or None,
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
        self._app_owner = None
        self._owner_checked_at = 0
        self.guilds = set()
        self.delivery_lock = asyncio.Lock()
        self.audit_lock = asyncio.Lock()
        self.last_error = None
        self.audit_cursor = {}
        self.audit_capped = set()
        self._policies = OrderedDict()
        self._policy_lock = asyncio.Lock()

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
        return self.owner_id or await self.resolve_application_owner()

    async def can_read_report(self, guild, user):
        if user.id in {guild.owner_id, self.settings.owner_id}:
            return True
        return user.id == await self.resolve_application_owner()

    async def resolve_application_owner(self):
        if self._app_owner is None or self.clock() - self._owner_checked_at >= 300:
            app = await self.bot.application_info()
            self._app_owner = app.team.owner_id if app.team else app.owner.id
            self._owner_checked_at = self.clock()
        return self._app_owner

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

    async def policy(self, guild_id, update=None):
        mode = self.settings.mode
        if mode not in {"delete", "warn", "disabled"}:
            mode = "delete" if self.settings.anti_ping_delete else "disabled"
        defaults = {"mode": mode, "seconds": 10800,
                    "exceptions": sorted(self.settings.trusted_channels)}
        async with self._policy_lock:
            key = (guild_id, self.settings)
            if update is None and key in self._policies:
                self._policies.move_to_end(key)
                return self._policies[key]
            policy = await self.store.policy(guild_id, defaults, update)
            self._policies[key] = policy
            if len(self._policies) > 256:
                self._policies.popitem(last=False)
            return policy

    async def exempt(self, guild_id, channel):
        return is_trusted_channel(channel, (await self.policy(guild_id))["exceptions"])

    async def mention(self, message):
        guild = message.guild
        if (guild is None or not self.enabled(guild.id) or not message.mention_everyone
                or message.author.id == getattr(self.bot.user, "id", None)):
            return False
        policy = await self.policy(guild.id)
        if policy["mode"] == "disabled" or is_trusted_channel(
            getattr(message, "channel", None), policy["exceptions"]
        ) or await trap_owns(self.bot, message):
            return False
        decision = await self.store.ping(
            guild.id, message.author.id, message.author.display_name, message.id, policy["seconds"]
        )
        if decision is None:
            return False
        if decision.get("duplicate"):
            return True
        rule = f"Repeat mass mention within {duration(policy['seconds'])}"
        warn_only = policy["mode"] == "warn"
        if warn_only:
            reason = "Warn only"
            await log_automatic_removal(message, "Butler anti-ping", rule, reason)
        else:
            reason = await coordinator(self.bot).remove(message, "Butler anti-ping", rule)
        await self.store.complete_ping(
            guild.id, message.author.id, message.author.display_name, guild.name,
            decision, reason in {"Deleted", "Already absent"}, reason, warn_only=warn_only,
            dm_user=not message.author.bot and not message.webhook_id,
        )
        return True

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
        try:
            target = getattr(getattr(entry, "target", None), "id", 0) or 0
        except TypeError:
            if not isinstance(entry, discord.AuditLogEntry) or entry._target_id is not None:
                raise
            target = 0
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
            except (TimeoutError, ClientConnectionError):
                status = "partial: audit-log connection timed out or reset"
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
        report_dates = []
        for guild in guilds:
            if not self.enabled(guild["id"]) or guild["id"] not in self.guilds:
                continue
            for age in range(1, min(7, self.settings.retention_days) + 1):
                day = now.date() - timedelta(days=age)
                _, end = day_bounds(day, self.zone)
                if end <= guild["first_seen"] or age == 1 and now < due:
                    continue
                report_dates.append((guild["id"], str(day)))
        if report_dates:
            await self.store.enqueue_daily_reports(report_dates)

    async def report(self, guild, day):
        start, end = day_bounds(day, self.zone)
        data = await self.store.report(guild, start, end)
        return self.render(data, day, start, end)

    def render(self, data, day, start, end):
        guild = data["guild"]
        counts = data["counts"]
        title = f"Server maintenance report — {day}"
        description = (
            f"**{discord.utils.escape_markdown(guild['name'])[:100]}** · {self.settings.timezone_name}\n"
            f"**Activity** · {counts.get('join',0)} joins · {counts.get('leave',0)} departures · {duration(data['voice_total_seconds'])} in voice\n"
            f"**Review** · {counts.get('audit',0)} audit entries · {counts.get('moderation',0)} recorded enforcement actions · {counts.get('ping_violation',0)} repeat pings\n"
            f"**Coverage** · {duration(data['coverage_seconds'])} observed online · {guild['audit_status']}\n"
            "Open the TXT attachment for timestamps, actors, targets, and review flags."
        )
        embed = discord.Embed(title=title, description=description, color=0x5865F2)
        embed.set_footer(
            text="Private owner report · Activity and audit metadata"
        )

        def stamp(value):
            return datetime.fromtimestamp(value, self.zone).isoformat(
                timespec="seconds"
            )

        lines = [
            "SERVER MAINTENANCE REPORT",
            f"{day} | {self.settings.timezone_name}",
            f"Server: {json.dumps(guild['name'], ensure_ascii=False)} ({guild['id']})",
            f"Period: {stamp(start)} to {stamp(end)} (end exclusive)",
            f"Generated: {stamp(self.clock())}",
            "",
            "AT A GLANCE",
            f"Activity: {counts.get('join',0)} joins | {counts.get('leave',0)} departures | {duration(data['voice_total_seconds'])} in voice",
            f"Review: {counts.get('audit',0)} audit entries | {counts.get('moderation',0)} recorded enforcement actions | {counts.get('ping_violation',0)} repeat pings",
            f"Audit coverage: {guild['audit_status']}",
            f"Delivery queue status: {data['delivery']}",
            "",
            "COLLECTION COVERAGE",
            f"Tracking began: {stamp(guild['first_seen'])}",
            f"Online coverage observed in this day: {duration(data['coverage_seconds'])}",
            "Coverage: departures and voice time before tracking/during disconnection are unknown, not zero.",
            "Current-member join timestamps are imported from cache on startup (at most 2,000); those members may already have left again after import.",
            "Voice time means presence in a voice/stage channel, including mute/AFK; it does not measure speaking, audio or attention.",
            "Startup voice occupants have unknown join time; count 0 new joins until an actual join event is seen.",
            "Audit flags are for review. Discord audit reasons, message bodies, and voice audio are excluded.",
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
        lines += ["", "ENFORCEMENT OUTCOMES — first 500"]
        for row in data.get("moderation", []):
            actor = row['user'] or 'Discord'
            lines.append(f"{stamp(row['at'])} | {row['action']} | actor {actor} | target {row['target']} | {json.dumps(json.loads(row['details']).get('summary',''), ensure_ascii=False)}")
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
            reports = {}
            for job in jobs:
                if not self.enabled(job["guild"]):
                    await self.store.failed(job["id"], "disabled_guild", permanent=True)
                    continue
                if job["guild"] not in self.guilds:
                    getter = getattr(self.bot, "get_guild", lambda identifier: None)
                    if getter(job["guild"]) is None:
                        await self.store.failed(job["id"], "guild_unavailable", permanent=True)
                    else:
                        await self.store.postpone(job["id"])
                    continue
                try:
                    payload = json.loads(job["payload"])
                    if job["kind"] in OWNER_DELIVERIES:
                        guild = self.bot.get_guild(job["guild"])
                        if guild is None or guild.unavailable or not guild.owner_id:
                            await self.store.postpone(job["id"])
                            continue
                        application_owner = await self.resolve_application_owner()
                        if not isinstance(application_owner, int) or application_owner <= 0:
                            raise ValueError("Missing application owner for private delivery")
                        guild_owner = guild.owner_id
                        if job["recipient"] is None:
                            sibling = await self.store.assign_owner_delivery(job, application_owner, guild_owner)
                            if sibling:
                                jobs.append(sibling)
                            recipient = application_owner
                        elif job["recipient"] in {application_owner, guild_owner}:
                            recipient = job["recipient"]
                        else:
                            await self.store.failed(job["id"], "owner_changed", permanent=True)
                            continue
                    elif job["kind"] == "moderation" and not job["recipient"]:
                        recipient = await self.resolve_application_owner()
                    else:
                        recipient = job["recipient"] or await self.resolve_owner()
                    if not isinstance(recipient, int) or recipient <= 0:
                        raise ValueError("Missing delivery recipient")
                    user = self.bot.get_user(recipient) or await self.bot.fetch_user(
                        recipient
                    )
                    if job["kind"] in {"initial", "daily"}:
                        day = datetime.strptime(payload["day"], "%Y-%m-%d").date()
                        key = (job["guild"], day)
                        if key not in reports:
                            reports[key] = await self.report(*key)
                        embed, text = reports[key]
                        if job["kind"] == "initial":
                            embed = embed.copy()
                            embed.title = f"Today's first ledger — {day}"
                        await user.send(
                            embed=embed,
                            file=discord.File(
                                io.BytesIO(text.encode()),
                                filename=f"server-{job['guild']}-{day}.txt",
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        if job["kind"] == "moderation":
                            text = payload["summary"]
                        elif job["kind"] == "warning":
                            text = (
                                f"Please keep mass mentions in {payload['server']} to once every {duration(payload.get('cooldown_seconds', 10800))}. "
                                + (
                                    "I removed the repeat-mention message. "
                                    if payload["deleted"]
                                    else ("" if payload.get("warn_only") else "I could not remove it automatically; please tidy it up. ")
                                )
                                + "Thank you for helping keep the server calm. Configured channels are exempt."
                            )
                        elif job["kind"] == "incident":
                            text = f"Housekeeping report • Server {job['guild']}: {payload['name']} ({payload['user']}) repeated a mass mention. Deletion: {payload['delete_result']}. Further incidents will be counted in the daily ledger; I will avoid repeated DMs during this cooldown window."
                        else:
                            text = f"Audit review requested • Observed {datetime.fromtimestamp(job['created'],self.zone).isoformat(timespec='minutes')} • Server {job['guild']}: actor {payload['name']} ({payload['user']}), action {payload['action']}, target {payload['target']}. Recent same-action entries: {payload['recent_count']}. {'; '.join(payload['flags']) or 'Elevated activity in a ten-minute window'}. This may be legitimate administration; please review before taking action."
                        await user.send(
                            text[:1900], allowed_mentions=discord.AllowedMentions.none()
                        )
                    await self.store.delivered(job["id"])
                except Exception as exc:
                    reason, permanent, retry_after = delivery_failure(exc)
                    terminal = await self.store.failed(job["id"], reason, permanent=permanent, retry_after=retry_after)
                    self.last_error = f"Private delivery failed: {reason}"
                    log.warning("Butler delivery guild=%s kind=%s job=%s reason=%s",
                                job["guild"], job["kind"], job["id"], reason)
                    guild = getattr(self.bot, "get_guild", lambda identifier: None)(job["guild"])
                    if terminal and guild and job["kind"] in {"warning", "incident", "moderation"}:
                        embed = discord.Embed(
                            title="Private moderation notification failed",
                            description=f"Delivery type: {job['kind']}\nReason: {reason}\nCorrelation: {job['id']}",
                            timestamp=discord.utils.utcnow(),
                        )
                        await private_mod_log(guild, embed)
                await asyncio.sleep(0.25)


def delivery_failure(exc):
    if isinstance(exc, discord.RateLimited):
        return "rate_limited", False, exc.retry_after
    if isinstance(exc, discord.NotFound):
        return "missing_user" if exc.code in (0, 10013) else "discord_not_found", True, 0
    if isinstance(exc, discord.Forbidden):
        return "closed_dms" if exc.code == 50007 else "missing_permissions", True, 0
    if isinstance(exc, discord.HTTPException):
        if exc.status == 429:
            try:
                retry = float(exc.response.headers.get("Retry-After", "0"))
            except (AttributeError, ValueError, TypeError):
                retry = 0
            return "rate_limited", False, retry
        return "discord_api_error", exc.status < 500, 0
    if isinstance(exc, (TimeoutError, ClientConnectionError, OSError)):
        return "connection_failure", False, 0
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_configuration", True, 0
    return "unexpected_error", False, 0

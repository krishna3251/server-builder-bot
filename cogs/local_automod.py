"""Automatic moderation for links and burst image spam."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import error_embed, info_embed, success_embed, warning_embed
from services.moderation_notices import notify_action
from utils.interactions import acknowledged, respond
from utils.paginator import Paginator
from utils.storage import write_json_atomic

log = logging.getLogger("cogs.automod")

LINK_RE = re.compile(
    r"(?i)\b(?:https?://|www\.|discord\.gg/|discord\.com/invite/)[^\s<>()]+"
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")
IMAGE_WINDOW_SECONDS = 1.0
MAX_IMAGES_PER_WINDOW = 3
REPEAT_TIMEOUT_MINUTES = 10
OFFENSE_RESET_HOURS = 24
ROLE_IDS = "bypass_role_ids"
CHANNEL_IDS = "ignored_channel_ids"
ID_RE = re.compile(r"[0-9]{1,20}")
MENTION_RE = re.compile(r"<(@&|#)([0-9]{1,20})>")


def snowflake(value: object) -> int | None:
    if not isinstance(value, (int, str)) or not ID_RE.fullmatch(str(value)):
        return None
    identifier = int(value)
    return identifier if 0 < identifier < 2**64 else None


class LocalAutoModCog(commands.Cog, name="Local AutoMod"):
    """Optimized automatic moderation for common raid/spam patterns."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._image_windows: dict[tuple[int, int], deque[tuple[float, int]]] = defaultdict(deque)
        self._offenses: dict[str, Any] = {}
        self._settings: dict[str, Any] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._load_retry_at = 0.0

    async def _load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                raw = await asyncio.to_thread(Path(config.AUTOMOD_FILE).read_text, encoding="utf-8")
            except FileNotFoundError:
                raw = ""
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("AutoMod storage must contain an object.")
            wrapped = "offenses" in data or "settings" in data
            offenses = data.get("offenses", {}) if wrapped else data
            settings = data.get("settings", {}) if wrapped else {}
            if not isinstance(offenses, dict) or not isinstance(settings, dict):
                raise ValueError("AutoMod settings and offenses must contain objects.")
            normalized = {}
            for guild_id, guild_settings in settings.items():
                if not isinstance(guild_settings, dict):
                    raise ValueError("AutoMod guild settings must contain an object.")
                normalized[guild_id] = dict(guild_settings)
                for key in (CHANNEL_IDS, ROLE_IDS):
                    values = guild_settings.get(key, [])
                    if not isinstance(values, list):
                        raise ValueError("AutoMod exception IDs must contain a list.")
                    normalized[guild_id][key] = sorted({item for value in values if (item := snowflake(value))})
            self._offenses, self._settings = offenses, normalized
            self._loaded = True

    async def _save(self, *, offenses=None, settings=None) -> None:
        """Commit disk and memory together while the caller holds the lock."""
        offenses = self._offenses if offenses is None else offenses
        settings = self._settings if settings is None else settings

        async def commit():
            await write_json_atomic(config.AUTOMOD_FILE, {"offenses": offenses, "settings": settings})
            self._offenses, self._settings = offenses, settings

        task = asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").lower()
        filename = attachment.filename.lower()
        return content_type.startswith("image/") or filename.endswith(IMAGE_EXTENSIONS)

    @staticmethod
    def _is_exempt(member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.administrator or perms.manage_guild or perms.manage_messages

    def _guild_settings(self, guild_id: int) -> dict[str, Any]:
        return self._settings.get(str(guild_id), {})

    def _is_message_exempt(self, guild: discord.Guild, channel, member: discord.Member) -> bool:
        if member.bot or self._is_exempt(member):
            return True
        settings = self._guild_settings(guild.id)
        if settings.get("enabled") is not True:
            return True
        # Forum posts and threads inherit exceptions from their parent channel.
        channel_ids = {channel.id, getattr(channel, "category_id", None)}
        if isinstance(channel, discord.Thread):
            channel_ids.add(channel.parent_id)
            channel_ids.add(getattr(channel.parent, "category_id", None))
        return bool(
            channel_ids.intersection(settings.get(CHANNEL_IDS, []))
            or any(role.id in settings.get(ROLE_IDS, []) for role in member.roles)
        )

    def _count_recent_images(self, guild_id: int, user_id: int, image_count: int) -> int:
        now = time.monotonic()
        window = self._image_windows[(guild_id, user_id)]
        cutoff = now - IMAGE_WINDOW_SECONDS
        while window and window[0][0] < cutoff:
            window.popleft()
        if image_count:
            window.append((now, image_count))
        return sum(count for _, count in window)

    async def _record_offense(self, message: discord.Message, reason: str) -> int:
        guild = message.guild
        assert guild is not None
        now = datetime.datetime.now(datetime.timezone.utc)
        async with self._lock:
            member = guild.get_member(message.author.id) or message.author
            if self._is_message_exempt(guild, message.channel, member):
                return 0
            key = self._key(guild.id, member.id)
            record = dict(self._offenses.get(key, {"count": 0, "last_reason": "", "last_at": ""}))
            last_at = record.get("last_at")
            if last_at:
                try:
                    last_dt = datetime.datetime.fromisoformat(last_at)
                    if now - last_dt > datetime.timedelta(hours=OFFENSE_RESET_HOURS):
                        record["count"] = 0
                except (ValueError, TypeError):
                    record["count"] = 0
            record["count"] = int(record.get("count", 0)) + 1
            record["last_reason"] = reason
            record["last_at"] = now.isoformat()
            await self._save(offenses={**self._offenses, key: record})
            return int(record["count"])

    async def _dm_user_warning(self, member: discord.Member, reason: str, count: int) -> None:
        embed = warning_embed(
            "AutoMod Warning",
            (
                f"Your message in **{member.guild.name}** was removed.\n"
                f"Reason: **{reason}**\n"
                f"AutoMod offenses in the last {OFFENSE_RESET_HOURS}h: **{count}**"
            ),
        )
        try:
            await member.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _notify_owners(self, member: discord.Member, reason: str, count: int, *, failure: str | None = None) -> None:
        owner_ids = {member.guild.owner_id}
        embed = warning_embed(
            "AutoMod Timeout Failed" if failure else "AutoMod Timeout Applied",
            (
                f"Server: **{member.guild.name}** (`{member.guild.id}`)\n"
                f"User: {member.mention} (`{member.id}`)\n"
                f"Reason: **{reason}**\n"
                f"Offense count: **{count}**\n"
                f"Action: **{failure or '10 minute timeout'}**"
            ),
        )
        for user_id in owner_ids:
            if not user_id:
                continue
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
            except discord.HTTPException:
                pass

    async def _send_channel_notice(self, message: discord.Message, reason: str, timed_out: bool) -> None:
        if not isinstance(message.channel, discord.TextChannel):
            return
        action = "removed and timed out for 10 minutes" if timed_out else "removed and warned"
        try:
            await message.channel.send(
                embed=warning_embed(
                    "AutoMod Action",
                    f"{message.author.mention}, your message was {action}.\nReason: **{reason}**",
                ),
                delete_after=10,
            )
        except discord.HTTPException:
            pass

    async def _handle_violation(self, message: discord.Message, reason: str) -> None:
        guild = message.guild
        if guild is None or not isinstance(message.author, discord.Member):
            return

        member = guild.get_member(message.author.id) or message.author
        if self._is_message_exempt(guild, message.channel, member):
            return
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.HTTPException as error:
            log.warning("AutoMod could not remove a message in guild %s (HTTP %s)", guild.id, error.status)
            return

        count = await self._record_offense(message, reason)
        if not count:
            return
        timed_out = False
        member = guild.get_member(member.id) or member
        until = discord.utils.utcnow() + datetime.timedelta(minutes=REPEAT_TIMEOUT_MINUTES)
        if count >= 2 and not self._is_message_exempt(guild, message.channel, member) and (
            member.timed_out_until is None or member.timed_out_until < until
        ):
            try:
                await member.timeout(until, reason=f"AutoMod repeat offense: {reason}")
                timed_out = True
            except discord.Forbidden:
                await self._notify_owners(member, reason, count, failure="Check role hierarchy and Moderate Members")
            except discord.HTTPException:
                await self._notify_owners(member, reason, count, failure="Discord rejected the timeout")
            else:
                await notify_action(self.bot, guild, guild.me or self.bot.user, member,
                                    "AutoMod timeout", f"{reason} · offense {count}", message.id)

        await self._dm_user_warning(member, reason, count)
        await self._send_channel_notice(message, reason, timed_out)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or not isinstance(message.author, discord.Member) or message.author.bot:
            return
        if not self._loaded and time.monotonic() < self._load_retry_at:
            return
        try:
            await self._load()
        except (OSError, ValueError):
            now = time.monotonic()
            if now >= self._load_retry_at:
                self._load_retry_at = now + 60
                log.exception("AutoMod deferred enforcement because its settings could not be loaded.")
            return
        if self._is_message_exempt(message.guild, message.channel, message.author):
            return

        if LINK_RE.search(message.content or ""):
            await self._handle_violation(message, "Links are not allowed")
            return

        image_count = sum(1 for attachment in message.attachments if self._is_image_attachment(attachment))
        if image_count:
            total_images = self._count_recent_images(message.guild.id, message.author.id, image_count)
            if total_images > MAX_IMAGES_PER_WINDOW:
                await self._handle_violation(
                    message,
                    f"Image spam: more than {MAX_IMAGES_PER_WINDOW} images in {IMAGE_WINDOW_SECONDS:.0f}s",
                )

    @app_commands.command(name="automod_status", description="Show AutoMod settings and recent offense count.")
    @app_commands.describe(enabled="Manage Server only. Turn the bot's link and image-spam filter on or off.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def automod_status(self, interaction: discord.Interaction, enabled: bool | None = None) -> None:
        if not interaction.guild:
            return
        await self._load()
        if enabled is not None:
            if not interaction.user.guild_permissions.manage_guild:
                return await respond(interaction, "You need Manage Server to enable or disable this filter.")
            bot = interaction.guild.me
            if enabled and (bot is None or not (bot.guild_permissions.manage_messages and bot.guild_permissions.moderate_members)):
                return await respond(interaction, "The bot needs Manage Messages and Moderate Members to enable this filter.")
            async with self._lock:
                guild_key = str(interaction.guild.id)
                settings = {**self._guild_settings(interaction.guild.id), "enabled": enabled}
                await self._save(settings={**self._settings, guild_key: settings})
        prefix = f"{interaction.guild.id}:"
        total = sum(1 for key in self._offenses if key.startswith(prefix))
        settings = self._guild_settings(interaction.guild.id)
        ignored_channels = settings.get("ignored_channel_ids", [])
        bypass_roles = settings.get("bypass_role_ids", [])
        embed = info_embed(
            "AutoMod Status",
            (
                f"**Enabled:** {'yes' if settings.get('enabled') is True else 'no'}\n"
                "**Deletes:** links, image bursts over 3 images in 1 second\n"
                "**First offense:** delete + warn\n"
                "**Repeat offense:** 10 minute timeout + private owner notices\n"
                f"**Tracked users in this server:** {total}\n"
                f"**Ignored channels:** {len(ignored_channels)}\n"
                f"**Bypass roles:** {len(bypass_roles)}"
            ),
        )
        await respond(interaction, embed=embed, ephemeral=True)

    async def _show_exceptions(self, interaction: discord.Interaction) -> None:
        settings = self._guild_settings(interaction.guild_id)
        lines = []
        for label, key, prefix in (("Ignored channels", CHANNEL_IDS, "#"), ("Bypass roles", ROLE_IDS, "@&")):
            ids = settings.get(key, [])
            lines.append(f"**{label} ({len(ids)})**")
            lines.extend(f"<{prefix}{identifier}> (`{identifier}`)" for identifier in ids)
            if not ids:
                lines.append("None")
        pages = [
            info_embed("AutoMod Exceptions", "\n".join(lines[index:index + 60]))
            for index in range(0, len(lines), 60)
        ]
        if len(pages) == 1:
            await respond(interaction, embed=pages[0], allowed_mentions=discord.AllowedMentions.none())
        else:
            view = Paginator(pages, author_id=interaction.user.id)
            view.message = await respond(interaction, embed=pages[0], view=view,
                                         allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="automod_exception", description="List, add, or remove ignored channels and bypass roles.")
    @app_commands.describe(
        action="List exceptions by default. Select Add or Remove to edit.",
        target="Channel or role type for a raw ID",
        id_value="Existing ID or pasted mention",
        role="Select a bypass role by @mention",
        channel="Select an ignored channel or thread by #mention",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add", value="add"),
            app_commands.Choice(name="Remove", value="remove"),
            app_commands.Choice(name="List", value="list"),
        ],
        target=[
            app_commands.Choice(name="Ignored Channel", value="channel"),
            app_commands.Choice(name="Bypass Role", value="role"),
        ],
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @acknowledged(ephemeral=True)
    async def automod_exception(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str] | None = None,
        target: app_commands.Choice[str] | None = None,
        id_value: str | None = None,
        role: discord.Role | None = None,
        channel: app_commands.AppCommandChannel | app_commands.AppCommandThread | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        await self._load()
        if action is None and (role or channel or id_value):
            return await respond(interaction, "Select Add or Remove to change exceptions.")
        if action is None or action.value == "list":
            return await self._show_exceptions(interaction)

        role_id = role.id if role else None
        channel_id = channel.id if channel else None
        if id_value:
            if role_id or channel_id:
                return await respond(interaction, "Use the role/channel options or the ID field.")
            value = id_value.strip()
            mention = MENTION_RE.fullmatch(value)
            kind = target.value if target else None
            if mention:
                detected = "role" if mention[1] == "@&" else "channel"
                if kind and kind != detected:
                    return await respond(interaction, "The mention does not match the selected exception type.")
                kind, value = detected, mention[2]
            identifier = snowflake(value)
            if not kind or not identifier:
                return await respond(interaction, "Select a role or channel, paste its mention, or provide its ID and target type.")
            if kind == "role":
                role_id = identifier
            else:
                channel_id = identifier
        if not (role_id or channel_id):
            return await respond(interaction, "Select the role or channel to update.")

        add = action.value == "add"
        if add and role_id and (role_id == guild.id or not (role or guild.get_role(role_id))):
            return await respond(interaction, "Choose an existing role in this server other than @everyone.")
        if add and channel_id and channel is None and guild.get_channel_or_thread(channel_id) is None:
            try:
                await guild.fetch_channel(channel_id)
            except (discord.HTTPException, discord.InvalidData):
                return await respond(interaction, "Choose a channel or thread the bot can find in this server.")

        guild_key = str(guild.id)
        changed = []
        async with self._lock:
            settings = dict(self._guild_settings(guild.id))
            for key, identifier, prefix in ((ROLE_IDS, role_id, "@&"), (CHANNEL_IDS, channel_id, "#")):
                if identifier is None:
                    continue
                values = set(settings.get(key, []))
                if add == (identifier in values):
                    continue
                if add:
                    values.add(identifier)
                else:
                    values.discard(identifier)
                settings[key] = sorted(values)
                changed.append(f"<{prefix}{identifier}>")
            if changed:
                await self._save(settings={**self._settings, guild_key: settings})

        verb = "Added" if add else "Removed"
        detail = f"{verb} {', '.join(changed)}." if changed else "The exceptions already match your selection."
        await respond(interaction, embed=success_embed("AutoMod Exceptions", detail),
                      allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="automod_reset", description="Reset AutoMod offenses for a member.")
    @app_commands.describe(user="Member whose AutoMod offenses should be cleared")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def automod_reset(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild:
            return
        await self._load()
        async with self._lock:
            offenses = dict(self._offenses)
            if offenses.pop(self._key(interaction.guild.id, user.id), None) is not None:
                await self._save(offenses=offenses)
        await respond(interaction, embed=success_embed(
            "AutoMod Reset", f"AutoMod offenses cleared for {user.mention}."
        ), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LocalAutoModCog(bot))

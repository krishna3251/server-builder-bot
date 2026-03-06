"""Moderation cog — kick, ban, mute, warn, clear, lock, unlock, slowmode."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiofiles
import discord
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import error_embed, success_embed, warning_embed
from services.permission_service import bot_can_act, is_above
from utils.decorators import guild_only, mod_only
from utils.helpers import parse_duration

log = logging.getLogger("cogs.moderation")

# ── Persistent warnings store ─────────────────────────────────────────────
_warnings: dict[str, list[dict[str, Any]]] = {}
_warnings_loaded: bool = False


async def _load_warnings() -> None:
    global _warnings, _warnings_loaded
    if _warnings_loaded:
        return
    if os.path.exists(config.WARNINGS_FILE):
        try:
            async with aiofiles.open(config.WARNINGS_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _warnings = json.loads(raw) if raw.strip() else {}
        except Exception:
            _warnings = {}
    _warnings_loaded = True


async def _save_warnings() -> None:
    async with aiofiles.open(config.WARNINGS_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_warnings, indent=2))


def _warn_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}_{user_id}"


async def _mod_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Send an embed to the configured mod-log channel, if it exists."""
    if not config.MOD_LOG_CHANNEL_ID:
        return
    ch = guild.get_channel(config.MOD_LOG_CHANNEL_ID)
    if ch and isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


async def _dm_user(user: discord.User | discord.Member, embed: discord.Embed) -> None:
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


class ModerationCog(commands.Cog, name="Moderation"):
    """Full-featured moderation suite."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /kick ─────────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="Member to kick", reason="Reason for kick")
    @mod_only()
    @guild_only()
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided") -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_above(interaction.user, user):
            return await interaction.response.send_message(embed=error_embed("Error", "You cannot kick someone with a higher role."), ephemeral=True)
        if not bot_can_act(interaction.guild, user):
            return await interaction.response.send_message(embed=error_embed("Error", "I cannot kick this user (role hierarchy)."), ephemeral=True)

        dm_em = warning_embed("Kicked", f"You were kicked from **{interaction.guild.name}**.\nReason: {reason}")
        await _dm_user(user, dm_em)
        await user.kick(reason=reason)

        em = success_embed("Kicked", f"**{user}** has been kicked.\nReason: {reason}")
        await interaction.response.send_message(embed=em)
        log_em = warning_embed("Member Kicked", f"**{user}** by {interaction.user.mention}\nReason: {reason}")
        await _mod_log(interaction.guild, log_em)

    # ── /ban ──────────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(user="Member to ban", reason="Reason for ban", delete_days="Days of messages to delete (0-7)")
    @mod_only()
    @guild_only()
    async def ban(
        self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided", delete_days: int = 0
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_above(interaction.user, user):
            return await interaction.response.send_message(embed=error_embed("Error", "You cannot ban someone with a higher role."), ephemeral=True)
        if not bot_can_act(interaction.guild, user):
            return await interaction.response.send_message(embed=error_embed("Error", "I cannot ban this user (role hierarchy)."), ephemeral=True)

        dm_em = warning_embed("Banned", f"You were banned from **{interaction.guild.name}**.\nReason: {reason}")
        await _dm_user(user, dm_em)
        await interaction.guild.ban(user, reason=reason, delete_message_days=min(delete_days, 7))

        em = success_embed("Banned", f"**{user}** has been banned.\nReason: {reason}")
        await interaction.response.send_message(embed=em)
        log_em = warning_embed("Member Banned", f"**{user}** by {interaction.user.mention}\nReason: {reason}")
        await _mod_log(interaction.guild, log_em)

    # ── /unban ────────────────────────────────────────────────────────────
    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="The user ID to unban")
    @mod_only()
    @guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str) -> None:
        assert interaction.guild is not None
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"Unbanned by {interaction.user}")
            em = success_embed("Unbanned", f"**{user}** has been unbanned.")
            await interaction.response.send_message(embed=em)
        except discord.NotFound:
            await interaction.response.send_message(embed=error_embed("Error", "User not found or not banned."), ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(embed=error_embed("Error", str(exc)), ephemeral=True)

    # ── /mute ─────────────────────────────────────────────────────────────
    @app_commands.command(name="mute", description="Timeout a member.")
    @app_commands.describe(user="Member to mute", duration="Duration (e.g. 10m, 2h, 1d)", reason="Reason")
    @mod_only()
    @guild_only()
    async def mute(
        self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str = "No reason provided"
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_above(interaction.user, user):
            return await interaction.response.send_message(embed=error_embed("Error", "You cannot mute someone with a higher role."), ephemeral=True)
        if not bot_can_act(interaction.guild, user):
            return await interaction.response.send_message(embed=error_embed("Error", "I cannot mute this user."), ephemeral=True)

        try:
            td = parse_duration(duration)
        except ValueError:
            return await interaction.response.send_message(embed=error_embed("Error", "Invalid duration format. Use e.g. `10m`, `2h`, `1d`."), ephemeral=True)

        await user.timeout(td, reason=reason)
        dm_em = warning_embed("Muted", f"You were muted in **{interaction.guild.name}** for {duration}.\nReason: {reason}")
        await _dm_user(user, dm_em)

        em = success_embed("Muted", f"**{user}** muted for **{duration}**.\nReason: {reason}")
        await interaction.response.send_message(embed=em)
        log_em = warning_embed("Member Muted", f"**{user}** by {interaction.user.mention} for {duration}\nReason: {reason}")
        await _mod_log(interaction.guild, log_em)

    # ── /unmute ───────────────────────────────────────────────────────────
    @app_commands.command(name="unmute", description="Remove timeout from a member.")
    @app_commands.describe(user="Member to unmute")
    @mod_only()
    @guild_only()
    async def unmute(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild is not None
        await user.timeout(None, reason=f"Unmuted by {interaction.user}")
        em = success_embed("Unmuted", f"**{user}** has been unmuted.")
        await interaction.response.send_message(embed=em)

    # ── /clear ────────────────────────────────────────────────────────────
    @app_commands.command(name="clear", description="Bulk delete messages.")
    @app_commands.describe(amount="Number of messages to delete (1-100)", user="Only delete messages from this user")
    @mod_only()
    @guild_only()
    async def clear(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], user: discord.Member | None = None
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.channel, discord.TextChannel)
        await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            return user is None or m.author.id == user.id

        deleted = await interaction.channel.purge(limit=amount, check=check)
        em = success_embed("Cleared", f"Deleted **{len(deleted)}** messages.")
        await interaction.followup.send(embed=em, ephemeral=True)

    # ── /warn ─────────────────────────────────────────────────────────────
    @app_commands.command(name="warn", description="Warn a member.")
    @app_commands.describe(user="Member to warn", reason="Reason for warning")
    @mod_only()
    @guild_only()
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided") -> None:
        assert interaction.guild is not None
        await _load_warnings()
        k = _warn_key(interaction.guild.id, user.id)
        if k not in _warnings:
            _warnings[k] = []
        _warnings[k].append({
            "reason": reason,
            "moderator": interaction.user.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await _save_warnings()

        count = len(_warnings[k])
        dm_em = warning_embed("Warning", f"You received a warning in **{interaction.guild.name}**.\nReason: {reason}\nTotal warnings: {count}")
        await _dm_user(user, dm_em)

        em = success_embed("Warned", f"**{user}** warned (#{count}).\nReason: {reason}")
        await interaction.response.send_message(embed=em)

        log_em = warning_embed("Member Warned", f"**{user}** by {interaction.user.mention}\nReason: {reason}\nTotal: {count}")
        await _mod_log(interaction.guild, log_em)

        # Auto-mute on 3rd warning
        if count >= 3 and count % 3 == 0:
            try:
                td = parse_duration("1h")
                await user.timeout(td, reason=f"Auto-mute: {count} warnings reached")
                auto_em = warning_embed("Auto-Muted", f"**{user}** auto-muted for 1h after reaching {count} warnings.")
                await interaction.followup.send(embed=auto_em)
            except Exception:
                pass

    # ── /warnings ─────────────────────────────────────────────────────────
    @app_commands.command(name="warnings", description="View warnings for a member.")
    @app_commands.describe(user="Member to check")
    @mod_only()
    @guild_only()
    async def warnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild is not None
        await _load_warnings()
        k = _warn_key(interaction.guild.id, user.id)
        warns = _warnings.get(k, [])
        if not warns:
            return await interaction.response.send_message(embed=success_embed("No Warnings", f"**{user}** has no warnings."), ephemeral=True)

        lines: list[str] = []
        for i, w in enumerate(warns, 1):
            lines.append(f"**{i}.** {w['reason']} — <t:{int(datetime.fromisoformat(w['timestamp']).timestamp())}:R>")
        em = warning_embed(f"Warnings for {user}", "\n".join(lines))
        await interaction.response.send_message(embed=em)

    # ── /clearwarnings ────────────────────────────────────────────────────
    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.describe(user="Member to clear warnings for")
    @mod_only()
    @guild_only()
    async def clearwarnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild is not None
        await _load_warnings()
        k = _warn_key(interaction.guild.id, user.id)
        _warnings.pop(k, None)
        await _save_warnings()
        em = success_embed("Cleared", f"All warnings for **{user}** have been removed.")
        await interaction.response.send_message(embed=em)

    # ── /slowmode ─────────────────────────────────────────────────────────
    @app_commands.command(name="slowmode", description="Set slowmode on a channel.")
    @app_commands.describe(channel="Target channel", seconds="Slowmode delay in seconds (0 to disable)")
    @mod_only()
    @guild_only()
    async def slowmode(self, interaction: discord.Interaction, channel: discord.TextChannel, seconds: app_commands.Range[int, 0, 21600]) -> None:
        await channel.edit(slowmode_delay=seconds)
        em = success_embed("Slowmode", f"Slowmode for {channel.mention} set to **{seconds}s**.")
        await interaction.response.send_message(embed=em)

    # ── /lock ─────────────────────────────────────────────────────────────
    @app_commands.command(name="lock", description="Lock a channel (prevent sending messages).")
    @app_commands.describe(channel="Channel to lock")
    @mod_only()
    @guild_only()
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        em = success_embed("Locked", f"{channel.mention} has been locked.")
        await interaction.response.send_message(embed=em)

    # ── /unlock ───────────────────────────────────────────────────────────
    @app_commands.command(name="unlock", description="Unlock a channel.")
    @app_commands.describe(channel="Channel to unlock")
    @mod_only()
    @guild_only()
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        em = success_embed("Unlocked", f"{channel.mention} has been unlocked.")
        await interaction.response.send_message(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))

"""Moderation cog — kick, ban, mute, warn, clear, lock, unlock, slowmode."""

from __future__ import annotations

from utils.interactions import acknowledged, respond, defer_response

import asyncio
import copy
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiofiles
import discord
from services.error_service import SafeView
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import error_embed, info_embed, success_embed, warning_embed
from utils.storage import write_json_atomic
from utils.paginator import Paginator
from services.moderation_notices import notify_action
from services.spam_guard import PrivilegedSpamGuard
from services.permission_service import bot_can_act, is_above
from utils.decorators import guild_only, mod_only
from utils.helpers import parse_duration

log = logging.getLogger("cogs.moderation")

# ── Persistent warnings store ───────────────────────────────────────────────
_warnings: dict[str, list[dict[str, Any]]] = {}
_warnings_loaded: bool = False
_warnings_lock = asyncio.Lock()


async def _load_warnings() -> None:
    global _warnings, _warnings_loaded
    if _warnings_loaded:
        return
    if os.path.exists(config.WARNINGS_FILE):
        try:
            async with aiofiles.open(config.WARNINGS_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _warnings = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            raise RuntimeError("Warning data could not be read; refusing to overwrite it.") from exc
    _warnings_loaded = True


async def _save_warnings() -> None:
    await write_json_atomic(config.WARNINGS_FILE, _warnings)


def _warn_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}_{user_id}"


async def _mod_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Send an embed to the configured mod-log channel, if it exists."""
    if not config.MOD_LOG_CHANNEL_ID:
        return
    ch = guild.get_channel(config.MOD_LOG_CHANNEL_ID)
    if ch and isinstance(ch, discord.TextChannel):
        if ch.permissions_for(guild.default_role).view_channel:
            log.warning("Public moderation log skipped: guild=%s channel=%s", guild.id, ch.id)
            return
        for target, overwrite in ch.overwrites.items():
            if overwrite.view_channel is not True:
                continue
            permissions = getattr(target, 'guild_permissions', getattr(target, 'permissions', discord.Permissions.none()))
            if target.id not in (guild.owner_id, guild.me.id) and not (
                permissions.administrator or permissions.manage_guild or permissions.moderate_members
            ):
                log.warning("Moderation log with nonstaff access skipped: guild=%s channel=%s", guild.id, ch.id)
                return
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


async def _dm_user(user: discord.User | discord.Member, embed: discord.Embed) -> None:
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


class ClearWarningsConfirmView(SafeView):
    """Require an explicit confirmation before deleting a member's warning history."""

    def __init__(self, *, guild_id: int, member: discord.Member, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.member = member
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await respond(interaction,
                "Only the moderator who started this action can confirm it.",
                ephemeral=True,
            )
            return False
        perms = interaction.user.guild_permissions
        if not (perms.kick_members or perms.ban_members or perms.manage_messages):
            await respond(interaction, "Your moderation permission is no longer available.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Clear warnings", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Defer the component update before loading and saving the warning store.
        await defer_response(interaction, )
        async with _warnings_lock:
            await _load_warnings()
            key = _warn_key(self.guild_id, self.member.id)
            previous = _warnings.pop(key, None)
            try:
                await _save_warnings()
            except Exception:
                if previous is not None:
                    _warnings[key] = previous
                raise
        for child in self.children:
            child.disabled = True
        em = success_embed(
            "Warning history cleared",
            f"All recorded warnings for **{self.member.display_name}** have been removed.",
        )
        await interaction.edit_original_response(embed=em, view=self)
        self.stop()

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=info_embed("Action cancelled", "The member's warning history was not changed."),
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=info_embed("Confirmation expired", "The warning history was left unchanged."),
                    view=self,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class ModerationCog(commands.Cog, name="Moderation"):
    """Full-featured moderation suite."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.spam_guard = PrivilegedSpamGuard()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        batch, alert = self.spam_guard.observe(message)
        if not batch:
            return
        guild = message.guild
        assert guild is not None
        # Delete by captured ID, even in different channels or threads. A channel
        # history purge could also pick up innocent messages between copies.
        by_channel = {}
        for item in batch:
            by_channel.setdefault(item.channel, []).append(item)
        missed = 0
        for channel, items in by_channel.items():
            if len(items) > 1 and isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    await channel.delete_messages(items)
                    continue
                except discord.Forbidden:
                    missed += len(items)
                    continue
                except discord.HTTPException:
                    # A concurrent delete or failed batch must not skip surviving copies.
                    pass
            for item in items:
                try:
                    await item.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    missed += 1
        if missed:
            log.warning("Spam cleanup incomplete: guild=%s author=%s unconfirmed=%s",
                        guild.id, message.author.id, missed)
        if alert:
            log.warning("Privileged-account spam detected: guild=%s author=%s matched=%s",
                        guild.id, message.author.id, len(batch))
            details = (
                f"Server ID: {guild.id} • Account ID: {message.author.id}\n"
                f"Flagged messages: {len(batch)} • Removal unconfirmed: {missed}\n"
                "If this activity was unexpected, secure the account and review its active sessions."
            )
            embed = warning_embed("Possible privileged-account spam", details)
            await _mod_log(guild, embed)
            await _dm_user(message.author, embed)
            if guild.owner is not None and guild.owner_id != message.author.id:
                await _dm_user(guild.owner, embed)

    # ── /kick ─────────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="Member to kick", reason="Reason for kick")
    @app_commands.checks.has_permissions(kick_members=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided") -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_above(interaction.user, user):
            return await respond(interaction, embed=error_embed("Error", "You cannot kick someone with a higher role."), ephemeral=True)
        if not bot_can_act(interaction.guild, user):
            return await respond(interaction, embed=error_embed("Error", "I cannot kick this user (role hierarchy)."), ephemeral=True)

        dm_em = warning_embed("Kicked", f"You were kicked from **{interaction.guild.name}**.\nReason: {reason}")
        await user.kick(reason=reason)
        await _dm_user(user, dm_em)
        await notify_action(self.bot, interaction.guild, interaction.user, user, "kick", reason, interaction.id)

        em = success_embed("Kicked", f"**{user}** has been kicked.\nReason: {reason}")
        await respond(interaction, embed=em)
        log_em = warning_embed("Member Kicked", f"**{user}** by {interaction.user.mention}\nReason: {reason}")
        await _mod_log(interaction.guild, log_em)

    # ── /ban ──────────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(user="Member to ban", reason="Reason for ban", delete_days="Days of messages to delete (0-7)")
    @app_commands.checks.has_permissions(ban_members=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_above(interaction.user, user):
            return await respond(interaction, embed=error_embed("Error", "You cannot ban someone with a higher role."), ephemeral=True)
        if not bot_can_act(interaction.guild, user):
            return await respond(interaction, embed=error_embed("Error", "I cannot ban this user (role hierarchy)."), ephemeral=True)

        dm_em = warning_embed("Banned", f"You were banned from **{interaction.guild.name}**.\nReason: {reason}")
        await interaction.guild.ban(user, reason=reason, delete_message_days=min(delete_days, 7))
        await _dm_user(user, dm_em)
        await notify_action(self.bot, interaction.guild, interaction.user, user, "ban", reason, interaction.id)

        em = success_embed("Banned", f"**{user}** has been banned.\nReason: {reason}")
        await respond(interaction, embed=em)
        log_em = warning_embed("Member Banned", f"**{user}** by {interaction.user.mention}\nReason: {reason}")
        await _mod_log(interaction.guild, log_em)

    # ── /unban ────────────────────────────────────────────────────────────
    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="The user ID to unban")
    @app_commands.checks.has_permissions(ban_members=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str) -> None:
        assert interaction.guild is not None
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"Unbanned by {interaction.user}")
            em = success_embed("Unbanned", f"**{user}** has been unbanned.")
            await respond(interaction, embed=em)
        except discord.NotFound:
            await respond(interaction, embed=error_embed("Error", "User not found or not banned."), ephemeral=True)
        except ValueError:
            await respond(interaction, embed=error_embed("Invalid user ID", "Enter a numeric Discord user ID."), ephemeral=True)

    # ── /mute ─────────────────────────────────────────────────────────────
    @app_commands.command(name="mute", description="Timeout a member.")
    @app_commands.describe(user="Member to mute", duration="Duration (e.g. 10m, 2h, 1d)", reason="Reason")
    @app_commands.checks.has_permissions(moderate_members=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def mute(
        self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str = "No reason provided"
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not is_above(interaction.user, user):
            return await respond(interaction, embed=error_embed("Error", "You cannot mute someone with a higher role."), ephemeral=True)
        if not bot_can_act(interaction.guild, user):
            return await respond(interaction, embed=error_embed("Error", "I cannot mute this user."), ephemeral=True)

        if user.guild_permissions.administrator:
            return await respond(interaction, embed=error_embed("Timeout unavailable", "Discord exempts Administrator accounts from timeouts."), ephemeral=True)
        try:
            td = parse_duration(duration)
        except ValueError:
            return await respond(interaction, embed=error_embed("Error", "Invalid duration format. Use e.g. `10m`, `2h`, `1d`."), ephemeral=True)

        if td.total_seconds() <= 0 or td.total_seconds() > 28 * 86400:
            return await respond(interaction, embed=error_embed("Invalid timeout", "Choose a duration greater than zero and no longer than 28 days."), ephemeral=True)
        await user.timeout(td, reason=reason)
        await notify_action(self.bot, interaction.guild, interaction.user, user, "timeout", f"{duration} • {reason}", interaction.id)
        dm_em = warning_embed("Muted", f"You were muted in **{interaction.guild.name}** for {duration}.\nReason: {reason}")
        await _dm_user(user, dm_em)

        em = success_embed("Muted", f"**{user}** muted for **{duration}**.\nReason: {reason}")
        await respond(interaction, embed=em)
        log_em = warning_embed("Member Muted", f"**{user}** by {interaction.user.mention} for {duration}\nReason: {reason}")
        await _mod_log(interaction.guild, log_em)

    # ── /unmute ───────────────────────────────────────────────────────────
    @app_commands.command(name="unmute", description="Remove timeout from a member.")
    @app_commands.describe(user="Member to unmute")
    @app_commands.checks.has_permissions(moderate_members=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild is not None
        if not is_above(interaction.user, user) or not bot_can_act(interaction.guild, user):
            return await respond(interaction, embed=error_embed("Role hierarchy", "You and the bot must outrank this member."), ephemeral=True)
        await user.timeout(None, reason=f"Unmuted by {interaction.user}")
        em = success_embed("Unmuted", f"**{user}** has been unmuted.")
        await respond(interaction, embed=em)

    # ── /clear ────────────────────────────────────────────────────────────
    @app_commands.command(name="clear", description="Bulk delete messages.")
    @app_commands.describe(amount="Number of messages to delete (1-100)", user="Only delete messages from this user")
    @app_commands.checks.has_permissions(manage_messages=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def clear(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], user: discord.Member | None = None
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.channel, (discord.TextChannel, discord.Thread))
        await defer_response(interaction, ephemeral=True)

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
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided") -> None:
        assert interaction.guild is not None
        if not is_above(interaction.user, user):
            return await respond(interaction, embed=error_embed("Role hierarchy", "You must outrank the member you warn."), ephemeral=True)
        async with _warnings_lock:
            await _load_warnings()
            k = _warn_key(interaction.guild.id, user.id)
            previous = copy.deepcopy(_warnings.get(k))
            _warnings.setdefault(k, []).append({"reason": reason[:2000], "moderator": interaction.user.id, "timestamp": datetime.now(timezone.utc).isoformat()})
            try:
                await _save_warnings()
            except Exception:
                if previous is None:
                    _warnings.pop(k, None)
                else:
                    _warnings[k] = previous
                raise
            count = len(_warnings[k])
        dm_em = warning_embed("Warning", f"You received a warning in **{interaction.guild.name}**.\nReason: {reason}\nTotal warnings: {count}")
        await _dm_user(user, dm_em)

        em = success_embed("Warned", f"**{user}** warned (#{count}).\nReason: {reason}")
        await respond(interaction, embed=em)

        log_em = warning_embed("Member Warned", f"**{user}** by {interaction.user.mention}\nReason: {reason}\nTotal: {count}")
        await _mod_log(interaction.guild, log_em)

        # Auto-mute on 3rd warning
        if count >= 3 and count % 3 == 0 and interaction.guild.me.guild_permissions.moderate_members and not user.guild_permissions.administrator and interaction.user.guild_permissions.moderate_members and bot_can_act(interaction.guild, user) and is_above(interaction.user, user):
            try:
                td = parse_duration("1h")
                await user.timeout(td, reason=f"Auto-mute: {count} warnings reached")
                await notify_action(self.bot, interaction.guild, interaction.user, user, "automatic timeout", f"1 hour after {count} warnings", interaction.id)
                auto_em = warning_embed("Auto-Muted", f"**{user}** auto-muted for 1h after reaching {count} warnings.")
                await interaction.followup.send(embed=auto_em, ephemeral=True)
            except Exception:
                pass

    # ── /warnings ─────────────────────────────────────────────────────────
    @app_commands.command(name="warnings", description="View warnings for a member.")
    @app_commands.describe(user="Member to check")
    @mod_only()
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild is not None
        await _load_warnings()
        k = _warn_key(interaction.guild.id, user.id)
        warns = copy.deepcopy(_warnings.get(k, []))
        if not warns:
            return await respond(interaction, embed=success_embed("No Warnings", f"**{user}** has no warnings."), ephemeral=True)

        pages: list[discord.Embed] = []
        for offset in range(0, len(warns), 5):
            chunk = warns[offset:offset + 5]
            lines: list[str] = []
            for index, item in enumerate(chunk, start=offset + 1):
                reason = str(item.get("reason", "No reason provided")).replace("\n", " ").strip()
                if len(reason) > 280:
                    reason = reason[:277] + "..."
                timestamp = datetime.fromisoformat(item["timestamp"])
                lines.append(
                    f"**#{index}** · <t:{int(timestamp.timestamp())}:R>\n"
                    f"> {discord.utils.escape_markdown(reason)}"
                )
            page = warning_embed(f"Warning history · {user.display_name}", "\n\n".join(lines))
            page.set_thumbnail(url=user.display_avatar.url)
            page.add_field(name="Total warnings", value=str(len(warns)), inline=True)
            pages.append(page)

        if len(pages) == 1:
            return await respond(interaction, embed=pages[0], ephemeral=True)
        view = Paginator(pages, author_id=interaction.user.id)
        await respond(interaction, embed=pages[0], view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # ── /clearwarnings ────────────────────────────────────────────────────
    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.describe(user="Member to clear warnings for")
    @mod_only()
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def clearwarnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild is not None
        await _load_warnings()
        k = _warn_key(interaction.guild.id, user.id)
        count = len(_warnings.get(k, []))
        if count == 0:
            return await respond(interaction,
                embed=success_embed("No warnings to clear", f"**{user.display_name}** has no recorded warnings."),
                ephemeral=True,
            )

        view = ClearWarningsConfirmView(
            guild_id=interaction.guild.id,
            member=user,
            owner_id=interaction.user.id,
        )
        em = warning_embed(
            "Confirm warning reset",
            f"This will permanently remove **{count}** warning(s) recorded for {user.mention}. "
            "This action cannot be undone.",
        )
        await respond(interaction, embed=em, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # ── /slowmode ─────────────────────────────────────────────────────────
    @app_commands.command(name="slowmode", description="Set slowmode on a channel.")
    @app_commands.describe(channel="Target channel", seconds="Slowmode delay in seconds (0 to disable)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, channel: discord.TextChannel, seconds: app_commands.Range[int, 0, 21600]) -> None:
        await channel.edit(slowmode_delay=seconds)
        em = success_embed("Slowmode", f"Slowmode for {channel.mention} set to **{seconds}s**.")
        await respond(interaction, embed=em)

    # ── /lock ─────────────────────────────────────────────────────────────
    @app_commands.command(name="lock", description="Lock a channel (prevent sending messages).")
    @app_commands.describe(channel="Channel to lock")
    @app_commands.checks.has_permissions(manage_roles=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        em = success_embed("Locked", f"{channel.mention} has been locked.")
        await respond(interaction, embed=em)

    # ── /unlock ───────────────────────────────────────────────────────────
    @app_commands.command(name="unlock", description="Unlock a channel.")
    @app_commands.describe(channel="Channel to unlock")
    @app_commands.checks.has_permissions(manage_roles=True)
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        em = success_embed("Unlocked", f"{channel.mention} has been unlocked.")
        await respond(interaction, embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))

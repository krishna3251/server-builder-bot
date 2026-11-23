"""A configurable top-of-server trap channel using the existing reversible quarantine.

No automatic role creation: /quarantine_setup must be completed first.
Moderation records never include message content and are delivered privately.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.interactions import acknowledged, respond
from utils.storage import write_json_atomic

log = logging.getLogger(__name__)
CHANNEL_NAME = "dont-write-there"
WARNING = (
    "🚫 **DO NOT WRITE HERE**\n"
    "This channel is a moderation trap. Regular members who post here "
    "will have their message deleted and may be quarantined for staff review.\n"
    "Server owner and Administrators are exempt."
)
STAFF_REPLY = "Malik, kyun likh rahe ho? 👀"
REPLY_COOLDOWN = 20.0


def is_admin(member: discord.Member) -> bool:
    return member.id == member.guild.owner_id or member.guild_permissions.administrator


def is_staff_role(role: discord.Role) -> bool:
    perms = role.permissions
    return bool(perms.administrator or perms.manage_guild or
                perms.moderate_members or perms.manage_messages)


def private_log_channel(channel: discord.TextChannel, mod_role: discord.Role) -> bool:
    """Reject a log destination visible to ordinary roles or explicit member grants."""
    guild = channel.guild
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    if not channel.permissions_for(mod_role).view_channel:
        return False
    for role in guild.roles:
        if role.id == mod_role.id or role.is_default() or is_staff_role(role):
            continue
        if channel.permissions_for(role).view_channel:
            return False
    for subject in channel.overwrites:
        if isinstance(subject, discord.Member) and subject.id not in (
            guild.owner_id, guild.me.id if guild.me else 0
        ) and not is_admin(subject) and channel.permissions_for(subject).view_channel:
            return False
    return True


def trap_log_embed(guild: discord.Guild, cfg: dict, member_id: int,
                   message_id: int, action: str) -> discord.Embed:
    """Create an easy-to-scan private moderation record, without message content."""
    is_error = "ERROR:" in action
    is_partial = "PARTIAL" in action
    is_quarantine = "Quarantined" in action and not is_error
    is_exempt = "exempt" in action.lower()
    title = (
        "🚨 Moderation action failed" if is_error else
        "⚠️ Quarantine needs review" if is_partial else
        "🔒 Member quarantined" if is_quarantine else
        "🛡️ Administrator exempt" if is_exempt else
        "📋 Trap-channel moderation log"
    )
    colour = (
        discord.Colour.red() if is_error else
        discord.Colour.orange() if is_partial else
        discord.Colour.green() if is_quarantine else
        discord.Colour.gold() if is_exempt else
        discord.Colour.blurple()
    )
    embed = discord.Embed(
        title=title,
        description=action[:1000],
        colour=colour,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    label = "Staff member" if message_id == 0 else "Member"
    embed.add_field(name=label, value=f"<@{member_id}>\n**User ID:** \`{member_id}\`", inline=False)
    channel_id = cfg.get("channel_id")
    if channel_id:
        embed.add_field(name="Protected channel", value=f"<#{channel_id}>", inline=True)
    if message_id:
        embed.add_field(name="Message ID", value=f"\`{message_id}\`", inline=True)
    embed.add_field(
        name="Server",
        value=f"{discord.utils.escape_markdown(guild.name)[:100]}\nID: \`{guild.id}\`",
        inline=False,
    )
    embed.set_footer(text="Server Builder • Soft Banned Diddy • Private staff log")
    return embed


class SoftBannedDiddy(commands.Cog, name="Soft Banned Diddy"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.path = Path(config.DATA_DIR) / "soft_banned_diddy.json"
        self._lock = asyncio.Lock()
        self._settings: dict[str, dict] = {}
        self._loaded = False
        self._load_retry_at = 0.0
        self._inflight: set[tuple[int, int]] = set()
        self._recent_quarantines: dict[tuple[int, int], float] = {}
        self._staff_replies: dict[tuple[int, int], float] = {}

    async def _load(self) -> None:
        if self._loaded:
            return
        if time.monotonic() < self._load_retry_at:
            raise ValueError("Saved channel settings could not be loaded.")
        async with self._lock:
            if self._loaded:
                return
            try:
                def read():
                    if not self.path.exists():
                        return {}
                    return json.loads(self.path.read_text(encoding="utf-8"))
                data = await asyncio.to_thread(read)
                if not isinstance(data, dict) or any(
                    not isinstance(value, dict) for value in data.values()
                ):
                    raise ValueError("Invalid soft-banned channel configuration")
                self._settings = data
                self._loaded = True
            except (ValueError, OSError, json.JSONDecodeError):
                self._load_retry_at = time.monotonic() + 60
                log.exception("Could not load protected-channel configuration")
                raise

    async def _save(self, new_settings: dict) -> None:
        # Caller holds the lock; publish new state only after a durable write.
        await write_json_atomic(str(self.path), new_settings)
        self._settings = new_settings

    def _cfg(self, guild_id: int) -> dict:
        return self._settings.get(str(guild_id), {})

    def _security(self):
        return self.bot.get_cog("Security")

    async def _quarantine_ready(self, guild: discord.Guild) -> tuple[bool, str]:
        security = self._security()
        if security is None:
            return False, "The Security cog is not loaded."
        service = security.quarantine_service
        async with service.lock:
            state = await service.load()
            record = state.get(str(guild.id), {})
            role = guild.get_role(record.get("role", 0))
            if not record.get("configured") or not all((
                role, guild.get_channel(record.get("category", 0)),
                guild.get_channel(record.get("room", 0))
            )):
                return False, "Run /quarantine_setup before enabling this feature."
            if role.managed or role >= guild.me.top_role or role.permissions.value != 0:
                return False, "The saved quarantine role is no longer safe or editable."
        return True, ""

    async def _log_action(self, guild: discord.Guild, cfg: dict, member_id: int,
                          message_id: int, action: str) -> None:
        # Do not include original messages, attachments or sensitive content in logs.
        embed = trap_log_embed(guild, cfg, member_id, message_id, action)
        channel = guild.get_channel(cfg.get("log_channel_id", 0))
        mod_role = guild.get_role(cfg.get("mod_role_id", 0))
        if isinstance(channel, discord.TextChannel) and mod_role and guild.me and (
            private_log_channel(channel, mod_role)
        ):
            permissions = channel.permissions_for(guild.me)
            if permissions.view_channel and permissions.send_messages and permissions.embed_links:
                try:
                    await channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                except discord.HTTPException:
                    log.warning("Soft Banned Diddy embed delivery failed for guild %s", guild.id)
            else:
                log.warning(
                    "Soft Banned Diddy log channel lacks View Channel, Send Messages "
                    "or Embed Links in guild %s", guild.id,
                )
        # If the channel is deleted or becomes public, deliver only in a private DM.
        embed.add_field(
            name="Delivery",
            value="Private log channel unavailable or missing Embed Links; sent to the server owner.",
            inline=False,
        )
        try:
            owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
            await owner.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError):
            log.error("Soft Banned Diddy could not deliver a private log for guild %s", guild.id)

    async def _startup_audit(self) -> None:
        """Non-destructive restore check: the saved enabled flag is authoritative."""
        try:
            await self._load()
        except (ValueError, OSError) as exc:
            log.error(
                "Trap restart check failed to read %s (%s); no configuration was enabled. "
                "Restore the data file from persistent storage.",
                self.path, type(exc).__name__,
            )
            return

        if not self.path.is_file():
            log.warning(
                "No trap settings file at %s. If you configured this before a restart, "
                "Wispbyte did not preserve this file or BOT_DATA_DIR points elsewhere.",
                self.path,
            )
            return

        configured = sum(bool(cfg.get("enabled")) for cfg in self._settings.values())
        log.info(
            "Trap restart check: loaded %d saved servers (%d enabled) from %s; "
            "saved settings are reused without a slash command.",
            len(self._settings), configured, self.path,
        )

        for guild in self.bot.guilds:
            cfg = self._cfg(guild.id)
            if not cfg:
                continue
            if not cfg.get("enabled", False):
                log.info("Trap remains intentionally disabled for guild %s.", guild.id)
                continue
            channel = guild.get_channel(cfg.get("channel_id", 0))
            me = guild.me
            if not isinstance(channel, discord.TextChannel) or me is None:
                log.error(
                    "Trap loaded but cannot run in guild %s: saved channel or bot "
                    "member missing. Run /soft_banned_diddy_status.",
                    guild.id,
                )
                continue
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.manage_messages):
                log.error(
                    "Trap loaded but cannot moderate guild %s: missing View Channel "
                    "or Manage Messages. Run /soft_banned_diddy_status.",
                    guild.id,
                )
                continue
            try:
                ready, reason = await self._quarantine_ready(guild)
            except (ValueError, OSError, discord.HTTPException) as exc:
                log.error(
                    "Trap restored but quarantine journal is unreadable in guild %s "
                    "(%s). Restore the saved data directory.",
                    guild.id, type(exc).__name__,
                )
                continue
            if ready:
                log.info(
                    "Trap restored automatically for guild %s, channel %s. "
                    "No setup command required.",
                    guild.id, channel.id,
                )
            else:
                log.error(
                    "Trap enabled but quarantine unavailable in guild %s: %s",
                    guild.id, reason,
                )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Reload settings once when the bot connects, not only on first message.
        # Never auto-create or enable a channel merely because its name matches.
        await self._startup_audit()

    @app_commands.command(name="soft_banned_diddy", description="Set up the top trap channel and private moderation logs.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.cooldown(2, 30, key=lambda i: (i.guild_id, i.user.id))
    @acknowledged(ephemeral=True)
    async def soft_banned_diddy(self, interaction: discord.Interaction,
                                log_channel: discord.TextChannel, mod_role: discord.Role,
                                channel: discord.TextChannel | None = None):
        guild = interaction.guild
        me = guild.me
        if not me or not all((
            me.guild_permissions.manage_channels, me.guild_permissions.manage_roles,
            me.guild_permissions.manage_messages
        )):
            return await respond(interaction, "Bot needs Manage Channels, Manage Roles and Manage Messages.", ephemeral=True)
        if log_channel.guild.id != guild.id or mod_role.guild.id != guild.id or (
            channel is not None and channel.guild.id != guild.id
        ):
            return await respond(interaction, "Select a log channel and role from this server.", ephemeral=True)
        if mod_role.is_default() or mod_role.managed or not is_staff_role(mod_role):
            return await respond(interaction, "Select an existing, editable moderator role with moderation permissions.", ephemeral=True)
        if not private_log_channel(log_channel, mod_role):
            return await respond(interaction, "The log channel must be private to staff, with access for the selected moderator role.", ephemeral=True)
        if not all((log_channel.permissions_for(me).view_channel,
                    log_channel.permissions_for(me).send_messages,
                    log_channel.permissions_for(me).embed_links)):
            return await respond(interaction, "Bot needs View Channel, Send Messages and Embed Links in the selected private log channel.", ephemeral=True)
        ready, reason = await self._quarantine_ready(guild)
        if not ready:
            return await respond(interaction, reason, ephemeral=True)
        try:
            await self._load()
        except (ValueError, OSError):
            return await respond(interaction, "Saved settings are unreadable; no changes made. Restore the backup first.", ephemeral=True)
        saved = self._cfg(guild.id)
        target = channel or guild.get_channel(saved.get("channel_id", 0))
        if target is not None and not isinstance(target, discord.TextChannel):
            target = None
        if target and (target.id == log_channel.id or target.id in (
            (await self._security().quarantine_service.load()).get(str(guild.id), {}).get("room", 0),
        )):
            return await respond(interaction, "Choose a different channel from logs and quarantine review.", ephemeral=True)
        if target is None and any(c.name == CHANNEL_NAME for c in guild.text_channels):
            return await respond(interaction, "A channel named dont-write-there already exists. Select it using the channel option to confirm reuse.", ephemeral=True)
        created = False
        try:
            if target is None:
                target = await guild.create_text_channel(
                    CHANNEL_NAME,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True,
                            create_public_threads=False, create_private_threads=False,
                            send_messages_in_threads=False
                        ),
                        me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        manage_messages=True)
                    },
                    position=0,
                    reason=f"Soft Banned Diddy setup by {interaction.user.id}"
                )
                created = True
            if not (target.permissions_for(guild.default_role).view_channel and
                    target.permissions_for(guild.default_role).send_messages):
                return await respond(interaction, "The selected channel must be visible and writable to @everyone. No protections enabled.", ephemeral=True)
            if not all((target.permissions_for(me).view_channel,
                        target.permissions_for(me).send_messages,
                        target.permissions_for(me).read_message_history,
                        target.permissions_for(me).manage_messages)):
                return await respond(interaction, "Bot needs View, Send and Manage Messages in the selected channel.", ephemeral=True)
            # Move only a deliberately selected/saved channel; no other channels are reordered.
            await target.edit(category=None, position=0, reason="Keep the warning channel at the top")
            await self._security().quarantine_service.channel_changed(target)
            note = None
            if saved.get("channel_id") == target.id and saved.get("warning_message_id"):
                try:
                    candidate = await target.fetch_message(saved["warning_message_id"])
                    if candidate.author.id == me.id:
                        note = candidate
                except discord.NotFound:
                    pass
            if note is None:
                note = await target.send(WARNING, allowed_mentions=discord.AllowedMentions.none())
            elif note.content != WARNING:
                await note.edit(content=WARNING, allowed_mentions=discord.AllowedMentions.none())
            if not note.pinned:
                await note.pin(reason="Persistent trap-channel warning")
            async with self._lock:
                new_settings = dict(self._settings)
                new_settings[str(guild.id)] = {
                    "enabled": True,
                    "channel_id": target.id,
                    "log_channel_id": log_channel.id,
                    "mod_role_id": mod_role.id,
                    "warning_message_id": note.id
                }
                await self._save(new_settings)
        except (discord.HTTPException, ValueError, OSError) as exc:
            log.warning("Soft Banned Diddy setup incomplete in guild %s: %s", guild.id, type(exc).__name__)
            # Do not activate the trap on partial setup.
            return await respond(interaction, "Setup was incomplete. Check bot permissions and retry. Existing config was not activated by this attempt.", ephemeral=True)
        await self._log_action(guild, self._cfg(guild.id), interaction.user.id, 0, "Trap configured")
        verb = "created" if created else "updated"
        await respond(interaction, f"Trap channel {target.mention} {verb} and moved to the top. "
                      f"Quarantine is active; logs go to {log_channel.mention}. "
                      f"Moderator role: {mod_role.mention}. "
                      "Use /quarantine_release to reverse an automatic quarantine.", ephemeral=True)

    @app_commands.command(name="soft_banned_diddy_disable", description="Turn off the trap without deleting the channel or saved quarantine history.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def soft_banned_diddy_disable(self, interaction: discord.Interaction):
        try:
            await self._load()
            async with self._lock:
                cfg = self._cfg(interaction.guild.id)
                if not cfg:
                    return await respond(interaction, "This server has no trap configuration.", ephemeral=True)
                new_settings = dict(self._settings)
                new_settings[str(interaction.guild.id)] = dict(cfg, enabled=False)
                await self._save(new_settings)
            await self._log_action(interaction.guild, self._cfg(interaction.guild.id),
                                   interaction.user.id, 0, "Trap disabled")
            await respond(interaction, "Trap enforcement disabled. No channel or quarantine records were deleted.", ephemeral=True)
        except (ValueError, OSError):
            await respond(interaction, "Cannot safely update settings; existing configuration remains unchanged.", ephemeral=True)

    @app_commands.command(name="soft_banned_diddy_status", description="Check trap-channel configuration and permissions.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def soft_banned_diddy_status(self, interaction: discord.Interaction):
        try:
            await self._load()
        except (ValueError, OSError):
            return await respond(interaction, "Config unreadable. Enforcement is temporarily unavailable.", ephemeral=True)
        cfg = self._cfg(interaction.guild.id)
        if not cfg:
            return await respond(
                interaction,
                "Not configured for this server. Saved settings path: "
                f"{self.path} (file exists: {self.path.is_file()}). "
                "If setup worked before a restart, check Wispbyte data persistence and "
                "BOT_DATA_DIR. Otherwise run /quarantine_setup, then /soft_banned_diddy once.",
                ephemeral=True,
            )
        try:
            ready, reason = await self._quarantine_ready(interaction.guild)
        except (ValueError, OSError, discord.HTTPException) as exc:
            ready, reason = False, f"Cannot read quarantine journal ({type(exc).__name__})"
        channel = interaction.guild.get_channel(cfg.get("channel_id", 0))
        me = interaction.guild.me
        permissions_ok = bool(
            isinstance(channel, discord.TextChannel) and me
            and channel.permissions_for(me).view_channel
            and channel.permissions_for(me).manage_messages
        )
        await respond(interaction,
                      f"Enabled: {bool(cfg.get('enabled'))}\nChannel: <#{cfg.get('channel_id', 0)}>"
                      f"\nPrivate logs: <#{cfg.get('log_channel_id', 0)}>"
                      f"\nModerator role: <@&{cfg.get('mod_role_id', 0)}>"
                      f"\nSaved settings file: {self.path} (exists: {self.path.is_file()})"
                      f"\nChannel/permissions ready: {permissions_ok}"
                      f"\nQuarantine ready: {ready} {reason}"
                      "\nNo setup command is needed after restart if the saved files are present and both checks are ready.",
                      ephemeral=True,
                      allowed_mentions=discord.AllowedMentions.none())

    async def _process(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        try:
            await self._load()
        except (ValueError, OSError):
            return
        cfg = self._cfg(guild.id)
        if not cfg.get("enabled") or message.channel.id != cfg.get("channel_id"):
            return
        member = message.author
        if not isinstance(member, discord.Member):
            try:
                member = await guild.fetch_member(message.author.id)
            except discord.HTTPException:
                # Unknown privileges: never treat an unresolved author as regular.
                return
        if is_admin(member):
            key = (guild.id, member.id)
            now = time.monotonic()
            if key not in self._staff_replies or now - self._staff_replies[key] >= REPLY_COOLDOWN:
                self._staff_replies[key] = now
                try:
                    await message.channel.send(STAFF_REPLY, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning("Could not reply to administrator in guild %s", guild.id)
            await self._log_action(guild, cfg, member.id, message.id, "Administrator/owner exempt")
            return
        me = guild.me
        if not me or not message.channel.permissions_for(me).manage_messages:
            await self._log_action(guild, cfg, member.id, message.id, "ERROR: cannot delete message")
            return
        try:
            # discord.Message/PartialMessage.delete accepts delay, not audit-log reason.
            # Deletion itself does not offer a Discord audit reason; log separately.
            await message.delete()
        except discord.NotFound:
            return
        except discord.HTTPException:
            await self._log_action(guild, cfg, member.id, message.id, "ERROR: message deletion failed")
            return
        security = self._security()
        if security is None:
            await self._log_action(guild, cfg, member.id, message.id, "ERROR: Security cog unavailable")
            return
        key = (guild.id, member.id)
        if key in self._inflight:
            await self._log_action(guild, cfg, member.id, message.id, "Deleted; quarantine already in progress")
            return
        if key in self._recent_quarantines and time.monotonic() - self._recent_quarantines[key] < 45:
            await self._log_action(guild, cfg, member.id, message.id, "Deleted; quarantine recently applied")
            return
        self._inflight.add(key)
        try:
            record = await security.quarantine_service.quarantine(
                guild, me, member, f"Posted in {CHANNEL_NAME} (message {message.id})")
            if record.get("phase") == "active":
                self._recent_quarantines[key] = time.monotonic()
            outcome = "Quarantined" if record.get("phase") == "active" else "PARTIAL quarantine; staff review required"
        except (ValueError, discord.HTTPException, OSError) as exc:
            log.warning("Trap quarantine failed in guild %s: %s", guild.id, type(exc).__name__)
            outcome = f"ERROR: quarantine failed ({type(exc).__name__}); staff review required"
        finally:
            self._inflight.discard(key)
        await self._log_action(guild, cfg, member.id, message.id, "Deleted; " + outcome)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await self._process(message)
        except Exception:
            log.exception("Unexpected trap-channel listener failure")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if not payload.guild_id:
            return
        try:
            await self._load()
            cfg = self._cfg(payload.guild_id)
            if not cfg.get("enabled") or cfg.get("channel_id") != payload.channel_id:
                return
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                return
            message = await channel.fetch_message(payload.message_id)
            await self._process(message)
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception:
            log.exception("Unexpected trap-channel message-edit failure")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SoftBannedDiddy(bot))

"""Safe automatic moderation policy and private message-action receipts."""
from __future__ import annotations

from collections import OrderedDict
import logging
import os
import time

import discord
import config
from services.moderation_notices import record_action

log = logging.getLogger(__name__)


def env_enabled(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def trusted_channel_ids() -> frozenset[int]:
    return frozenset(identifier for value in os.getenv("AUTO_MOD_TRUSTED_CHANNEL_IDS", "").split(",")
                     if (identifier := config.configured_id(value, "AUTO_MOD_TRUSTED_CHANNEL_IDS")))


def is_trusted_channel(channel, ids: frozenset[int]) -> bool:
    return any(identifier in ids for identifier in (
        getattr(channel, "id", None),
        getattr(channel, "category_id", None),
        getattr(channel, "parent_id", None),
        getattr(getattr(channel, "parent", None), "category_id", None),
    ))


class ModerationCoordinator:
    """Coalesce message deletions across the bot's independent listeners."""

    def __init__(self, bot=None):
        self.bot = bot
        self.claimed = OrderedDict()
        self.native_timeouts = OrderedDict()

    def record_native_timeout(self, guild_id, user_id):
        key = (guild_id, user_id)
        self.native_timeouts[key] = time.monotonic()
        self.native_timeouts.move_to_end(key)
        if len(self.native_timeouts) > 2048:
            self.native_timeouts.popitem(last=False)

    def recent_native_timeout(self, guild_id, user_id):
        return time.monotonic() - self.native_timeouts.get((guild_id, user_id), float("-inf")) < 10

    def claim(self, message, module):
        now = time.monotonic()
        while self.claimed and next(iter(self.claimed.values()))[0] < now - 600:
            self.claimed.popitem(last=False)
        key = (message.guild.id, message.id)
        if key in self.claimed:
            return False
        self.claimed[key] = (now, module)
        if len(self.claimed) > 8192:
            self.claimed.popitem(last=False)
        return True

    async def remove(self, message, module, reason):
        if not self.claim(message, module):
            return "Already handled by another listener"
        guild, channel = message.guild, getattr(message, "channel", None)
        if isinstance(guild, discord.Guild) and (
            guild.me is None or not channel.permissions_for(guild.me).manage_messages
        ):
            result = "missing Manage Messages permission"
        else:
            try:
                await message.delete()
            except discord.NotFound:
                result = "Already absent"
            except discord.Forbidden:
                result = "missing Manage Messages permission"
            except discord.RateLimited as exc:
                result = f"Discord rate limited deletion ({exc.retry_after:.0f}s)"
            except discord.HTTPException as exc:
                result = f"Discord deletion failed (HTTP {exc.status})"
            else:
                result = "Deleted"
        await log_automatic_removal(message, module, reason, result, bot=self.bot)
        return result


def coordinator(bot):
    existing = getattr(bot, "moderation_coordinator", None)
    if not isinstance(existing, ModerationCoordinator):
        existing = bot.moderation_coordinator = ModerationCoordinator(bot)
    return existing


async def trap_owns(bot, message):
    trap = getattr(bot, "get_cog", lambda name: None)("Soft Banned Diddy")
    from cogs.soft_banned_diddy import SoftBannedDiddy
    return isinstance(trap, SoftBannedDiddy) and await trap.owns_message(message)


def butler_cog(bot):
    from cogs.butler import ButlerCog
    cog = getattr(bot, "get_cog", lambda name: None)("Server Butler")
    return cog if isinstance(cog, ButlerCog) else None


def privileged_spam_mode() -> str:
    value = os.getenv("PRIVILEGED_SPAM_MODE", "alert").strip().lower()
    return value if value in {"off", "alert", "delete"} else "alert"


async def private_mod_log(guild, embed):
    """Never post moderation metadata to a public or non-staff channel."""
    if not config.MOD_LOG_CHANNEL_ID:
        return False
    channel = guild.get_channel(config.MOD_LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel) or guild.me is None:
        return False
    if channel.permissions_for(guild.default_role).view_channel:
        log.warning("Public moderation log skipped: guild=%s channel=%s", guild.id, channel.id)
        return False
    for role in guild.roles:
        perms = role.permissions
        if channel.permissions_for(role).view_channel and not (
            perms.administrator or perms.manage_guild or perms.moderate_members
            or role.id in {r.id for r in guild.me.roles}
        ):
            log.warning("Nonstaff moderation log skipped: guild=%s channel=%s", guild.id, channel.id)
            return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True:
            continue
        perms = getattr(target, "guild_permissions", getattr(target, "permissions", discord.Permissions.none()))
        if target.id not in (guild.owner_id, guild.me.id) and not (
            perms.administrator or perms.manage_guild or perms.moderate_members
        ):
            return False
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        log.warning("Moderation log lacks bot permissions: guild=%s channel=%s", guild.id, channel.id)
        return False
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.HTTPException, discord.RateLimited):
        log.warning("Moderation receipt delivery failed: guild=%s channel=%s", guild.id, channel.id)
        return False


async def log_automatic_removal(message, module: str, reason: str, result: str, *, bot=None):
    guild = message.guild
    if guild is None:
        return
    channel_id = getattr(getattr(message, "channel", None), "id", 0)
    user_id = message.author.id
    log.info("Automatic moderation: module=%s guild=%s channel=%s user=%s message=%s result=%s",
             module, guild.id, channel_id, user_id, message.id, result)
    if bot is not None and module != "Butler anti-ping":  # Repeat pings already have their own ledger section.
        await record_action(bot, guild, getattr(getattr(guild, 'me', None), 'id', 0), user_id,
                            module, f"{reason} | {result} | Channel {channel_id} | Message {message.id}",
                            f"removal:{guild.id}:{message.id}:{module}")
    if not config.MOD_LOG_CHANNEL_ID:
        return
    embed = discord.Embed(title="Automatic message moderation",
                          description=reason, colour=discord.Colour.orange(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Module", value=module)
    embed.add_field(name="Action and result", value=result)
    me = getattr(guild, "me", None)
    embed.add_field(name="Actor", value=f"<@{me.id}> • `{me.id}`" if getattr(me, "id", None) else "Bot")
    embed.add_field(name="Exception", value="No")
    embed.add_field(name="Correlation ID", value=f"`message:{guild.id}:{message.id}`", inline=False)
    embed.add_field(name="User", value=f"<@{user_id}> • `{user_id}`", inline=False)
    embed.add_field(name="Channel", value=f"<#{channel_id}> • `{channel_id}`")
    embed.add_field(name="Message ID", value=str(message.id))
    await private_mod_log(guild, embed)

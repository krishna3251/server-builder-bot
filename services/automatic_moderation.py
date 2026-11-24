"""Safe automatic moderation policy and private message-action receipts."""
from __future__ import annotations

import logging
import os

import discord
import config

log = logging.getLogger(__name__)


def env_enabled(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def trusted_channel_ids() -> frozenset[int]:
    return frozenset(int(value.strip()) for value in
                     os.getenv("AUTO_MOD_TRUSTED_CHANNEL_IDS", "").split(",")
                     if value.strip().isdigit())


def is_trusted_channel(channel, ids: frozenset[int]) -> bool:
    return (getattr(channel, "id", None) in ids
            or getattr(channel, "parent_id", None) in ids)


def privileged_spam_mode() -> str:
    value = os.getenv("PRIVILEGED_SPAM_MODE", "alert").strip().lower()
    return value if value in {"off", "alert", "delete"} else "alert"


async def private_mod_log(guild, embed):
    """Never post moderation metadata to a public or non-staff channel."""
    if not config.MOD_LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(config.MOD_LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel) or guild.me is None:
        return
    if channel.permissions_for(guild.default_role).view_channel:
        log.warning("Public moderation log skipped: guild=%s channel=%s", guild.id, channel.id)
        return
    for role in guild.roles:
        perms = role.permissions
        if channel.permissions_for(role).view_channel and not (
            perms.administrator or perms.manage_guild or perms.moderate_members
            or role.id in {r.id for r in guild.me.roles}
        ):
            log.warning("Nonstaff moderation log skipped: guild=%s channel=%s", guild.id, channel.id)
            return
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True:
            continue
        perms = getattr(target, "guild_permissions", getattr(target, "permissions", discord.Permissions.none()))
        if target.id not in (guild.owner_id, guild.me.id) and not (
            perms.administrator or perms.manage_guild or perms.moderate_members
        ):
            return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning("Moderation receipt delivery failed: guild=%s channel=%s", guild.id, channel.id)


async def log_automatic_removal(message, module: str, reason: str, result: str):
    guild = message.guild
    if guild is None:
        return
    channel_id = getattr(getattr(message, "channel", None), "id", 0)
    user_id = message.author.id
    log.info("Automatic moderation: module=%s guild=%s channel=%s user=%s message=%s result=%s",
             module, guild.id, channel_id, user_id, message.id, result)
    if not config.MOD_LOG_CHANNEL_ID:
        return
    embed = discord.Embed(title="Automatic message moderation",
                          description=reason, colour=discord.Colour.orange(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Module", value=module)
    embed.add_field(name="Result", value=result)
    embed.add_field(name="User", value=f"<@{user_id}> • `{user_id}`", inline=False)
    embed.add_field(name="Channel", value=f"<#{channel_id}> • `{channel_id}`")
    embed.add_field(name="Message ID", value=str(message.id))
    await private_mod_log(guild, embed)

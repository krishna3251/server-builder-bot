"""Permission service — helpers for checking and documenting required permissions."""

from __future__ import annotations

import discord


AUTOMATIC_ROLE_RESTRICTED = frozenset({
    "administrator", "manage_guild", "manage_channels", "manage_roles",
    "manage_messages", "manage_threads", "manage_webhooks", "manage_nicknames",
    "manage_events", "manage_expressions", "create_events", "create_expressions",
    "ban_members", "kick_members", "moderate_members", "mute_members",
    "deafen_members", "move_members", "mention_everyone", "view_audit_log",
    "view_guild_insights",
})


def can_assign_reward_role(guild, role) -> bool:
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles or role.is_default() or role.managed or role >= me.top_role:
        return False
    if any(getattr(role.permissions, name) for name in AUTOMATIC_ROLE_RESTRICTED):
        return False
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        if any(getattr(overwrite, name) is True for name in AUTOMATIC_ROLE_RESTRICTED):
            return False
    return True


def can_moderate(member: discord.Member) -> bool:
    """Return ``True`` if the member has basic moderation perms."""
    p = member.guild_permissions
    return p.kick_members or p.ban_members or p.manage_messages


def is_above(actor: discord.Member, target: discord.Member) -> bool:
    """Return ``True`` if *actor*'s top role is higher than *target*'s."""
    return actor.id != target.id and target.id != target.guild.owner_id and (actor.id == actor.guild.owner_id or actor.top_role > target.top_role)


def bot_can_act(guild: discord.Guild, target: discord.Member) -> bool:
    """Return ``True`` if the bot's top role outranks *target*."""
    me = guild.me
    if me is None:
        return False
    return target.id != guild.owner_id and target.id != me.id and me.top_role > target.top_role

"""Permission service — helpers for checking and documenting required permissions."""

from __future__ import annotations

import discord


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

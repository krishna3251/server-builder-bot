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


def can_assign_role(guild: discord.Guild, role: discord.Role, *, actor: discord.Member | None = None,
                    automated: bool = False) -> bool:
    """Check bot hierarchy, caller hierarchy, and the permissions of automatic roles."""
    bot = guild.me
    if (bot is None or not bot.guild_permissions.manage_roles or role.is_default()
            or role.managed or role >= bot.top_role):
        return False
    if actor is not None and actor.id != guild.owner_id and role >= actor.top_role:
        return False
    if automated:
        return not any(getattr(role.permissions, permission) for permission in (
            "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
            "moderate_members", "manage_messages", "ban_members", "kick_members", "view_audit_log",
            "mention_everyone",
        ))
    return True

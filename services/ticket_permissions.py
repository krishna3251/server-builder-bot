"""Check that saved ticket details are only sent to a staff channel."""
from __future__ import annotations

import discord


def ticket_log_is_private(channel: discord.TextChannel, staff_role_ids: set[int]) -> bool:
    guild = channel.guild
    if guild.id in staff_role_ids or channel.permissions_for(guild.default_role).view_channel:
        return False
    if any(role.id not in staff_role_ids and not role.permissions.administrator
           and channel.permissions_for(role).view_channel for role in guild.roles):
        return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True or isinstance(target, discord.Role):
            continue
        if target.id == guild.owner_id or guild.me is not None and target.id == guild.me.id:
            continue
        if not isinstance(target, discord.Member) or not (
            target.guild_permissions.administrator or staff_role_ids.intersection(role.id for role in target.roles)
        ):
            return False
    return True

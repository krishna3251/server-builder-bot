"""Explain selected Discord permissions using the live channel and member objects."""

from __future__ import annotations

import asyncio
from typing import Literal

import discord

AccessAction = Literal["view", "post", "join_voice", "manage_messages", "manage_channel", "manage_permissions"]


def _role_names(roles):
    names = [discord.utils.escape_markdown(role.name)[:100] for role in roles[:4]]
    if len(roles) > 4:
        names.append(f"+{len(roles) - 4} more")
    return ", ".join(names)


def _permission_source(channel, member, permission):
    if member.id == member.guild.owner_id:
        return "Server ownership grants this permission."
    roles = [role for role in member.roles if not role.is_default()]
    administrators = [role for role in member.roles if role.permissions.administrator]
    if administrators:
        return f"Administrator from {_role_names(administrators)} bypasses channel overwrites."

    member_value = getattr(channel.overwrites_for(member), permission)
    if member_value is not None:
        return f"Member overwrite {'allows' if member_value else 'denies'} this permission."

    role_values = [(role, getattr(channel.overwrites_for(role), permission)) for role in roles]
    allows = [role for role, value in role_values if value is True]
    denies = [role for role, value in role_values if value is False]
    if allows:
        explanation = f"Role overwrite allows this permission via {_role_names(allows)}."
        return explanation + (f" This takes precedence over role denies from {_role_names(denies)}." if denies else "")
    if denies:
        return f"Role overwrite denies this permission via {_role_names(denies)}."

    everyone = member.guild.default_role
    everyone_value = getattr(channel.overwrites_for(everyone), permission)
    if everyone_value is not None:
        return f"The @everyone channel overwrite {'allows' if everyone_value else 'denies'} this permission."
    grants = [role for role in member.roles if getattr(role.permissions, permission)]
    if grants:
        return f"Guild permission granted by {_role_names(grants)}."
    return "No assigned guild role grants this permission and there is no applicable overwrite."


async def _private_thread_membership(thread, member):
    if getattr(thread.me, "id", None) == member.id or any(participant.id == member.id for participant in thread.members):
        return True
    try:
        await asyncio.wait_for(thread.fetch_member(member.id), timeout=2)
    except discord.NotFound as exc:
        return None if exc.code == 10003 else False
    except (discord.HTTPException, discord.RateLimited, TimeoutError, OSError):
        return None
    return True


async def explain_access(channel, member, action: AccessAction):
    thread = isinstance(channel, discord.Thread)
    voice = isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
    source = channel.parent if thread else channel
    if source is None:
        return "Unknown", "The thread's parent channel is unavailable to the bot."
    if action == "join_voice" and not voice:
        return "Unavailable", "Select a voice or stage channel for this action."
    if action == "post" and isinstance(channel, discord.ForumChannel):
        return "Unavailable", "Select a forum post to check replies in its thread."
    if action == "manage_permissions" and thread:
        return "Unavailable", "Thread permission overwrites belong to its parent channel."

    effective = channel.permissions_for(member)
    if not effective.view_channel:
        return "Denied", "View Channel is required. " + _permission_source(source, member, "view_channel")
    thread_access = ""
    if thread and channel.is_private():
        if effective.manage_threads:
            thread_access = " Manage Threads grants access to this private thread."
        else:
            membership = await _private_thread_membership(channel, member)
            if membership is None:
                return "Unknown", "Discord did not let the bot verify private-thread membership."
            if not membership:
                return "Denied", "This private thread requires membership or Manage Threads."
            thread_access = " Private-thread membership confirmed."

    permission = {
        "view": "view_channel",
        "post": "send_messages_in_threads" if thread else "send_messages",
        "join_voice": "connect",
        "manage_messages": "manage_messages",
        "manage_channel": "manage_threads" if thread else "manage_channels",
        "manage_permissions": "manage_roles",
    }[action]
    if not getattr(effective, permission):
        if member.is_timed_out():
            return "Denied", f"The member has an active timeout. Underlying permission: {_permission_source(source, member, permission)}"
        if voice and action in {"manage_channel", "manage_permissions"} and not effective.connect:
            return "Denied", "Connect is required for this channel action. " + _permission_source(source, member, "connect")
        return "Denied", _permission_source(source, member, permission)
    if action == "post" and thread and channel.archived:
        if channel.locked:
            if effective.manage_threads:
                return "Requires action", "Reopen this locked thread with Manage Threads before posting."
            return "Denied", "This thread is locked and archived. Manage Threads is required to reopen it."
        thread_access += " Posting can reopen this archived thread."
    return "Allowed", _permission_source(source, member, permission) + thread_access

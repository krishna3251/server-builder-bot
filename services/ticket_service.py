"""Ticket service — manages ticket creation, closing, and transcripts."""

from __future__ import annotations

import json
import asyncio
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

import aiofiles
import discord

import config

log = logging.getLogger("services.ticket")


def staff_log_channel(channel, guild, staff_role_ids: set[int]) -> bool:
    """Verify the channel's visibility rules, including role and member grants."""
    if not isinstance(channel, discord.TextChannel) or guild.me is None:
        return False
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True:
            continue
        if target.id == guild.id:
            return False
        if target.id in (guild.me.id, guild.owner_id):
            continue
        if isinstance(target, discord.Role):
            if target.id in staff_role_ids or target.permissions.administrator:
                continue
        elif isinstance(target, discord.Member):
            if target.guild_permissions.administrator or any(role.id in staff_role_ids for role in target.roles):
                continue
        return False
    return True


async def archive_ticket(channel: discord.TextChannel, max_bytes: int) -> io.BytesIO:
    """Collect the full channel history and copy attachments before deletion."""
    archive = io.BytesIO()
    size = 0
    attachments = []
    try:
        with zipfile.ZipFile(archive, 'w') as output:
            with output.open('transcript.txt', 'w') as transcript:
                async for message in channel.history(limit=None, oldest_first=True):
                    line = f'[{message.created_at.isoformat()}] {message.author} ({message.author.id}): {message.content}\n'
                    for attachment in message.attachments:
                        filename = re.sub(r'[^\w. -]', '_', attachment.filename)[-160:]
                        path = f'attachments/{attachment.id}-{filename}'
                        line += f'    Attachment: {path}\n'
                        size += attachment.size + 128 + 2 * len(path.encode('utf-8'))
                        attachments.append((path, attachment))
                    data = line.encode('utf-8')
                    size += len(data)
                    if size > max_bytes:
                        raise ValueError('The full ticket archive exceeds the upload limit.')
                    transcript.write(data)
            for path, attachment in attachments:
                data = await attachment.read()
                size += max(0, len(data) - attachment.size)
                if size > max_bytes:
                    raise ValueError('The full ticket archive exceeds the upload limit.')
                output.writestr(path, data)
        if archive.tell() > max_bytes:
            raise ValueError('The full ticket archive exceeds the upload limit.')
        archive.seek(0)
        return archive
    except (Exception, asyncio.CancelledError):
        archive.close()
        raise

_tickets: dict[str, Any] = {}  # guild_user -> ticket data
_loaded: bool = False


async def _ensure_loaded() -> None:
    global _tickets, _loaded
    if _loaded:
        return
    if os.path.exists(config.TICKET_LOG_FILE):
        try:
            async with aiofiles.open(config.TICKET_LOG_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _tickets = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            log.warning("Could not load ticket log: %s", exc)
            _tickets = {}
    _loaded = True


async def _save() -> None:
    async with aiofiles.open(config.TICKET_LOG_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_tickets, indent=2))


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}_{user_id}"


async def has_open_ticket(guild_id: int, user_id: int) -> bool:
    await _ensure_loaded()
    k = _key(guild_id, user_id)
    return k in _tickets and _tickets[k].get("open", False)


async def open_ticket(guild_id: int, user_id: int, channel_id: int) -> None:
    await _ensure_loaded()
    k = _key(guild_id, user_id)
    _tickets[k] = {
        "channel_id": channel_id,
        "user_id": user_id,
        "guild_id": guild_id,
        "open": True,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "messages": [],
    }
    await _save()


async def close_ticket(guild_id: int, user_id: int, reason: str) -> dict[str, Any] | None:
    await _ensure_loaded()
    k = _key(guild_id, user_id)
    if k not in _tickets:
        return None
    _tickets[k]["open"] = False
    _tickets[k]["closed_at"] = datetime.now(timezone.utc).isoformat()
    _tickets[k]["close_reason"] = reason
    data = dict(_tickets[k])
    await _save()
    return data


async def get_ticket(guild_id: int, user_id: int) -> dict[str, Any] | None:
    await _ensure_loaded()
    return _tickets.get(_key(guild_id, user_id))


async def generate_transcript(channel: discord.TextChannel, limit: int = 500) -> str:
    """Read up to *limit* messages from *channel* and format as a plaintext transcript."""
    lines: list[str] = []
    async for msg in channel.history(limit=limit, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {msg.author}: {msg.content}")
    return "\n".join(lines)

"""Ticket service — manages ticket creation, closing, and transcripts."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiofiles
import discord

import config

log = logging.getLogger("services.ticket")

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

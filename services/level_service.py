"""Level / XP service — per-user per-guild experience tracking."""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

import aiofiles

import config

log = logging.getLogger("services.level")

_xp_data: dict[str, dict[str, Any]] = {}
_loaded: bool = False


async def _ensure_loaded() -> None:
    global _xp_data, _loaded
    if _loaded:
        return
    if os.path.exists(config.XP_FILE):
        try:
            async with aiofiles.open(config.XP_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _xp_data = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            log.warning("Could not load XP data: %s", exc)
            _xp_data = {}
    _loaded = True


async def _save() -> None:
    os.makedirs(os.path.dirname(config.XP_FILE), exist_ok=True)
    async with aiofiles.open(config.XP_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_xp_data, indent=2))


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}_{user_id}"


def xp_for_level(level: int) -> int:
    """XP required to reach *level* from level 0 (exponential curve)."""
    return int(100 * (level ** 1.5))


def level_from_xp(xp: int) -> int:
    """Compute level from total XP."""
    level = 0
    while xp_for_level(level + 1) <= xp:
        level += 1
    return level


async def add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]:
    """Add *amount* XP. Returns ``(new_xp, new_level, leveled_up)``."""
    await _ensure_loaded()
    k = _key(guild_id, user_id)
    if k not in _xp_data:
        _xp_data[k] = {"xp": 0, "level": 0}

    old_level = _xp_data[k]["level"]
    _xp_data[k]["xp"] += amount
    new_level = level_from_xp(_xp_data[k]["xp"])
    leveled_up = new_level > old_level
    _xp_data[k]["level"] = new_level
    await _save()
    return _xp_data[k]["xp"], new_level, leveled_up


async def get_stats(guild_id: int, user_id: int) -> dict[str, int]:
    await _ensure_loaded()
    k = _key(guild_id, user_id)
    data = _xp_data.get(k, {"xp": 0, "level": 0})
    return {"xp": data["xp"], "level": data["level"]}


async def set_xp(guild_id: int, user_id: int, xp: int) -> int:
    await _ensure_loaded()
    k = _key(guild_id, user_id)
    _xp_data[k] = {"xp": xp, "level": level_from_xp(xp)}
    await _save()
    return _xp_data[k]["level"]


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
    """Return ``[(user_id, xp, level), \u2026]`` sorted by XP descending."""
    await _ensure_loaded()
    prefix = f"{guild_id}_"
    entries: list[tuple[int, int, int]] = []
    for k, v in _xp_data.items():
        if k.startswith(prefix):
            uid = int(k.split("_", 1)[1])
            entries.append((uid, v["xp"], v["level"]))
    entries.sort(key=lambda x: x[1], reverse=True)
    return entries[:limit]

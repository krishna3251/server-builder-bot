"""Level / XP service — per-user per-guild experience tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import copy
import os
from typing import Any

import aiofiles

import config
from utils.storage import write_json_atomic

log = logging.getLogger("services.level")

_xp_data: dict[str, dict[str, Any]] = {}
_loaded: bool = False
_xp_lock = asyncio.Lock()


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
            raise RuntimeError("XP data could not be read; refusing to overwrite it.") from exc
    _loaded = True


async def _save() -> None:
    await write_json_atomic(config.XP_FILE, _xp_data)


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}_{user_id}"


def xp_for_level(level: int) -> int:
    """XP required to reach *level* from level 0 (exponential curve)."""
    return int(100 * (level ** 1.5))


def level_from_xp(xp: int) -> int:
    """Compute level from total XP."""
    if type(xp) is not int or xp < 0:
        raise ValueError('XP must be a non-negative integer.')
    # Integer search avoids linear work for large /setxp inputs.
    low, high = 0, 1
    while xp_for_level(high) <= xp:
        high *= 2
    while low + 1 < high:
        mid = (low + high) // 2
        if xp_for_level(mid) <= xp:
            low = mid
        else:
            high = mid
    return low


async def add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]:
    """Add *amount* XP. Returns ``(new_xp, new_level, leveled_up)``."""
    async with _xp_lock:
        await _ensure_loaded()
        k = _key(guild_id, user_id)
        if k not in _xp_data:
            _xp_data[k] = {"xp": 0, "level": 0}

        previous = copy.deepcopy(_xp_data[k])
        old_level = _xp_data[k]["level"]
        _xp_data[k]["xp"] += amount
        new_level = level_from_xp(_xp_data[k]["xp"])
        leveled_up = new_level > old_level
        _xp_data[k]["level"] = new_level
        try:
            await _save()
        except Exception:
            _xp_data[k] = previous
            raise
        return _xp_data[k]["xp"], new_level, leveled_up


async def get_stats(guild_id: int, user_id: int) -> dict[str, int]:
    async with _xp_lock:
        await _ensure_loaded()
        k = _key(guild_id, user_id)
        data = _xp_data.get(k, {"xp": 0, "level": 0})
        return {"xp": data["xp"], "level": data["level"]}


async def set_xp(guild_id: int, user_id: int, xp: int) -> int:
    async with _xp_lock:
        await _ensure_loaded()
        k = _key(guild_id, user_id)
        previous = copy.deepcopy(_xp_data.get(k))
        _xp_data[k] = {"xp": xp, "level": level_from_xp(xp)}
        try:
            await _save()
        except Exception:
            if previous is None:
                _xp_data.pop(k, None)
            else:
                _xp_data[k] = previous
            raise
        return _xp_data[k]["level"]


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
    """Return ``[(user_id, xp, level), \u2026]`` sorted by XP descending."""
    async with _xp_lock:
        await _ensure_loaded()
        prefix = f"{guild_id}_"
        entries: list[tuple[int, int, int]] = []
        for k, v in _xp_data.items():
            if k.startswith(prefix):
                uid = int(k.split("_", 1)[1])
                entries.append((uid, v["xp"], v["level"]))
        entries.sort(key=lambda x: x[1], reverse=True)
        return entries[:limit]

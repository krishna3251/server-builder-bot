"""Economy service — JSON-backed wallet and shop system."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import aiofiles

import config

log = logging.getLogger("services.economy")

_wallets: dict[str, dict[str, Any]] = {}
_shop: list[dict[str, Any]] = []
_loaded: bool = False


async def _ensure_loaded() -> None:
    global _wallets, _shop, _loaded
    if _loaded:
        return

    # Load wallets
    if os.path.exists(config.WALLETS_FILE):
        try:
            async with aiofiles.open(config.WALLETS_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _wallets = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            log.warning("Could not load wallets: %s", exc)
            _wallets = {}
    # Load shop
    if os.path.exists(config.SHOP_FILE):
        try:
            async with aiofiles.open(config.SHOP_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _shop = json.loads(raw) if raw.strip() else []
        except Exception as exc:
            log.warning("Could not load shop: %s", exc)
            _shop = _default_shop()
    else:
        _shop = _default_shop()
        await _save_shop()

    _loaded = True


def _default_shop() -> list[dict[str, Any]]:
    return [
        {"name": "VIP Role Badge", "price": 1000, "description": "Cosmetic VIP badge."},
        {"name": "Custom Colour", "price": 2500, "description": "Pick a custom name colour."},
        {"name": "XP Boost", "price": 5000, "description": "2\u00d7 XP for 1 hour."},
    ]


async def _save_wallets() -> None:
    async with aiofiles.open(config.WALLETS_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_wallets, indent=2))


async def _save_shop() -> None:
    async with aiofiles.open(config.SHOP_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_shop, indent=2))


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}_{user_id}"


def _ensure_wallet(key: str) -> dict[str, Any]:
    if key not in _wallets:
        _wallets[key] = {
            "balance": config.STARTING_BALANCE,
            "inventory": [],
            "last_daily": None,
            "last_work": None,
        }
    return _wallets[key]


async def get_balance(guild_id: int, user_id: int) -> int:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    return w["balance"]


async def add_balance(guild_id: int, user_id: int, amount: int) -> int:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    w["balance"] += amount
    await _save_wallets()
    return w["balance"]


async def remove_balance(guild_id: int, user_id: int, amount: int) -> int:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    w["balance"] = max(0, w["balance"] - amount)
    await _save_wallets()
    return w["balance"]


async def transfer(guild_id: int, from_id: int, to_id: int, amount: int) -> bool:
    await _ensure_loaded()
    sender = _ensure_wallet(_key(guild_id, from_id))
    if sender["balance"] < amount:
        return False
    sender["balance"] -= amount
    receiver = _ensure_wallet(_key(guild_id, to_id))
    receiver["balance"] += amount
    await _save_wallets()
    return True


async def get_last_daily(guild_id: int, user_id: int) -> str | None:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    return w.get("last_daily")


async def set_last_daily(guild_id: int, user_id: int, ts: str) -> None:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    w["last_daily"] = ts
    await _save_wallets()


async def get_last_work(guild_id: int, user_id: int) -> str | None:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    return w.get("last_work")


async def set_last_work(guild_id: int, user_id: int, ts: str) -> None:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    w["last_work"] = ts
    await _save_wallets()


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    """Return top users as ``[(user_id, balance), \u2026]``."""
    await _ensure_loaded()
    prefix = f"{guild_id}_"
    entries: list[tuple[int, int]] = []
    for k, v in _wallets.items():
        if k.startswith(prefix):
            uid = int(k.split("_", 1)[1])
            entries.append((uid, v["balance"]))
    entries.sort(key=lambda x: x[1], reverse=True)
    return entries[:limit]


async def get_shop_items() -> list[dict[str, Any]]:
    await _ensure_loaded()
    return _shop


async def buy_item(guild_id: int, user_id: int, item_name: str) -> tuple[bool, str]:
    """Purchase an item. Returns ``(success, message)``."""
    await _ensure_loaded()
    item = next((i for i in _shop if i["name"].lower() == item_name.lower()), None)
    if not item:
        return False, "Item not found in shop."
    w = _ensure_wallet(_key(guild_id, user_id))
    if w["balance"] < item["price"]:
        return False, f"Not enough coins. You need **{item['price']}** but have **{w['balance']}**."
    w["balance"] -= item["price"]
    w["inventory"].append(item["name"])
    await _save_wallets()
    return True, f"Purchased **{item['name']}**!"


async def get_inventory(guild_id: int, user_id: int) -> list[str]:
    await _ensure_loaded()
    w = _ensure_wallet(_key(guild_id, user_id))
    return w.get("inventory", [])

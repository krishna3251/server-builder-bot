"""General-purpose helper functions used across the bot."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_DURATION_RE = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)

_UNIT_MAP: dict[str, str] = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    """Parse a human-readable duration string (e.g. ``10m``, ``2h``, ``1d``) into a *timedelta*.

    Supports compounding: ``1d12h30m``.
    Raises ``ValueError`` if nothing matches.
    """
    matches = _DURATION_RE.findall(text)
    if not matches:
        raise ValueError(f"Cannot parse duration from '{text}'.")
    kwargs: dict[str, int] = {}
    for amount, unit in matches:
        kwargs[_UNIT_MAP[unit.lower()]] = kwargs.get(_UNIT_MAP[unit.lower()], 0) + int(amount)
    return timedelta(**kwargs)


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    """Split *text* into chunks of at most *limit* characters, breaking on newlines when possible."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to break at a newline
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def format_number(n: int) -> str:
    """Format an integer with comma separators (e.g. ``1000`` \u2192 ``1,000``)."""
    return f"{n:,}"


def time_until(dt: datetime) -> str:
    """Return a human-readable countdown string from *now* to *dt* (UTC)."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "now"

    parts: list[str] = []
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not days:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "now"

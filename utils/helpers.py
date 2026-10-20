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
    cleaned = text.strip()
    matches = list(_DURATION_RE.finditer(cleaned))
    if not matches or _DURATION_RE.sub('', cleaned).strip():
        raise ValueError('Use a complete duration such as 10m, 2h, or 1d12h.')
    seconds = sum(int(m.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2).lower()] for m in matches)
    if not 0 < seconds <= 365 * 86400:
        raise ValueError('Duration must be greater than zero and no longer than 365 days.')
    return timedelta(seconds=seconds)


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

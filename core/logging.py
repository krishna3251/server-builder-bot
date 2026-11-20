"""Structured logging for DAMU Core Engine with automatic secret masking."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("core")


def sanitize_data(data: Any) -> Any:
    """Recursively scrub any sensitive keys from logs."""
    if isinstance(data, dict):
        scrubbed = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(secret_term in k_lower for secret_term in ("token", "secret", "password", "key", "uri", "auth")):
                scrubbed[k] = "[REDACTED]"
            else:
                scrubbed[k] = sanitize_data(v)
        return scrubbed
    elif isinstance(data, list):
        return [sanitize_data(item) for item in data]
    return data


def log_event(event_name: str, guild_id: int | None = None, user_id: int | None = None, **kwargs: Any) -> None:
    """Log a structured audit/engine event."""
    payload = {
        "event": event_name,
        "guild_id": guild_id,
        "user_id": user_id,
        "details": sanitize_data(kwargs),
    }
    log.info("DAMU_EVENT: %s", json.dumps(payload, default=str))

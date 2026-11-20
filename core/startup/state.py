"""Explicit startup states and health status model for the Discord connection lifecycle."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class StartupState(str, enum.Enum):
    """Lifecycle states for the bot startup manager."""

    INITIALIZING = "INITIALIZING"
    VALIDATING = "VALIDATING"
    CONNECTING = "CONNECTING"
    READY = "READY"
    RATE_LIMITED = "RATE_LIMITED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    INVALID_TOKEN = "INVALID_TOKEN"
    FATAL_ERROR = "FATAL_ERROR"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"

    @property
    def is_terminal(self) -> bool:
        """Return True if this is a terminal state that should not be retried."""
        return self in (
            StartupState.INVALID_TOKEN,
            StartupState.FATAL_ERROR,
            StartupState.STOPPED,
        )

    @property
    def is_connected(self) -> bool:
        """Return True if Discord connection is active."""
        return self == StartupState.READY


@dataclass
class StartupStatus:
    """Read-only snapshot of current startup lifecycle and Discord connection health.

    Security Rule: Never includes tokens, secrets, API keys, or sensitive auth headers.
    """

    state: StartupState
    discord_ready: bool
    startup_attempt: int
    last_error: str | None = None
    last_error_category: str | None = None
    last_success_at: str | None = None
    next_retry_at: str | None = None
    uptime_seconds: float = 0.0
    is_cloudflare_1015: bool = False
    retry_delay_seconds: float | None = None
    retry_source: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Serialize status to a dictionary safe for internal health and readiness endpoints."""
        return {
            "state": self.state.value,
            "discord_ready": self.discord_ready,
            "startup_attempt": self.startup_attempt,
            "last_error": self.last_error,
            "last_error_category": self.last_error_category,
            "last_success_at": self.last_success_at,
            "next_retry_at": self.next_retry_at,
            "retry_delay_seconds": (
                int(self.retry_delay_seconds)
                if self.retry_delay_seconds is not None and self.retry_delay_seconds.is_integer()
                else (round(self.retry_delay_seconds, 2) if self.retry_delay_seconds is not None else None)
            ),
            "retry_source": self.retry_source,
            "uptime_seconds": round(self.uptime_seconds, 2),
        }

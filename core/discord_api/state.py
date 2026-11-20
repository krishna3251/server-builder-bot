"""Global Discord API resilience states."""

from __future__ import annotations

from enum import Enum


class APIState(str, Enum):
    """Process-wide Discord API health and restriction state."""

    AVAILABLE = "AVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    CLOUDFLARE_BLOCKED = "CLOUDFLARE_BLOCKED"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"

    @property
    def is_blocked(self) -> bool:
        """Return True if calls to Discord API should be gated/deferred."""
        return self in (APIState.RATE_LIMITED, APIState.CLOUDFLARE_BLOCKED)

    @property
    def is_available(self) -> bool:
        """Return True if Discord API is fully operational."""
        return self == APIState.AVAILABLE

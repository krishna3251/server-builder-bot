"""Diagnostic status model for health and monitoring."""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class DiagnosticStatus:
    """Diagnostic health snapshot of Discord connection and API resilience."""

    startup_state: str
    api_state: str
    discord_ready: bool
    startup_attempt: int
    retry_delay: float
    retry_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_category: Optional[str] = None
    last_success_at: Optional[str] = None
    uptime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

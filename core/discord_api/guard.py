"""Shared DiscordAPIGuard process-wide resilience controller."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import random
from typing import Any, Mapping, Optional

from core.discord_api.health import DiagnosticStatus
from core.discord_api.rate_limit import (
    format_concise_discord_log,
    is_cloudflare_1015,
    parse_retry_after,
)
from core.discord_api.state import APIState

log = logging.getLogger("core.discord_api.guard")


class DiscordAPIGuard:
    """Process-wide guard and rate-limit coordinator for all Discord REST interactions.

    Guarantees that when Discord or Cloudflare rate-limits or blocks the bot,
    no cog or helper hammers the API with subsequent fallback requests.
    """

    _instance: Optional[DiscordAPIGuard] = None

    def __new__(cls) -> DiscordAPIGuard:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._state: APIState = APIState.AVAILABLE
        self._retry_at: Optional[datetime] = None
        self._retry_delay: float = 0.0
        self._provider: str = "discord"
        self._is_cloudflare: bool = False
        self._last_error: Optional[str] = None
        self._last_error_category: Optional[str] = None
        self._last_success_at: Optional[datetime] = None
        self._consecutive_failures: int = 0
        self._lock = asyncio.Lock()
        self._probe_in_progress: bool = False

    @property
    def state(self) -> APIState:
        """Current API state."""
        return self._state

    @property
    def retry_at(self) -> Optional[datetime]:
        """Expiration time for the current restriction, if active."""
        return self._retry_at

    @property
    def retry_delay(self) -> float:
        """Retry delay in seconds."""
        return self._retry_delay

    @property
    def provider(self) -> str:
        """Provider responsible for current restriction ('discord' or 'cloudflare')."""
        return self._provider

    @property
    def is_cloudflare(self) -> bool:
        """True if the current restriction was caused by Cloudflare 1015."""
        return self._is_cloudflare

    @property
    def last_error(self) -> Optional[str]:
        """Description of the last observed error."""
        return self._last_error

    @property
    def last_error_category(self) -> Optional[str]:
        """Category of the last observed error."""
        return self._last_error_category

    @property
    def last_success_at(self) -> Optional[datetime]:
        """Timestamp of last successful API interaction."""
        return self._last_success_at

    @property
    def consecutive_failures(self) -> int:
        """Count of sequential failures."""
        return self._consecutive_failures

    def can_execute(self) -> tuple[bool, APIState, Optional[datetime]]:
        """Check whether a Discord API REST operation is permitted.

        Returns:
            (allowed: bool, current_state: APIState, retry_at: Optional[datetime])

        Behavior:
            - If AVAILABLE: (True, AVAILABLE, None)
            - If RATE_LIMITED or CLOUDFLARE_BLOCKED:
              - If deadline expired: transitions to RECOVERING and allows ONE probe request.
              - If deadline active: (False, state, retry_at)
            - If RECOVERING:
              - If probe already running: (False, RECOVERING, retry_at)
              - Otherwise allows single probe: (True, RECOVERING, None)
        """
        now = datetime.now(timezone.utc)

        if self._state == APIState.AVAILABLE:
            return True, APIState.AVAILABLE, None

        if self._state in (APIState.RATE_LIMITED, APIState.CLOUDFLARE_BLOCKED):
            if self._retry_at and now >= self._retry_at:
                # Deadline reached: transition to RECOVERING to probe
                log.info(
                    "[DISCORD_API] Retry deadline reached. Transitioning from %s to RECOVERING.",
                    self._state.value,
                )
                self._state = APIState.RECOVERING
                self._probe_in_progress = True
                return True, APIState.RECOVERING, None
            else:
                return False, self._state, self._retry_at

        if self._state == APIState.RECOVERING:
            if not self._probe_in_progress:
                self._probe_in_progress = True
                return True, APIState.RECOVERING, None
            return False, APIState.RECOVERING, self._retry_at

        # DEGRADED state allows calls with caution
        return True, self._state, None

    def record_success(self) -> None:
        """Record a successful Discord REST operation."""
        if self._state != APIState.AVAILABLE:
            log.info(
                "[DISCORD_API] Recovery verified. Transitioning from %s to AVAILABLE.",
                self._state.value,
            )

        self._state = APIState.AVAILABLE
        self._retry_at = None
        self._retry_delay = 0.0
        self._consecutive_failures = 0
        self._probe_in_progress = False
        self._last_success_at = datetime.now(timezone.utc)

    def record_failure(
        self,
        status: int,
        body: str = "",
        headers: Optional[Mapping[str, str]] = None,
        error: Optional[Exception] = None,
    ) -> float:
        """Record a Discord REST failure, update API state, and calculate backoff.

        Returns:
            Calculated retry delay in seconds.
        """
        self._consecutive_failures += 1
        self._probe_in_progress = False

        # 1. Determine error type and provider
        is_cf = is_cloudflare_1015(status, body, headers)
        self._is_cloudflare = is_cf
        self._provider = "cloudflare" if is_cf else "discord"

        error_code = "1015" if is_cf else str(status)
        if is_cf:
            self._state = APIState.CLOUDFLARE_BLOCKED
            self._last_error_category = "CLOUDFLARE_BLOCKED"
        elif status == 429:
            self._state = APIState.RATE_LIMITED
            self._last_error_category = "RATE_LIMITED"
        elif status in (401, 403):
            self._state = APIState.DEGRADED
            self._last_error_category = "PERMISSION_DENIED"
        elif status == 404:
            self._state = APIState.DEGRADED
            self._last_error_category = "NOT_FOUND"
        else:
            self._state = APIState.DEGRADED
            self._last_error_category = "NETWORK_ERROR"

        # 2. Extract or calculate retry delay
        server_retry = parse_retry_after(headers, body)
        if server_retry is not None and server_retry > 0:
            # Respect server Retry-After up to 86400s (do NOT cap to 600s!)
            delay = min(float(server_retry), 86400.0)
        else:
            # Zero-second or missing retry after: fall back to exponential backoff with jitter
            exponent = min(self._consecutive_failures, 6)
            delay = min(600.0, 5.0 * (2**exponent)) + random.uniform(1.0, 3.0)

        self._retry_delay = delay
        self._retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self._last_error = f"HTTP {status} ({error_code})"

        # 3. Log structured concise output without HTML dumps or secrets
        log.warning(
            format_concise_discord_log(
                status=status,
                provider=self._provider,
                error=error_code,
                retry_after=delay,
                state=self._state.value,
            )
        )

        return delay

    def reset(self) -> None:
        """Reset state back to AVAILABLE (primarily for testing)."""
        self._state = APIState.AVAILABLE
        self._retry_at = None
        self._retry_delay = 0.0
        self._provider = "discord"
        self._is_cloudflare = False
        self._last_error = None
        self._last_error_category = None
        self._consecutive_failures = 0
        self._probe_in_progress = False


# Process-wide singleton
api_guard = DiscordAPIGuard()

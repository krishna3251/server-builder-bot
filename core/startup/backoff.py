"""Exponential backoff calculation and server Retry-After handler.

Strictly separates:
1. SERVER-PROVIDED RETRY-AFTER (capped by STARTUP_MAX_SERVER_RETRY_AFTER, default 24h)
2. LOCALLY CALCULATED EXPONENTIAL BACKOFF (capped by STARTUP_MAX_BACKOFF, default 10m)
"""

from __future__ import annotations

import logging
import math
import os
import random
from dataclasses import dataclass

log = logging.getLogger("core.startup.backoff")

# Defaults for environment configurations
DEFAULT_BASE_BACKOFF: float = 5.0
DEFAULT_MAX_BACKOFF: float = 600.0  # 10 minutes (for local backoff)
DEFAULT_MAX_SERVER_RETRY_AFTER: float = 86400.0  # 24 hours (for server Retry-After)
DEFAULT_JITTER_RATIO: float = 0.25
DEFAULT_MAX_ATTEMPTS: int = 0  # 0 = unlimited retries for transient failures
DEFAULT_MAX_RETRY_WINDOW: float = 0.0  # 0 = unlimited window

STARTUP_MAX_BACKOFF: float = DEFAULT_MAX_BACKOFF
STARTUP_MAX_SERVER_RETRY_AFTER: float = DEFAULT_MAX_SERVER_RETRY_AFTER
STARTUP_BASE_BACKOFF: float = DEFAULT_BASE_BACKOFF


def _get_float_env(var_name: str, default: float, min_val: float, max_val: float) -> float:
    raw = os.getenv(var_name)
    if not raw:
        return default
    try:
        val = float(raw.strip().strip('"').strip("'"))
        return max(min_val, min(max_val, val))
    except (ValueError, TypeError):
        return default


def _get_int_env(var_name: str, default: int, min_val: int, max_val: int) -> int:
    raw = os.getenv(var_name)
    if not raw:
        return default
    try:
        val = int(raw.strip().strip('"').strip("'"))
        return max(min_val, min(max_val, val))
    except (ValueError, TypeError):
        return default


@dataclass
class DelayResult:
    """Detailed result of retry delay calculation."""

    delay: float
    source: str  # "server_retry_after" or "exponential_backoff"
    raw_retry_after: float | None = None
    bounded_retry_after: float | None = None


@dataclass
class StartupBackoff:
    """Calculates retry delays, distinguishing server-supplied Retry-After from local backoff."""

    base_delay: float = DEFAULT_BASE_BACKOFF
    max_delay: float = DEFAULT_MAX_BACKOFF
    max_server_retry_after: float = DEFAULT_MAX_SERVER_RETRY_AFTER
    jitter_ratio: float = DEFAULT_JITTER_RATIO
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_retry_window: float = DEFAULT_MAX_RETRY_WINDOW

    @classmethod
    def from_env(cls) -> StartupBackoff:
        """Create backoff configuration parsed and clamped from environment variables."""
        base = _get_float_env("STARTUP_BASE_BACKOFF", DEFAULT_BASE_BACKOFF, 1.0, 60.0)
        max_d = _get_float_env("STARTUP_MAX_BACKOFF", DEFAULT_MAX_BACKOFF, 10.0, 3600.0)
        if base > max_d:
            base = max_d
        max_server_ra = _get_float_env(
            "STARTUP_MAX_SERVER_RETRY_AFTER", DEFAULT_MAX_SERVER_RETRY_AFTER, 60.0, 604800.0
        )
        jitter = _get_float_env("STARTUP_JITTER", DEFAULT_JITTER_RATIO, 0.0, 1.0)
        max_att = _get_int_env("STARTUP_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 0, 1000)
        max_win = _get_float_env("STARTUP_MAX_RETRY_WINDOW", DEFAULT_MAX_RETRY_WINDOW, 0.0, 86400.0)

        return cls(
            base_delay=base,
            max_delay=max_d,
            max_server_retry_after=max_server_ra,
            jitter_ratio=jitter,
            max_attempts=max_att,
            max_retry_window=max_win,
        )

    def calculate_delay_details(self, attempt: int, retry_after: float | None = None) -> DelayResult:
        """Calculate the next retry sleep duration with detailed source attribution.

        Rule 1: Server-provided Retry-After uses STARTUP_MAX_SERVER_RETRY_AFTER (default 24h)
                and is NOT capped to the local backoff limit (600s).
        Rule 2: Local exponential backoff uses STARTUP_MAX_BACKOFF (default 10m).
        """
        attempt = max(1, attempt)
        is_zero_retry_after: bool = False

        # ── 1. SERVER-PROVIDED RETRY-AFTER ────────────────────────────────────
        if retry_after is not None:
            is_valid = False
            raw_val: float = 0.0
            try:
                raw_val = float(retry_after)
                if not math.isnan(raw_val) and not math.isinf(raw_val) and raw_val >= 0:
                    is_valid = True
            except (ValueError, TypeError):
                is_valid = False

            if is_valid:
                if raw_val <= 0:
                    log.warning(
                        "[STARTUP] Non-positive Retry-After received (%.2fs). "
                        "Preventing zero-second retry storm; falling back to exponential backoff with jitter.",
                        raw_val,
                    )
                    is_zero_retry_after = True
                elif raw_val <= self.max_server_retry_after:
                    bounded = raw_val
                    final_delay = bounded
                    log.info(
                        "[STARTUP] Server Retry-After received. raw=%.2fs bounded=%.2fs final_delay=%.2fs source=discord/cloudflare",
                        raw_val,
                        bounded,
                        final_delay,
                    )
                    return DelayResult(
                        delay=round(final_delay, 2),
                        source="server_retry_after",
                        raw_retry_after=raw_val,
                        bounded_retry_after=bounded,
                    )
                else:
                    bounded = self.max_server_retry_after
                    final_delay = bounded
                    log.warning(
                        "[STARTUP] Server Retry-After exceeded safety maximum. raw=%.2fs bounded=%.2fs source=server_retry_after reason=maximum_server_retry_after final_delay=%.2fs",
                        raw_val,
                        bounded,
                        final_delay,
                    )
                    return DelayResult(
                        delay=round(final_delay, 2),
                        source="server_retry_after",
                        raw_retry_after=raw_val,
                        bounded_retry_after=bounded,
                    )
            else:
                log.warning(
                    "[STARTUP] Invalid Retry-After: %s. Falling back to exponential backoff.",
                    retry_after,
                )

        # ── 2. LOCALLY CALCULATED EXPONENTIAL BACKOFF ─────────────────────────
        exponent = min(10, attempt - 1)
        raw_backoff = self.base_delay * (2 ** exponent)
        clamped_backoff = min(self.max_delay, raw_backoff)

        # Apply bounded random jitter
        floor = min(1.0, self.base_delay)
        if self.jitter_ratio > 0:
            jitter_delta = clamped_backoff * self.jitter_ratio * random.uniform(-1.0, 1.0)
            final_delay = max(floor, min(self.max_delay, clamped_backoff + jitter_delta))
        else:
            final_delay = max(floor, clamped_backoff)

        return DelayResult(
            delay=round(final_delay, 2),
            source="zero_retry_after_fallback" if is_zero_retry_after else "exponential_backoff",
            raw_retry_after=raw_val if is_zero_retry_after else None,
            bounded_retry_after=None,
        )


    def calculate_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Calculate the next retry sleep duration float."""
        return self.calculate_delay_details(attempt=attempt, retry_after=retry_after).delay

    def is_attempt_allowed(self, attempt: int) -> bool:
        """Check if additional retry attempts are permitted under max_attempts."""
        if self.max_attempts <= 0:
            return True
        return attempt <= self.max_attempts

    def reset(self) -> None:
        """Reset state. Called ONLY after a genuine successful Discord connection."""
        log.debug("[STARTUP] Backoff state reset following successful connection.")


def calculate_backoff(
    attempt: int,
    retry_after: float | None = None,
    is_cloudflare_1015: bool = False,
    max_server_retry_after: float | None = None,
    max_backoff: float | None = None,
    base_backoff: float | None = None,
) -> tuple[float, str]:
    """Functional helper for retry delay calculation. Returns (delay, source)."""
    backoff = StartupBackoff(
        base_delay=base_backoff if base_backoff is not None else DEFAULT_BASE_BACKOFF,
        max_delay=max_backoff if max_backoff is not None else DEFAULT_MAX_BACKOFF,
        max_server_retry_after=max_server_retry_after if max_server_retry_after is not None else DEFAULT_MAX_SERVER_RETRY_AFTER,
    )
    res = backoff.calculate_delay_details(attempt=attempt, retry_after=retry_after)
    source = res.source
    if res.raw_retry_after is not None and res.raw_retry_after > backoff.max_server_retry_after:
        source = "server_retry_after_capped"
    return res.delay, source

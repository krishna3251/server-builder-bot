"""Diagnostic status aggregator for health monitoring and diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from core.discord_api.guard import api_guard
from core.discord_api.health import DiagnosticStatus
from core.startup.manager import StartupManager
from core.startup.state import StartupState


def get_diagnostic_status(startup_manager: Optional[StartupManager] = None) -> DiagnosticStatus:
    """Collect current process-wide diagnostics into a DiagnosticStatus model."""
    sm = startup_manager or getattr(StartupManager, "_instance", None) or StartupManager()
    guard = api_guard

    retry_at_str = (
        guard.retry_at.isoformat()
        if guard.retry_at
        else None
    )
    last_success_str = (
        guard.last_success_at.isoformat()
        if guard.last_success_at
        else None
    )

    discord_ready = (
        sm.state == StartupState.READY
        and guard.state.is_available
        and sm.bot is not None
        and sm.bot.is_ready()
    )

    return DiagnosticStatus(
        startup_state=sm.state.value,
        api_state=guard.state.value,
        discord_ready=discord_ready,
        startup_attempt=sm.attempt,
        retry_delay=guard.retry_delay,
        retry_at=retry_at_str,
        last_error=guard.last_error or sm.last_error,
        last_error_category=guard.last_error_category or sm.last_error_category,
        last_success_at=last_success_str,
        uptime=sm.uptime_seconds,
    )

"""Production-grade Discord startup and connection lifecycle manager.

Provides resilient startup against Discord HTTP 429 / Cloudflare 1015, single-instance
connection locking, separate health vs readiness reporting, and graceful teardown.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import config
from discord.ext import commands

from core.startup.backoff import StartupBackoff
from core.startup.classifier import StartupErrorCategory, classify_startup_error
from core.startup.lifecycle import ConnectionGuard, ShutdownCoordinator, cleanup_bot_for_retry
from core.startup.state import StartupState, StartupStatus

log = logging.getLogger("core.startup.manager")


class StartupManager:
    """Manages the lifecycle, error recovery, and readiness of the Discord bot connection."""

    def __init__(
        self,
        bot: commands.Bot | None = None,
        token: str | None = None,
        *,
        backoff: StartupBackoff | None = None,
        config_validator: Callable[[str], None] | None = None,
        keep_alive_on_degraded: bool | None = None,
        **kwargs: Any,
    ) -> None:
        if bot is None:
            try:
                import main
                bot = getattr(main, "bot", None)
            except Exception:
                bot = None
        if bot is None:
            bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

        self.bot = bot
        self._token = token
        StartupManager._instance = self
        self._backoff = backoff or StartupBackoff.from_env()
        self._config_validator = config_validator or config.validate_discord_token

        if keep_alive_on_degraded is None:
            import os
            self._keep_alive_on_degraded = (
                os.getenv("STARTUP_KEEP_ALIVE_ON_DEGRADED", "").lower() in ("1", "true", "yes")
                or os.getenv("RENDER", "").lower() in ("1", "true", "yes")
            )
        else:
            self._keep_alive_on_degraded = keep_alive_on_degraded

        self._guard = ConnectionGuard()
        self._coordinator = ShutdownCoordinator(bot)
        self._state: StartupState = StartupState.INITIALIZING
        self._start_time: datetime = datetime.now(timezone.utc)
        self._attempt: int = 0
        self._last_error: str | None = None
        self._last_error_category: str | None = None
        self._last_success_at: str | None = None
        self._next_retry_at: str | None = None
        self._last_retry_delay: float | None = None
        self._last_retry_source: str | None = None
        self._ready_event = asyncio.Event()
        self._listener_attached: bool = False

    # ── Public Status Properties ──────────────────────────────────────────────

    @property
    def state(self) -> StartupState:
        """Current lifecycle state."""
        return self._state

    def _set_state(self, state: StartupState) -> None:
        """Set lifecycle state (used for testing and simulation)."""
        self._state = state

    @property
    def is_ready(self) -> bool:
        """True only when the bot has connected and Discord is fully ready."""
        return self._state == StartupState.READY and bool(self.bot.is_ready())

    @property
    def attempt(self) -> int:
        """Current startup attempt count."""
        return self._attempt

    @property
    def uptime_seconds(self) -> float:
        """Seconds elapsed since manager initialization."""
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()

    def get_health_status(self) -> dict[str, Any]:
        """Return safe, structured health and readiness status model."""
        uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        status = StartupStatus(
            state=self._state,
            discord_ready=self.is_ready,
            startup_attempt=self._attempt,
            last_error=self._last_error,
            last_error_category=self._last_error_category,
            last_success_at=self._last_success_at,
            next_retry_at=self._next_retry_at,
            retry_delay_seconds=self._last_retry_delay,
            retry_source=self._last_retry_source,
            uptime_seconds=uptime,
        )
        return status.to_dict()

    # ── Shutdown Controls ─────────────────────────────────────────────────────

    def request_shutdown(self, reason: str = "User request") -> None:
        """Signal that startup and bot operations should stop."""
        self._state = StartupState.STOPPING
        self._coordinator.request_shutdown(reason=reason)

    async def shutdown(self) -> None:
        """Perform graceful shutdown of the bot and background components."""
        self._state = StartupState.STOPPING
        await self._coordinator.execute_shutdown()
        self._state = StartupState.STOPPED

    async def stop(self) -> None:
        """Perform graceful shutdown (alias for shutdown)."""
        await self.shutdown()

    # ── Connection Lifecycle Loop ─────────────────────────────────────────────

    async def run(self, token: str | None = None) -> None:
        """Execute the primary connection loop with automatic transient error recovery."""
        # 1. Enforce single in-process connection lifecycle
        await self._guard.acquire()

        try:
            self._start_time = datetime.now(timezone.utc)
            self._state = StartupState.INITIALIZING
            log.info("[STARTUP] StartupManager initialized. Configuring lifecycle...")

            # Attach signal handlers for graceful teardown
            self._coordinator.attach_signal_handlers()

            # Normalize token
            target_token = config.normalize_token(token or self._token or config.DISCORD_TOKEN)

            # 2. Validate configuration
            self._state = StartupState.VALIDATING
            try:
                self._config_validator(target_token)
                log.info("[STARTUP] Environment and token validation passed.")
            except Exception as val_exc:
                log.critical(
                    "[STARTUP] Startup validation failed: %s. Entering safe degraded mode.",
                    val_exc,
                )
                log.error("Startup validation failed: %s", val_exc)
                self._state = StartupState.INVALID_TOKEN
                self._last_error = str(val_exc)
                self._last_error_category = StartupErrorCategory.CONFIGURATION_ERROR.value
                if self._keep_alive_on_degraded:
                    await self._enter_degraded_wait()
                return

            # Attach ready listener to detect genuine successful connection
            self._attach_ready_listener()

            # 3. Connection loop
            while not self._coordinator.is_shutdown_requested:
                self._attempt += 1

                if not self._backoff.is_attempt_allowed(self._attempt):
                    log.error(
                        "[STARTUP] Max attempts limit reached (%d). Entering degraded mode.",
                        self._attempt,
                    )
                    self._state = StartupState.DEGRADED
                    if self._keep_alive_on_degraded:
                        await self._enter_degraded_wait()
                    break

                self._state = StartupState.CONNECTING
                log.info("[STARTUP] state=CONNECTING attempt=%d", self._attempt)

                try:
                    await self.bot.start(target_token)

                    # If bot.start() returned cleanly without exception:
                    if self._coordinator.is_shutdown_requested:
                        break
                    # Clean close was initiated externally
                    log.info("[STARTUP] Bot closed cleanly.")
                    break

                except asyncio.CancelledError:
                    log.info("[STARTUP] Connection loop cancelled.")
                    self._state = StartupState.STOPPING
                    break

                except Exception as exc:
                    if self._coordinator.is_shutdown_requested:
                        break

                    classification = classify_startup_error(exc)
                    self._last_error = classification.message
                    self._last_error_category = classification.category.value

                    # ── HTTP 429 / Cloudflare 1015 Rate Limited ────────────────
                    if classification.category == StartupErrorCategory.RATE_LIMITED:
                        self._state = StartupState.RATE_LIMITED
                        delay_res = self._backoff.calculate_delay_details(
                            self._attempt, retry_after=classification.retry_after
                        )
                        delay = delay_res.delay
                        self._last_retry_delay = delay
                        self._last_retry_source = delay_res.source
                        next_time = datetime.now(timezone.utc) + timedelta(seconds=delay)
                        self._next_retry_at = next_time.isoformat()

                        ra_val = classification.retry_after
                        if ra_val is not None:
                            ra_str = str(int(ra_val)) if ra_val.is_integer() else f"{ra_val:.2f}"
                        else:
                            ra_str = "unavailable"
                        delay_str = str(int(delay)) if delay.is_integer() else f"{delay:.2f}"

                        log.warning(
                            "[STARTUP] state=RATE_LIMITED status=%s attempt=%d retry_after=%s delay=%s retry_source=%s cloudflare_1015=%s",
                            classification.status_code or 429,
                            self._attempt,
                            ra_str,
                            delay_str,
                            delay_res.source,
                            classification.is_cloudflare_1015,
                        )

                        await cleanup_bot_for_retry(self.bot)

                        if await self._sleep_or_shutdown(delay):
                            break

                        self._state = StartupState.RECONNECTING
                        log.info("[STARTUP] state=RECONNECTING reason=rate_limit")
                        continue

                    # ── HTTP 401 / Invalid Token (Never endlessly retry) ───────
                    elif classification.category == StartupErrorCategory.INVALID_TOKEN:
                        self._state = StartupState.INVALID_TOKEN
                        log.critical(
                            "[STARTUP] state=INVALID_TOKEN Discord authentication failed (401). "
                            "Verify DISCORD_TOKEN in Render environment variables."
                        )
                        log.error(
                            "Discord authentication failed (401). The configured Discord bot token was rejected by Discord. "
                            "Verify that DISCORD_TOKEN contains the current Bot Token for this exact Discord application."
                        )
                        await cleanup_bot_for_retry(self.bot)
                        if self._keep_alive_on_degraded:
                            await self._enter_degraded_wait()
                        break

                    # ── HTTP 403 / Forbidden ──────────────────────────────────
                    elif classification.category == StartupErrorCategory.FORBIDDEN:
                        self._state = StartupState.DEGRADED
                        log.critical(
                            "[STARTUP] state=DEGRADED Discord API returned 403. %s",
                            classification.remediation_hint,
                        )
                        await cleanup_bot_for_retry(self.bot)
                        if self._keep_alive_on_degraded:
                            await self._enter_degraded_wait()
                        break

                    # ── Retryable Transient Network / DNS / Gateway Errors ────
                    elif classification.is_retryable:
                        self._state = StartupState.RECONNECTING
                        delay_res = self._backoff.calculate_delay_details(self._attempt)
                        delay = delay_res.delay
                        self._last_retry_delay = delay
                        self._last_retry_source = delay_res.source
                        next_time = datetime.now(timezone.utc) + timedelta(seconds=delay)
                        self._next_retry_at = next_time.isoformat()

                        log.warning(
                            "[STARTUP] state=RECONNECTING category=%s error=%s attempt=%d delay=%.2fs retry_source=%s",
                            classification.category.value,
                            classification.message,
                            self._attempt,
                            delay,
                            delay_res.source,
                        )

                        await cleanup_bot_for_retry(self.bot)

                        if await self._sleep_or_shutdown(delay):
                            break
                        continue

                    # ── Fatal / Unrecoverable ─────────────────────────────────
                    else:
                        self._state = StartupState.FATAL_ERROR
                        log.error(
                            "[STARTUP] state=FATAL_ERROR Non-retryable error: %s",
                            classification.message,
                        )
                        await cleanup_bot_for_retry(self.bot)
                        if self._keep_alive_on_degraded:
                            await self._enter_degraded_wait()
                        break

        finally:
            await self._guard.release()
            if self._coordinator.is_shutdown_requested and self._state != StartupState.STOPPED:
                await self.shutdown()

    # ── Internal Helpers ──────────────────────────────────────────────────────

    def _attach_ready_listener(self) -> None:
        """Attach on_ready listener to record genuine successful connection and reset backoff."""
        if self._listener_attached:
            return

        async def _on_ready_listener() -> None:
            self._state = StartupState.READY
            self._backoff.reset()
            self._last_success_at = datetime.now(timezone.utc).isoformat()
            self._next_retry_at = None
            self._last_retry_delay = None
            self._last_retry_source = None
            self._ready_event.set()

            duration = (datetime.now(timezone.utc) - self._start_time).total_seconds()
            user_str = str(getattr(self.bot, "user", "unknown"))
            user_id = getattr(self.bot.user, "id", "unknown") if self.bot.user else "unknown"
            guilds_count = len(getattr(self.bot, "guilds", []))

            log.info(
                "[STARTUP] state=READY Discord connection established as %s (ID: %s). "
                "Guilds: %d | Startup duration: %.2fs | Attempt: %d",
                user_str,
                user_id,
                guilds_count,
                duration,
                self._attempt,
            )

        self.bot.add_listener(_on_ready_listener, "on_ready")
        self._listener_attached = True

    async def _sleep_or_shutdown(self, delay: float) -> bool:
        """Sleep for delay seconds or terminate early if shutdown is signaled.

        Returns True if shutdown was requested, False if sleep elapsed.
        """
        try:
            await asyncio.wait_for(self._coordinator.shutdown_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _enter_degraded_wait(self) -> None:
        """Keep the process alive in degraded mode so hosting providers do not loop-restart."""
        log.info(
            "[STARTUP] Remaining alive in safe DEGRADED state. "
            "Process health check remains HTTP 200 while awaiting operator remediation."
        )
        try:
            await self._coordinator.shutdown_event.wait()
        except (asyncio.CancelledError, Exception):
            pass

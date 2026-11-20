"""In-process connection guard and graceful shutdown lifecycle coordinator."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any, Awaitable, Callable

import discord
from discord.ext import commands

log = logging.getLogger("core.startup.lifecycle")


class DuplicateStartupError(RuntimeError):
    """Raised when a concurrent startup or connection attempt is initiated."""


class ConnectionGuard:
    """Ensures exactly one active Discord connection lifecycle per process.

    Thread-safe and async-safe guard preventing duplicate bot.start() loops.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._is_active: bool = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    async def acquire(self) -> bool:
        """Attempt to acquire exclusive ownership of the Discord connection loop.

        Returns True if acquired, raises DuplicateStartupError if already running.
        """
        async with self._lock:
            if self._is_active:
                raise DuplicateStartupError(
                    "A Discord connection loop is already actively running in this process. "
                    "Duplicate connection attempt was blocked."
                )
            self._is_active = True
            log.debug("[STARTUP] ConnectionGuard acquired exclusively.")
            return True

    async def release(self) -> None:
        """Release ownership of the connection loop."""
        async with self._lock:
            self._is_active = False
            log.debug("[STARTUP] ConnectionGuard released.")


async def cleanup_bot_for_retry(bot: commands.Bot) -> None:
    """Cleanly close sessions and re-arm the bot instance for a safe retry.

    Ensures aiohttp sessions/connectors do not leak and discord.py does not raise
    'Session is closed' when reusing the existing bot object.
    """
    try:
        if not bot.is_closed():
            await bot.close()
    except Exception as exc:
        log.debug("[STARTUP] Non-critical error during bot.close() in retry cleanup: %s", exc)

    if hasattr(bot, "http"):
        http = bot.http
        # Cleanly close connector if still open
        connector = getattr(http, "connector", None)
        if connector and not getattr(connector, "closed", True):
            try:
                await connector.close()
            except Exception:
                pass
        http.connector = discord.utils.MISSING

    # Reset closed flag so the same client instance can re-initiate login
    bot._closed = False


class ShutdownCoordinator:
    """Coordinates deterministic, graceful teardown of Discord, databases, and tasks."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._shutdown_event = asyncio.Event()
        self._shutdown_requested: bool = False
        self._cleanup_callbacks: list[Callable[[], Awaitable[None] | None]] = []
        self._tracked_tasks: set[asyncio.Task] = set()
        self._signals_registered: bool = False

    @property
    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @property
    def shutdown_event(self) -> asyncio.Event:
        return self._shutdown_event

    def track_task(self, task: asyncio.Task) -> None:
        """Track an internal task so it can be cancelled during graceful shutdown."""
        self._tracked_tasks.add(task)
        task.add_done_callback(self._tracked_tasks.discard)

    def register_cleanup(self, callback: Callable[[], Awaitable[None] | None]) -> None:
        """Register an async or sync callback to execute during graceful shutdown."""
        self._cleanup_callbacks.append(callback)

    def request_shutdown(self, reason: str = "User/System request") -> None:
        """Signal that application shutdown has been requested."""
        if not self._shutdown_requested:
            self._shutdown_requested = True
            self._shutdown_event.set()
            log.info("[SHUTDOWN] Graceful shutdown requested: %s", reason)

    def attach_signal_handlers(self) -> None:
        """Attach SIGINT and SIGTERM handlers to trigger graceful shutdown."""
        if self._signals_registered:
            return

        def _handle_signal(sig_name: str) -> None:
            log.info("[SHUTDOWN] Received signal: %s", sig_name)
            self.request_shutdown(reason=f"Signal {sig_name}")

        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda s=sig.name: _handle_signal(s))
                except (NotImplementedError, AttributeError):
                    # Windows event loops do not support add_signal_handler for all signals
                    signal.signal(sig, lambda s_num, frame, s=sig.name: _handle_signal(s))
            self._signals_registered = True
        except Exception as exc:
            log.debug("[SHUTDOWN] Note: Unable to attach async signal handler: %s", exc)

    async def execute_shutdown(self) -> None:
        """Execute deterministic shutdown sequence."""
        self._shutdown_requested = True
        self._shutdown_event.set()
        log.info("[SHUTDOWN] Beginning graceful shutdown sequence...")

        # 1. Close Discord Bot
        try:
            if not self.bot.is_closed():
                await self.bot.close()
                log.info("[SHUTDOWN] Discord bot client closed.")
        except Exception as exc:
            log.warning("[SHUTDOWN] Error closing Discord bot: %s", exc)

        # 2. Execute custom cleanup callbacks (e.g. database connections)
        for cb in self._cleanup_callbacks:
            try:
                res = cb()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                log.warning("[SHUTDOWN] Error in cleanup callback: %s", exc)

        # 3. Cancel registered / tracked background tasks
        current_task = asyncio.current_task()
        pending_tasks = [t for t in self._tracked_tasks if t is not current_task and not t.done()]
        if pending_tasks:
            log.info("[SHUTDOWN] Cancelling %d tracked background tasks...", len(pending_tasks))
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        log.info("[SHUTDOWN] Graceful shutdown completed cleanly.")

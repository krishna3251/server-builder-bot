"""Health and readiness HTTP server for Render and monitoring.

Distinguishes between process health (/health) and Discord connection readiness (/ready)
to prevent hosting provider restart loops during temporary rate limits.
"""

from __future__ import annotations

import logging
import os
from threading import Thread
from typing import TYPE_CHECKING

from flask import Flask, jsonify, request

if TYPE_CHECKING:
    from core.startup.manager import StartupManager

log = logging.getLogger("core.startup.health_server")


def create_health_app(manager: StartupManager | None = None) -> Flask:
    """Create Flask application exposing decoupled health and readiness endpoints."""
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # Store manager reference on app object for route access and testing
    app.startup_manager = manager  # type: ignore[attr-defined]

    @app.route("/")
    @app.route("/health")
    def health_check():
        """Process Health Check.

        Rule: Returns HTTP 200 as long as the process is alive.
        Even during Discord HTTP 429 rate-limiting, /health stays 200 to prevent
        Render from triggering a process restart loop.
        """
        mgr: StartupManager | None = getattr(app, "startup_manager", None)
        state_str = mgr.state.value if mgr else "INITIALIZING"
        return (
            jsonify(
                {
                    "status": "ok",
                    "service": "damu",
                    "process": "alive",
                    "message": "Bot is alive!",
                    "startup_state": state_str,
                }
            ),
            200,
        )

    @app.route("/ready")
    def readiness_check():
        """Readiness Check.

        Returns HTTP 200 when Discord is genuinely ready.
        Returns HTTP 503 when Discord is disconnected, rate-limited, or reconnecting.
        """
        mgr: StartupManager | None = getattr(app, "startup_manager", None)
        is_ready = mgr.is_ready if mgr else False
        state_str = mgr.state.value if mgr else "NOT_CONFIGURED"

        if is_ready:
            return (
                jsonify(
                    {
                        "status": "ready",
                        "discord": "connected",
                        "startup_state": state_str,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "status": "not_ready",
                        "discord": "disconnected",
                        "startup_state": state_str,
                    }
                ),
                503,
            )

    @app.route("/status")
    def status_check():
        """Internal diagnostic health status model (zero secret exposure)."""
        mgr: StartupManager | None = getattr(app, "startup_manager", None)
        if mgr:
            return jsonify(mgr.get_health_status()), 200
        return jsonify({"status": "uninitialized"}), 200

    return app


def start_health_server(
    manager: StartupManager | None = None,
    host: str = "0.0.0.0",
    port: int | None = None,
) -> tuple[Flask, Thread]:
    """Launch the health server on a background daemon thread."""
    app = create_health_app(manager)
    actual_port = port if port is not None else int(os.environ.get("PORT", 8080))

    # Suppress werkzeug access logs during continuous health pings
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    server_thread = Thread(
        target=lambda: app.run(host=host, port=actual_port, use_reloader=False, threaded=True),
        name="damu-health-server",
        daemon=True,
    )
    server_thread.start()
    log.info("[STARTUP] Health server listening on %s:%d (/health, /ready, /status)", host, actual_port)
    return app, server_thread

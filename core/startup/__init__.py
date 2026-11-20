"""Startup and connection lifecycle management subsystem for DAMU Server Builder."""

from __future__ import annotations

from core.startup.backoff import StartupBackoff
from core.startup.classifier import (
    ClassificationResult,
    StartupErrorCategory,
    classify_startup_error,
)
from core.startup.health_server import create_health_app, start_health_server
from core.startup.lifecycle import (
    ConnectionGuard,
    DuplicateStartupError,
    ShutdownCoordinator,
    cleanup_bot_for_retry,
)
from core.startup.manager import StartupManager
from core.startup.state import StartupState, StartupStatus

__all__ = [
    "ClassificationResult",
    "ConnectionGuard",
    "DuplicateStartupError",
    "ShutdownCoordinator",
    "StartupBackoff",
    "StartupErrorCategory",
    "StartupManager",
    "StartupState",
    "StartupStatus",
    "classify_startup_error",
    "cleanup_bot_for_retry",
    "create_health_app",
    "start_health_server",
]

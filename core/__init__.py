"""DAMU Core Engine — Central architecture for Discord server management and resilience."""

from core.diagnostics import get_diagnostic_status
from core.discord_api.guard import DiscordAPIGuard, api_guard
from core.discord_api.state import APIState
from core.errors import (
    DamuError,
    ErrorCategory,
    classify_error,
    format_user_error,
)
from core.interaction import (
    InteractionResult,
    InteractionResultReason,
    InteractionSafety,
    interaction_alive,
    is_acknowledged,
    safe_defer,
    safe_edit,
    safe_followup,
    safe_modal,
    safe_send,
)
from core.models import (
    Action,
    ActionStatus,
    ActionType,
    BuildPlan,
    EngineResult,
    RiskLevel,
)
from core.startup.manager import StartupManager
from core.startup.state import StartupState

__all__ = [
    "DamuError",
    "ErrorCategory",
    "classify_error",
    "format_user_error",
    "InteractionSafety",
    "InteractionResult",
    "InteractionResultReason",
    "interaction_alive",
    "is_acknowledged",
    "safe_defer",
    "safe_send",
    "safe_followup",
    "safe_edit",
    "safe_modal",
    "Action",
    "ActionStatus",
    "ActionType",
    "BuildPlan",
    "EngineResult",
    "RiskLevel",
    "StartupState",
    "StartupManager",
    "APIState",
    "DiscordAPIGuard",
    "api_guard",
    "get_diagnostic_status",
]

"""Data models for DAMU Core Engine execution, plans, and results."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionType(str, enum.Enum):
    CREATE_ROLE = "create_role"
    EDIT_ROLE = "edit_role"
    DELETE_ROLE = "delete_role"
    CREATE_CATEGORY = "create_category"
    EDIT_CATEGORY = "edit_category"
    DELETE_CATEGORY = "delete_category"
    CREATE_CHANNEL = "create_channel"
    EDIT_CHANNEL = "edit_channel"
    DELETE_CHANNEL = "delete_channel"
    MOVE_CHANNEL = "move_channel"
    CLONE_CHANNEL = "clone_channel"
    SET_PERMISSIONS = "set_permissions"
    SYNC_PERMISSIONS = "sync_permissions"
    CONFIGURE_MODULE = "configure_module"


class ActionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"


def generate_build_id() -> str:
    """Generate a readable transaction ID: DAMU-YYYYMMDD-XXXXX."""
    now = datetime.now(timezone.utc)
    short_uid = uuid.uuid4().hex[:5].upper()
    return f"DAMU-{now.strftime('%Y%m%d')}-{short_uid}"


@dataclass
class Action:
    type: ActionType
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    rollback_data: dict[str, Any] = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    error: str | None = None
    created_resource_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "type": self.type.value,
            "target": self.target,
            "parameters": self.parameters,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "error": self.error,
            "created_resource_id": self.created_resource_id,
        }


@dataclass
class BuildPlan:
    guild_id: int
    requester_id: int
    operation: str
    risk_level: RiskLevel = RiskLevel.LOW
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    build_id: str = field(default_factory=generate_build_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def total_actions(self) -> int:
        return len(self.actions)

    def completed_actions(self) -> list[Action]:
        return [a for a in self.actions if a.status == ActionStatus.COMPLETED]

    def failed_actions(self) -> list[Action]:
        return [a for a in self.actions if a.status == ActionStatus.FAILED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "guild_id": self.guild_id,
            "requester_id": self.requester_id,
            "operation": self.operation,
            "risk_level": self.risk_level.value,
            "actions": [a.to_dict() for a in self.actions],
            "warnings": self.warnings,
            "requires_confirmation": self.requires_confirmation,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class EngineResult:
    success: bool
    operation: str
    resource_id: int | None = None
    message: str = ""
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    recovery: str | None = None
    build_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        operation: str,
        message: str,
        resource_id: int | None = None,
        actions: list[Action] | None = None,
        warnings: list[str] | None = None,
        build_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> EngineResult:
        return cls(
            success=True,
            operation=operation,
            resource_id=resource_id,
            message=message,
            actions=actions or [],
            warnings=warnings or [],
            build_id=build_id,
            data=data or {},
        )

    @classmethod
    def fail(
        cls,
        operation: str,
        message: str,
        error_code: str = "ERROR",
        recovery: str | None = None,
        warnings: list[str] | None = None,
        build_id: str | None = None,
        actions: list[Action] | None = None,
    ) -> EngineResult:
        return cls(
            success=False,
            operation=operation,
            message=message,
            error_code=error_code,
            recovery=recovery,
            warnings=warnings or [],
            build_id=build_id,
            actions=actions or [],
        )

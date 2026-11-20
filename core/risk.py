"""Classify visible changes before exposing a confirmation control."""
from core.models import ActionType, BuildPlan, RiskLevel
from engines.guild.permission_engine import permission_engine


def assess_plan_risk(plan: BuildPlan) -> RiskLevel:
    plan.requires_confirmation = bool(plan.actions)
    elevated = set()
    risky_action = False
    for action in plan.actions:
        params = action.parameters
        grants = permission_engine.check_dangerous_permissions(params.get("permissions") or [])
        if "administrator" in grants:
            return RiskLevel.CRITICAL
        elevated.update(grants)
        for spec in params.get("permission_overwrites", []):
            elevated.update(permission_engine.check_dangerous_permissions(spec.get("allow", [])))
        elevated.update(permission_engine.check_dangerous_permissions(
            [name for name, enabled in params.get("overwrites", {}).items() if enabled is True]
        ))
        risky_action |= action.type in (
            ActionType.DELETE_CHANNEL, ActionType.DELETE_CATEGORY, ActionType.DELETE_ROLE,
            ActionType.SET_PERMISSIONS, ActionType.RENAME_GUILD,
        ) or params.get("permission_preset", "inherit") != "inherit"
    if risky_action or elevated or len(plan.actions) > 15:
        return RiskLevel.HIGH
    if len(plan.actions) > 5 or any(action.type == ActionType.CREATE_ROLE for action in plan.actions):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW

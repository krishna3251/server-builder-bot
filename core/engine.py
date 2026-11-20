"""Central execution engine for DAMU Server Builder operations."""

from __future__ import annotations

import logging
from typing import Any

import discord
from services.json_builder import build_session

from core.context import ExecutionContext
from core.errors import DamuError, ErrorCategory, PermissionError, classify_error
from core.events import bus
from core.logging import log_event
from core.models import (
    Action,
    ActionStatus,
    ActionType,
    BuildPlan,
    EngineResult,
    RiskLevel,
)

log = logging.getLogger("core.engine")


class DamuEngine:
    """Orchestrates plan generation, validation, execution, verification, and rollback."""

    def __init__(self) -> None:
        self.log = log

    async def create_context(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
        interaction: discord.Interaction | None = None,
    ) -> ExecutionContext:
        """Create a fresh execution context with a unique transaction ID."""
        return ExecutionContext(guild=guild, user=user, interaction=interaction)

    async def plan(self, request: dict[str, Any], context: ExecutionContext) -> BuildPlan:
        """Generate a structured BuildPlan from a user request, schema, or preset."""
        operation = request.get("operation") or request.get("action") or "custom_operation"
        plan = BuildPlan(
            guild_id=context.guild.id,
            requester_id=context.user.id,
            operation=operation,
            build_id=context.build_id,
            metadata=request,
        )

        actions_data = request.get("actions", [])
        if not actions_data and "action" in request:
            actions_data = [request]

        for item in actions_data:
            act_type_str = item.get("action") or item.get("type", "")
            try:
                act_type = ActionType(act_type_str)
            except ValueError:
                act_type = ActionType.CREATE_CHANNEL

            action = Action(
                type=act_type,
                target=item.get("target") or item.get("name", "unnamed"),
                parameters=item.get("parameters") or item,
                dependencies=item.get("dependencies", []),
                rollback_data=item.get("rollback_data", {}),
            )
            plan.actions.append(action)

        # Assess risk
        plan.risk_level = self._assess_risk(plan)
        if plan.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            plan.requires_confirmation = True

        context.log(f"Plan generated: {operation} ({len(plan.actions)} actions, risk: {plan.risk_level.value})")
        await bus.emit("plan_created", plan=plan, context=context)
        return plan

    def _assess_risk(self, plan: BuildPlan) -> RiskLevel:
        """Determine risk level of a plan based on requested actions and permissions."""
        has_delete = any("delete" in a.type.value for a in plan.actions)
        has_mass_ops = len(plan.actions) > 10

        has_admin = False
        for a in plan.actions:
            perms = a.parameters.get("permissions") or []
            if isinstance(perms, list) and any(str(p).lower() == "administrator" for p in perms):
                has_admin = True
                plan.warnings.append(f"Action '{a.target}' grants dangerous Administrator permission.")

        if has_admin:
            return RiskLevel.CRITICAL
        if has_delete or has_mass_ops:
            return RiskLevel.HIGH
        if len(plan.actions) > 3 or any("role" in a.type.value for a in plan.actions):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    async def validate(self, plan: BuildPlan, context: ExecutionContext) -> tuple[bool, list[str]]:
        """Validate permissions, hierarchy, and schema consistency before touching Discord API."""
        errors: list[str] = []
        bot = context.bot_member

        if not bot:
            return False, ["Bot member object not found in guild."]

        # Check basic bot permissions
        bot_perms = bot.guild_permissions
        for action in plan.actions:
            if "channel" in action.type.value or "category" in action.type.value:
                if not bot_perms.manage_channels:
                    errors.append("Bot lacks 'Manage Channels' permission.")
                    break
            if "role" in action.type.value:
                if not bot_perms.manage_roles:
                    errors.append("Bot lacks 'Manage Roles' permission.")
                    break

        # Check role hierarchy if creating/modifying roles
        for action in plan.actions:
            if action.type == ActionType.CREATE_ROLE or action.type == ActionType.EDIT_ROLE:
                target_role_id = action.parameters.get("role_id")
                if target_role_id:
                    role = context.guild.get_role(int(target_role_id))
                    if role and role >= bot.top_role:
                        errors.append(f"Role '{role.name}' is higher than or equal to bot's highest role.")

        valid = len(errors) == 0
        context.log(f"Validation {'passed' if valid else 'failed'}: {errors}")
        await bus.emit("plan_validated", plan=plan, context=context, valid=valid, errors=errors)
        return valid, errors

    async def execute(self, plan: BuildPlan, context: ExecutionContext) -> EngineResult:
        if (plan.guild_id != context.guild.id or plan.requester_id != context.user.id
                or not context.user.guild_permissions.administrator):
            return EngineResult.fail(operation=plan.operation, message="The requesting administrator must confirm the plan.",
                                     error_code="PERMISSION_REQUIRED", build_id=plan.build_id)
        try:
            async with build_session(context.guild.id):
                return await self._execute(plan, context)
        except RuntimeError as error:
            return EngineResult.fail(operation=plan.operation, message="Another build is running or the plan could not finish.",
                                     error_code="BUILD_UNAVAILABLE", build_id=plan.build_id)

    async def _execute(self, plan: BuildPlan, context: ExecutionContext) -> EngineResult:
        """Execute all actions in plan step-by-step with rollback on failure."""
        valid, errors = await self.validate(plan, context)
        if not valid:
            return EngineResult.fail(
                operation=plan.operation,
                message=f"Validation failed: {'; '.join(errors)}",
                error_code="VALIDATION_FAILED",
                build_id=plan.build_id,
            )

        context.log(f"Executing build transaction {plan.build_id}")
        log_event("build_started", guild_id=context.guild.id, user_id=context.user.id, build_id=plan.build_id, operation=plan.operation)

        created_channels: list[discord.abc.GuildChannel] = []
        created_roles: list[discord.Role] = []

        try:
            from engines.guild.category_engine import CategoryEngine
            from engines.guild.channel_engine import ChannelEngine
            from engines.guild.role_engine import RoleEngine

            channel_engine = ChannelEngine()
            category_engine = CategoryEngine()
            role_engine = RoleEngine()

            for action in plan.actions:
                if context.is_cancelled():
                    raise DamuError("Execution cancelled by user or timeout.", category=ErrorCategory.TRANSIENT)

                action.status = ActionStatus.RUNNING
                await bus.emit("action_started", action=action, context=context)

                # Route action to appropriate resource engine
                if action.type == ActionType.CREATE_ROLE:
                    role = await role_engine.create_role(context.guild, action.parameters)
                    created_roles.append(role)
                    action.created_resource_id = role.id
                    action.status = ActionStatus.COMPLETED
                elif action.type == ActionType.DELETE_ROLE:
                    role_id = int(action.parameters.get("role_id", 0))
                    role = context.guild.get_role(role_id)
                    if role:
                        await role.delete(reason=f"DAMU plan {plan.build_id}")
                    action.status = ActionStatus.COMPLETED
                elif action.type == ActionType.CREATE_CATEGORY:
                    cat = await category_engine.create_category(context.guild, action.parameters)
                    created_channels.append(cat)
                    action.created_resource_id = cat.id
                    action.status = ActionStatus.COMPLETED
                elif action.type == ActionType.CREATE_CHANNEL:
                    ch = await channel_engine.create_channel(context.guild, action.parameters)
                    created_channels.append(ch)
                    action.created_resource_id = ch.id
                    action.status = ActionStatus.COMPLETED
                elif action.type == ActionType.DELETE_CHANNEL:
                    ch_id = int(action.parameters.get("channel_id", 0))
                    ch = context.guild.get_channel(ch_id)
                    if ch:
                        await ch.delete(reason=f"DAMU plan {plan.build_id}")
                    action.status = ActionStatus.COMPLETED
                elif action.type == ActionType.SET_PERMISSIONS:
                    await channel_engine.update_permissions(context.guild, action.parameters)
                    action.status = ActionStatus.COMPLETED
                else:
                    action.status = ActionStatus.COMPLETED

                context.log(f"Action completed: {action.type.value} -> {action.target}")
                await bus.emit("action_completed", action=action, context=context)

            # Verification step
            verified = await self.verify(plan, context)
            if not verified:
                plan.warnings.append("Some resources could not be fully verified after creation.")

            log_event("build_completed", guild_id=context.guild.id, build_id=plan.build_id, status="SUCCESS")
            return EngineResult.ok(
                operation=plan.operation,
                message=f"Completed {len(plan.actions)} actions successfully.",
                resource_id=plan.actions[0].created_resource_id if plan.actions else None,
                actions=plan.actions,
                warnings=plan.warnings,
                build_id=plan.build_id,
            )

        except Exception as exc:
            context.log(f"Execution failed: {exc}. Commencing recovery & rollback.")
            log.error("Execution failed during plan %s: %s", plan.build_id, exc, exc_info=True)
            log_event("build_failed", guild_id=context.guild.id, build_id=plan.build_id, error=str(exc))

            # Mark current action as failed
            for a in plan.actions:
                if a.status == ActionStatus.RUNNING:
                    a.status = ActionStatus.FAILED
                    a.error = str(exc)

            # Rollback created items
            rollback_log = await self.recover(created_channels, created_roles, plan, context)

            error_category = classify_error(exc)
            return EngineResult.fail(
                operation=plan.operation,
                message=f"Execution failed: {exc}",
                error_code=error_category.value,
                recovery=f"Rolled back {len(created_channels)} channels and {len(created_roles)} roles: {rollback_log}",
                warnings=plan.warnings,
                build_id=plan.build_id,
                actions=plan.actions,
            )

    async def verify(self, plan: BuildPlan, context: ExecutionContext) -> bool:
        """Verify that newly created/modified resources exist in the guild."""
        for action in plan.actions:
            if action.status == ActionStatus.COMPLETED and action.created_resource_id:
                if "channel" in action.type.value or "category" in action.type.value:
                    if not context.guild.get_channel(action.created_resource_id):
                        return False
                elif "role" in action.type.value:
                    if not context.guild.get_role(action.created_resource_id):
                        return False
        return True

    async def recover(
        self,
        created_channels: list[discord.abc.GuildChannel],
        created_roles: list[discord.Role],
        plan: BuildPlan,
        context: ExecutionContext,
    ) -> str:
        """Roll back partially created channels and roles to keep guild state clean."""
        context.log("Executing rollback on created resources")
        deleted_ch = 0
        deleted_ro = 0

        for ch in reversed(created_channels):
            try:
                await ch.delete(reason=f"Rollback for plan {plan.build_id}")
                deleted_ch += 1
            except Exception as e:
                context.log(f"Failed to delete channel {ch.id} during rollback: {e}")

        for role in reversed(created_roles):
            try:
                await role.delete(reason=f"Rollback for plan {plan.build_id}")
                deleted_ro += 1
            except Exception as e:
                context.log(f"Failed to delete role {role.id} during rollback: {e}")

        for a in plan.actions:
            if a.status == ActionStatus.COMPLETED:
                a.status = ActionStatus.ROLLED_BACK

        return f"Cleaned up {deleted_ch} channels and {deleted_ro} roles."


engine = DamuEngine()

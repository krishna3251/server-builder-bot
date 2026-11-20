"""Central execution engine for DAMU Server Builder operations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from core.context import ExecutionContext
from core.errors import DamuError, ErrorCategory, ValidationError, classify_error
from core.events import bus
from core.logging import log_event
from core.models import Action, ActionStatus, ActionType, BuildPlan, EngineResult
from core.risk import assess_plan_risk
from engines.guild.category_engine import CategoryEngine
from engines.guild.channel_engine import ChannelEngine
from engines.guild.permission_engine import permission_engine
from engines.guild.role_engine import RoleEngine
from services.json_builder import build_session

log = logging.getLogger("core.engine")

SUPPORTED_ACTIONS = {
    ActionType.CREATE_ROLE, ActionType.DELETE_ROLE, ActionType.CREATE_CATEGORY,
    ActionType.CREATE_CHANNEL, ActionType.DELETE_CHANNEL, ActionType.SET_PERMISSIONS,
    ActionType.RENAME_GUILD,
}


class DamuEngine:
    """Validate and execute the plan reviewed by the requesting administrator."""

    async def create_context(
        self, guild: discord.Guild, user: discord.User | discord.Member,
        interaction: discord.Interaction | None = None,
    ) -> ExecutionContext:
        return ExecutionContext(guild=guild, user=user, interaction=interaction)

    async def plan(self, request: dict[str, Any], context: ExecutionContext) -> BuildPlan:
        operation = request.get("operation") or request.get("action") or "custom_operation"
        plan = BuildPlan(context.guild.id, context.user.id, operation, build_id=context.build_id, metadata=request)
        items = request.get("actions") or ([request] if "action" in request else [])
        for item in items:
            try:
                action_type = ActionType(item.get("action") or item.get("type", ""))
            except ValueError as error:
                raise ValidationError("The plan contains an unknown action.") from error
            if action_type not in SUPPORTED_ACTIONS:
                raise ValidationError(f"The manager cannot apply {action_type.value} actions.")
            plan.actions.append(Action(
                type=action_type, target=item.get("target") or item.get("name", "unnamed"),
                parameters=item.get("parameters") or item,
                dependencies=item.get("dependencies", []),
            ))
        plan.risk_level = assess_plan_risk(plan)
        await bus.emit("plan_created", plan=plan, context=context)
        return plan

    async def validate(self, plan: BuildPlan, context: ExecutionContext) -> tuple[bool, list[str]]:
        guild = context.guild
        bot = context.bot_member
        if not bot:
            return False, ["The bot's server membership could not be resolved."]
        if not plan.actions:
            return False, ["This plan has no changes."]

        errors: list[str] = []
        categories: dict[str, str] = {}
        action_ids: set[str] = set()
        for action in plan.actions:
            params = action.parameters
            kind = action.type
            if kind not in SUPPORTED_ACTIONS:
                errors.append(f"Unsupported manager action: {kind.value}.")
                continue
            if action.action_id in action_ids or action.status != ActionStatus.PENDING:
                errors.append("An action is duplicated or has already been processed.")
            action_ids.add(action.action_id)
            if action.dependencies or params.get("category_target"):
                if (kind != ActionType.CREATE_CHANNEL or len(action.dependencies) != 1
                        or categories.get(action.dependencies[0]) != params.get("category_target")
                        or action.dependencies[0] not in categories):
                    errors.append(f"Invalid category dependency for {action.target}.")
            if kind == ActionType.CREATE_CATEGORY:
                categories[action.action_id] = action.target

            if kind in (ActionType.CREATE_CATEGORY, ActionType.CREATE_CHANNEL) and not bot.guild_permissions.manage_channels:
                errors.append("The bot needs Manage Channels to create channels or categories.")
            if kind in (ActionType.CREATE_ROLE, ActionType.DELETE_ROLE) and not bot.guild_permissions.manage_roles:
                errors.append("The bot needs Manage Roles to create or delete roles.")
            if kind == ActionType.RENAME_GUILD and not bot.guild_permissions.manage_guild:
                errors.append("The bot needs Manage Server to rename the server.")
            if kind in (ActionType.DELETE_CHANNEL, ActionType.SET_PERMISSIONS):
                channel = guild.get_channel(params.get("channel_id"))
                permission = "manage_channels" if kind == ActionType.DELETE_CHANNEL else "manage_roles"
                if channel is None:
                    errors.append(f"Channel unavailable: {action.target}.")
                elif not getattr(channel.permissions_for(bot), permission):
                    errors.append(f"The bot needs {permission.replace('_', ' ').title()} in {action.target}.")
            if kind == ActionType.DELETE_ROLE:
                role = guild.get_role(params.get("role_id"))
                if (role is None or role.is_default() or not permission_engine.bot_can_manage_role(guild, role)
                        or not permission_engine.can_manage_role(context.user, role)):
                    errors.append(f"Role unavailable or outside the permitted hierarchy: {action.target}.")
            if kind == ActionType.CREATE_ROLE:
                try:
                    RoleEngine.validate_permissions(bot, params.get("permissions", []), allow_administrator=True)
                except DamuError as error:
                    errors.append(str(error))
        errors = list(dict.fromkeys(errors))
        await bus.emit("plan_validated", plan=plan, context=context, valid=not errors, errors=errors)
        return not errors, errors

    async def execute(self, plan: BuildPlan, context: ExecutionContext, *, confirmed: bool = False) -> EngineResult:
        if (not confirmed or plan.guild_id != context.guild.id or plan.requester_id != context.user.id
                or not getattr(getattr(context.user, "guild_permissions", None), "administrator", False)):
            return EngineResult.fail(
                plan.operation, "The requesting administrator must confirm the plan.",
                error_code="PERMISSION_REQUIRED", build_id=plan.build_id,
            )
        try:
            async with build_session(context.guild.id):
                return await self._execute(plan, context)
        except RuntimeError:
            return EngineResult.fail(
                plan.operation, "Another build is running or the plan could not finish.",
                error_code="BUILD_UNAVAILABLE", build_id=plan.build_id,
            )

    async def _execute(self, plan: BuildPlan, context: ExecutionContext) -> EngineResult:
        valid, errors = await self.validate(plan, context)
        if not valid:
            return EngineResult.fail(plan.operation, "\n".join(errors), error_code="VALIDATION_FAILED", build_id=plan.build_id)

        guild = context.guild
        log_event("build_started", guild_id=guild.id, user_id=context.user.id, build_id=plan.build_id, operation=plan.operation)
        channel_engine, category_engine, role_engine = ChannelEngine(), CategoryEngine(), RoleEngine()
        categories: dict[str, discord.CategoryChannel] = {}
        roles: dict[str, discord.Role] = {}
        created: list[tuple[Action, discord.abc.GuildChannel | discord.Role]] = []

        try:
            for action in plan.actions:
                if context.is_cancelled():
                    raise DamuError("The build was cancelled.", category=ErrorCategory.TRANSIENT)
                action.status = ActionStatus.RUNNING
                await bus.emit("action_started", action=action, context=context)
                params = action.parameters
                resource = None

                if action.type == ActionType.CREATE_ROLE:
                    resource = await role_engine.create_role(guild, params, allow_administrator=True)
                    roles[action.target] = resource
                elif action.type == ActionType.CREATE_CATEGORY:
                    resource = await category_engine.create_category(guild, params, created_roles=roles)
                    categories[action.action_id] = resource
                elif action.type == ActionType.CREATE_CHANNEL:
                    if action.dependencies:
                        params = {**params, "category": categories[action.dependencies[0]]}
                    resource = await channel_engine.create_channel(guild, params, created_roles=roles)
                elif action.type == ActionType.DELETE_ROLE:
                    role = guild.get_role(params["role_id"])
                    if role is None or not permission_engine.can_manage_role(context.user, role):
                        raise ValidationError("The role is missing or outside the permitted hierarchy.")
                    await role_engine.delete_role(guild, role.id, reason=f"DAMU plan {plan.build_id}")
                elif action.type == ActionType.DELETE_CHANNEL:
                    await channel_engine.delete_channel(guild, params)
                elif action.type == ActionType.SET_PERMISSIONS:
                    await channel_engine.update_permissions(guild, params)
                elif action.type == ActionType.RENAME_GUILD:
                    await guild.edit(name=params["name"], reason=f"DAMU plan {plan.build_id}")

                if resource is not None:
                    created.append((action, resource))
                    action.created_resource_id = resource.id
                action.status = ActionStatus.COMPLETED
                await bus.emit("action_completed", action=action, context=context)
        except (Exception, asyncio.CancelledError) as exc:
            log.exception("Execution failed during plan %s", plan.build_id)
            for action in plan.actions:
                if action.status == ActionStatus.RUNNING:
                    action.status = ActionStatus.FAILED
                    action.error = classify_error(exc).value
                elif action.status == ActionStatus.PENDING:
                    action.status = ActionStatus.SKIPPED
            removed = await self.recover(created, plan.build_id)
            if isinstance(exc, asyncio.CancelledError):
                raise
            message = str(exc) if isinstance(exc, DamuError) else "Discord could not complete the build. Review the execution audit."
            recovery = f"Reverted {removed} of {len(created)} newly created resources."
            if any(action.status == ActionStatus.COMPLETED for action in plan.actions):
                recovery += " Completed changes still listed may require manual review."
            return EngineResult.fail(
                plan.operation, message, error_code=classify_error(exc).value, recovery=recovery,
                warnings=plan.warnings, build_id=plan.build_id, actions=plan.actions,
            )

        log_event("build_completed", guild_id=guild.id, build_id=plan.build_id, status="SUCCESS")
        return EngineResult.ok(
            plan.operation, f"Completed {len(plan.actions)} actions.",
            resource_id=created[0][1].id if created else None, actions=plan.actions,
            warnings=plan.warnings, build_id=plan.build_id,
        )

    async def recover(self, created, build_id: str) -> int:
        removed = 0
        for action, resource in reversed(created):
            try:
                await resource.delete(reason=f"Rollback for plan {build_id}")
            except discord.NotFound:
                pass
            except Exception:
                log.exception("Could not remove resource %s during rollback of %s", resource.id, build_id)
                continue
            action.status = ActionStatus.ROLLED_BACK
            removed += 1
        return removed


engine = DamuEngine()

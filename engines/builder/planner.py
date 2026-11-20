"""Builder Planner — Converts server schemas and wizard configurations into structured BuildPlans."""

from __future__ import annotations

import logging
from typing import Any

import discord

from core.models import Action, ActionType, BuildPlan, RiskLevel
from engines.builder.schema import schema_validator
from engines.guild.permission_engine import permission_engine

log = logging.getLogger("engines.builder.planner")


class BuilderPlanner:
    """Generates execution plans from server schemas and module selections."""

    def create_plan_from_schema(
        self,
        guild: discord.Guild,
        requester: discord.User | discord.Member,
        schema: dict[str, Any],
        *,
        operation_name: str = "server_build",
        selected_modules: list[str] | None = None,
        skip_existing_roles: bool = True,
    ) -> BuildPlan:
        validated_schema = schema_validator.parse_and_validate(schema)
        warnings = schema_validator.check_security(validated_schema)

        plan = BuildPlan(
            guild_id=guild.id,
            requester_id=requester.id,
            operation=operation_name,
            warnings=warnings,
            metadata={"schema": validated_schema, "selected_modules": selected_modules or []},
        )

        # 1. Rename guild if requested
        if validated_schema.get("server_name"):
            plan.actions.append(
                Action(
                    type=ActionType.EDIT_CATEGORY,  # or generic edit
                    target=f"Guild: {validated_schema['server_name']}",
                    parameters={"action": "rename_guild", "name": validated_schema["server_name"]},
                )
            )

        # 2. Roles
        existing_role_names = {r.name.lower(): r for r in guild.roles}
        for role_data in validated_schema.get("roles", []):
            r_name = role_data.get("name", "")
            if skip_existing_roles and r_name.lower() in existing_role_names:
                continue

            plan.actions.append(
                Action(
                    type=ActionType.CREATE_ROLE,
                    target=r_name,
                    parameters=role_data,
                )
            )

        # 3. Categories and channels
        for cat_data in validated_schema.get("categories", []):
            cat_name = cat_data.get("name", "")
            cat_action_id = f"cat_{cat_name}"
            plan.actions.append(
                Action(
                    type=ActionType.CREATE_CATEGORY,
                    target=cat_name,
                    parameters=cat_data,
                    action_id=cat_action_id,
                )
            )

            for ch_data in cat_data.get("channels", []):
                ch_name = ch_data.get("name", "")
                ch_params = dict(ch_data)
                ch_params["category_target"] = cat_name
                plan.actions.append(
                    Action(
                        type=ActionType.CREATE_CHANNEL,
                        target=ch_name,
                        parameters=ch_params,
                        dependencies=[cat_action_id],
                    )
                )

        # 4. Optional modules
        if selected_modules:
            for mod in selected_modules:
                plan.actions.append(
                    Action(
                        type=ActionType.CONFIGURE_MODULE,
                        target=f"Module: {mod}",
                        parameters={"module": mod},
                    )
                )

        # Risk assessment
        has_admin = any("administrator" in w.lower() for w in warnings)
        if has_admin:
            plan.risk_level = RiskLevel.CRITICAL
            plan.requires_confirmation = True
        elif len(plan.actions) > 15:
            plan.risk_level = RiskLevel.HIGH
            plan.requires_confirmation = True
        elif len(plan.actions) > 5:
            plan.risk_level = RiskLevel.MEDIUM
        else:
            plan.risk_level = RiskLevel.LOW

        return plan


builder_planner = BuilderPlanner()

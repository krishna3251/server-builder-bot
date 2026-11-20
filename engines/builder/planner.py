"""Builder Planner — Converts server schemas and wizard configurations into structured BuildPlans."""

from __future__ import annotations

import logging
from typing import Any

import discord

from core.errors import ValidationError
from core.models import Action, ActionType, BuildPlan
from core.risk import assess_plan_risk
from engines.builder.schema import schema_validator

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

        # 2. Roles
        referenced_roles = {
            overwrite["role"]
            for category in validated_schema.get("categories", [])
            for resource in (category, *category.get("channels", []))
            for overwrite in resource.get("permission_overwrites", [])
        }
        for role_data in validated_schema.get("roles", []):
            r_name = role_data.get("name", "")
            if skip_existing_roles:
                matches = [r for r in guild.roles if r.name.casefold() == r_name.casefold()]
                if matches:
                    if r_name in referenced_roles:
                        if len(matches) != 1 or matches[0].name != r_name or matches[0].managed:
                            raise ValidationError(f"Resolve the ambiguous or managed role '{r_name}' before building.")
                        warnings.append(f"Channel access uses the existing role '{r_name}' and its members.")
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

        # Rename last so failed resource creation cannot leave a changed guild name.
        server_name = validated_schema.get("server_name")
        if server_name and server_name.strip() != guild.name:
            plan.actions.append(Action(
                type=ActionType.RENAME_GUILD,
                target=server_name.strip(),
                parameters={"name": server_name.strip()},
            ))

        plan.risk_level = assess_plan_risk(plan)
        return plan


builder_planner = BuilderPlanner()

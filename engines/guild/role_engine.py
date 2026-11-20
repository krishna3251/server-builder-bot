"""Role Engine — Role creation, editing, hierarchy enforcement, and Administrator safety guard."""

from __future__ import annotations

import logging
from typing import Any, Sequence

import discord

from core.errors import HierarchyError, PermissionError, ResourceNotFoundError, ValidationError
from engines.guild.permission_engine import DANGEROUS_PERMISSIONS, permission_engine
from services.json_builder import _parse_colour, _resolve_permissions

log = logging.getLogger("engines.guild.role_engine")


class RoleEngine:
    """Manages Discord roles with strict hierarchy verification and dangerous permission safety."""

    async def create_role(self, guild: discord.Guild, params: dict[str, Any]) -> discord.Role:
        """Create a new role after hierarchy and permission safety verification."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_roles:
            raise PermissionError("Bot lacks 'Manage Roles' permission.", required_permission="Manage Roles")

        raw_name = params.get("name")
        if not raw_name or not str(raw_name).strip():
            raise ValidationError("Role name cannot be empty.", field="name")

        name = str(raw_name).strip()[:100]
        color_val = params.get("color") or params.get("colour")
        color = _parse_colour(color_val) if color_val else discord.Colour.default()
        hoist = bool(params.get("hoist", False))
        mentionable = bool(params.get("mentionable", False))

        raw_perms = params.get("permissions", [])
        if isinstance(raw_perms, list):
            # Check for Administrator permission
            if any(str(p).lower() == "administrator" for p in raw_perms):
                explicit_confirmed = params.get("confirm_administrator", False)
                if not explicit_confirmed:
                    raise ValidationError(
                        "Administrator permission requested for role creation. Explicit confirmation required.",
                        field="permissions",
                    )
            perms = _resolve_permissions(raw_perms)
        elif isinstance(raw_perms, discord.Permissions):
            perms = raw_perms
        else:
            perms = discord.Permissions.none()

        return await guild.create_role(
            name=name,
            colour=color,
            hoist=hoist,
            mentionable=mentionable,
            permissions=perms,
            reason=params.get("reason", "Created via DAMU Role Engine"),
        )

    async def edit_role(self, guild: discord.Guild, role_id: int, params: dict[str, Any]) -> discord.Role:
        """Edit an existing role after hierarchy verification."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_roles:
            raise PermissionError("Bot lacks 'Manage Roles' permission.", required_permission="Manage Roles")

        role = guild.get_role(role_id)
        if not role:
            raise ResourceNotFoundError("Role", role_id)

        # Hierarchy check: bot cannot edit roles above or equal to its highest role
        if not permission_engine.bot_can_manage_role(guild, role):
            raise HierarchyError(f"Cannot edit role '{role.name}': Role is higher than or equal to bot's top role.")

        edit_kwargs: dict[str, Any] = {}
        if "name" in params:
            clean_name = str(params["name"]).strip()[:100]
            if clean_name:
                edit_kwargs["name"] = clean_name
        if "color" in params or "colour" in params:
            edit_kwargs["colour"] = _parse_colour(params.get("color") or params.get("colour"))
        if "hoist" in params:
            edit_kwargs["hoist"] = bool(params["hoist"])
        if "mentionable" in params:
            edit_kwargs["mentionable"] = bool(params["mentionable"])
        if "permissions" in params:
            raw_perms = params["permissions"]
            if isinstance(raw_perms, list):
                if any(str(p).lower() == "administrator" for p in raw_perms):
                    if not params.get("confirm_administrator", False):
                        raise ValidationError(
                            "Administrator permission requested for role modification. Explicit confirmation required.",
                            field="permissions",
                        )
                edit_kwargs["permissions"] = _resolve_permissions(raw_perms)
            elif isinstance(raw_perms, discord.Permissions):
                edit_kwargs["permissions"] = raw_perms

        if edit_kwargs:
            await role.edit(**edit_kwargs)
        return role

    async def delete_role(self, guild: discord.Guild, role_id: int, reason: str = "Deleted via DAMU Role Engine") -> bool:
        """Delete role with hierarchy check."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_roles:
            raise PermissionError("Bot lacks 'Manage Roles' permission.", required_permission="Manage Roles")

        role = guild.get_role(role_id)
        if not role:
            raise ResourceNotFoundError("Role", role_id)

        if not permission_engine.bot_can_manage_role(guild, role):
            raise HierarchyError(f"Cannot delete role '{role.name}': Role is higher than or equal to bot's top role.")

        if role.is_default():
            raise ValidationError("Cannot delete the @everyone role.")
        if role.managed:
            raise ValidationError("Cannot delete a bot integration/managed role.")

        await role.delete(reason=reason)
        return True

    def analyze_role_hierarchy(self, guild: discord.Guild) -> dict[str, Any]:
        """Inspect all guild roles and evaluate bot hierarchy and security risks."""
        bot = guild.me
        bot_top = bot.top_role if bot else None

        unmanageable_roles: list[str] = []
        admin_roles: list[str] = []
        dangerous_roles: list[str] = []

        for role in guild.roles:
            if role.is_default():
                continue
            if bot_top and role >= bot_top:
                unmanageable_roles.append(role.name)
            if role.permissions.administrator:
                admin_roles.append(role.name)
            else:
                for p in DANGEROUS_PERMISSIONS:
                    if getattr(role.permissions, p, False):
                        dangerous_roles.append(f"{role.name} ({p})")
                        break

        return {
            "total_roles": len(guild.roles),
            "bot_top_role": bot_top.name if bot_top else "Unknown",
            "unmanageable_roles": unmanageable_roles,
            "admin_roles": admin_roles,
            "dangerous_roles": dangerous_roles,
            "healthy": len(unmanageable_roles) == 0,
        }

"""Role Engine — Role creation, editing, hierarchy enforcement, and Administrator safety guard."""

from __future__ import annotations

import logging
from typing import Any

import discord

from core.errors import HierarchyError, PermissionError, ResourceNotFoundError, ValidationError
from engines.guild.permission_engine import permission_engine
from services.json_builder import _parse_colour, _resolve_permissions

log = logging.getLogger("engines.guild.role_engine")


class RoleEngine:
    """Manages Discord roles with strict hierarchy verification and dangerous permission safety."""

    @staticmethod
    def validate_permissions(bot: discord.Member, raw, *, allow_administrator: bool) -> discord.Permissions:
        permissions = _resolve_permissions(raw) if isinstance(raw, list) else raw
        if not isinstance(permissions, discord.Permissions):
            raise ValidationError("Provide supported role permissions.", field="permissions")
        if permissions.administrator and not allow_administrator:
            raise ValidationError("Administrator grants require explicit confirmation.", field="permissions")
        if not bot.guild_permissions.administrator and permissions.value & ~bot.guild_permissions.value:
            raise PermissionError("The bot cannot grant some of the requested role permissions.",
                                  required_permission="Requested role permissions")
        return permissions

    async def create_role(self, guild: discord.Guild, params: dict[str, Any], *,
                          allow_administrator: bool = False) -> discord.Role:
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

        perms = self.validate_permissions(bot, params.get("permissions", []),
                                          allow_administrator=allow_administrator)

        return await guild.create_role(
            name=name,
            colour=color,
            hoist=hoist,
            mentionable=mentionable,
            permissions=perms,
            reason=params.get("reason", "Created via DAMU Role Engine"),
        )

    async def edit_role(self, guild: discord.Guild, role_id: int, params: dict[str, Any], *,
                        allow_administrator: bool = False) -> discord.Role:
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
            edit_kwargs["permissions"] = self.validate_permissions(
                bot, params["permissions"], allow_administrator=allow_administrator,
            )

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
            if bot_top and not role.is_default() and role.id != bot_top.id and role >= bot_top:
                unmanageable_roles.append(role.name)
            if role.permissions.administrator:
                admin_roles.append("@everyone" if role.is_default() else role.name)
            grants = permission_engine.check_dangerous_permissions(role.permissions)
            if grants:
                name = "@everyone" if role.is_default() else role.name
                dangerous_roles.append(f"{name}: {', '.join(grants)}")

        return {
            "total_roles": len(guild.roles),
            "bot_top_role": bot_top.name if bot_top else "Unknown",
            "unmanageable_roles": unmanageable_roles,
            "admin_roles": admin_roles,
            "dangerous_roles": dangerous_roles,
            "healthy": len(unmanageable_roles) == 0,
        }

"""Permission Engine — Presets, dangerous permission analysis, overwrites, and hierarchy checks."""

from __future__ import annotations

import enum
import logging
from typing import Any, Sequence

import discord

from engines.guild.role_resolver import role_resolver

log = logging.getLogger("engines.guild.permission_engine")


class PermissionPreset(str, enum.Enum):
    PUBLIC = "PUBLIC"
    MEMBERS = "MEMBERS"
    VERIFIED = "VERIFIED"
    STAFF = "STAFF"
    MODERATOR = "MODERATOR"
    ADMIN = "ADMIN"
    OWNER = "OWNER"
    BOT_ONLY = "BOT_ONLY"
    PRIVATE = "PRIVATE"
    TICKET = "TICKET"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    MEDIA = "MEDIA"
    SUPPORT = "SUPPORT"
    CUSTOM = "CUSTOM"


DANGEROUS_PERMISSIONS: set[str] = {
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "mention_everyone",
}


class PermissionEngine:
    """Manages permission presets, safety audits, and channel overwrites."""

    def is_dangerous(self, perm_name: str) -> bool:
        return perm_name.lower().strip() in DANGEROUS_PERMISSIONS

    def check_dangerous_permissions(self, permissions: Sequence[str] | discord.Permissions) -> list[str]:
        """Return list of dangerous permission flags requested or enabled."""
        dangerous_found: list[str] = []
        if isinstance(permissions, discord.Permissions):
            for perm in DANGEROUS_PERMISSIONS:
                if getattr(permissions, perm, False):
                    dangerous_found.append(perm)
        else:
            for p in permissions:
                if self.is_dangerous(str(p)):
                    dangerous_found.append(str(p).lower())
        return dangerous_found

    def can_manage_role(self, actor: discord.Member, target_role: discord.Role) -> bool:
        """Verify role hierarchy allows actor to manage target_role."""
        if actor.guild.owner_id == actor.id:
            return True
        if target_role.is_default():
            return actor.guild_permissions.manage_roles
        return actor.top_role > target_role and actor.guild_permissions.manage_roles

    def bot_can_manage_role(self, guild: discord.Guild, target_role: discord.Role) -> bool:
        """Check if bot's top role is strictly above target_role."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_roles:
            return False
        if target_role.is_default():
            return True
        return bot.top_role > target_role

    def build_overwrites_for_preset(
        self,
        guild: discord.Guild,
        preset: PermissionPreset | str,
        *,
        selected_roles: Sequence[discord.Role] | None = None,
        ticket_opener: discord.Member | discord.User | None = None,
        custom_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] | None = None,
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        """Construct a complete dictionary of PermissionOverwrite mappings for a channel or category."""
        preset_val = PermissionPreset(preset) if isinstance(preset, str) else preset
        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        everyone = guild.default_role
        bot = guild.me

        # Always ensure bot maintains manage_channels and view_channel
        if bot:
            overwrites[bot] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            )

        if preset_val == PermissionPreset.PUBLIC:
            overwrites[everyone] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=True,
            )

        elif preset_val == PermissionPreset.MEMBERS:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            res = role_resolver.resolve(guild, "member", semantic_hint="verified")
            target = res.best_role
            if target:
                overwrites[target] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )

        elif preset_val == PermissionPreset.VERIFIED:
            overwrites[everyone] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )
            res = role_resolver.resolve(guild, "verified")
            verified_role = res.best_role
            if verified_role:
                overwrites[verified_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    add_reactions=True,
                )

        elif preset_val in (PermissionPreset.STAFF, PermissionPreset.MODERATOR):
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            for role_name in ("staff", "moderator", "admin"):
                res = role_resolver.resolve(guild, role_name)
                role = res.best_role
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_messages=True,
                        attach_files=True,
                    )

        elif preset_val == PermissionPreset.ADMIN:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            res = role_resolver.resolve(guild, "admin")
            admin_role = res.best_role
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                    manage_channels=True,
                )

        elif preset_val == PermissionPreset.BOT_ONLY:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            # bot is already permitted above

        elif preset_val == PermissionPreset.PRIVATE:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            if selected_roles:
                for role in selected_roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                    )

        elif preset_val == PermissionPreset.TICKET:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            if ticket_opener:
                overwrites[ticket_opener] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )
            # Resolve support and moderator roles
            for key in ("support", "moderator"):
                res = role_resolver.resolve(guild, key)
                r = res.best_role
                if r:
                    overwrites[r] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_messages=True,
                        attach_files=True,
                    )

        elif preset_val == PermissionPreset.ANNOUNCEMENT:
            overwrites[everyone] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                add_reactions=True,
            )
            for role_name in ("admin", "moderator"):
                res = role_resolver.resolve(guild, role_name)
                role = res.best_role
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        mention_everyone=True,
                    )

        elif preset_val == PermissionPreset.MEDIA:
            overwrites[everyone] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )

        elif preset_val == PermissionPreset.SUPPORT:
            overwrites[everyone] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                create_public_threads=True,
            )
            res = role_resolver.resolve(guild, "support")
            support_role = res.best_role
            if support_role:
                overwrites[support_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                )

        elif preset_val == PermissionPreset.CUSTOM and custom_overwrites:
            overwrites.update(custom_overwrites)

        return overwrites

    def audit_channel_permissions(self, channel: discord.abc.GuildChannel) -> list[str]:
        """Inspect channel overwrites and identify potential conflicts or permission leaks."""
        findings: list[str] = []
        guild = channel.guild
        everyone = guild.default_role
        bot = guild.me

        # 1. Check if bot is locked out
        bot_perms = channel.permissions_for(bot)
        if not bot_perms.view_channel:
            findings.append(f"Bot lacks View Channel in #{channel.name}.")
        if not bot_perms.manage_channels:
            findings.append(f"Bot lacks Manage Channels in #{channel.name}.")

        # 2. Check if announcements allow everyone to speak
        name_lower = channel.name.lower()
        if any(term in name_lower for term in ("announcement", "rules", "info")):
            everyone_perms = channel.permissions_for(everyone)
            if everyone_perms.send_messages:
                findings.append(f"Announcement channel #{channel.name} allows @everyone to send messages.")

        # 3. Check for orphan overrides (roles that no longer exist)
        for target in channel.overwrites:
            if isinstance(target, discord.Role) and target not in guild.roles:
                findings.append(f"#{channel.name} has overwrite for deleted role ID {target.id}.")

        return findings


permission_engine = PermissionEngine()

"""Permission Engine — Presets, dangerous permission analysis, overwrites, and hierarchy checks."""

from __future__ import annotations

import enum
import logging
import re
from typing import Any, Sequence

import discord

from core.errors import ValidationError
from engines.guild.role_resolver import SEMANTIC_ROLE_MAP
from services.json_builder import _resolve_permissions

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
    "manage_messages",
    "manage_threads",
    "manage_events",
    "manage_expressions",
    "moderate_members",
    "view_audit_log",
    "mute_members",
    "deafen_members",
    "move_members",
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
            for perm in sorted(DANGEROUS_PERMISSIONS):
                if getattr(permissions, perm, False):
                    dangerous_found.append(perm)
        else:
            for p in permissions:
                if self.is_dangerous(str(p)):
                    dangerous_found.append(str(p).strip().lower())
        return sorted(set(dangerous_found))

    def can_manage_role(self, actor: discord.Member, target_role: discord.Role) -> bool:
        """Verify role hierarchy allows actor to manage target_role."""
        if target_role.managed or target_role.guild.id != actor.guild.id:
            return False
        if actor.guild.owner_id == actor.id:
            return True
        if target_role.is_default():
            return actor.guild_permissions.manage_roles
        return actor.top_role > target_role and actor.guild_permissions.manage_roles

    def bot_can_manage_role(self, guild: discord.Guild, target_role: discord.Role) -> bool:
        """Check if bot's top role is strictly above target_role."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_roles or target_role.managed:
            return False
        if target_role.is_default():
            return True
        return bot.top_role > target_role

    @staticmethod
    def merge_overwrites(base: dict, updates: dict) -> dict:
        result = dict(base)
        for target, overwrite in updates.items():
            current = discord.PermissionOverwrite.from_pair(
                *result.get(target, discord.PermissionOverwrite()).pair()
            )
            current.update(**{key: value for key, value in overwrite if value is not None})
            result[target] = current
        return result

    @staticmethod
    def schema_overwrites(guild: discord.Guild, specs: list, created_roles: dict) -> dict:
        result = {}
        for spec in specs:
            name = spec["role"]
            if name.casefold() == "@everyone":
                role = guild.default_role
            else:
                role = created_roles.get(name)
                if role is None:
                    matches = [candidate for candidate in guild.roles if candidate.name == name]
                    if len(matches) != 1:
                        raise ValidationError(f"Choose an unambiguous role for the overwrite: {name}.")
                    role = matches[0]
            result[role] = discord.PermissionOverwrite.from_pair(
                _resolve_permissions(spec.get("allow", [])), _resolve_permissions(spec.get("deny", []))
            )
        return result

    @staticmethod
    def _preset_role(guild: discord.Guild, key: str, *, staff: bool = False) -> discord.Role | None:
        """Resolve an unambiguous name and require existing staff authority where appropriate."""
        candidates = [role for role in guild.roles if not role.is_default() and not role.managed
                      and role.name.strip().casefold() in SEMANTIC_ROLE_MAP[key]]
        if staff:
            candidates = [role for role in candidates if role.permissions.administrator or (
                key != "admin" and (role.permissions.manage_messages or role.permissions.moderate_members
                                    or role.permissions.manage_guild)
            )]
        exact = [role for role in candidates if role.name.strip().casefold() == key]
        return exact[0] if len(exact) == 1 else candidates[0] if len(candidates) == 1 else None

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
            target = self._preset_role(guild, "verified")
            if target is None:
                raise ValidationError("Set up one identifiable Verified or Member role before using Members.")
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
            verified_role = self._preset_role(guild, "verified")
            if verified_role is None:
                raise ValidationError("Set up one identifiable Verified or Member role before using Verified.")
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
                role = self._preset_role(guild, role_name, staff=True)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    )
            if len(overwrites) == (2 if bot else 1):
                raise ValidationError("The Staff preset needs an identifiable role with staff permissions.")

        elif preset_val == PermissionPreset.ADMIN:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            admin_role = self._preset_role(guild, "admin", staff=True)
            if admin_role is None:
                raise ValidationError("The Admin preset needs an identifiable Administrator role.")
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )

        elif preset_val == PermissionPreset.BOT_ONLY:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            # bot is already permitted above

        elif preset_val == PermissionPreset.PRIVATE:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            if selected_roles:
                for role in selected_roles:
                    if role.guild.id != guild.id or role.is_default() or role.managed:
                        raise ValidationError("Choose a regular role from this server.")
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
                r = self._preset_role(guild, key, staff=True)
                if r:
                    overwrites[r] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
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
                role = self._preset_role(guild, role_name, staff=True)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
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
            support_role = self._preset_role(guild, "support", staff=True)
            if support_role:
                overwrites[support_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                )

        elif preset_val == PermissionPreset.CUSTOM and custom_overwrites:
            overwrites.update(custom_overwrites)

        if preset_val == PermissionPreset.OWNER:
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
        return overwrites

    @staticmethod
    def public_posting_in_read_only_channel(channel: discord.abc.GuildChannel) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        words = set(re.findall(r"[a-z]+", channel.name.casefold()))
        read_only = channel.is_news() or bool(words & {"rules", "announcement", "announcements"}
                                             and not words & {"chat", "discussion", "feedback"})
        public = channel.permissions_for(channel.guild.default_role)
        return read_only and public.view_channel and public.send_messages

    def audit_channel_permissions(self, channel: discord.abc.GuildChannel) -> list[str]:
        """Inspect channel overwrites and identify potential conflicts or permission leaks."""
        findings: list[str] = []
        guild = channel.guild
        everyone = guild.default_role
        bot = guild.me

        # 1. Check if bot is locked out
        if bot and not channel.permissions_for(bot).view_channel:
            findings.append(f"Bot cannot access #{channel.name}.")

        # 2. Check if announcements allow everyone to speak
        if self.public_posting_in_read_only_channel(channel):
            findings.append(f"#{channel.name} allows @everyone to send messages. Review its read-only policy.")

        # 3. Check explicit elevated grants in channel overwrites.
        for target, overwrite in channel.overwrites.items():
            if target == bot:
                continue
            grants = self.check_dangerous_permissions(overwrite.pair()[0])
            if grants:
                target_name = "@everyone" if target == everyone else getattr(target, "name", str(target.id))
                findings.append(f"#{channel.name} grants {', '.join(grants)} to {target_name}.")

        return findings


permission_engine = PermissionEngine()

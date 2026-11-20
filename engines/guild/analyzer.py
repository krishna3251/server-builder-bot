"""Read-only audit of visible channels, role grants, and configured features."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from engines.guild.permission_engine import permission_engine
from engines.guild.role_engine import RoleEngine
from services.ticket_permissions import ticket_log_is_private

log = logging.getLogger(__name__)


@dataclass
class SuggestedFix:
    issue_id: str
    title: str
    description: str
    target_id: int
    target_type: str
    action_type: str
    parameters: dict[str, Any]


@dataclass
class ServerAnalysisReport:
    guild_id: int
    guild_name: str
    categories_count: int
    channels_count: int
    roles_count: int
    orphan_channels: list[str] = field(default_factory=list)
    hierarchy_issues: list[str] = field(default_factory=list)
    dangerous_roles: list[str] = field(default_factory=list)
    permission_conflicts: list[str] = field(default_factory=list)
    missing_bot_perms: list[str] = field(default_factory=list)
    admin_roles_count: int = 0
    channels_outside_scope: int = 0
    tickets_status: str = "Unknown"
    local_automod_status: str = "Unknown"
    native_automod_status: str = "Unknown"
    suggested_fixes: list[SuggestedFix] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return not (
            self.missing_bot_perms or self.orphan_channels or self.permission_conflicts
            or self.dangerous_roles or self.hierarchy_issues or self.channels_outside_scope
            or self.tickets_status not in {"Configured", "Unconfigured"}
            or "Unknown" in (self.local_automod_status, self.native_automod_status)
        )


class ServerAnalyzer:
    """Run the same audit from the slash command and the manager panel."""

    def __init__(self) -> None:
        self.role_engine = RoleEngine()

    async def analyze(self, guild: discord.Guild, ticket_config: dict[str, Any] | None = None, *,
                      viewer: discord.Member | None = None, client=None) -> ServerAnalysisReport:
        channels = [
            channel for channel in guild.channels
            if viewer is None or viewer.guild_permissions.administrator or channel.permissions_for(viewer).view_channel
        ]
        report = ServerAnalysisReport(
            guild_id=guild.id, guild_name=guild.name, roles_count=len(guild.roles),
            categories_count=sum(isinstance(channel, discord.CategoryChannel) for channel in channels),
            channels_count=len(channels), channels_outside_scope=len(guild.channels) - len(channels),
        )

        bot = guild.me
        if bot:
            for permission in (
                "manage_channels", "manage_roles", "manage_messages", "moderate_members", "view_audit_log",
                "kick_members", "ban_members",
            ):
                if not getattr(bot.guild_permissions, permission):
                    report.missing_bot_perms.append(permission.replace("_", " ").title())
        else:
            report.missing_bot_perms.append("Bot member unavailable")

        hierarchy = self.role_engine.analyze_role_hierarchy(guild)
        report.admin_roles_count = len(hierarchy["admin_roles"])
        report.dangerous_roles = hierarchy["dangerous_roles"]
        report.hierarchy_issues = hierarchy["unmanageable_roles"]

        for channel in channels:
            if not isinstance(channel, discord.CategoryChannel) and channel.category_id is None:
                report.orphan_channels.append(channel.name)
            report.permission_conflicts.extend(permission_engine.audit_channel_permissions(channel))
            if permission_engine.public_posting_in_read_only_channel(channel):
                report.suggested_fixes.append(SuggestedFix(
                    issue_id=f"public_posting_{channel.id}",
                    title=f"Restrict default posting in #{channel.name}",
                    description="Set Send Messages to Deny for @everyone. Other overwrite fields stay as configured.",
                    target_id=channel.id, target_type="channel", action_type="set_permissions",
                    parameters={"channel_id": channel.id, "target_name": "@everyone",
                                "overwrites": {"send_messages": False}},
                ))

        report.tickets_status, report.local_automod_status, report.native_automod_status = await asyncio.gather(
            self._ticket_status(guild, ticket_config, client),
            self._local_status(guild, client),
            self._native_status(guild),
        )
        return report

    @staticmethod
    async def _ticket_status(guild, configured, client):
        cog = client.get_cog("TicketSystem") if client else None
        try:
            if configured is None and cog is not None:
                configured = await asyncio.wait_for(cog.config_manager.get_guild(guild.id), timeout=3)
            if configured is None:
                return "Unknown"
            if not configured:
                return "Unconfigured"
            names = ("support_role_id", "mod_role_id")
            role_ids = {configured.get(key, 0) for key in names}
            log_channel = guild.get_channel(configured.get("log_channel_id", 0))
            if (not all(guild.get_role(role_id) is not None for role_id in role_ids)
                    or not isinstance(guild.get_channel(configured.get("ticket_category_id", 0)), discord.CategoryChannel)
                    or not isinstance(log_channel, discord.TextChannel)):
                return "Missing saved role or channel"
            if not ticket_log_is_private(log_channel, role_ids):
                return "Log channel needs private staff access"
            return "Configured"
        except Exception as error:
            log.warning("Ticket status unavailable for guild %s (%s)", guild.id, type(error).__name__)
            return "Unknown"

    @staticmethod
    async def _local_status(guild, client):
        cog = client.get_cog("Local AutoMod") if client else None
        if cog is None:
            return "Unknown"
        try:
            return "Enabled" if await asyncio.wait_for(cog.is_enabled(guild.id), timeout=3) else "Disabled"
        except (OSError, ValueError, asyncio.TimeoutError):
            return "Unknown"

    @staticmethod
    async def _native_status(guild):
        if guild.me is None or not guild.me.guild_permissions.manage_guild:
            return "Unknown"
        try:
            rules = await asyncio.wait_for(guild.fetch_automod_rules(), timeout=3)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            return "Unknown"
        return "Enabled" if any(rule.enabled for rule in rules) else "Disabled"


server_analyzer = ServerAnalyzer()

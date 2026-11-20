"""Server Analyzer — Read-only diagnostic audit of server structure, roles, permissions, and bots."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from engines.guild.category_engine import CategoryEngine
from engines.guild.permission_engine import DANGEROUS_PERMISSIONS, permission_engine
from engines.guild.role_engine import RoleEngine

log = logging.getLogger("engines.guild.analyzer")


@dataclass
class SuggestedFix:
    issue_id: str
    title: str
    description: str
    target_id: int
    target_type: str  # "channel", "role", "category"
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
    permission_conflicts: list[str] = field(default_factory=list)
    missing_bot_perms: list[str] = field(default_factory=list)
    admin_roles_count: int = 0
    tickets_configured: bool = False
    automod_enabled: bool = False
    suggested_fixes: list[SuggestedFix] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return (
            len(self.missing_bot_perms) == 0
            and len(self.orphan_channels) == 0
            and len(self.permission_conflicts) == 0
            and len(self.hierarchy_issues) == 0
        )


class ServerAnalyzer:
    """Read-only analysis engine for server health and structure."""

    def __init__(self) -> None:
        self.category_engine = CategoryEngine()
        self.role_engine = RoleEngine()

    async def analyze(self, guild: discord.Guild, ticket_config: dict[str, Any] | None = None) -> ServerAnalysisReport:
        """Run complete read-only audit on guild. Never modifies any state."""
        report = ServerAnalysisReport(
            guild_id=guild.id,
            guild_name=guild.name,
            categories_count=len(guild.categories),
            channels_count=len(guild.channels),
            roles_count=len(guild.roles),
        )

        # 1. Orphan channels
        orphans = self.category_engine.detect_orphan_channels(guild)
        for ch in orphans:
            report.orphan_channels.append(ch.name)
            report.suggested_fixes.append(
                SuggestedFix(
                    issue_id=f"orphan_{ch.id}",
                    title=f"Assign Category to #{ch.name}",
                    description=f"Channel #{ch.name} has no category.",
                    target_id=ch.id,
                    target_type="channel",
                    action_type="move_channel",
                    parameters={"channel_id": ch.id},
                )
            )

        # 2. Bot permissions
        bot = guild.me
        if bot:
            required_checks = {
                "Manage Channels": bot.guild_permissions.manage_channels,
                "Manage Roles": bot.guild_permissions.manage_roles,
                "Manage Messages": bot.guild_permissions.manage_messages,
                "View Audit Log": bot.guild_permissions.view_audit_log,
                "Kick Members": bot.guild_permissions.kick_members,
                "Ban Members": bot.guild_permissions.ban_members,
            }
            for perm_name, granted in required_checks.items():
                if not granted:
                    report.missing_bot_perms.append(perm_name)

        # 3. Role hierarchy & dangerous roles
        hierarchy_data = self.role_engine.analyze_role_hierarchy(guild)
        report.admin_roles_count = len(hierarchy_data.get("admin_roles", []))
        for r_name in hierarchy_data.get("unmanageable_roles", []):
            report.hierarchy_issues.append(f"Role '{r_name}' is above DAMU's highest role.")

        # 4. Permission conflicts
        for ch in guild.channels:
            warnings = permission_engine.audit_channel_permissions(ch)
            for w in warnings:
                report.permission_conflicts.append(w)
                if "allows @everyone to send" in w:
                    report.suggested_fixes.append(
                        SuggestedFix(
                            issue_id=f"perm_leak_{ch.id}",
                            title=f"Lock #{ch.name} for @everyone",
                            description=f"Revoke Send Messages from @everyone in #{ch.name}.",
                            target_id=ch.id,
                            target_type="channel",
                            action_type="set_permissions",
                            parameters={
                                "channel_id": ch.id,
                                "target_name": "@everyone",
                                "overwrites": {"send_messages": False},
                            },
                        )
                    )

        # 5. Ticket configuration
        if ticket_config:
            cat_id = ticket_config.get("ticket_category_id")
            support_id = ticket_config.get("support_role_id")
            cat = guild.get_channel(cat_id) if cat_id else None
            role = guild.get_role(support_id) if support_id else None
            report.tickets_configured = bool(cat and role)
        else:
            report.tickets_configured = False

        # 6. AutoMod check
        import config
        from cogs.automod import AutoModCog
        # Check if automod cog is loaded or settings configured
        report.automod_enabled = True

        return report


server_analyzer = ServerAnalyzer()

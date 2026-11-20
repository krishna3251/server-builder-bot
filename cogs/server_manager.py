"""Server Manager Cog — Interactive Discord UI control panel for server management, builder wizard, and analyzer."""

from __future__ import annotations

import io
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.engine import engine
from core.errors import DamuError, ValidationError
from core.interaction import safe_defer, safe_edit, safe_modal, safe_send
from core.models import Action, ActionType, BuildPlan, RiskLevel
from core.result import format_preview_embed, format_result_embed
from core.risk import assess_plan_risk
from engines.builder.planner import builder_planner
from engines.guild.analyzer import ServerAnalysisReport, SuggestedFix, server_analyzer
from engines.guild.category_engine import CategoryEngine
from engines.guild.channel_engine import ChannelEngine
from engines.guild.permission_engine import PermissionPreset, permission_engine
from engines.guild.role_engine import RoleEngine
from engines.health.health_engine import health_engine
from engines.intelligence.recommendation import recommendation_engine
from engines.intelligence.planner import intelligence_planner
from cogs.ai import _rate_limited
from services.error_service import report_error
from services.json_builder import build_session
from utils.interactions import acknowledged

log = logging.getLogger("cogs.server_manager")


class ManagerView(discord.ui.View):
    """Recheck identity and current permissions on every nested manager view."""

    permission = "administrator"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.user.id == self.user_id and interaction.guild_id == self.guild.id
                and getattr(interaction.user.guild_permissions, self.permission, False)):
            return True
        await safe_send(interaction, "The requester must still have permission to use these controls.", ephemeral=True)
        return False


# ── 1. Main Navigation View ───────────────────────────────────────────────────
class MainServerManagerView(ManagerView):
    """Primary navigation hub for DAMU Server Manager."""

    def __init__(self, guild: discord.Guild, user_id: int, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.guild = guild
        self.user_id = user_id

    @discord.ui.button(label="Builder", style=discord.ButtonStyle.primary, emoji="🏗️", row=0)
    async def btn_builder(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = ServerBuilderWizardView(self.guild, self.user_id)
        embed = view.get_step_embed()
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Channels", style=discord.ButtonStyle.secondary, emoji="💬", row=0)
    async def btn_channels(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = ChannelManagerView(self.guild, self.user_id)
        embed = view.get_embed()
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Categories", style=discord.ButtonStyle.secondary, emoji="📁", row=0)
    async def btn_categories(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = CategoryManagerView(self.guild, self.user_id)
        embed = view.get_embed()
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Roles", style=discord.ButtonStyle.secondary, emoji="👥", row=1)
    async def btn_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = RoleManagerView(self.guild, self.user_id)
        embed = view.get_embed()
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Permissions", style=discord.ButtonStyle.secondary, emoji="🔐", row=1)
    async def btn_permissions(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = PermissionManagerView(self.guild, self.user_id)
        embed = view.get_embed()
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Tickets", style=discord.ButtonStyle.secondary, emoji="🎫", row=1)
    async def btn_tickets(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        embed = discord.Embed(
            title="🎫 TICKET SYSTEM CONFIGURATION",
            description="Use `/setup_tickets` to configure panels, roles, and categories.",
            colour=0x5865F2,
        )
        view = BackToMainView(self.guild, self.user_id)
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Moderation", style=discord.ButtonStyle.secondary, emoji="🛡️", row=2)
    async def btn_moderation(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        embed = discord.Embed(
            title="🛡️ MODERATION & AUTOMOD",
            description="Moderation commands available:\n• `/kick`, `/ban`, `/mute`, `/warn`\n• `/automod_status` for the bot filter · `/automod exception` for Discord rules.",
            colour=0x5865F2,
        )
        view = BackToMainView(self.guild, self.user_id)
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Analyze", style=discord.ButtonStyle.success, emoji="🔎", row=2)
    async def btn_analyze(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        report = await server_analyzer.analyze(self.guild, viewer=interaction.user, client=interaction.client)
        view = ServerAnalyzerView(self.guild, self.user_id, report, full_access=True)
        embed = view.get_embed()
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, emoji="⚙️", row=2)
    async def btn_status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        status = await health_engine.get_status(interaction.client)  # type: ignore[arg-type]
        embed = status.format_embed()
        view = BackToMainView(self.guild, self.user_id)
        await safe_edit(interaction, embed=embed, view=view)


class BackToMainView(ManagerView):
    """Reusable back button to return to the main server manager."""

    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id

    @discord.ui.button(label="Back to Menu", style=discord.ButtonStyle.secondary, emoji="◀")
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        embed = build_main_embed(self.guild)
        await safe_edit(interaction, embed=embed, view=view)


def build_main_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🤖 DAMU SERVER MANAGER",
        description=f"Direct interactive control panel for **{guild.name}**.",
        colour=config.BOT_COLOR,
    )
    embed.add_field(name="Server", value=guild.name, inline=True)
    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Categories", value=str(len(guild.categories)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    embed.set_footer(text="Select a module below to inspect or modify server resources.")
    return embed


# ── 2. Channel Manager View ───────────────────────────────────────────────────
class ChannelManagerView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💬 CHANNEL MANAGER",
            description="Create or delete channels after reviewing a preview.",
            colour=0x5865F2,
        )
        embed.add_field(name="Total Channels", value=str(len(self.guild.channels)), inline=True)
        embed.add_field(name="Text", value=str(len(self.guild.text_channels)), inline=True)
        embed.add_field(name="Voice", value=str(len(self.guild.voice_channels)), inline=True)
        return embed

    @discord.ui.button(label="Create Channel", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = CreateChannelModal(self.guild, self.user_id)
        await safe_modal(interaction, modal)

    @discord.ui.button(label="Delete Channel", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = SelectChannelToDeleteView(self.guild, self.user_id)
        embed = discord.Embed(title="🗑️ Delete Channel", description="Select a channel to delete.", colour=0xE74C3C)
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        await safe_edit(interaction, embed=build_main_embed(self.guild), view=view)


class CreateChannelModal(discord.ui.Modal, title="Create New Channel"):
    channel_name = discord.ui.TextInput(label="Channel Name", placeholder="e.g. support-help", max_length=100, required=True)
    channel_type = discord.ui.TextInput(label="Type (text / voice / forum / stage)", placeholder="text", default="text", max_length=10, required=True)
    channel_topic = discord.ui.TextInput(label="Topic", placeholder="Channel topic or purpose", style=discord.TextStyle.paragraph, required=False, max_length=500)
    slowmode = discord.ui.TextInput(label="Slowmode (seconds)", placeholder="0", default="0", max_length=5, required=False)

    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__()
        self.guild = guild
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (interaction.user.id != self.user_id or interaction.guild_id != self.guild.id
                or not getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False)):
            await safe_send(interaction, "The requesting administrator must submit this form.", ephemeral=True)
            return
        if not await safe_defer(interaction, ephemeral=True):
            return
        ch_type = self.channel_type.value.strip().lower()
        if ch_type not in ("text", "voice", "forum", "stage") or not self.channel_name.value.strip():
            return await safe_send(interaction, "Enter a channel name and choose text, voice, forum, or stage.", ephemeral=True)
        topic = self.channel_topic.value.strip()
        try:
            sm = int(self.slowmode.value.strip() or "0")
            if not 0 <= sm <= 21600:
                raise ValueError
        except ValueError:
            return await safe_send(interaction, "Enter a slowmode between 0 and 21,600 seconds.", ephemeral=True)
        if ch_type in ("voice", "stage") and (topic or sm):
            return await safe_send(interaction, "Channel topics and slowmode in this form apply to text and forum channels.", ephemeral=True)

        channel_params = {
            "name": self.channel_name.value.strip().replace(" ", "-").lower(),
            "type": ch_type,
            "topic": topic,
            "slowmode": sm,
        }

        view = SelectCategoryAndPresetView(self.guild, self.user_id, channel_params)
        embed = discord.Embed(
            title="⚙️ Configure Channel Placement & Preset",
            description=f"Creating **#{channel_params['name']}** ({ch_type}).\nChoose a category and permission preset.",
            colour=0x5865F2,
        )
        await safe_edit(interaction, embed=embed, view=view)


class SelectCategoryAndPresetView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int, channel_params: dict[str, Any]) -> None:
        super().__init__(timeout=180.0)
        self.guild = guild
        self.user_id = user_id
        self.params = channel_params
        self.selected_category_id: int | None = None
        self.selected_preset = "inherit"

        # Category Select
        cat_options = [discord.SelectOption(label="No Category", value="none", default=True)]
        for cat in guild.categories[:24]:
            cat_options.append(discord.SelectOption(label=cat.name[:100], value=str(cat.id)))

        cat_select = discord.ui.Select(placeholder="Select Category...", options=cat_options, row=0)

        async def cat_callback(inter: discord.Interaction):
            await safe_defer(inter, ephemeral=True)
            val = cat_select.values[0]
            self.selected_category_id = int(val) if val != "none" else None

        cat_select.callback = cat_callback
        self.add_item(cat_select)

        # Preset Select
        preset_options = [
            discord.SelectOption(label="Inherit permissions", value="inherit", default=True),
            discord.SelectOption(label="Public", value=PermissionPreset.PUBLIC.value, description="Everyone can view & chat"),
            discord.SelectOption(label="Members", value=PermissionPreset.MEMBERS.value, description="Members only"),
            discord.SelectOption(label="Verified", value=PermissionPreset.VERIFIED.value, description="Verified members only"),
            discord.SelectOption(label="Staff Only", value=PermissionPreset.STAFF.value, description="Staff / Mod / Admin only"),
            discord.SelectOption(label="Announcement", value=PermissionPreset.ANNOUNCEMENT.value, description="Read-only news channel"),
            discord.SelectOption(label="Private", value=PermissionPreset.PRIVATE.value, description="Hidden from @everyone"),
        ]
        preset_select = discord.ui.Select(placeholder="Select Permission Preset...", options=preset_options, row=1)

        async def preset_callback(inter: discord.Interaction):
            await safe_defer(inter, ephemeral=True)
            self.selected_preset = preset_select.values[0]

        preset_select.callback = preset_callback
        self.add_item(preset_select)

    @discord.ui.button(label="Proceed to Preview", style=discord.ButtonStyle.primary, emoji="🔍", row=2)
    async def proceed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        action = Action(
            type=ActionType.CREATE_CHANNEL,
            target=self.params["name"],
            parameters={**self.params, "category_id": self.selected_category_id,
                        "permission_preset": self.selected_preset},
        )
        plan = BuildPlan(self.guild.id, self.user_id, "Create Channel", actions=[action])
        plan.risk_level = assess_plan_risk(plan)
        preview_embed = format_preview_embed(plan)
        category = self.guild.get_channel(self.selected_category_id) if self.selected_category_id else None
        placement = category.name if category else "Server root"
        preview_embed.add_field(name="Category", value=placement)
        preview_embed.add_field(name="Permissions", value=self.selected_preset.replace("_", " ").title())
        view = ChangePreviewView(self.guild, self.user_id, plan)
        await safe_edit(interaction, embed=preview_embed, view=view)

class SelectChannelToDeleteView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=180.0)
        self.guild = guild
        self.user_id = user_id

        select = discord.ui.ChannelSelect(
            placeholder="Select a channel to delete…",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice,
                           discord.ChannelType.stage_voice, discord.ChannelType.forum, discord.ChannelType.media],
        )

        async def callback(inter: discord.Interaction):
            if not await safe_defer(inter, ephemeral=True):
                return
            ch = self.guild.get_channel(select.values[0].id)
            if ch is None:
                return await safe_send(inter, "The selected channel is unavailable.", ephemeral=True)

            plan = BuildPlan(
                guild_id=self.guild.id,
                requester_id=self.user_id,
                operation=f"Delete Channel #{ch.name}",
                risk_level=RiskLevel.HIGH,
                requires_confirmation=True,
                warnings=[f"Deleting #{ch.name} is permanent and will delete all messages."],
                actions=[
                    Action(
                        type=ActionType.DELETE_CHANNEL,
                        target=ch.name,
                        parameters={"channel_id": ch.id},
                    )
                ],
            )
            embed = format_preview_embed(plan)
            view = ChangePreviewView(self.guild, self.user_id, plan)
            await safe_edit(inter, embed=embed, view=view)

        select.callback = callback
        self.add_item(select)


# ── 3. Category Manager View ──────────────────────────────────────────────────
class CategoryManagerView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id
        self.category_engine = CategoryEngine()

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📁 CATEGORY MANAGER",
            description="Manage categories, permissions, and detect orphan channels.",
            colour=0x5865F2,
        )
        orphans = self.category_engine.detect_orphan_channels(self.guild)
        embed.add_field(name="Total Categories", value=str(len(self.guild.categories)), inline=True)
        embed.add_field(name="Orphan Channels", value=str(len(orphans)), inline=True)
        return embed

    @discord.ui.button(label="Create Category", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_cat(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = CreateCategoryModal(self.guild, self.user_id)
        await safe_modal(interaction, modal)

    @discord.ui.button(label="Inspect Orphans", style=discord.ButtonStyle.secondary, emoji="🔍", row=0)
    async def orphans(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        orphans = self.category_engine.detect_orphan_channels(self.guild)
        if orphans:
            desc = "Channels without a category:\n" + "\n".join(f"• #{ch.name}" for ch in orphans[:20])
        else:
            desc = "✅ No orphan channels found! All channels belong to categories."
        embed = discord.Embed(title="🔍 Orphan Channels", description=desc, colour=0x2ECC71 if not orphans else 0xF1C40F)
        view = BackToMainView(self.guild, self.user_id)
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        await safe_edit(interaction, embed=build_main_embed(self.guild), view=view)


class CreateCategoryModal(discord.ui.Modal, title="Create Category"):
    cat_name = discord.ui.TextInput(label="Category Name", placeholder="e.g. 🛠️ SUPPORT", max_length=100, required=True)

    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__()
        self.guild = guild
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (interaction.user.id != self.user_id or interaction.guild_id != self.guild.id
                or not getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False)):
            return await safe_send(interaction, "The requesting administrator must submit this form.", ephemeral=True)
        if not await safe_defer(interaction, ephemeral=True):
            return
        if not self.cat_name.value.strip():
            return await safe_send(interaction, "Enter a category name.", ephemeral=True)
        plan = BuildPlan(
            guild_id=self.guild.id,
            requester_id=self.user_id,
            operation="Create Category",
            risk_level=RiskLevel.LOW,
            actions=[
                Action(
                    type=ActionType.CREATE_CATEGORY,
                    target=self.cat_name.value.strip(),
                    parameters={"name": self.cat_name.value.strip()},
                )
            ],
        )
        embed = format_preview_embed(plan)
        view = ChangePreviewView(self.guild, self.user_id, plan)
        await safe_edit(interaction, embed=embed, view=view)


# ── 4. Role Manager View ──────────────────────────────────────────────────────
class RoleManagerView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id
        self.role_engine = RoleEngine()

    def get_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="👥 ROLE MANAGER",
            description="Manage server roles, inspect role hierarchy, and check permissions.",
            colour=0x5865F2,
        )
        hierarchy = self.role_engine.analyze_role_hierarchy(self.guild)
        embed.add_field(name="Total Roles", value=str(len(self.guild.roles)), inline=True)
        embed.add_field(name="Bot Top Role", value=hierarchy["bot_top_role"], inline=True)
        embed.add_field(name="Hierarchy Status", value="🟢 Clean" if hierarchy["healthy"] else "⚠️ Unmanageable Roles Present", inline=True)
        return embed

    @discord.ui.button(label="Hierarchy Audit", style=discord.ButtonStyle.primary, emoji="🛡️", row=0)
    async def audit_hierarchy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        hierarchy = self.role_engine.analyze_role_hierarchy(self.guild)
        embed = discord.Embed(title="🛡️ Role Hierarchy Audit", colour=0x5865F2)
        embed.add_field(name="Bot Top Role", value=hierarchy["bot_top_role"], inline=True)
        unmanageable = hierarchy["unmanageable_roles"]
        embed.add_field(
            name="Roles Above Bot (Unmanageable)",
            value="\n".join(f"• {r}" for r in unmanageable[:10]) if unmanageable else "None (All roles manageable)",
            inline=False,
        )
        admin_roles = hierarchy["admin_roles"]
        embed.add_field(
            name="Administrator Roles",
            value="\n".join(f"• {r}" for r in admin_roles[:10]) if admin_roles else "None",
            inline=False,
        )
        view = BackToMainView(self.guild, self.user_id)
        await safe_edit(interaction, embed=embed, view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        await safe_edit(interaction, embed=build_main_embed(self.guild), view=view)


# ── 5. Permission Manager View ────────────────────────────────────────────────
class PermissionManagerView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id

    def get_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Permission Manager",
            description="Select a channel or category to inspect its access rules. "
                        "Use Channels → Create Channel to choose a preset for a new channel.",
            colour=0x5865F2,
        )

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Inspect a channel or category…", row=0,
                       channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice,
                                      discord.ChannelType.stage_voice, discord.ChannelType.forum,
                                      discord.ChannelType.media, discord.ChannelType.category])
    async def inspect_permissions(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        channel = self.guild.get_channel(select.values[0].id)
        if channel is None:
            return await safe_send(interaction, "The selected channel is unavailable.", ephemeral=True)
        embed = self.get_embed()
        embed.title = f"Permissions for {channel.name}"
        public = channel.permissions_for(self.guild.default_role)
        embed.add_field(name="@everyone", value=(
            f"View Channel {'allowed' if public.view_channel else 'denied'}\n"
            f"Send Messages {'allowed' if public.send_messages else 'denied'}"
        ))
        embed.add_field(name="Channel overrides", value=str(len(channel.overwrites)))
        if channel.category_id is not None:
            embed.add_field(name="Category permissions", value="Synced" if channel.permissions_synced else "Customized")
        findings = permission_engine.audit_channel_permissions(channel)
        embed.add_field(name="Findings", value="\n".join(findings)[:1024] or "None of the scan checks flagged this channel.", inline=False)
        await safe_edit(interaction, embed=embed, view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        await safe_edit(interaction, embed=build_main_embed(self.guild), view=view)


# ── 6. Change Preview View ────────────────────────────────────────────────────
class ChangePreviewView(ManagerView):
    """Universal change preview and confirmation screen before applying changes."""

    def __init__(self, guild: discord.Guild, user_id: int, plan: BuildPlan, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.guild = guild
        self.user_id = user_id
        self.plan = plan
        self.plan.risk_level = assess_plan_risk(self.plan)
        self.busy = False
        if self.plan.risk_level == RiskLevel.CRITICAL:
            self.apply.label = "Confirm Administrator Grants"
            self.apply.style = discord.ButtonStyle.danger

    @discord.ui.button(label="Apply Changes", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction):
            return
        if self.busy or self.is_finished():
            return await safe_send(interaction, "This change is already being applied or has finished.", ephemeral=True)
        self.busy = True
        try:
            if not await safe_defer(interaction, ephemeral=True):
                return
            ctx = await engine.create_context(self.guild, interaction.user, interaction)
            result = await engine.execute(self.plan, ctx, confirmed=True)
            embed = format_result_embed(result)
            view = BackToMainView(self.guild, self.user_id)
            await safe_edit(interaction, embed=embed, view=view)
            self.stop()
        finally:
            self.busy = False

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌", row=0)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.busy or self.is_finished():
            return await safe_send(interaction, "This change is already being applied or has finished.", ephemeral=True)
        self.stop()
        await safe_defer(interaction, ephemeral=True)
        embed = discord.Embed(
            title="🚫 Action Cancelled",
            description=f"Transaction `{self.plan.build_id}` was cancelled. No changes were made.",
            colour=0x95A5A6,
        )
        view = BackToMainView(self.guild, self.user_id)
        await safe_edit(interaction, embed=embed, view=view)


# ── 7. Server Builder Wizard View ─────────────────────────────────────────────
class ServerBuilderWizardView(ManagerView):
    """Interactive multi-step server building wizard."""

    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id
        self.selected_template: str = "community"
        self.selected_modules: list[str] = ["roles", "categories", "channels", "permissions"]

    def get_step_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🏗️ DAMU SERVER BUILDER WIZARD",
            description="**Step 1:** Choose a server template below, then generate a preview.",
            colour=0x5865F2,
        )
        embed.add_field(name="Selected Template", value=f"**{self.selected_template.title()}**", inline=True)
        embed.add_field(name="Plan includes", value=", ".join(self.selected_modules), inline=False)
        return embed

    @discord.ui.button(label="Gaming", style=discord.ButtonStyle.primary, emoji="🎮", row=0)
    async def tmpl_gaming(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        self.selected_template = "gaming"
        await safe_edit(interaction, embed=self.get_step_embed(), view=self)

    @discord.ui.button(label="Community", style=discord.ButtonStyle.primary, emoji="👥", row=0)
    async def tmpl_community(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        self.selected_template = "community"
        await safe_edit(interaction, embed=self.get_step_embed(), view=self)

    @discord.ui.button(label="Study", style=discord.ButtonStyle.primary, emoji="📚", row=0)
    async def tmpl_study(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        self.selected_template = "study"
        await safe_edit(interaction, embed=self.get_step_embed(), view=self)

    @discord.ui.button(label="Generate Plan", style=discord.ButtonStyle.success, emoji="⚡", row=1)
    async def generate_plan(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        from cogs.server_builder import TEMPLATES
        schema = TEMPLATES.get(self.selected_template, TEMPLATES["community"])
        try:
            plan = builder_planner.create_plan_from_schema(
                self.guild, interaction.user, schema,
                operation_name=f"Template: {self.selected_template.title()}",
                selected_modules=self.selected_modules,
            )
        except ValidationError as error:
            return await safe_send(interaction, str(error), ephemeral=True)
        preview_embed = format_preview_embed(plan)
        view = ChangePreviewView(self.guild, self.user_id, plan)
        await safe_edit(interaction, embed=preview_embed, view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        await safe_edit(interaction, embed=build_main_embed(self.guild), view=view)

    @discord.ui.button(label="Plan from prompt", style=discord.ButtonStyle.secondary, row=1)
    async def describe_plan(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if (interaction.user.id != self.user_id or interaction.guild_id != self.guild.id
                or not getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False)):
            return await safe_send(interaction, "The requesting administrator must open this form.", ephemeral=True)
        await safe_modal(interaction, ManagerPlanModal(self.guild, self.user_id))


class ManagerPlanModal(discord.ui.Modal, title="Describe a server layout"):
    prompt = discord.ui.TextInput(label="What should the server include?", style=discord.TextStyle.paragraph,
                                  max_length=800)

    def __init__(self, guild: discord.Guild, user_id: int) -> None:
        super().__init__()
        self.guild = guild
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (interaction.user.id != self.user_id or interaction.guild_id != self.guild.id
                or not getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False)):
            return await safe_send(interaction, "The requesting administrator must submit this form.", ephemeral=True)
        if not await safe_defer(interaction, ephemeral=True, thinking=True):
            return
        if _rate_limited(interaction.user.id):
            return await safe_send(interaction, "Please wait before generating another plan.", ephemeral=True)
        try:
            schema, tier, warnings = await intelligence_planner.plan_request(
                self.guild, interaction.user, str(self.prompt.value),
            )
            plan = builder_planner.create_plan_from_schema(self.guild, interaction.user, schema,
                                                           operation_name="Server layout")
            plan.warnings.extend(warnings)
            embed = format_preview_embed(plan)
            embed.add_field(name="Plan source", value=tier.replace("_", " ").title())
            await safe_send(interaction, embed=embed, view=ChangePreviewView(self.guild, self.user_id, plan),
                            ephemeral=True)
        except ValidationError as error:
            await safe_send(interaction, str(error), ephemeral=True)
        except Exception as error:
            reference = report_error(error, context="Server manager prompt", guild_id=self.guild.id)
            await safe_send(interaction, f"Could not prepare a plan. Reference: `{reference}`.", ephemeral=True)


# ── 8. Server Analyzer & Smart Fixes View ─────────────────────────────────────
class ServerAnalyzerView(ManagerView):
    def __init__(self, guild: discord.Guild, user_id: int, report: ServerAnalysisReport, *, full_access: bool = False):
        super().__init__(timeout=300.0)
        self.guild = guild
        self.user_id = user_id
        self.report = report
        self.permission = "administrator" if full_access else "manage_guild"
        if not full_access:
            self.remove_item(self.back)
            self.remove_item(self.suggested_fixes)

    def get_embed(self) -> discord.Embed:
        report = self.report
        status = "No findings in the checked scope" if report.is_healthy else "Review findings and scan coverage"
        embed = discord.Embed(
            title="Server permission scan",
            description=f"**{status}**\n{report.channels_count} channels and categories checked"
                        f" · {report.channels_outside_scope} outside your channel access",
            colour=0x2ECC71 if report.is_healthy else 0xF1C40F,
        )
        embed.add_field(
            name="Roles",
            value=(f"{report.roles_count} checked · {report.admin_roles_count} with Administrator\n"
                   f"{len(report.dangerous_roles)} with elevated permissions\n"
                   f"{len(report.hierarchy_issues)} at or above the bot's role"),
            inline=True,
        )
        embed.add_field(name="Channel findings", value=(
            f"{len(report.permission_conflicts)} permission findings\n"
            f"{len(report.orphan_channels)} uncategorized channels"
        ), inline=True)
        embed.add_field(name="Bot permission gaps",
                        value=", ".join(report.missing_bot_perms) or "None in the server-level checklist",
                        inline=False)
        embed.add_field(name="Feature configuration", value=(
            f"Tickets: **{report.tickets_status}**\n"
            f"Bot-run AutoMod: **{report.local_automod_status}**\n"
            f"Discord AutoMod: **{report.native_automod_status}**"
        ), inline=False)
        return embed

    @discord.ui.button(label="Download findings", style=discord.ButtonStyle.secondary, row=0)
    async def findings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        report = self.report
        lines = [f"Server permission scan for {report.guild_name}",
                 f"Channels outside scope: {report.channels_outside_scope}",
                 f"Tickets: {report.tickets_status}",
                 f"Bot-run AutoMod: {report.local_automod_status}",
                 f"Discord AutoMod: {report.native_automod_status}"]
        for title, entries in (
            ("Roles with elevated permissions", report.dangerous_roles),
            ("Roles at or above the bot's role", report.hierarchy_issues),
            ("Channel permission findings", report.permission_conflicts),
            ("Bot permission gaps", report.missing_bot_perms),
            ("Uncategorized channels", report.orphan_channels),
        ):
            lines.extend(["", title, *(f"- {entry}" for entry in (entries or ["None"]))])
        await safe_send(interaction, file=discord.File(io.BytesIO("\n".join(lines).encode()),
                        filename="server-permission-scan.txt"), ephemeral=True)

    @discord.ui.button(label="Suggested repairs", style=discord.ButtonStyle.primary, emoji="🔧", row=0)
    async def suggested_fixes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.user.guild_permissions.administrator:
            return await safe_send(interaction, "Administrator permission is required to open repairs.", ephemeral=True)
        if not self.report.suggested_fixes:
            return await safe_send(interaction, "This scan has no automatic repairs. Download the findings for items to review.", ephemeral=True)
        if not await safe_defer(interaction, ephemeral=True):
            return
        view = SmartFixView(self.guild, self.user_id, self.report.suggested_fixes)
        await safe_edit(interaction, embed=view.get_current_embed(), view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀", row=0)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(self.guild, self.user_id)
        await safe_edit(interaction, embed=build_main_embed(self.guild), view=view)


class SmartFixView(ManagerView):
    """Presents suggested repairs one by one, requiring explicit confirmation."""

    def __init__(self, guild: discord.Guild, user_id: int, fixes: list[SuggestedFix], current_index: int = 0) -> None:
        super().__init__(timeout=180.0)
        self.guild = guild
        self.user_id = user_id
        self.fixes = fixes
        self.index = current_index
        self.busy = False

    def get_current_embed(self) -> discord.Embed:
        fix = self.fixes[self.index]
        return recommendation_engine.format_fix_embed(fix, self.index + 1, len(self.fixes))

    @discord.ui.button(label="Apply Fix", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def apply_fix(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction):
            return
        if self.busy or self.is_finished():
            return await safe_send(interaction, "This repair is already running or finished.", ephemeral=True)
        self.busy = True
        if not await safe_defer(interaction, ephemeral=True):
            self.busy = False
            return
        fix = self.fixes[self.index]

        # Execute fix via appropriate engine
        try:
            channel_engine = ChannelEngine()
            async with build_session(self.guild.id):
                channel = self.guild.get_channel(fix.target_id)
                if fix.action_type != "set_permissions":
                    raise ValueError("Unsupported automatic repair")
                if channel is None or not permission_engine.public_posting_in_read_only_channel(channel):
                    msg = "This finding has changed. Run /server_analyze again to refresh it."
                else:
                    await channel_engine.update_permissions(self.guild, fix.parameters)
                    msg = f"✅ Applied fix: **{fix.title}**"
        except DamuError as exc:
            msg = str(exc)
        except Exception as exc:
            reference = report_error(exc, context="Server manager repair", guild_id=self.guild.id)
            msg = f"❌ Could not apply the repair. Reference: `{reference}`."

        await safe_send(interaction, msg, ephemeral=True)
        await self._next_or_finish(interaction)
        self.stop()

    @discord.ui.button(label="Skip / Ignore", style=discord.ButtonStyle.secondary, emoji="⏭️", row=0)
    async def skip_fix(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id or self.busy or self.is_finished():
            return await safe_send(interaction, "This repair belongs to the requester or is already running.", ephemeral=True)
        await safe_defer(interaction, ephemeral=True)
        await self._next_or_finish(interaction)
        self.stop()

    async def _next_or_finish(self, interaction: discord.Interaction) -> None:
        if self.index + 1 < len(self.fixes):
            next_view = SmartFixView(self.guild, self.user_id, self.fixes, self.index + 1)
            embed = next_view.get_current_embed()
            await safe_edit(interaction, embed=embed, view=next_view)
        else:
            embed = discord.Embed(title="🏁 All Suggested Fixes Processed", description="Completed reviewing suggested repairs.", colour=0x2ECC71)
            view = BackToMainView(self.guild, self.user_id)
            await safe_edit(interaction, embed=embed, view=view)


# ── Cog Definition ────────────────────────────────────────────────────────────
class ServerManagerCog(commands.Cog, name="Server Manager"):
    """Unified UI-first Discord server control panel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="server_manager", description="Open the DAMU interactive server management control panel.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def server_manager(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await safe_defer(interaction, ephemeral=True)
        view = MainServerManagerView(interaction.guild, interaction.user.id)
        embed = build_main_embed(interaction.guild)
        await safe_send(interaction, embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="damu", description="Shortcut to open the DAMU server management control panel.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def damu(self, interaction: discord.Interaction) -> None:
        await self.server_manager.callback(self, interaction)

    @app_commands.command(name="server_analyze", description="Run a read-only structural and security audit of the server.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @acknowledged(ephemeral=True)
    async def server_analyze(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await safe_defer(interaction, ephemeral=True)
        report = await server_analyzer.analyze(interaction.guild, viewer=interaction.user, client=interaction.client)
        view = ServerAnalyzerView(interaction.guild, interaction.user.id, report,
                                  full_access=interaction.user.guild_permissions.administrator)
        embed = view.get_embed()
        await safe_send(interaction, embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="damu_status", description="Inspect real-time health and latency metrics of DAMU.")
    @acknowledged(ephemeral=True)
    async def damu_status(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        status = await health_engine.get_status(self.bot)
        embed = status.format_embed()
        await safe_send(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="damu_doctor", description="Run deep diagnostic check on bot permissions, database, and event loop.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def damu_doctor(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        embed = await health_engine.run_doctor(self.bot, interaction.guild)
        await safe_send(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerManagerCog(bot))

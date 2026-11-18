"""Server builder cog — /setup_server, /setup_custom, /server_json, /generate_server."""

from __future__ import annotations

from asyncio import TimeoutError

from utils.interactions import acknowledged, respond, defer_response

import io
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.template_service import parse_plan, generate_plan, read_plan_attachment
from services.error_service import SafeView, SafeModal, report_error
from services.embed_service import error_embed, info_embed, success_embed, warning_embed
import config
from services.json_builder import BuildInProgress, apply_cleanup, build_server, _validate_schema
from services.permission_service import can_assign_reward_role


class ModernBuilderPanel(discord.ui.LayoutView):
    """Current Discord UI entry point for the existing guarded build workflows."""
    def __init__(self, cog, owner_id, command_channel_id, guild):
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.command_channel_id = cog, owner_id, command_channel_id
        self.message = None
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay('## Build your community\nChoose a starting point, review the plan and apply it.'),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f'**{len(guild.channels)}** channels • **{len(guild.roles)}** roles\n'
                                   '`/create_channel` creates one channel. `/security` opens moderation setup.'),
            accent_colour=0x5865F2))
        presets = discord.ui.Button(label='Browse presets', style=discord.ButtonStyle.primary)
        presets.callback = self.open_presets
        custom = discord.ui.Button(label='Custom JSON', style=discord.ButtonStyle.secondary)
        custom.callback = self.open_custom
        ai = discord.ui.Button(label='Describe with AI', style=discord.ButtonStyle.secondary)
        ai.callback = self.open_ai
        channel = discord.ui.Button(label='Create a channel', style=discord.ButtonStyle.success)
        channel.callback = self.open_channel
        self.add_item(discord.ui.ActionRow(presets, custom, ai))
        self.add_item(discord.ui.ActionRow(channel))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or not interaction.guild or not interaction.user.guild_permissions.administrator:
            await respond(interaction, 'The administrator who opened this panel must use its controls.', ephemeral=True, view=None)
            return False
        return True

    async def open_presets(self, interaction):
        view = PresetPickerView(self.cog, self.owner_id, self.command_channel_id)
        await interaction.response.send_message(embed=info_embed('Choose a layout', 'Select a preset to review its roles, categories and channels.'), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def open_custom(self, interaction):
        await interaction.response.send_modal(CustomSchemaModal(self.cog, owner_id=self.owner_id,
                                              command_channel_id=self.command_channel_id, build_label='Custom build'))

    async def open_ai(self, interaction):
        await interaction.response.send_modal(AIThemeModal(self.cog, owner_id=self.owner_id,
                                              command_channel_id=self.command_channel_id))

    async def open_channel(self, interaction):
        from cogs.security import ChannelCreateModal
        security = self.cog.bot.get_cog('Security')
        if security is None:
            return await respond(interaction, 'The security module is unavailable. Check /diagnostics.', ephemeral=True, view=None)
        await interaction.response.send_modal(ChannelCreateModal(security, self.owner_id))

    async def on_error(self, interaction, error, item):
        from services.error_service import respond_error
        await respond_error(interaction, error, context='Builder panel')

    async def on_timeout(self):
        for child in self.walk_children():
            if hasattr(child, 'disabled'):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class BuilderQuickstartView(SafeView):
    """Interactive quick-start menu that opens the actual builder workflows."""

    def __init__(
        self,
        cog: "ServerBuilderCog",
        *,
        owner_id: int,
        command_channel_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.command_channel_id = command_channel_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await respond(interaction,
                "This builder panel belongs to the administrator who opened it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Choose preset", emoji="🧩", style=discord.ButtonStyle.primary, row=0)
    async def preset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        view = PresetPickerView(self.cog, self.owner_id, self.command_channel_id)
        await interaction.response.edit_message(
            embed=info_embed("Choose a server preset", "Select a template to review its roles, categories, and channels before anything is created."),
            view=view,
        )
        view.message = interaction.message

    @discord.ui.button(label="Custom JSON", emoji="🛠️", style=discord.ButtonStyle.secondary, row=0)
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CustomSchemaModal(
                self.cog,
                owner_id=self.owner_id,
                command_channel_id=self.command_channel_id,
                build_label="Custom build",
            )
        )

    @discord.ui.button(label="AI generator", emoji="✨", style=discord.ButtonStyle.success, row=0)
    async def ai_builder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            AIThemeModal(
                self.cog,
                owner_id=self.owner_id,
                command_channel_id=self.command_channel_id,
            )
        )

    @discord.ui.button(label="JSON format", emoji="📄", style=discord.ButtonStyle.secondary, row=1)
    async def json_builder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.json_tools import JsonReferenceView, plan_card
        from services.template_service import example_plan
        schema = example_plan()
        view = JsonReferenceView(schema, interaction.user.id)
        await respond(interaction, embed=plan_card(schema), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @discord.ui.button(label="Channel guide", emoji="📚", style=discord.ButtonStyle.secondary, row=1)
    async def channel_guide(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await respond(interaction,
            embed=info_embed(
                "Channel guide",
                "Use /channel_summaries to get suggested purposes for your server's text channels. "
                "Summaries use names, existing topics, and common channel patterns.",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Close panel", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(embed=info_embed("Builder closed", "Open /builder_panel whenever you're ready."), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PresetSelect(discord.ui.Select):
    """Select a preset, then replace the menu with a preview and confirmation controls."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Gaming Server", value="gaming", emoji="🎮", description="Gaming community roles and channels"),
            discord.SelectOption(label="Community Server", value="community", emoji="🌐", description="General social and community layout"),
            discord.SelectOption(label="Study Group", value="study", emoji="📚", description="Learning-focused roles and channels"),
            discord.SelectOption(label="Business / Team", value="business", emoji="💼", description="Work and team collaboration layout"),
        ]
        super().__init__(
            placeholder="Choose a server preset…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, PresetPickerView):
            return
        if interaction.user.id != view.owner_id:
            return await respond(interaction,
                "Only the person who opened this panel can choose a preset.",
                ephemeral=True,
            )
        if interaction.guild is None:
            return await respond(interaction,
                embed=error_embed("Server required", "Use this menu inside a Discord server."),
                ephemeral=True,
            )

        schema = TEMPLATES.get(self.values[0])
        if not schema:
            return await respond(interaction,
                embed=error_embed("Preset unavailable", "That preset could not be loaded."),
                ephemeral=True,
            )
        try:
            _validate_schema(schema)
        except ValueError as exc:
            return await respond(interaction,
                embed=error_embed("Invalid preset", str(exc)),
                ephemeral=True,
            )

        title = next((option.label for option in self.options if option.value == self.values[0]), "Server")
        preview = view.cog._schema_preview_embed(
            schema,
            title=f"🧩 {title} preview",
            description="Review the proposed layout before building. Existing channels and roles will be kept.",
        )
        confirm_view = CustomBuildConfirmView(
            view.cog,
            schema,
            owner_id=view.owner_id,
            command_channel_id=view.command_channel_id,
            build_label=title,
        )
        view.stop()
        await interaction.response.edit_message(embed=preview, view=confirm_view)
        confirm_view.message = interaction.message


class PresetPickerView(SafeView):
    def __init__(self, cog: "ServerBuilderCog", owner_id: int, command_channel_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.command_channel_id = command_channel_id
        self.add_item(PresetSelect())
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await respond(interaction, "This menu belongs to the administrator who opened it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Back to builder", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        self.stop()
        view = BuilderQuickstartView(self.cog, owner_id=self.owner_id, command_channel_id=self.command_channel_id)
        await interaction.response.edit_message(embed=info_embed("🧱 Build your community", "Step 1 of 3 · Choose a preset, custom JSON, or AI generation."), view=view)
        view.message = interaction.message

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass



class CustomSchemaModal(SafeModal):
    def __init__(
        self,
        cog: "ServerBuilderCog",
        *,
        owner_id: int,
        command_channel_id: int,
        build_label: str,
    ) -> None:
        super().__init__(title="Build from JSON", timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.command_channel_id = command_channel_id
        self.build_label = build_label
        self.schema_input = discord.ui.TextInput(
            label="Server schema JSON",
            placeholder='{"roles": [], "categories": [...]}',
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.schema_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            return await respond(interaction,
                "Only the person who opened this builder can submit the schema.",
                ephemeral=True,
            )
        try:
            raw = self.schema_input.value
            schema = parse_plan(raw)
            _validate_schema(schema)
        except (json.JSONDecodeError, ValueError) as exc:
            return await respond(interaction,
                embed=error_embed("Invalid server schema", str(exc)),
                ephemeral=True,
            )

        preview = self.cog._schema_preview_embed(
            schema,
            title="🧱 Custom server preview",
            description="Review the schema before creating anything. Existing resources will be kept.",
        )
        view = CustomBuildConfirmView(
            self.cog,
            schema,
            owner_id=interaction.user.id,
            command_channel_id=self.command_channel_id,
            build_label=self.build_label,
        )
        await respond(interaction, embed=preview, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


class AIThemeModal(SafeModal):
    def __init__(self, cog: "ServerBuilderCog", *, owner_id: int, command_channel_id: int) -> None:
        super().__init__(title="Generate a server template", timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.command_channel_id = command_channel_id
        self.theme_input = discord.ui.TextInput(
            label="What is your server about?",
            placeholder="Gaming community, anime club, study group…",
            style=discord.TextStyle.short,
            required=True,
            min_length=3,
            max_length=150,
        )
        self.add_item(self.theme_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            return await respond(interaction,
                "Only the person who opened this builder can submit the theme.",
                ephemeral=True,
            )

        from cogs.ai import _rate_limited
        if _rate_limited(interaction.user.id):
            return await respond(interaction, "AI limit reached. Try again in a minute.", ephemeral=True)
        theme = self.theme_input.value.strip()
        await defer_response(interaction, ephemeral=True, thinking=True)
        try:
            schema = await generate_plan(theme, interaction.user.id)
        except (ValueError, TimeoutError) as exc:
            return await interaction.edit_original_response(embed=error_embed("AI draft unavailable", str(exc) or "AI timed out. Try again."))

        preview = self.cog._schema_preview_embed(
            schema,
            title="✨ AI server preview",
            description=f"Generated for {theme}. Nothing has been created yet.",
        )
        view = CustomBuildConfirmView(
            self.cog,
            schema,
            owner_id=interaction.user.id,
            command_channel_id=self.command_channel_id,
            build_label=f"AI build · {theme[:50]}",
        )
        message = await interaction.edit_original_response(embed=preview, view=view)
        view.message = message


class PlanSectionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Inspect this plan before building…", row=0, options=[
            discord.SelectOption(label="Overview", value="overview", emoji="🧱"),
            discord.SelectOption(label="Channels & categories", value="channels", emoji="📁"),
            discord.SelectOption(label="Roles & permissions", value="roles", emoji="🛡️"),
            discord.SelectOption(label="Channel access", value="access", emoji="🔒"),
        ])

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.section = self.values[0]
        view.detail_page = 0
        for option in self.options:
            option.default = option.value == view.section
        view.update_navigation()
        await interaction.response.edit_message(embed=view.review_embed(), view=view)


class CustomBuildConfirmView(SafeView):
    """Owner-locked preview gate for custom and AI-generated server builds."""

    def __init__(
        self,
        cog: "ServerBuilderCog",
        schema: dict,
        *,
        owner_id: int,
        command_channel_id: int,
        clean_existing: bool = False,
        build_label: str = "Server build",
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.schema = schema
        self.owner_id = owner_id
        self.command_channel_id = command_channel_id
        self.clean_existing = clean_existing
        self.started = False
        self.section = "overview"
        self.detail_page = 0
        self.add_item(PlanSectionSelect())
        self.update_navigation()
        self.build_label = build_label
        self.message: discord.Message | None = None

    def access_entries(self):
        for category in self.schema.get("categories", []):
            locations = [(category['name'], category)]
            locations.extend((f"{category['name']} / {channel['name']}", channel)
                             for channel in category.get('channels', []))
            for location, entry in locations:
                for overwrite in entry.get('permission_overwrites', []):
                    allow = ', '.join(overwrite.get('allow', [])) or 'None'
                    deny = ', '.join(overwrite.get('deny', [])) or 'None'
                    yield f"{location} · {overwrite['role']}", f"Allow: {allow}\nDeny: {deny}"

    def detail_entries(self):
        if self.section == "access":
            return list(self.access_entries())
        return self.schema.get("categories" if self.section == "channels" else "roles", [])

    @property
    def detail_page_count(self):
        return max(1, (len(self.detail_entries()) + 4) // 5) if self.section != "overview" else 1

    def update_navigation(self):
        self.previous.disabled = self.detail_page == 0
        self.next_page.disabled = self.detail_page >= self.detail_page_count - 1

    def review_embed(self):
        embed = self.cog._schema_preview_embed(self.schema, title="Review your server plan", description="Step 2 of 3 · Inspect the plan, then confirm when ready.")
        if self.section != "overview":
            embed.clear_fields()
            entries = self.detail_entries()
            for entry in entries[self.detail_page * 5:(self.detail_page + 1) * 5]:
                if self.section == "access":
                    location, value = entry
                    embed.add_field(name=discord.utils.escape_markdown(location)[:240], value=value[:850], inline=False)
                    continue
                if self.section == "channels":
                    lines = [f"{'🔊' if c.get('type') == 'voice' else '#'} {c['name']}" for c in entry.get("channels", [])]
                    value = "\n".join(lines) or "No channels"
                    if len(value) > 800:
                        value = value[:750] + "\n… Export JSON for all channels."
                else:
                    value = ", ".join(entry.get("permissions", [])) or "No extra server permissions"
                embed.add_field(name=entry['name'], value=value[:800], inline=False)
            if self.section == "access":
                embed.description += "\nChannel rules inherit the category unless that channel overrides the same permission."
            if not entries:
                embed.description += "\nNo entries in this section."
            embed.set_footer(text=f"{self.section.title()} • Page {self.detail_page + 1}/{self.detail_page_count} • Export JSON for the complete plan")
        if self.clean_existing:
            embed.description += "\nThe old layout stays in place during the build. Cleanup requires a separate owner approval."
        return embed

    @discord.ui.button(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction, button):
        self.detail_page = max(0, self.detail_page - 1)
        self.update_navigation()
        await interaction.response.edit_message(embed=self.review_embed(), view=self)

    @discord.ui.button(label="Next", emoji="▶", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction, button):
        self.detail_page = min(self.detail_page_count - 1, self.detail_page + 1)
        self.update_navigation()
        await interaction.response.edit_message(embed=self.review_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await respond(interaction,
                "Only the administrator who started this build can confirm it.",
                ephemeral=True,
            )
            return False
        if self.started:
            await respond(interaction, "This build has already started.", ephemeral=True)
            return False
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await respond(interaction, "Administrator permission is required to use this plan.", ephemeral=True)
            return False
        return True

    @discord.ui.button(
        label="Confirm build",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="server_builder_confirm",
        row=1,
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction):
            return
        if self.clean_existing and interaction.user.id != interaction.guild.owner_id:
            return await respond(interaction, "Only the server owner can prepare cleanup.", ephemeral=True)
        self.started = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=info_embed("Build confirmed", "Preparing the server build…"),
            view=self,
        )
        await self.cog._execute_custom_build(
            interaction,
            self.schema,
            clean_existing=self.clean_existing,
            safe_channel_id=self.command_channel_id,
            build_label=self.build_label,
        )
        self.stop()

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
        custom_id="server_builder_cancel",
        row=1,
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=info_embed("Build cancelled", "No server changes were made."),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Export JSON", emoji="📄", style=discord.ButtonStyle.secondary, row=1)
    async def export(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = json.dumps(self.schema, ensure_ascii=False, indent=2).encode("utf-8")
        await respond(interaction, file=discord.File(io.BytesIO(data), filename="server-plan.json"), ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=info_embed("Preview expired", "The preview expired before confirmation. No build was started."),
                    view=self,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


log = logging.getLogger("cogs.server_builder")


class BuildFinalizationError(RuntimeError):
    """The layout is ready, with remaining role assignments or welcome messages."""


class CleanupConfirmView(SafeView):
    """Apply the saved deletion list only on a separate owner confirmation."""

    def __init__(self, cog, guild_id, owner_id, path, snapshot):
        super().__init__(timeout=300)
        self.cog, self.snapshot = cog, snapshot
        self.guild_id, self.owner_id, self.path = guild_id, owner_id, path
        self.running = False

    async def interaction_check(self, interaction):
        guild = interaction.guild
        if guild is not None and guild.id == self.guild_id and interaction.user.id == self.owner_id == guild.owner_id:
            return True
        await respond(interaction, "Only the current server owner can approve this cleanup.", ephemeral=True)
        return False

    @discord.ui.button(label="Delete listed resources", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        if self.running:
            return await respond(interaction, "Cleanup is already in progress.", ephemeral=True)
        self.running = True
        try:
            await defer_response(interaction, ephemeral=True, thinking=True)
            channels, roles = await self.cog._cleanup_protections(interaction.guild)
            deleted, failures = await apply_cleanup(interaction.guild, interaction.user, self.path, self.snapshot, channels, roles)
            if failures:
                await respond(interaction, f"Deleted {deleted} resources. {len(failures)} could not be removed. Check the bot’s permissions and click again to retry.", ephemeral=True)
                return
            await respond(interaction, f"Cleanup complete. Deleted {deleted} listed resources.", ephemeral=True)
            button.disabled = True
            self.stop()
            await interaction.message.edit(view=self)
        except (ValueError, BuildInProgress) as exc:
            await respond(interaction, str(exc), ephemeral=True)
        finally:
            self.running = False


# ── Preset server templates ──────────────────────────────────────────────────────
TEMPLATES: dict[str, dict] = {
    "gaming": {
        "server_name": None,
        "roles": [
            {"name": "Owner", "color": "gold", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Admin", "color": "red", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Moderator", "color": "green", "hoist": True, "mentionable": True, "permissions": ["kick_members", "ban_members", "manage_messages", "manage_channels"]},
            {"name": "VIP", "color": "magenta", "hoist": True, "mentionable": False, "permissions": ["send_messages", "read_messages", "embed_links", "attach_files"]},
            {"name": "Gamer", "color": "purple", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages", "connect", "speak"]},
            {"name": "Member", "color": "blue", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages"]},
        ],
        "categories": [
            {
                "name": "\U0001f4e2 INFO",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": ["read_messages"], "deny": ["send_messages"]},
                    {"role": "Admin", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4dcrules", "topic": "Read the rules before chatting!"},
                    {
                        "type": "text", "name": "\U0001f4e3announcements", "topic": "Server announcements",
                        "permission_overwrites": [
                            {"role": "Admin", "allow": ["send_messages", "mention_everyone"], "deny": []},
                            {"role": "Moderator", "allow": ["send_messages"], "deny": []},
                        ],
                    },
                    {"type": "text", "name": "\U0001f44bwelcome", "topic": "Welcome new members!"},
                ],
            },
            {
                "name": "\U0001f4ac GENERAL",
                "channels": [
                    {"type": "text", "name": "\U0001f4acgeneral-chat", "topic": "Talk about anything"},
                    {"type": "text", "name": "\U0001f916bot-commands", "topic": "Use bot commands here"},
                    {
                        "type": "text", "name": "\U0001f5bcmedia", "topic": "Share images, videos, memes",
                        "permission_overwrites": [
                            {"role": "Member", "allow": ["read_messages", "attach_files", "embed_links"], "deny": []},
                        ],
                    },
                ],
            },
            {
                "name": "\U0001f3ae GAMING",
                "channels": [
                    {"type": "text", "name": "\U0001f3aegame-chat", "topic": "Talk about games"},
                    {"type": "text", "name": "\U0001f3c6clips-highlights", "topic": "Share your best moments"},
                    {"type": "text", "name": "\U0001f3aflooking-for-group", "topic": "Find teammates"},
                    {"type": "voice", "name": "\U0001f3ae Game Lobby", "bitrate": 96000, "user_limit": 0},
                    {
                        "type": "voice", "name": "\U0001f3ae Game Room 1", "bitrate": 96000, "user_limit": 5,
                        "permission_overwrites": [
                            {"role": "Gamer", "allow": ["connect", "speak"], "deny": []},
                        ],
                    },
                    {
                        "type": "voice", "name": "\U0001f3ae Game Room 2", "bitrate": 96000, "user_limit": 5,
                        "permission_overwrites": [
                            {"role": "Gamer", "allow": ["connect", "speak"], "deny": []},
                        ],
                    },
                ],
            },
            {
                "name": "\U0001f3b5 MUSIC & CHILL",
                "channels": [
                    {"type": "text", "name": "\U0001f3b5music-requests", "topic": "Request songs here"},
                    {
                        "type": "voice", "name": "\U0001f3b5 Music Lounge", "bitrate": 96000, "user_limit": 0,
                        "permission_overwrites": [
                            {"role": "VIP", "allow": ["connect", "speak"], "deny": []},
                            {"role": "Member", "allow": ["connect"], "deny": ["speak"]},
                        ],
                    },
                    {"type": "voice", "name": "\u2615 Chill Zone", "bitrate": 64000, "user_limit": 10},
                ],
            },
            {
                "name": "\U0001f512 STAFF",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": [], "deny": ["read_messages"]},
                    {"role": "Moderator", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Admin", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4cbmod-log", "topic": "Moderation logs"},
                    {"type": "text", "name": "\U0001f4acstaff-chat", "topic": "Staff discussion"},
                    {"type": "voice", "name": "\U0001f512 Staff Room", "bitrate": 64000, "user_limit": 0},
                ],
            },
        ],
        "auto_assign": "Member",
    },
    "community": {
        "server_name": None,
        "roles": [
            {"name": "Owner", "color": "gold", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Admin", "color": "crimson", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Moderator", "color": "emerald", "hoist": True, "mentionable": True, "permissions": ["kick_members", "ban_members", "manage_messages", "manage_channels"]},
            {"name": "Helper", "color": "amber", "hoist": True, "mentionable": True, "permissions": ["manage_messages"]},
            {"name": "Active Member", "color": "magenta", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages", "embed_links", "attach_files"]},
            {"name": "Member", "color": "blue", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages"]},
        ],
        "categories": [
            {
                "name": "\U0001f4cb INFORMATION",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": ["read_messages"], "deny": ["send_messages"]},
                    {"role": "Admin", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4dcrules", "topic": "Server rules"},
                    {
                        "type": "text", "name": "\U0001f4e3announcements", "topic": "Important updates",
                        "permission_overwrites": [
                            {"role": "Admin", "allow": ["send_messages", "mention_everyone"], "deny": []},
                            {"role": "Moderator", "allow": ["send_messages"], "deny": []},
                        ],
                    },
                    {"type": "text", "name": "\U0001f4ccroles-info", "topic": "Get your roles here"},
                    {"type": "text", "name": "\U0001f44bintroductions", "topic": "Introduce yourself!"},
                ],
            },
            {
                "name": "\U0001f4ac COMMUNITY",
                "channels": [
                    {"type": "text", "name": "\U0001f4acgeneral", "topic": "Main chat"},
                    {"type": "text", "name": "\U0001f916bot-cmds", "topic": "Bot commands"},
                    {
                        "type": "text", "name": "\U0001f5bcmedia-share", "topic": "Share media",
                        "permission_overwrites": [
                            {"role": "Active Member", "allow": ["attach_files", "embed_links"], "deny": []},
                            {"role": "Member", "allow": ["read_messages"], "deny": ["attach_files"]},
                        ],
                    },
                    {"type": "text", "name": "\U0001f4a1suggestions", "topic": "Suggest improvements"},
                    {"type": "text", "name": "\U0001f4capolls", "topic": "Community polls"},
                ],
            },
            {
                "name": "\U0001f3a8 CREATIVE",
                "channels": [
                    {"type": "text", "name": "\U0001f3a8art-gallery", "topic": "Share your creations"},
                    {"type": "text", "name": "\u270dwriting", "topic": "Stories, poems, ideas"},
                    {"type": "text", "name": "\U0001f4f8photography", "topic": "Share your photos"},
                ],
            },
            {
                "name": "\U0001f50a VOICE",
                "channels": [
                    {"type": "voice", "name": "\U0001f50a General Voice", "bitrate": 96000, "user_limit": 0},
                    {"type": "voice", "name": "\U0001f3b5 Music", "bitrate": 96000, "user_limit": 0},
                    {"type": "voice", "name": "\u2615 Chill Lounge", "bitrate": 64000, "user_limit": 10},
                    {"type": "voice", "name": "\U0001f4da Study Room", "bitrate": 64000, "user_limit": 5},
                ],
            },
            {
                "name": "\U0001f512 STAFF AREA",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": [], "deny": ["read_messages"]},
                    {"role": "Moderator", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Admin", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Helper", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4cbmod-log", "topic": "Moderation logs"},
                    {"type": "text", "name": "\U0001f4acstaff-chat", "topic": "Staff only"},
                    {"type": "voice", "name": "\U0001f512 Staff Voice", "bitrate": 64000},
                ],
            },
        ],
        "auto_assign": "Member",
    },
    "study": {
        "server_name": None,
        "roles": [
            {"name": "Owner", "color": "gold", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Admin", "color": "crimson", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Tutor", "color": "emerald", "hoist": True, "mentionable": True, "permissions": ["manage_messages", "kick_members"]},
            {"name": "Student", "color": "blue", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages"]},
        ],
        "categories": [
            {
                "name": "\U0001f4cb INFO",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": ["read_messages"], "deny": ["send_messages"]},
                    {"role": "Admin", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4dcrules", "topic": "Read before participating"},
                    {
                        "type": "text", "name": "\U0001f4e3announcements", "topic": "Updates & schedules",
                        "permission_overwrites": [
                            {"role": "Admin", "allow": ["send_messages"], "deny": []},
                            {"role": "Tutor", "allow": ["send_messages"], "deny": []},
                        ],
                    },
                    {"type": "text", "name": "\U0001f4daresources", "topic": "Helpful links & materials"},
                ],
            },
            {
                "name": "\U0001f4ac DISCUSSION",
                "channels": [
                    {"type": "text", "name": "\U0001f4acgeneral", "topic": "Off-topic chat"},
                    {
                        "type": "text", "name": "\u2753questions", "topic": "Ask for help here",
                        "permission_overwrites": [
                            {"role": "Student", "allow": ["send_messages", "attach_files"], "deny": []},
                            {"role": "Tutor", "allow": ["send_messages", "manage_messages"], "deny": []},
                        ],
                    },
                    {"type": "text", "name": "\U0001f4ddhomework-help", "topic": "Get homework assistance"},
                    {"type": "text", "name": "\U0001f916bot-commands", "topic": "Bot commands"},
                ],
            },
            {
                "name": "\U0001f4d6 SUBJECTS",
                "permission_overwrites": [
                    {"role": "Student", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Tutor", "allow": ["read_messages", "send_messages", "manage_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f522math", "topic": "Mathematics discussion"},
                    {"type": "text", "name": "\U0001f52cscience", "topic": "Science discussion"},
                    {"type": "text", "name": "\U0001f4bbcoding", "topic": "Programming help"},
                    {"type": "text", "name": "\U0001f4ddenglish", "topic": "English & writing"},
                ],
            },
            {
                "name": "\U0001f50a STUDY ROOMS",
                "channels": [
                    {"type": "voice", "name": "\U0001f4da Study Room 1", "bitrate": 64000, "user_limit": 5},
                    {"type": "voice", "name": "\U0001f4da Study Room 2", "bitrate": 64000, "user_limit": 5},
                    {
                        "type": "voice", "name": "\U0001f465 Group Session", "bitrate": 96000, "user_limit": 10,
                        "permission_overwrites": [
                            {"role": "Tutor", "allow": ["connect", "speak", "mute_members"], "deny": []},
                        ],
                    },
                    {"type": "voice", "name": "\U0001f3b5 Lo-Fi Study", "bitrate": 96000, "user_limit": 0},
                ],
            },
            {
                "name": "\U0001f512 STAFF",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": [], "deny": ["read_messages"]},
                    {"role": "Tutor", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Admin", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4cbstaff-log", "topic": "Staff logs"},
                    {"type": "text", "name": "\U0001f4actutor-chat", "topic": "Tutor discussions"},
                ],
            },
        ],
        "auto_assign": "Student",
    },
    "business": {
        "server_name": None,
        "roles": [
            {"name": "CEO", "color": "gold", "hoist": True, "mentionable": False, "permissions": ["administrator"]},
            {"name": "Manager", "color": "crimson", "hoist": True, "mentionable": True, "permissions": ["manage_channels", "manage_messages", "kick_members"]},
            {"name": "Team Lead", "color": "emerald", "hoist": True, "mentionable": True, "permissions": ["manage_messages"]},
            {"name": "Employee", "color": "blue", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages", "connect", "speak"]},
            {"name": "Intern", "color": "grey", "hoist": False, "mentionable": False, "permissions": ["send_messages", "read_messages"]},
        ],
        "categories": [
            {
                "name": "\U0001f4cb COMPANY",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": ["read_messages"], "deny": ["send_messages"]},
                    {"role": "CEO", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Manager", "allow": ["send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4dcguidelines", "topic": "Company rules & policies"},
                    {
                        "type": "text", "name": "\U0001f4e3announcements", "topic": "Company announcements",
                        "permission_overwrites": [
                            {"role": "CEO", "allow": ["send_messages", "mention_everyone"], "deny": []},
                            {"role": "Manager", "allow": ["send_messages"], "deny": []},
                        ],
                    },
                    {"type": "text", "name": "\U0001f5d3schedule", "topic": "Meeting schedules"},
                ],
            },
            {
                "name": "\U0001f4bc WORK",
                "channels": [
                    {"type": "text", "name": "\U0001f4acgeneral-work", "topic": "General work discussion"},
                    {
                        "type": "text", "name": "\U0001f4cbtasks", "topic": "Task assignments & tracking",
                        "permission_overwrites": [
                            {"role": "Team Lead", "allow": ["send_messages", "manage_messages"], "deny": []},
                            {"role": "Employee", "allow": ["send_messages"], "deny": []},
                            {"role": "Intern", "allow": ["read_messages"], "deny": ["send_messages"]},
                        ],
                    },
                    {"type": "text", "name": "\U0001f4c8reports", "topic": "Weekly reports"},
                    {"type": "text", "name": "\U0001f4a1ideas", "topic": "Brainstorming & ideas"},
                ],
            },
            {
                "name": "\U0001f3e2 DEPARTMENTS",
                "permission_overwrites": [
                    {"role": "Employee", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "Team Lead", "allow": ["read_messages", "send_messages", "manage_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4bbdev-team", "topic": "Development team"},
                    {"type": "text", "name": "\U0001f3a8design-team", "topic": "Design team"},
                    {"type": "text", "name": "\U0001f4camarketing", "topic": "Marketing team"},
                    {"type": "text", "name": "\U0001f91dhr", "topic": "Human resources"},
                ],
            },
            {
                "name": "\U0001f50a MEETINGS",
                "channels": [
                    {"type": "voice", "name": "\U0001f4de Meeting Room 1", "bitrate": 96000, "user_limit": 10},
                    {"type": "voice", "name": "\U0001f4de Meeting Room 2", "bitrate": 96000, "user_limit": 10},
                    {
                        "type": "voice", "name": "\u2615 Break Room", "bitrate": 64000, "user_limit": 0,
                        "permission_overwrites": [
                            {"role": "Employee", "allow": ["connect", "speak"], "deny": []},
                            {"role": "Intern", "allow": ["connect", "speak"], "deny": []},
                        ],
                    },
                ],
            },
            {
                "name": "\U0001f512 MANAGEMENT",
                "permission_overwrites": [
                    {"role": "@everyone", "allow": [], "deny": ["read_messages"]},
                    {"role": "Manager", "allow": ["read_messages", "send_messages"], "deny": []},
                    {"role": "CEO", "allow": ["read_messages", "send_messages"], "deny": []},
                ],
                "channels": [
                    {"type": "text", "name": "\U0001f4cbmanagement-log", "topic": "Management logs"},
                    {"type": "text", "name": "\U0001f4acprivate-chat", "topic": "Management only"},
                    {"type": "voice", "name": "\U0001f512 Private Office", "bitrate": 64000},
                ],
            },
        ],
        "auto_assign": "Employee",
    },
}

class ServerBuilderCog(commands.Cog, name="Server Builder"):
    """Server generation — AI-powered or from preset templates."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _schema_preview_embed(
        self,
        schema: dict,
        *,
        title: str,
        description: str,
    ) -> discord.Embed:
        roles = schema.get("roles", [])
        categories = schema.get("categories", [])
        channel_count = sum(len(category.get("channels", [])) for category in categories)
        role_names = [str(role.get("name", "Unnamed")) for role in roles]
        category_lines = [
            f"• **{str(category.get('name', 'Unnamed'))[:80]}** · {len(category.get('channels', []))} channels"
            for category in categories[:8]
        ]
        if len(categories) > 8:
            category_lines.append(f"…and {len(categories) - 8} more categories")

        em = info_embed(title, description)
        em.add_field(name="Server name", value=str(schema.get("server_name") or "Keep current name")[:100], inline=True)
        em.add_field(name="Roles", value=str(len(roles)), inline=True)
        em.add_field(name="Categories", value=str(len(categories)), inline=True)
        em.add_field(name="Channels", value=str(channel_count), inline=True)
        em.add_field(
            name="Role names",
            value=(", ".join(role_names[:12]) + (f" … +{len(role_names) - 12}" if len(role_names) > 12 else ""))[:1000] or "None",
            inline=False,
        )
        em.add_field(name="Structure", value="\n".join(category_lines) or "No categories", inline=False)
        privileged = [r["name"] for r in roles if "administrator" in [p.lower() for p in r.get("permissions", [])]]
        if privileged:
            em.add_field(name="Administrator roles", value=", ".join(privileged)[:1000], inline=False)
        if schema.get("auto_assign"):
            em.add_field(name="Member role assignment", value=f"Assign **{schema['auto_assign']}** to existing human members after building."[:1000], inline=False)
        em.set_footer(text="Confirm to build • Cancel to leave the server unchanged")
        return em

    @staticmethod
    def _safe_command_channel_id(interaction: discord.Interaction) -> int:
        channel = interaction.channel
        if isinstance(channel, discord.Thread) and channel.parent_id:
            return channel.parent_id
        return interaction.channel_id

    async def _cleanup_protections(self, guild):
        channels = {config.MOD_LOG_CHANNEL_ID, config.TICKET_LOG_CHANNEL_ID}
        roles = {config.SUPPORT_ROLE_ID}
        tickets = self.bot.get_cog('TicketSystem')
        if tickets is not None:
            settings = await tickets.config_manager.get_guild(guild.id)
            channels.update(settings.get('open_tickets', {}).values())
            channels.update(int(identifier) for identifier in settings.get('pending_deletions', {}))
            channels.update(settings.get(key) for key in ('panel_channel_id', 'log_channel_id', 'ticket_category_id'))
            roles.update(settings.get(key) for key in ('support_role_id', 'mod_role_id'))
            for case_roles in settings.get('ticket_staff_roles', {}).values():
                roles.update(case_roles)
        security = self.bot.get_cog('Security')
        if security is not None:
            async with security.quarantine_service.lock:
                state = await security.quarantine_service.load()
                settings = state.get(str(guild.id), {})
                category = settings.get('category')
                channels.update((category, settings.get('room')))
                channels.update(settings.get('review_rooms', []))
                if category:
                    channels.update(channel.id for channel in guild.channels if channel.category_id == category)
                roles.update((settings.get('role'), settings.get('staff')))
                for record in settings.get('members', {}).values():
                    channels.add(record.get('room'))
                    roles.update(record.get('roles', []))
        channels.update(channel.category_id for channel in guild.channels if channel.id in channels)
        return channels, roles

    @app_commands.command(
        name="builder_panel",
        description="Open the interactive server builder quick-start panel.",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def builder_panel(self, interaction: discord.Interaction) -> None:
        view = ModernBuilderPanel(self, interaction.user.id, self._safe_command_channel_id(interaction), interaction.guild)
        await respond(interaction, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @staticmethod
    def _infer_channel_summary(channel_name: str, topic: str | None = None) -> str:
        if topic and topic.strip():
            return topic.strip()

        lowered = channel_name.lower()
        checks: list[tuple[tuple[str, ...], str]] = [
            (("rule",), "Read the server rules and important guidelines here."),
            (("announce", "news", "update"), "Official updates and important notices are posted here."),
            (("welcome", "intro"), "Welcome messages and introductions happen here."),
            (("bot", "cmd", "command"), "Use bot commands and automation features in this channel."),
            (("media", "gallery", "photo", "video", "art", "meme"), "Share images, videos, and media content here."),
            (("general", "chat", "talk"), "General discussion channel for day-to-day conversations."),
            (("support", "help", "question", "ticket"), "Ask questions and get support from staff and members here."),
            (("staff", "mod", "admin", "management"), "Private coordination channel for staff and moderation."),
            (("log", "audit"), "Server logs and moderation records are kept here."),
            (("voice", "lounge", "room", "meeting", "call"), "Join voice conversations and live discussions here."),
            (("music",), "Music sessions, listening parties, and music-related chat happen here."),
            (("study", "homework", "resource"), "Study resources, learning discussions, and academic help are shared here."),
        ]

        for keywords, summary in checks:
            if any(word in lowered for word in keywords):
                return summary

        return "Use this channel for discussions related to its name and category."

    async def _post_created_channel_summaries(self, guild, schema, actor_mention, journal):
        sent = failed = 0
        published = journal.state['summaries']
        for cat_index, category in enumerate(schema.get("categories", [])):
            for ch_index, channel_plan in enumerate(category.get("channels", [])):
                if channel_plan.get("type", "text") != "text":
                    continue
                channel = journal.resources[f"channel:{cat_index}:{ch_index}"]
                if channel.id in published:
                    continue
                if not channel.permissions_for(guild.me).send_messages:
                    failed += 1
                    continue
                summary = self._infer_channel_summary(channel.name, channel_plan.get("topic"))
                msg = f"👋 Welcome to {channel.mention}!\n📝 {summary}\nCreated by {actor_mention}."
                try:
                    message = await channel.send(msg, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    failed += 1
                    continue
                published.append(channel.id)
                try:
                    await journal.save()
                except Exception:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        log.exception("Could not remove unrecorded welcome message %s", message.id)
                    raise
                sent += 1
        return sent, failed

    def _build_server_summary_lines(self, guild: discord.Guild) -> list[str]:
        lines: list[str] = []
        for category in guild.categories:
            text_channels = [
                ch
                for ch in guild.text_channels
                if ch.category_id == category.id
            ]
            for channel in text_channels:
                purpose = self._infer_channel_summary(channel.name, channel.topic)
                lines.append(f"{channel.mention} \u2014 {purpose}")

        uncategorized = [
            ch
            for ch in guild.text_channels
            if ch.category_id is None
        ]
        for channel in uncategorized:
            purpose = self._infer_channel_summary(channel.name, channel.topic)
            lines.append(f"{channel.mention} \u2014 {purpose}")

        return lines

    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    # /setup_server \u2014 preset template builder (NO AI needed)
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="setup_server",
        description="Auto-setup server with roles, channels, and categories from a template.",
    )
    @app_commands.describe(
        template="Choose a server template",
        clean_existing="Owner only. Prepare a separate preview to remove the old layout after building",
    )
    @app_commands.choices(
        template=[
            app_commands.Choice(name="Gaming Server", value="gaming"),
            app_commands.Choice(name="Community Server", value="community"),
            app_commands.Choice(name="Study Group", value="study"),
            app_commands.Choice(name="Business / Team", value="business"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def setup_server(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
        clean_existing: bool = False,
    ) -> None:
        if not interaction.guild:
            return
        if clean_existing and interaction.user.id != interaction.guild.owner_id:
            return await respond(interaction, "Only the server owner can request cleanup.", ephemeral=True)

        schema = TEMPLATES.get(template.value)
        if not schema:
            return await respond(interaction,
                embed=error_embed("Template not found", "Choose one of the available presets and retry."),
                ephemeral=True,
            )
        try:
            _validate_schema(schema)
        except ValueError as exc:
            return await respond(interaction,
                embed=error_embed("Invalid preset", str(exc)),
                ephemeral=True,
            )

        preview = self._schema_preview_embed(schema, title=f"🧩 {template.name} preview", description="Step 2 of 3 · Review this preset with the menu below.")
        if clean_existing:
            preview.colour = discord.Colour(0xED4245)
            preview.description += "\nCleanup will be offered after the replacement is ready. The server owner must approve the deletion list."
        view = CustomBuildConfirmView(
            self,
            schema,
            owner_id=interaction.user.id,
            command_channel_id=self._safe_command_channel_id(interaction),
            clean_existing=clean_existing,
            build_label=f"{template.name} preset",
        )
        await respond(interaction, embed=preview, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # /server_json \u2014 show the JSON schema / export a template
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="server_json",
        description="Get the JSON schema/template you can use with /setup_custom.",
    )
    @app_commands.describe(
        template="Export a preset template as JSON, or leave blank for blank schema",
    )
    @app_commands.choices(
        template=[
            app_commands.Choice(name="Minimal JSON example", value="blank"),
            app_commands.Choice(name="Gaming Server", value="gaming"),
            app_commands.Choice(name="Community Server", value="community"),
            app_commands.Choice(name="Study Group", value="study"),
            app_commands.Choice(name="Business / Team", value="business"),
        ]
    )
    @acknowledged(ephemeral=True)
    async def server_json(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str] | None = None,
    ) -> None:
        from cogs.json_tools import JsonReferenceView, plan_card, plan_file
        from services.template_service import example_plan
        kind = template.value if template and template.value != "blank" else "minimal"
        schema = example_plan(kind)
        view = JsonReferenceView(schema, interaction.user.id)
        await respond(interaction, embed=plan_card(schema), file=plan_file(schema), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    # /setup_custom \u2014 build from user-provided JSON
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="setup_custom",
        description="Build a server from your own JSON template (paste or attach .json).",
    )
    @app_commands.describe(
        json_text="Paste your server JSON here (or attach a .json file)",
        json_file="Upload a .json file with your server template",
        clean_existing="Owner only. Prepare a separate preview to remove the old layout after building",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def setup_custom(
        self,
        interaction: discord.Interaction,
        json_text: str | None = None,
        json_file: discord.Attachment | None = None,
        clean_existing: bool = False,
    ) -> None:
        if not interaction.guild:
            return
        if clean_existing and interaction.user.id != interaction.guild.owner_id:
            return await respond(interaction, "Only the server owner can request cleanup.", ephemeral=True)

        try:
            if json_file:
                schema = await read_plan_attachment(json_file)
            elif json_text:
                schema = parse_plan(json_text)
            else:
                return await respond(interaction, "Provide JSON text or a file. Use /server_json for an example.", ephemeral=True)
        except ValueError as exc:
            return await respond(interaction, embed=error_embed("Invalid server plan", str(exc)[:1000]), ephemeral=True)

        preview = self._schema_preview_embed(schema, title="🛠️ Custom server preview", description="Step 2 of 3 · Review your roles, channels, and permissions below.")
        if clean_existing:
            preview.colour = discord.Colour(0xED4245)
            preview.description += "\nCleanup will be offered after the replacement is ready. The server owner must approve the deletion list."
        view = CustomBuildConfirmView(
            self,
            schema,
            owner_id=interaction.user.id,
            command_channel_id=self._safe_command_channel_id(interaction),
            clean_existing=clean_existing,
            build_label="Custom build",
        )
        await interaction.edit_original_response(embed=preview, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass
        return

    async def _execute_custom_build(
        self,
        interaction: discord.Interaction,
        schema: dict,
        *,
        clean_existing: bool,
        safe_channel_id: int,
        build_label: str = "Server build",
        progress_msg: discord.Message | None = None,
    ) -> None:
        """Execute a custom build only after its preview is explicitly confirmed."""
        guild = interaction.guild
        if guild is None:
            return

        if clean_existing and interaction.user.id != guild.owner_id:
            return await respond(interaction, "Only the server owner can request cleanup.", ephemeral=True)
        if progress_msg is None:
            progress_msg = await interaction.followup.send(
                embed=info_embed("Building server", "Preparing the layout…"), ephemeral=True, wait=True
            )

        journal_result = None
        assignment_logs = []

        async def finalize(role_map, journal):
            nonlocal journal_result
            journal_result = journal
            assignment_failures = 0
            if interaction.user.id == guild.owner_id and isinstance(interaction.user, discord.Member):
                for role_data in schema.get("roles", []):
                    if any(p.lower() == "administrator" for p in role_data.get("permissions", [])):
                        role = role_map[role_data["name"]]
                        if role not in interaction.user.roles:
                            try:
                                await interaction.user.add_roles(role, reason="Confirmed build by server owner")
                            except discord.HTTPException:
                                assignment_failures += 1
                        break

            auto_role = role_map.get(schema.get("auto_assign"))
            completed_assignments = journal.state.setdefault('assigned_roles', [])
            if auto_role is not None and auto_role.id not in completed_assignments:
                if not can_assign_reward_role(guild, auto_role):
                    raise BuildFinalizationError("The automatic member role now has restricted permissions or is above the bot. Review the role and retry.")
                quarantined = set()
                security = self.bot.get_cog('Security')
                if security is not None:
                    async with security.quarantine_service.lock:
                        state = await security.quarantine_service.load()
                        quarantined = {int(member_id) for member_id in state.get(str(guild.id), {}).get('members', {})}
                assigned = 0
                for member in guild.members:
                    if member.bot or member.id == interaction.user.id or member.id in quarantined or auto_role in member.roles:
                        continue
                    try:
                        await member.add_roles(auto_role, reason="Confirmed server build auto-assignment")
                        assigned += 1
                    except discord.HTTPException:
                        assignment_failures += 1
                if assigned:
                    assignment_logs.append(f"Assigned **{auto_role.name}** to **{assigned}** existing members")
                if not assignment_failures:
                    completed_assignments.append(auto_role.id)
                    await journal.save()

            sent, failed = await self._post_created_channel_summaries(guild, schema, interaction.user.mention, journal)
            if sent:
                assignment_logs.append(f"Posted channel summaries in **{sent}** channels")
            if assignment_failures or failed:
                raise BuildFinalizationError(f"{assignment_failures} role assignments and {failed} channel summaries need a retry.")

        try:
            protected_channels, protected_roles = await self._cleanup_protections(guild) if clean_existing else (set(), set())
            logs, _ = await build_server(
                guild, schema, progress_msg, clean_existing=clean_existing,
                safe_channel_id=safe_channel_id, finalize=finalize,
                protected_channels=protected_channels, protected_roles=protected_roles,
            )
        except Exception as exc:
            if isinstance(exc, (ValueError, BuildInProgress, BuildFinalizationError)):
                message = str(exc)[:1000]
            else:
                reference = report_error(exc, context=build_label, guild_id=guild.id)
                message = f"The build was interrupted. Error reference: `{reference}`."
            from cogs.json_tools import plan_file
            await progress_msg.edit(
                embed=error_embed("Build needs attention", message + "\nRun the same plan again, or upload the attached JSON to /setup_custom to resume."),
                attachments=[plan_file(schema, 'server-plan.json')],
            )
            return

        result = "\n".join(logs + assignment_logs) or "The plan is ready."
        if len(result) > 3900:
            result = result[:3900] + "\n…"
        await progress_msg.edit(embed=success_embed(f"{build_label} complete", result))
        cleanup = journal_result.state.get('cleanup') if clean_existing else None
        if cleanup and not cleanup['completed']:
            channels, roles = cleanup['channels'], cleanup['roles']
            if not channels and not roles:
                return
            lines = ['Channels', *(f'{identifier}  {name}' for identifier, name in channels),
                     '', 'Roles', *(f'{identifier}  {name}' for identifier, name in roles)]
            await interaction.followup.send(
                embed=warning_embed("Review the old layout", f"The replacement is ready. The attached list identifies the {len(channels)} channels and {len(roles)} roles eligible for deletion. Deleted messages and role assignments cannot be restored."),
                file=discord.File(io.BytesIO("\n".join(lines).encode()), filename='cleanup-preview.txt'),
                view=CleanupConfirmView(self, guild.id, interaction.user.id, journal_result.path, cleanup),
                ephemeral=True,
            )

    # /channel_summaries \u2014 existing server summary in current chat
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="channel_summaries",
        description="Post a small summary of channel purposes in this chat.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def channel_summaries(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await respond(interaction,
                embed=error_embed("Error", "This command can only be used in a server."),
                ephemeral=True,
            )

        lines = self._build_server_summary_lines(interaction.guild)
        if not lines:
            return await respond(interaction,
                embed=info_embed("Channel Summaries", "No text channels found to summarize."),
                ephemeral=True,
            )

        content = "\n".join(lines)
        if len(content) > 3900:
            content = content[:3900] + "\n..."

        em = info_embed("Channel Summaries", content)
        em.set_footer(text="These summaries are generated from channel topics/names.")
        await respond(interaction, embed=em)

    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    # /generate_server \u2014 AI-powered server builder
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="generate_server",
        description="Generate a server template for private review and confirmation.",
    )
    @app_commands.describe(
        theme="Describe the server theme (e.g., 'gaming community', 'study group')",
        preview="Legacy option. All AI builds now require confirmation.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def generate_server(
        self, interaction: discord.Interaction, theme: str, preview: bool = True
    ) -> None:
        if not interaction.guild:
            return
        from cogs.ai import _rate_limited
        if _rate_limited(interaction.user.id):
            return await respond(interaction, embed=error_embed("AI limit", "Try again in a minute."), ephemeral=True)
        try:
            schema = await generate_plan(theme, interaction.user.id)
        except (ValueError, TimeoutError) as exc:
            return await interaction.edit_original_response(embed=error_embed("AI draft unavailable", str(exc) or "AI timed out. Try again."))

        em = self._schema_preview_embed(schema, title="✨ AI server preview", description="Step 2 of 3 · Review the generated plan before creating anything.")
        view = CustomBuildConfirmView(
            self,
            schema,
            owner_id=interaction.user.id,
            command_channel_id=self._safe_command_channel_id(interaction),
            build_label=f"AI build · {theme[:50]}",
        )
        message = await interaction.edit_original_response(embed=em, view=view)
        view.message = message


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerBuilderCog(bot))

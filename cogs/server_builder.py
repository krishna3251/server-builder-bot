"""Server builder cog — /setup_server, /setup_custom, /server_json, /generate_server."""

from __future__ import annotations

import asyncio
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from services import ai_service
from services.embed_service import error_embed, info_embed, success_embed
from services.json_builder import build_server, _validate_schema


class BuilderQuickstartView(discord.ui.View):
    """Interactive help panel that points administrators to existing builder commands."""

    def __init__(self) -> None:
        super().__init__(timeout=120)

    async def _show(self, interaction: discord.Interaction, title: str, description: str) -> None:
        await interaction.response.send_message(
            embed=info_embed(title, description),
            ephemeral=True,
        )

    @discord.ui.button(label="Preset setup", emoji="🧩", style=discord.ButtonStyle.primary)
    async def preset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(
            interaction,
            "Preset server setup",
            "Run `/setup_server` and choose a template from Discord's option list. "
            "Use this for a fast starter layout, then customize roles and channels afterward.",
        )

    @discord.ui.button(label="Custom build", emoji="🛠️", style=discord.ButtonStyle.secondary)
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(
            interaction,
            "Custom server build",
            "Run `/setup_custom` to configure your own structure. Review the preview before "
            "applying changes, and keep a backup of any configuration you need to preserve.",
        )

    @discord.ui.button(label="AI generator", emoji="✨", style=discord.ButtonStyle.success)
    async def ai_builder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(
            interaction,
            "AI server generator",
            "Run `/generate_server` with a clear theme, such as `gaming community` or "
            "`study group`. Enable preview mode to inspect the generated schema first.",
        )

    @discord.ui.button(label="JSON builder", emoji="📄", style=discord.ButtonStyle.secondary)
    async def json_builder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(
            interaction,
            "JSON server builder",
            "Run `/server_json` to build from a structured schema. Review role permissions, "
            "category/channel definitions, and overwrite rules. Start with a small test schema.",
        )

    @discord.ui.button(label="Channel guide", emoji="📚", style=discord.ButtonStyle.secondary)
    async def channel_guide(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(
            interaction,
            "Channel guide",
            "Run `/channel_summaries` to get suggested purposes for the server's text channels. "
            "Summaries use names, existing topics, and common channel patterns.",
        )

class CustomBuildConfirmView(discord.ui.View):
    """Owner-locked preview gate for custom and AI-generated server builds."""

    def __init__(
        self,
        cog: "ServerBuilderCog",
        schema: dict,
        *,
        owner_id: int,
        command_channel_id: int,
        clean_existing: bool = False,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.schema = schema
        self.owner_id = owner_id
        self.command_channel_id = command_channel_id
        self.clean_existing = clean_existing
        self.message: discord.Message | None = None
        if clean_existing:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == "server_builder_confirm":
                    child.label = "Delete & build"
                    child.style = discord.ButtonStyle.danger

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the administrator who started this build can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirm build",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="server_builder_confirm",
        row=0,
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
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
        )
        self.stop()

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
        custom_id="server_builder_cancel",
        row=0,
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=info_embed("Build cancelled", "No server changes were made."),
            view=self,
        )
        self.stop()

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

_GENERATION_PROMPT = (
    "Generate a structured JSON object for a Discord server with the theme: '{theme}'.\n"
    "The JSON MUST follow this exact schema:\n"
    '{{\n'
    '  "server_name": "string",\n'
    '  "roles": [\n'
    '    {{ "name": "string", "color": "named_color_or_#HEX", "hoist": bool, "mentionable": bool, '
    '"permissions": ["permission_name"] }}\n'
    '  ],\n'
    '  "categories": [\n'
    '    {{\n'
    '      "name": "string",\n'
    '      "permission_overwrites": [\n'
    '        {{ "role": "RoleName_or_@everyone", "allow": ["perm"], "deny": ["perm"] }}\n'
    '      ],\n'
    '      "channels": [\n'
    '        {{ "type": "text", "name": "string", "topic": "string", "slowmode": 0, "nsfw": false,\n'
    '           "permission_overwrites": [\n'
    '             {{ "role": "RoleName", "allow": ["send_messages"], "deny": [] }}\n'
    '           ]\n'
    '        }},\n'
    '        {{ "type": "voice", "name": "string", "bitrate": 64000, "user_limit": 0 }}\n'
    '      ]\n'
    '    }}\n'
    '  ],\n'
    '  "auto_assign": "Member"\n'
    '}}\n'
    "IMPORTANT RULES:\n"
    "- For role colors use named colors: red, gold, blue, green, purple, magenta, crimson, emerald, teal, orange, pink, grey, blurple (or #HEX)\n"
    "- Both categories AND individual channels can have permission_overwrites\n"
    "- Use '@everyone' as role name in overwrites to target the default role\n"
    "- INFO/announcement channels: deny send_messages for @everyone, allow only for Admin/Mod\n"
    "- Staff categories: deny read_messages for @everyone, allow only for staff roles\n"
    "- Keep bitrate at 64000-96000 (no higher)\n"
    "Return ONLY valid JSON, no explanation."
)


class ServerBuilderCog(commands.Cog, name="Server Builder"):
    """Server generation — AI-powered or from preset templates."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="builder_panel",
        description="Open the interactive server builder quick-start panel.",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def builder_panel(self, interaction: discord.Interaction) -> None:
        em = info_embed(
            "🧱 Server Builder",
            "Choose a workflow below. Each option explains when to use it and points to the matching command.",
        )
        em.add_field(name="Preset", value="Fast start from a template", inline=True)
        em.add_field(name="Custom", value="Design your own structure", inline=True)
        em.add_field(name="AI / JSON", value="Generate or supply a schema", inline=True)
        em.set_footer(text="Preview first • Check permissions • Avoid destructive cleanup unless intended")
        await interaction.response.send_message(embed=em, view=BuilderQuickstartView(), ephemeral=True)

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

    async def _post_created_channel_summaries(
        self,
        guild: discord.Guild,
        schema: dict,
        actor_mention: str,
    ) -> tuple[int, int]:
        sent = 0
        failed = 0

        for cat in schema.get("categories", []):
            for ch_data in cat.get("channels", []):
                if ch_data.get("type", "text").lower() != "text":
                    continue

                channel_name = ch_data.get("name", "")
                if not channel_name:
                    continue

                channel = discord.utils.get(guild.text_channels, name=channel_name)
                if not channel:
                    failed += 1
                    continue

                if not channel.permissions_for(guild.me).send_messages:
                    failed += 1
                    continue

                summary = self._infer_channel_summary(channel_name, ch_data.get("topic"))
                msg = (
                    f"\U0001f44b Welcome to {channel.mention}!\n"
                    f"\U0001f4dd {summary}\n"
                    f"Created by {actor_mention}."
                )

                try:
                    await channel.send(msg)
                    sent += 1
                except discord.HTTPException:
                    failed += 1

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
        clean_existing="Delete ALL existing channels/roles first (keeps command channel)",
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
    async def setup_server(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
        clean_existing: bool = False,
    ) -> None:
        if not interaction.guild:
            return

        schema = TEMPLATES.get(template.value)
        if not schema:
            return await interaction.response.send_message(
                embed=error_embed("Error", "Template not found."), ephemeral=True
            )

        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        safe_channel_id = interaction.channel_id  # never delete this channel

        # \u2500\u2500 Optionally wipe existing channels/roles \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        if clean_existing:
            status_em = info_embed("Cleaning Server", "Removing existing channels and roles (keeping this channel)...")
            status_msg = await interaction.followup.send(embed=status_em, wait=True)
            for ch in list(guild.channels):
                if ch.id == safe_channel_id:
                    continue  # protect command channel
                try:
                    await ch.delete(reason="Server setup - clean existing")
                except discord.HTTPException:
                    pass
            for role in list(guild.roles):
                if role.is_default() or role.managed or role >= guild.me.top_role:
                    continue
                try:
                    await role.delete(reason="Server setup - clean existing")
                except discord.HTTPException:
                    pass
        else:
            status_msg = None

        # \u2500\u2500 Build from template \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        progress_em = info_embed("Building Server", "Starting...")
        if status_msg:
            try:
                await status_msg.edit(embed=progress_em)
            except discord.NotFound:
                status_msg = None
            progress_msg = status_msg or await interaction.followup.send(embed=progress_em, wait=True)
        else:
            progress_msg = await interaction.followup.send(embed=progress_em, wait=True)

        try:
            logs, role_map = await build_server(guild, schema, progress_msg)

            # \u2500\u2500 Auto-assign roles \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
            assign_logs: list[str] = []

            # Give highest admin role to the person who ran the command
            for r in schema.get("roles", []):
                if "administrator" in r.get("permissions", []):
                    if r["name"] in role_map:
                        try:
                            assert isinstance(interaction.user, discord.Member)
                            await interaction.user.add_roles(
                                role_map[r["name"]], reason="Server setup - owner role"
                            )
                            assign_logs.append(
                                f"Assigned **{r['name']}** to {interaction.user.mention}"
                            )
                        except discord.HTTPException:
                            pass
                    break

            # Give default role to all existing members
            auto_role_name = schema.get("auto_assign")
            if auto_role_name and auto_role_name in role_map:
                default_role = role_map[auto_role_name]
                assigned = 0
                for member in guild.members:
                    if member.bot or member.id == interaction.user.id:
                        continue
                    try:
                        await member.add_roles(default_role, reason="Server setup - auto-assign")
                        assigned += 1
                    except discord.HTTPException:
                        pass
                if assigned:
                    assign_logs.append(
                        f"Assigned **{auto_role_name}** to **{assigned}** existing members"
                    )

            summary_sent, summary_failed = await self._post_created_channel_summaries(
                guild,
                schema,
                interaction.user.mention,
            )
            if summary_sent:
                assign_logs.append(f"Posted channel summary messages in **{summary_sent}** channels")
            if summary_failed:
                assign_logs.append(f"Skipped/failed channel summary messages in **{summary_failed}** channels")

            all_logs = logs + assign_logs
            result = "\n".join(all_logs) if all_logs else "Nothing was created."
            if len(result) > 4000:
                result = result[:4000] + "\n..."
            em = success_embed(f"{template.name} Ready!", result)
            await progress_msg.edit(embed=em)

        except Exception as exc:
            log.exception("Server setup failed: %s", exc)
            em = error_embed(
                "Build Failed",
                f"Build failed. Newly created resources were rolled back where possible; pre-existing resources are not restored.\n`{exc}`",
            )
            await progress_msg.edit(embed=em)

    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
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
            app_commands.Choice(name="Blank Schema (empty)", value="blank"),
            app_commands.Choice(name="Gaming Server", value="gaming"),
            app_commands.Choice(name="Community Server", value="community"),
            app_commands.Choice(name="Study Group", value="study"),
            app_commands.Choice(name="Business / Team", value="business"),
        ]
    )
    async def server_json(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str] | None = None,
    ) -> None:
        choice = template.value if template else "blank"

        if choice == "blank":
            schema = {
                "server_name": "My Awesome Server",
                "roles": [
                    {
                        "name": "Admin",
                        "color": "red",
                        "hoist": True,
                        "mentionable": False,
                        "permissions": ["administrator"],
                    },
                    {
                        "name": "Moderator",
                        "color": "green",
                        "hoist": True,
                        "mentionable": True,
                        "permissions": [
                            "kick_members",
                            "ban_members",
                            "manage_messages",
                            "manage_channels",
                        ],
                    },
                    {
                        "name": "Member",
                        "color": "blue",
                        "hoist": False,
                        "mentionable": False,
                        "permissions": ["send_messages", "read_messages"],
                    },
                ],
                "categories": [
                    {
                        "name": "INFO",
                        "permission_overwrites": [
                            {
                                "role": "@everyone",
                                "allow": ["read_messages"],
                                "deny": ["send_messages"],
                            },
                            {
                                "role": "Admin",
                                "allow": ["send_messages"],
                                "deny": [],
                            },
                        ],
                        "channels": [
                            {
                                "type": "text",
                                "name": "rules",
                                "topic": "Server rules",
                                "slowmode": 0,
                                "nsfw": False,
                            },
                            {
                                "type": "text",
                                "name": "announcements",
                                "topic": "Important updates",
                                "permission_overwrites": [
                                    {
                                        "role": "Admin",
                                        "allow": ["send_messages", "mention_everyone"],
                                        "deny": [],
                                    },
                                    {
                                        "role": "Moderator",
                                        "allow": ["send_messages"],
                                        "deny": [],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "name": "GENERAL",
                        "channels": [
                            {
                                "type": "text",
                                "name": "general-chat",
                                "topic": "Main chat",
                            },
                            {
                                "type": "voice",
                                "name": "Voice Chat",
                                "bitrate": 96000,
                                "user_limit": 0,
                            },
                        ],
                    },
                    {
                        "name": "STAFF ONLY",
                        "permission_overwrites": [
                            {
                                "role": "@everyone",
                                "allow": [],
                                "deny": ["read_messages"],
                            },
                            {
                                "role": "Moderator",
                                "allow": ["read_messages", "send_messages"],
                                "deny": [],
                            },
                            {
                                "role": "Admin",
                                "allow": ["read_messages", "send_messages"],
                                "deny": [],
                            },
                        ],
                        "channels": [
                            {
                                "type": "text",
                                "name": "staff-chat",
                                "topic": "Staff only",
                            },
                        ],
                    },
                ],
                "auto_assign": "Member",
            }
            title = "Server JSON Schema"
        else:
            schema = TEMPLATES.get(choice)
            if not schema:
                return await interaction.response.send_message(
                    embed=error_embed("Error", "Template not found."), ephemeral=True
                )
            title = f"{template.name} Template JSON"  # type: ignore[union-attr]

        text = json.dumps(schema, indent=2, ensure_ascii=False)

        # If it fits in an embed, send it; otherwise send as a .json file
        if len(text) <= 4000:
            em = info_embed(title, f"```json\n{text}\n```")
            em.set_footer(text="Copy this JSON, edit it, and use /setup_custom to build!")
            await interaction.response.send_message(embed=em, ephemeral=True)
        else:
            # Send as attachment
            import io

            file = discord.File(
                io.BytesIO(text.encode("utf-8")),
                filename=f"server_template_{choice}.json",
            )
            em = info_embed(title, "Template is too large for embed \u2014 attached as file.\nEdit and use `/setup_custom` to build!")
            await interaction.response.send_message(embed=em, file=file, ephemeral=True)

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
        clean_existing="Delete ALL existing channels/roles first (keeps command channel)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_custom(
        self,
        interaction: discord.Interaction,
        json_text: str | None = None,
        json_file: discord.Attachment | None = None,
        clean_existing: bool = False,
    ) -> None:
        if not interaction.guild:
            return

        # Get JSON from either text or file
        raw_json: str | None = None
        if json_file:
            if not json_file.filename.endswith(".json"):
                return await interaction.response.send_message(
                    embed=error_embed("Error", "Please upload a `.json` file."),
                    ephemeral=True,
                )
            raw_json = (await json_file.read()).decode("utf-8")
        elif json_text:
            raw_json = json_text
        else:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Error",
                    "Provide either `json_text` or attach a `.json` file.\nUse `/server_json` to get the schema.",
                ),
                ephemeral=True,
            )

        # Parse JSON
        try:
            start = raw_json.find("{")
            end = raw_json.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON object found.")
            schema = json.loads(raw_json[start:end])
        except (json.JSONDecodeError, ValueError) as exc:
            return await interaction.response.send_message(
                embed=error_embed("Invalid JSON", f"Could not parse your JSON:\n`{exc}`"),
                ephemeral=True,
            )

        # Validate the schema before showing a preview or touching server resources.
        if not isinstance(schema, dict):
            return await interaction.response.send_message(
                embed=error_embed("Invalid Schema", "The JSON root must be an object."),
                ephemeral=True,
            )
        if not isinstance(schema.get("roles", []), list) or not isinstance(schema.get("categories", []), list):
            return await interaction.response.send_message(
                embed=error_embed("Invalid Schema", "`roles` and `categories` must be arrays."),
                ephemeral=True,
            )
        if not schema.get("roles") and not schema.get("categories"):
            return await interaction.response.send_message(
                embed=error_embed("Empty Schema", "Add at least one role or category before building."),
                ephemeral=True,
            )
        try:
            _validate_schema(schema)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=error_embed("Invalid Server Plan", str(exc)),
                ephemeral=True,
            )

        categories = schema.get("categories", [])
        channel_count = sum(len(category.get("channels", [])) for category in categories)
        role_names = [str(role.get("name", "Unnamed")) for role in schema.get("roles", [])]
        category_lines = [
            f"• **{str(category.get('name', 'Unnamed'))[:80]}** · {len(category.get('channels', []))} channels"
            for category in categories[:8]
        ]
        if len(categories) > 8:
            category_lines.append(f"…and {len(categories) - 8} more categories")

        if clean_existing:
            preview = warning_embed(
                "Destructive build preview",
                "This plan will remove existing channels and eligible roles before building. "
                "The command channel is preserved. **Deleted content cannot be restored by this bot.**",
            )
        else:
            preview = info_embed(
                "🧱 Server build preview",
                "Review the proposed structure before applying changes. No resources have been created yet.",
            )
        preview.add_field(name="Server name", value=str(schema.get("server_name") or interaction.guild.name)[:100], inline=True)
        preview.add_field(name="New roles", value=str(len(role_names)), inline=True)
        preview.add_field(name="Categories", value=str(len(categories)), inline=True)
        preview.add_field(name="Channels", value=str(channel_count), inline=True)
        preview.add_field(
            name="Roles in this plan",
            value=", ".join(role_names[:12]) + (f" … +{len(role_names) - 12}" if len(role_names) > 12 else "") or "None",
            inline=False,
        )
        preview.add_field(name="Structure", value="\n".join(category_lines) or "No categories", inline=False)
        preview.set_footer(text="Confirm to start • Cancel to leave the server unchanged")

        view = CustomBuildConfirmView(
            self,
            schema,
            owner_id=interaction.user.id,
            command_channel_id=interaction.channel_id,
            clean_existing=clean_existing,
        )
        await interaction.response.send_message(embed=preview, view=view, ephemeral=True)
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
    ) -> None:
        """Execute a custom build only after its preview is explicitly confirmed."""
        guild = interaction.guild
        if guild is None:
            return

        progress_embed = (
            warning_embed(
                "Cleaning server",
                "Removing existing channels and eligible roles as explicitly confirmed. The command channel is kept.",
            )
            if clean_existing
            else info_embed("Building server", "Starting the build…")
        )
        progress_msg = await interaction.followup.send(
            embed=progress_embed,
            ephemeral=True,
            wait=True,
        )

        if clean_existing:
            deleted_channels = 0
            deleted_roles = 0
            for channel in list(guild.channels):
                if channel.id == safe_channel_id:
                    continue
                try:
                    await channel.delete(reason="Custom setup - confirmed clean existing")
                    deleted_channels += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
            bot_member = guild.me
            for role in list(guild.roles):
                if role.is_default() or role.managed or (bot_member and role >= bot_member.top_role):
                    continue
                try:
                    await role.delete(reason="Custom setup - confirmed clean existing")
                    deleted_roles += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
            await progress_msg.edit(
                embed=info_embed(
                    "Cleanup complete",
                    f"Deleted **{deleted_channels}** channels and **{deleted_roles}** eligible roles. Creating the requested structure now.",
                )
            )

        try:
            logs, role_map = await build_server(guild, schema, progress_msg)
            assign_logs: list[str] = []

            for role_data in schema.get("roles", []):
                if "administrator" in role_data.get("permissions", []):
                    role = role_map.get(role_data.get("name", ""))
                    if role and isinstance(interaction.user, discord.Member):
                        try:
                            await interaction.user.add_roles(role, reason="Confirmed server build - owner role")
                            assign_logs.append(f"Assigned **{role.name}** to {interaction.user.mention}")
                        except discord.HTTPException:
                            assign_logs.append(f"Could not automatically assign **{role.name}**")
                    break

            auto_role_name = schema.get("auto_assign")
            auto_role = role_map.get(auto_role_name) if auto_role_name else None
            if auto_role:
                assigned = 0
                for member in guild.members:
                    if member.bot or member.id == interaction.user.id:
                        continue
                    try:
                        await member.add_roles(auto_role, reason="Confirmed server build - auto-assign")
                        assigned += 1
                    except discord.HTTPException:
                        continue
                if assigned:
                    assign_logs.append(f"Assigned **{auto_role.name}** to **{assigned}** existing members")

            sent, failed = await self._post_created_channel_summaries(
                guild, schema, interaction.user.mention
            )
            if sent:
                assign_logs.append(f"Posted channel summaries in **{sent}** channels")
            if failed:
                assign_logs.append(f"Skipped/failed channel summaries in **{failed}** channels")

            result = "\n".join(logs + assign_logs) or "The build completed without additional log messages."
            if len(result) > 3900:
                result = result[:3900] + "\n…"
            em = success_embed("Server build complete", result)
            em.add_field(name="Created roles", value=str(len(role_map)), inline=True)
            em.add_field(name="Categories", value=str(len(schema.get("categories", []))), inline=True)
            em.add_field(
                name="Channels",
                value=str(sum(len(cat.get("channels", [])) for cat in schema.get("categories", []))),
                inline=True,
            )
            await progress_msg.edit(embed=em)
        except Exception as exc:
            log.exception("Confirmed custom setup failed: %s", exc)
            await progress_msg.edit(
                embed=error_embed(
                    "Build failed",
                    "Newly created resources were rolled back where possible. Pre-existing resources removed by cleanup cannot be restored.\n"
                    + f"`{str(exc)[:900]}`",
                )
            )

    # /channel_summaries \u2014 existing server summary in current chat
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="channel_summaries",
        description="Post a small summary of channel purposes in this chat.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def channel_summaries(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message(
                embed=error_embed("Error", "This command can only be used in a server."),
                ephemeral=True,
            )

        lines = self._build_server_summary_lines(interaction.guild)
        if not lines:
            return await interaction.response.send_message(
                embed=info_embed("Channel Summaries", "No text channels found to summarize."),
                ephemeral=True,
            )

        content = "\n".join(lines)
        if len(content) > 3900:
            content = content[:3900] + "\n..."

        em = info_embed("Channel Summaries", content)
        em.set_footer(text="These summaries are generated from channel topics/names.")
        await interaction.response.send_message(embed=em)

    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    # /generate_server \u2014 AI-powered server builder
    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
    @app_commands.command(
        name="generate_server",
        description="Generate a server template with an optional review and confirmation step.",
    )
    @app_commands.describe(
        theme="Describe the server theme (e.g., 'gaming community', 'study group')",
        preview="Show a preview and require confirmation before building",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def generate_server(
        self, interaction: discord.Interaction, theme: str, preview: bool = False
    ) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer(thinking=True, ephemeral=preview)

        # Ask AI for a server schema
        raw = await ai_service.get_ai_response(
            _GENERATION_PROMPT.format(theme=theme), interaction.user.id
        )

        if raw.startswith("\\u26a0\\ufe0f"):
            await interaction.edit_original_response(embed=error_embed("AI Error", raw))
            return

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON object found in response.")
            schema = json.loads(raw[start:end])
            if not isinstance(schema, dict):
                raise ValueError("The generated template root must be a JSON object.")
            _validate_schema(schema)
        except (json.JSONDecodeError, ValueError) as exc:
            await interaction.edit_original_response(
                embed=error_embed(
                    "Invalid generated template",
                    f"The AI response could not be used as a server plan.\\n`{str(exc)[:500]}`",
                )
            )
            return

        if preview:
            preview_text = json.dumps(schema, indent=2, ensure_ascii=False)
            if len(preview_text) > 3400:
                preview_text = preview_text[:3400] + "\\n… (preview truncated)"
            categories = schema.get("categories", [])
            roles = schema.get("roles", [])
            channel_count = sum(len(category.get("channels", [])) for category in categories)
            em = info_embed(
                "✨ AI server build preview",
                "Review the generated structure below. **Nothing has been created yet.**\\n\\n"
                + f"```json\\n{preview_text}\\n```",
            )
            em.add_field(name="Roles", value=str(len(roles)), inline=True)
            em.add_field(name="Categories", value=str(len(categories)), inline=True)
            em.add_field(name="Channels", value=str(channel_count), inline=True)
            em.set_footer(text="Confirm to build • Cancel to leave the server unchanged")
            view = CustomBuildConfirmView(
                self,
                schema,
                owner_id=interaction.user.id,
                command_channel_id=interaction.channel_id,
            )
            message = await interaction.edit_original_response(embed=em, view=view)
            view.message = message
            return

        progress_em = info_embed("🧱 Building server", "Starting the build…")
        progress_msg = await interaction.edit_original_response(embed=progress_em)

        try:
            logs, role_map = await build_server(interaction.guild, schema, progress_msg)

            # Auto-assign roles
            auto_role_name = schema.get("auto_assign")
            if auto_role_name and auto_role_name in role_map:
                for r in schema.get("roles", []):
                    if "administrator" in r.get("permissions", []):
                        if r["name"] in role_map:
                            try:
                                assert isinstance(interaction.user, discord.Member)
                                await interaction.user.add_roles(
                                    role_map[r["name"]], reason="Server build - owner"
                                )
                                logs.append(
                                    f"Assigned **{r['name']}** to {interaction.user.mention}"
                                )
                            except discord.HTTPException:
                                pass
                        break

            summary_sent, summary_failed = await self._post_created_channel_summaries(
                interaction.guild,
                schema,
                interaction.user.mention,
            )
            if summary_sent:
                logs.append(f"Posted channel summary messages in **{summary_sent}** channels")
            if summary_failed:
                logs.append(f"Skipped/failed channel summary messages in **{summary_failed}** channels")

            result = "\n".join(logs) if logs else "Nothing was created."
            if len(result) > 4000:
                result = result[:4000] + "\n..."
            em = success_embed("Server Built!", result)
            await progress_msg.edit(embed=em)
        except Exception as exc:
            log.exception("Server build failed: %s", exc)
            em = error_embed(
                "Build Failed",
                f"Build failed. Newly created resources were rolled back where possible; pre-existing resources are not restored.\n`{exc}`",
            )
            await progress_msg.edit(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerBuilderCog(bot))

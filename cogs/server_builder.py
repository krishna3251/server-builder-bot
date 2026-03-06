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
from services.json_builder import build_server

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
                f"An error occurred and changes were rolled back.\n`{exc}`",
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

        # Validate basic structure
        if not isinstance(schema.get("roles"), list) and not isinstance(
            schema.get("categories"), list
        ):
            return await interaction.response.send_message(
                embed=error_embed(
                    "Invalid Schema",
                    "JSON must have at least `roles` or `categories` array.\nUse `/server_json` to see the correct format.",
                ),
                ephemeral=True,
            )

        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        safe_channel_id = interaction.channel_id

        # Optionally clean
        if clean_existing:
            status_em = info_embed("Cleaning Server", "Removing existing channels and roles (keeping this channel)...")
            status_msg = await interaction.followup.send(embed=status_em, wait=True)
            for ch in list(guild.channels):
                if ch.id == safe_channel_id:
                    continue  # protect command channel
                try:
                    await ch.delete(reason="Custom setup - clean existing")
                except discord.HTTPException:
                    pass
            for role in list(guild.roles):
                if role.is_default() or role.managed or role >= guild.me.top_role:
                    continue
                try:
                    await role.delete(reason="Custom setup - clean existing")
                except discord.HTTPException:
                    pass
        else:
            status_msg = None

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

            # Auto-assign
            assign_logs: list[str] = []
            for r in schema.get("roles", []):
                if "administrator" in r.get("permissions", []):
                    if r["name"] in role_map:
                        try:
                            assert isinstance(interaction.user, discord.Member)
                            await interaction.user.add_roles(
                                role_map[r["name"]], reason="Custom setup - owner"
                            )
                            assign_logs.append(
                                f"Assigned **{r['name']}** to {interaction.user.mention}"
                            )
                        except discord.HTTPException:
                            pass
                    break

            auto_role_name = schema.get("auto_assign")
            if auto_role_name and auto_role_name in role_map:
                default_role = role_map[auto_role_name]
                assigned = 0
                for member in guild.members:
                    if member.bot or member.id == interaction.user.id:
                        continue
                    try:
                        await member.add_roles(default_role, reason="Custom setup - auto-assign")
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
            em = success_embed("Custom Server Ready!", result)
            await progress_msg.edit(embed=em)

        except Exception as exc:
            log.exception("Custom setup failed: %s", exc)
            em = error_embed(
                "Build Failed",
                f"An error occurred and changes were rolled back.\n`{exc}`",
            )
            await progress_msg.edit(embed=em)

    # \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
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
        description="Generate and build a server from an AI-generated template.",
    )
    @app_commands.describe(
        theme="Describe the server theme (e.g., 'gaming community', 'study group')",
        preview="If true, shows the JSON before building",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def generate_server(
        self, interaction: discord.Interaction, theme: str, preview: bool = False
    ) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer(thinking=True)

        # Ask AI for a server schema
        raw = await ai_service.get_ai_response(
            _GENERATION_PROMPT.format(theme=theme), interaction.user.id
        )

        if raw.startswith("\u26a0\ufe0f"):
            em = error_embed("AI Error", raw)
            await interaction.followup.send(embed=em)
            return

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON object found in response.")
            schema = json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            em = error_embed("Parse Error", f"AI returned invalid JSON.\n```\n{raw[:500]}\n```")
            await interaction.followup.send(embed=em)
            return

        if preview:
            preview_text = json.dumps(schema, indent=2)
            if len(preview_text) > 4000:
                preview_text = preview_text[:4000] + "\n..."
            em = info_embed("Server Preview", f"```json\n{preview_text}\n```")
            em.set_footer(text="Building will start in 5 seconds...")
            await interaction.followup.send(embed=em)
            await asyncio.sleep(5)

        progress_em = info_embed("Building Server", "Starting...")
        progress_msg = await interaction.followup.send(embed=progress_em, wait=True)

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
                f"An error occurred and changes were rolled back.\n`{exc}`",
            )
            await progress_msg.edit(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerBuilderCog(bot))

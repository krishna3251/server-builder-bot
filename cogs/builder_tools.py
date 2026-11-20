"""Template navigation, delegated additive builds, and permission comparisons."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.server_builder import CustomBuildConfirmView
from engines.guild.permission_engine import permission_engine
from services.embed_service import error_embed, info_embed
from services.guild_settings import GuildSettings
from services.json_builder import _resolve_permissions, _styled_name, preflight_build
from services.template_service import example_plan, parse_plan, read_plan_attachment
from utils.interactions import acknowledged, respond

TEMPLATE_NAMES = {
    "gaming": "Gaming Server", "community": "Community Server", "study": "Study Group",
    "business": "Business / Team", "minimal": "Minimal JSON example", "creator": "Creator Server",
}
TEMPLATE_CHOICES = [app_commands.Choice(name=title, value=key) for key, title in TEMPLATE_NAMES.items()]


def compare_permissions(guild: discord.Guild, schema: dict) -> list[str]:
    issues = []
    roles = {"@everyone": guild.default_role}
    for entry in schema.get("roles", []):
        names = (entry["name"], _styled_name(entry, schema.get("role_font")))
        matches = [role for role in guild.roles if role.name in names]
        if len(matches) != 1:
            status = "Ambiguous" if matches else "Missing"
            issues.append(f"{status} role: {entry['name']}")
            continue
        role = matches[0]
        roles[entry["name"]] = role
        if role.permissions != _resolve_permissions(entry.get("permissions", [])):
            issues.append(f"Role permissions differ: {entry['name']}")

    def expected_overwrites(specs):
        return {
            spec["role"] if spec["role"].lower() != "@everyone" else "@everyone":
            discord.PermissionOverwrite.from_pair(*(_resolve_permissions(spec.get(key, [])) for key in ("allow", "deny")))
            for spec in specs
        }

    def check_channel(channel, expected):
        for name, overwrite in expected.items():
            target = roles.get(name)
            if target is None:
                issues.append(f"{channel.name}: unresolved overwrite role {name}")
                continue
            current = channel.overwrites_for(target)
            fields = [field for field, value in overwrite if value is not None and getattr(current, field) != value]
            if fields:
                issues.append(f"{channel.name}: overwrite differs for {name} ({', '.join(fields)})")

    font = schema.get("font") or schema.get("name_font") or schema.get("name_style")
    for entry in schema.get("categories", []):
        names = (entry["name"], _styled_name(entry, schema.get("category_font") or font))
        matches = [item for item in guild.categories if item.name in names]
        if len(matches) != 1:
            status = "Ambiguous" if matches else "Missing"
            issues.append(f"{status} category: {entry['name']}")
            continue
        category = matches[0]
        inherited = expected_overwrites(entry.get("permission_overwrites", []))
        check_channel(category, inherited)
        for channel_data in entry.get("channels", []):
            names = (channel_data["name"], _styled_name(channel_data, schema.get("channel_font") or font))
            matches = [item for item in category.channels if item.name in names]
            if len(matches) != 1:
                status = "Ambiguous" if matches else "Missing"
                issues.append(f"{status} channel: {entry['name']} / {channel_data['name']}")
                continue
            explicit = expected_overwrites(channel_data.get("permission_overwrites", []))
            check_channel(matches[0], permission_engine.merge_overwrites(inherited, explicit))
    return issues


class BuilderTools(commands.Cog, name="Builder Tools"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = GuildSettings(str(Path(config.DATA_DIR) / "builder_tools.json"))
        self.delegate_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        await self.settings.load()

    def can_delegate(self, guild: discord.Guild, actor: discord.Member, schema: dict) -> bool:
        return (
            actor.guild_permissions.manage_channels
            and actor.id in self.settings.get(guild.id).get("delegates", [])
            and not schema.get("server_name")
            and not schema.get("roles")
            and schema.keys() <= {"server_name", "roles", "categories"}
            and len(schema.get("categories", [])) <= 5
            and all(
                category.keys() <= {"name", "channels"} and len(category.get("channels", [])) <= 10
                and all(channel.keys() <= {"name", "type"} for channel in category.get("channels", []))
                for category in schema.get("categories", [])
            )
        )

    @app_commands.command(name="server_templates", description="Browse Server Builder presets and starter JSON plans.")
    @acknowledged(ephemeral=True)
    async def server_templates(self, interaction: discord.Interaction) -> None:
        lines = [f"• {name}" for name in TEMPLATE_NAMES.values()]
        await respond(interaction, embed=info_embed("Server templates", "\n".join(
            lines + ["", "Open /template_details to inspect a template or /builder_panel to plan a build."]
        )))

    @app_commands.command(name="template_details", description="Show the category, channel, and role counts for a preset.")
    @app_commands.choices(template=TEMPLATE_CHOICES)
    @acknowledged(ephemeral=True)
    async def template_details(self, interaction: discord.Interaction, template: app_commands.Choice[str]) -> None:
        schema = example_plan(template.value)
        categories = schema.get("categories", [])
        channels = sum(len(category.get("channels", [])) for category in categories)
        roles = len(schema.get("roles", []))
        details = "\n".join(f"• {category['name']}" for category in categories)
        await respond(interaction, embed=info_embed(
            template.name, f"{len(categories)} categories · {channels} channels · {roles} roles\n\n{details}"
        ))

    @app_commands.command(name="example_template", description="Download the ready-to-edit example server JSON.")
    @acknowledged(ephemeral=True)
    async def example_template(self, interaction: discord.Interaction) -> None:
        from cogs.json_tools import plan_file
        await respond(interaction, "Use this JSON with /json_validate or /setup_paste_json.",
                      file=plan_file(example_plan(), "server-example.json"))

    @app_commands.command(name="setup_paste_json", description="Preview and confirm a server layout pasted as JSON.")
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def setup_paste_json(self, interaction: discord.Interaction, json_text: str) -> None:
        guild = interaction.guild
        builder = self.bot.get_cog("Server Builder")
        if builder is None:
            return await respond(interaction, "The server builder is unavailable. Check /diagnostics.")
        try:
            schema = parse_plan(json_text)
            if not interaction.user.guild_permissions.administrator and not self.can_delegate(guild, interaction.user, schema):
                return await respond(interaction, "Administrator permission is required. Approved delegates can submit plans that only add categories and empty channels.")
            preflight_build(guild, schema)
        except ValueError as error:
            return await respond(interaction, embed=error_embed("Review your JSON", str(error)[:2000]))
        view = CustomBuildConfirmView(
            builder, schema, owner_id=interaction.user.id, command_channel_id=interaction.channel_id,
            delegate_check=self.can_delegate,
        )
        view.message = await respond(interaction, embed=view.review_embed(), view=view)

    @app_commands.command(name="perm_sync_check", description="Compare server roles and channel permissions with a JSON plan.")
    @app_commands.choices(template=TEMPLATE_CHOICES)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def perm_sync_check(self, interaction: discord.Interaction, template: app_commands.Choice[str] | None = None,
                              json_text: str | None = None, json_file: discord.Attachment | None = None) -> None:
        if sum(item is not None for item in (template, json_text, json_file)) > 1:
            return await respond(interaction, "Choose one template, pasted plan, or file.")
        try:
            if json_file is not None:
                schema = await read_plan_attachment(json_file)
            elif json_text is not None:
                schema = parse_plan(json_text)
            elif template is not None:
                schema = example_plan(template.value)
            else:
                schema = self.settings.get(interaction.guild_id).get("last_build")
                if schema is None:
                    return await respond(interaction, "Choose a template or include a JSON plan. There is no saved build for this server yet.")
        except ValueError as error:
            return await respond(interaction, embed=error_embed("Review your JSON", str(error)[:2000]))
        issues = compare_permissions(interaction.guild, schema)
        description = "\n".join(f"• {issue}" for issue in issues)
        if len(description) > 3800:
            file = discord.File(io.BytesIO(description.encode()), filename="permission-comparison.txt")
            return await respond(interaction, f"{len(issues)} findings. Download the full comparison.", file=file)
        await respond(interaction, embed=info_embed("Permission comparison",
                                                   description or "All permission fields specified in the plan matched."))

    async def update_delegate(self, interaction: discord.Interaction, member: discord.Member, *, add: bool) -> None:
        guild = interaction.guild
        if interaction.user.id != guild.owner_id:
            return await respond(interaction, "Only this server's owner can manage builder delegates.")
        if add and (member.bot or not member.guild_permissions.manage_channels):
            return await respond(interaction, "Choose a member with Manage Channels.")
        async with self.delegate_lock:
            members = set(self.settings.get(guild.id).get("delegates", []))
            if add:
                members.add(member.id)
            else:
                members.discard(member.id)
            await self.settings.update(guild.id, delegates=sorted(members))
        await respond(interaction, "Builder delegate added." if add else "Builder delegate removed.")

    @app_commands.command(name="builder_bypass_add", description="Server owner: approve a member to run additive channel plans.")
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def builder_bypass_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.update_delegate(interaction, member, add=True)

    @app_commands.command(name="builder_bypass_remove", description="Server owner: revoke a member's additive builder access.")
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def builder_bypass_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.update_delegate(interaction, member, add=False)

    @app_commands.command(name="builder_bypass_list", description="Server owner: list approved builder delegates.")
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def builder_bypass_list(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != interaction.guild.owner_id:
            return await respond(interaction, "Only this server's owner can view builder delegates.")
        members = self.settings.get(interaction.guild_id).get("delegates", [])
        text = "\n".join(f"<@{member}>" for member in members)
        if len(text) > 3700:
            file = discord.File(io.BytesIO(json.dumps(members).encode()), filename="builder-delegates.json")
            return await respond(interaction, f"{len(members)} approved delegates.", file=file)
        await respond(interaction, embed=info_embed("Builder delegates", text or "No approved delegates."),
                      allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BuilderTools(bot))

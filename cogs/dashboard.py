"""Dashboard/admin cog - welcome, auto-role, command channel, mass roles."""

from __future__ import annotations

import io
import logging
import random
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import error_embed, info_embed, success_embed
from services.guild_settings import GuildSettings
from services.quarantine_service import has_quarantine_record
from services.permission_service import bot_can_act, can_assign_role, is_above
from utils.interactions import acknowledged, respond

log = logging.getLogger("cogs.dashboard")

DEFAULT_RULES = [
    "1. Respect everyone.",
    "2. No spam or harassment.",
    "3. Follow Discord Terms of Service.",
]

WELCOME_ASSETS_DIR = Path(config.DATA_DIR) / "welcome_assets"
WELCOME_IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _default_guild_config() -> dict[str, Any]:
    return {
        "welcome": {
            "channel_id": 0,
            "title": "Welcome to {server}!",
            "description": "Glad to have you here, {member}.",
            "footer": "Enjoy your stay.",
            "thumbnail_url": "",
            "banner_url": "",
            "banner_file": "",
            "use_giphy": False,
            "giphy_query": "welcome discord",
        },
        "auto_roles": {
            "human_role_id": 0,
            "bot_role_id": 0,
        },
        "commands_channel_id": 0,
    }


class DashboardCog(commands.Cog, name="Dashboard"):
    """Server dashboard controls exposed as slash commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = GuildSettings(config.DASHBOARD_CONFIG_FILE)

    async def cog_load(self) -> None:
        await self.settings.load()

    def _guild_config(self, guild_id: int) -> dict[str, Any]:
        return {**_default_guild_config(), **self.settings.get(guild_id)}

    async def _random_giphy_url(self, query: str) -> str:
        if not config.GIPHY_API_KEY:
            return ""

        params = {
            "api_key": config.GIPHY_API_KEY,
            "q": query or "welcome discord",
            "limit": 25,
            "rating": "pg",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.giphy.com/v1/gifs/search", params=params, timeout=10) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError):
            return ""

        gifs = data.get("data", [])
        if not gifs:
            return ""
        item = random.choice(gifs)
        return (
            item.get("images", {})
            .get("original", {})
            .get("url", "")
        )

    async def _save_welcome_image(self, guild_id: int, image: discord.Attachment) -> str:
        if image.size > 8 * 1024 * 1024:
            raise ValueError("Upload a welcome image under 8 MiB.")
        content_type = (image.content_type or "").split(";")[0].lower()
        extension = WELCOME_IMAGE_EXTENSIONS.get(content_type)
        if extension is None:
            lower_name = image.filename.lower()
            extension = next(
                (ext for ext in WELCOME_IMAGE_EXTENSIONS.values() if lower_name.endswith(ext)),
                "",
            )
        if not extension:
            raise ValueError("Unsupported image type. Use PNG, JPG, WEBP, or GIF.")

        WELCOME_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        file_path = WELCOME_ASSETS_DIR / f"{guild_id}-{image.id}{extension}"
        await image.save(file_path)
        return file_path.name

    @staticmethod
    def _format_welcome_text(text: str, member: discord.Member) -> str:
        return (
            text.replace("{member}", member.mention)
            .replace("{user}", member.display_name)
            .replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count or 0))
        )

    async def _apply_join_role(self, member: discord.Member, role_id: int, reason: str) -> bool:
        if not role_id or await has_quarantine_record(self.bot, member):
            return False

        role = member.guild.get_role(role_id)
        if not role or not can_assign_role(member.guild, role, automated=True):
            return False

        try:
            await member.add_roles(role, reason=reason)
            return True
        except discord.HTTPException:
            return False

    async def grant_configured_auto_role(self, member: discord.Member) -> None:
        auto_roles = self._guild_config(member.guild.id)["auto_roles"]
        key = "bot_role_id" if member.bot else "human_role_id"
        await self._apply_join_role(member, auto_roles.get(key, 0), "Dashboard auto-role")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild_config = self._guild_config(member.guild.id)
        verification = self.bot.get_cog("Verification")
        if member.bot or verification is None or not verification.is_enabled(member.guild.id):
            await self.grant_configured_auto_role(member)

        welcome = guild_config.get("welcome", {})
        channel_id = welcome.get("channel_id", 0)
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title=self._format_welcome_text(welcome.get("title", "Welcome!"), member)[:256],
            description=self._format_welcome_text(welcome.get("description", ""), member)[:4096],
            colour=config.BOT_COLOR,
        )
        embed.add_field(name="Server Rules", value="\n".join(DEFAULT_RULES), inline=False)

        footer = welcome.get("footer", "")
        if footer:
            embed.set_footer(text=self._format_welcome_text(footer, member)[:2048])

        thumbnail_url = welcome.get("thumbnail_url", "")
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        else:
            embed.set_thumbnail(url=member.display_avatar.url)

        image_url = welcome.get("banner_url", "")
        file: discord.File | None = None
        if welcome.get("use_giphy"):
            image_url = await self._random_giphy_url(welcome.get("giphy_query", "welcome discord")) or image_url
        file_name = Path(welcome.get("banner_file") or "").name
        file_path = WELCOME_ASSETS_DIR / file_name
        stem = file_path.stem.split("-", 1)[0]
        if (not image_url and stem == str(member.guild.id)
                and file_path.suffix.lower() in WELCOME_IMAGE_EXTENSIONS.values()
                and file_path.is_file() and not file_path.is_symlink()):
            file = discord.File(file_path, filename=file_name)
            embed.set_image(url=f"attachment://{file_name}")

        if image_url:
            embed.set_image(url=image_url)

        try:
            await channel.send(content=member.mention, embed=embed, file=file,
                               allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False))
        except discord.HTTPException:
            log.warning("Failed to send welcome message in guild %s", member.guild.id)

    @app_commands.command(name="welcome_setup", description="Configure embedded welcome messages.")
    @app_commands.describe(
        channel="Channel where welcome messages are sent",
        title="Embed title. Supports {member}, {user}, {server}, {count}",
        description="Embed message. Supports {member}, {user}, {server}, {count}",
        footer="Footer text",
        thumbnail_url="Optional thumbnail URL",
        image="Optional uploaded banner/welcome image",
        use_giphy="Use random welcome GIFs from GIPHY",
        giphy_query="Search query for random welcome GIFs",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def welcome_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = "Welcome to {server}!",
        description: str = "Glad to have you here, {member}.",
        footer: str = "Respect everyone and enjoy your stay.",
        thumbnail_url: str = "",
        image: discord.Attachment | None = None,
        use_giphy: bool = False,
        giphy_query: str = "welcome discord",
    ) -> None:
        assert interaction.guild is not None
        current_welcome = self._guild_config(interaction.guild.id).get("welcome", {})
        banner_file = current_welcome.get("banner_file", "")
        if image:
            try:
                banner_file = await self._save_welcome_image(interaction.guild.id, image)
            except (discord.HTTPException, OSError, ValueError):
                return await respond(interaction,
                    embed=error_embed("Image Upload Failed", "Use a PNG, JPG, WEBP, or GIF under 8 MiB."),
                    ephemeral=True,
                )

        welcome = {
            "channel_id": channel.id,
            "title": title[:200],
            "description": description[:3000],
            "footer": footer[:1600],
            "thumbnail_url": thumbnail_url,
            "banner_url": "",
            "banner_file": banner_file,
            "use_giphy": use_giphy,
            "giphy_query": giphy_query,
        }
        await self.settings.update(interaction.guild.id, welcome=welcome)

        embed = success_embed(
            "Welcome System Updated",
            f"Welcome channel: {channel.mention}\n"
            f"GIPHY: **{'enabled' if use_giphy else 'disabled'}**\n"
            "The embed will ping the new member and include 3 short default rules.",
        )
        await respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="autorole_setup", description="Configure separate auto roles for humans and bots.")
    @app_commands.describe(
        human_role="Role given to human members when they join",
        bot_role="Role given to bots when they join",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def autorole_setup(
        self,
        interaction: discord.Interaction,
        human_role: discord.Role | None = None,
        bot_role: discord.Role | None = None,
    ) -> None:
        assert interaction.guild is not None
        if any(role is not None and not can_assign_role(
            interaction.guild, role, actor=interaction.user, automated=True,
        ) for role in (human_role, bot_role)):
            return await respond(interaction, "Choose assignable roles without staff permissions.")
        await self.settings.update(interaction.guild.id, auto_roles={
            "human_role_id": human_role.id if human_role else 0,
            "bot_role_id": bot_role.id if bot_role else 0,
        })

        await respond(interaction,
            embed=success_embed(
                "Auto Roles Updated",
                f"Humans: {human_role.mention if human_role else '**disabled**'}\n"
                f"Bots: {bot_role.mention if bot_role else '**disabled**'}",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="commands_channel_setup", description="Set the dedicated commands channel.")
    @app_commands.describe(channel="Channel for bot commands")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def commands_channel_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild is not None
        await self.settings.update(interaction.guild.id, commands_channel_id=channel.id)
        await respond(interaction,
            embed=success_embed("Commands Channel Set", f"Commands channel: {channel.mention}"),
            ephemeral=True,
        )

    @app_commands.command(name="mass_role", description="Give or remove a role from all, bots, or humans.")
    @app_commands.describe(
        action="Give or remove the role",
        target="Which members should be affected",
        role="Role to give or remove",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Give", value="give"),
            app_commands.Choice(name="Remove", value="remove"),
        ],
        target=[
            app_commands.Choice(name="All Members", value="all"),
            app_commands.Choice(name="Only Humans", value="humans"),
            app_commands.Choice(name="Only Bots", value="bots"),
        ],
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def mass_role(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        target: app_commands.Choice[str],
        role: discord.Role,
    ) -> None:
        assert interaction.guild is not None
        if not can_assign_role(interaction.guild, role, actor=interaction.user):
            return await respond(interaction,
                embed=error_embed("Role Too High", "Move my bot role above that role first."),
                ephemeral=True,
            )

        members = [
            member
            for member in interaction.guild.members
            if target.value == "all"
            or (target.value == "bots" and member.bot)
            or (target.value == "humans" and not member.bot)
        ]

        changed = 0
        failed = 0
        for member in members:
            if not bot_can_act(interaction.guild, member) or not is_above(interaction.user, member):
                failed += 1
                continue
            try:
                if action.value == "give":
                    if role not in member.roles:
                        await member.add_roles(role, reason=f"Mass role by {interaction.user}")
                        changed += 1
                else:
                    if role in member.roles:
                        await member.remove_roles(role, reason=f"Mass role by {interaction.user}")
                        changed += 1
            except discord.HTTPException:
                failed += 1

        await respond(interaction,
            embed=success_embed(
                "Mass Role Complete",
                f"Action: **{action.name}**\nTarget: **{target.name}**\nRole: {role.mention}\n"
                f"Changed: **{changed}**\nFailed/skipped: **{failed}**",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="dashboard_status", description="Show dashboard configuration and server overview.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def dashboard_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_config = self._guild_config(guild.id)
        welcome = guild_config.get("welcome", {})
        auto_roles = guild_config.get("auto_roles", {})

        humans = sum(1 for member in guild.members if not member.bot)
        bots = sum(1 for member in guild.members if member.bot)
        welcome_channel = guild.get_channel(welcome.get("channel_id", 0))
        commands_channel = guild.get_channel(guild_config.get("commands_channel_id", 0))
        human_role = guild.get_role(auto_roles.get("human_role_id", 0))
        bot_role = guild.get_role(auto_roles.get("bot_role_id", 0))

        embed = info_embed(
            "Moderation Dashboard Overview",
            "Clean control surface for welcome, auto-role, mass roles, commands, and moderation.",
        )
        embed.add_field(name="Members", value=f"Humans: **{humans}**\nBots: **{bots}**", inline=True)
        embed.add_field(name="Channels", value=f"Text: **{len(guild.text_channels)}**\nVoice: **{len(guild.voice_channels)}**", inline=True)
        embed.add_field(name="Roles", value=f"Total: **{len(guild.roles)}**", inline=True)
        embed.add_field(name="Welcome", value=welcome_channel.mention if welcome_channel else "Not set", inline=True)
        embed.add_field(name="Commands", value=commands_channel.mention if commands_channel else "Not set", inline=True)
        embed.add_field(
            name="Auto Roles",
            value=f"Humans: {human_role.mention if human_role else 'Off'}\nBots: {bot_role.mention if bot_role else 'Off'}",
            inline=True,
        )
        embed.add_field(name="Command Categories", value="Moderation\nWelcome\nRoles\nTickets\nUtility\nAI", inline=False)
        await respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="send_rules", description="Post a rules embed with an optional image.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def send_rules(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
        title: app_commands.Range[str, 1, 256] = "Server Rules",
        rules: app_commands.Range[str, 1, 4096] = "\n".join(DEFAULT_RULES),
        image: discord.Attachment | None = None,
        footer: app_commands.Range[str, 0, 1600] = "Please follow the rules to keep the server safe.",
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await respond(interaction, "Choose a text channel for the rules.")
        embed = info_embed(title, rules.replace("|", "\n"))
        embed.set_footer(text=footer)
        file = None
        if image:
            extension = WELCOME_IMAGE_EXTENSIONS.get((image.content_type or "").split(";")[0].lower())
            if extension is None or image.size > 8 * 1024 * 1024:
                return await respond(interaction, "Choose a PNG, JPG, WEBP, or GIF under 8 MiB.")
            filename = f"rules{extension}"
            file = discord.File(io.BytesIO(await image.read()), filename=filename)
            embed.set_image(url=f"attachment://{filename}")
        options = {"file": file} if file else {}
        await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **options)
        await respond(interaction, embed=success_embed("Rules posted", f"Posted in {target.mention}."))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DashboardCog(bot))

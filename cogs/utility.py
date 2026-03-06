"""Utility cog — ping, serverinfo, userinfo, avatar, poll, remind, and more."""

from __future__ import annotations

import asyncio
import datetime
import logging
import platform

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import info_embed, success_embed, error_embed
from utils.decorators import guild_only
from utils.helpers import format_number, parse_duration, time_until

log = logging.getLogger("cogs.utility")


class UtilityCog(commands.Cog, name="Utility"):
    """General-purpose utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /ping ─────────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        ws_latency = round(self.bot.latency * 1000, 1)
        em = info_embed("🏓 Pong!", f"WebSocket: **{ws_latency}ms**")
        start = datetime.datetime.now(datetime.timezone.utc)
        await interaction.response.send_message(embed=em)
        end = datetime.datetime.now(datetime.timezone.utc)
        api_latency = round((end - start).total_seconds() * 1000, 1)
        em.description += f"\nAPI: **{api_latency}ms**"
        await interaction.edit_original_response(embed=em)

    # ── /serverinfo ───────────────────────────────────────────────────────
    @app_commands.command(name="serverinfo", description="Display server information.")
    @guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        g = interaction.guild
        assert g is not None
        em = info_embed(g.name, "")
        if g.icon:
            em.set_thumbnail(url=g.icon.url)
        em.add_field(name="Owner", value=str(g.owner), inline=True)
        em.add_field(name="Members", value=format_number(g.member_count or 0), inline=True)
        em.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        em.add_field(name="Text Channels", value=str(len(g.text_channels)), inline=True)
        em.add_field(name="Voice Channels", value=str(len(g.voice_channels)), inline=True)
        em.add_field(name="Boosts", value=str(g.premium_subscription_count), inline=True)
        em.add_field(name="Created", value=f"<t:{int(g.created_at.timestamp())}:R>", inline=True)
        em.add_field(name="ID", value=str(g.id), inline=True)
        await interaction.response.send_message(embed=em)

    # ── /userinfo ─────────────────────────────────────────────────────────
    @app_commands.command(name="userinfo", description="Display information about a user.")
    @app_commands.describe(user="User to inspect")
    @guild_only()
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        target = user or interaction.user
        assert isinstance(target, discord.Member)
        em = info_embed(str(target), "")
        em.set_thumbnail(url=target.display_avatar.url)
        em.add_field(name="ID", value=str(target.id), inline=True)
        em.add_field(name="Nickname", value=target.nick or "None", inline=True)
        em.add_field(name="Top Role", value=target.top_role.mention, inline=True)
        em.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:R>" if target.joined_at else "Unknown", inline=True)
        em.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
        em.add_field(name="Roles", value=str(len(target.roles) - 1), inline=True)
        await interaction.response.send_message(embed=em)

    # ── /roleinfo ─────────────────────────────────────────────────────────
    @app_commands.command(name="roleinfo", description="Display information about a role.")
    @app_commands.describe(role="Role to inspect")
    @guild_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role) -> None:
        em = info_embed(role.name, "")
        em.colour = role.colour
        em.add_field(name="ID", value=str(role.id), inline=True)
        em.add_field(name="Colour", value=str(role.colour), inline=True)
        em.add_field(name="Members", value=str(len(role.members)), inline=True)
        em.add_field(name="Hoisted", value=str(role.hoist), inline=True)
        em.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
        em.add_field(name="Position", value=str(role.position), inline=True)
        em.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
        await interaction.response.send_message(embed=em)

    # ── /avatar ───────────────────────────────────────────────────────────
    @app_commands.command(name="avatar", description="Get a user's avatar.")
    @app_commands.describe(user="User whose avatar to show")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        em = info_embed(f"{target}'s Avatar", "")
        em.set_image(url=target.display_avatar.with_size(1024).url)
        await interaction.response.send_message(embed=em)

    # ── /banner ───────────────────────────────────────────────────────────
    @app_commands.command(name="banner", description="Get a user's banner.")
    @app_commands.describe(user="User whose banner to show")
    async def banner(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        fetched = await self.bot.fetch_user(target.id)
        if fetched.banner:
            em = info_embed(f"{target}'s Banner", "")
            em.set_image(url=fetched.banner.with_size(1024).url)
            await interaction.response.send_message(embed=em)
        else:
            await interaction.response.send_message(embed=error_embed("No Banner", "This user has no banner."), ephemeral=True)

    # ── /invite ───────────────────────────────────────────────────────────
    @app_commands.command(name="invite", description="Get the bot's invite link.")
    async def invite(self, interaction: discord.Interaction) -> None:
        assert self.bot.user is not None
        url = discord.utils.oauth_url(self.bot.user.id, permissions=discord.Permissions(administrator=True))
        em = info_embed("Invite Me!", f"[Click here to invite]({url})")
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ── /uptime ───────────────────────────────────────────────────────────
    @app_commands.command(name="uptime", description="Show how long the bot has been running.")
    async def uptime(self, interaction: discord.Interaction) -> None:
        up = time_until(self.bot.start_time + (datetime.datetime.now(datetime.timezone.utc) - self.bot.start_time) * 2)  # type: ignore
        delta = datetime.datetime.now(datetime.timezone.utc) - self.bot.start_time  # type: ignore
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        em = info_embed("Uptime", f"**{delta.days}d {hours % 24}h {minutes}m {seconds}s**")
        await interaction.response.send_message(embed=em)

    # ── /botinfo ──────────────────────────────────────────────────────────
    @app_commands.command(name="botinfo", description="Show bot statistics.")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        em = info_embed(config.BOT_NAME, "")
        em.add_field(name="Version", value=config.BOT_VERSION, inline=True)
        em.add_field(name="Guilds", value=str(len(self.bot.guilds)), inline=True)
        cmd_count = len(self.bot.tree.get_commands())
        em.add_field(name="Commands", value=str(cmd_count), inline=True)
        em.add_field(name="Python", value=platform.python_version(), inline=True)
        em.add_field(name="discord.py", value=discord.__version__, inline=True)
        await interaction.response.send_message(embed=em)

    # ── /poll ─────────────────────────────────────────────────────────────
    @app_commands.command(name="poll", description="Create a reaction poll.")
    @app_commands.describe(question="The poll question", options="Comma-separated options (max 10)")
    async def poll(self, interaction: discord.Interaction, question: str, options: str) -> None:
        items = [o.strip() for o in options.split(",") if o.strip()]
        if len(items) < 2:
            return await interaction.response.send_message(embed=error_embed("Error", "Provide at least 2 options."), ephemeral=True)
        if len(items) > 10:
            return await interaction.response.send_message(embed=error_embed("Error", "Maximum 10 options."), ephemeral=True)

        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        desc = "\n".join(f"{number_emojis[i]} {item}" for i, item in enumerate(items))
        em = info_embed(f"📊 {question}", desc)
        em.set_footer(text=f"Poll by {interaction.user}")
        await interaction.response.send_message(embed=em)
        msg = await interaction.original_response()
        for i in range(len(items)):
            await msg.add_reaction(number_emojis[i])

    # ── /remind ───────────────────────────────────────────────────────────
    @app_commands.command(name="remind", description="Set a DM reminder.")
    @app_commands.describe(time="When to remind (e.g. 10m, 2h, 1d)", message="Reminder message")
    async def remind(self, interaction: discord.Interaction, time: str, message: str) -> None:
        try:
            td = parse_duration(time)
        except ValueError:
            return await interaction.response.send_message(embed=error_embed("Error", "Invalid time format."), ephemeral=True)

        target_dt = datetime.datetime.now(datetime.timezone.utc) + td
        em = success_embed("Reminder Set", f"I'll remind you <t:{int(target_dt.timestamp())}:R>.")
        await interaction.response.send_message(embed=em, ephemeral=True)

        await asyncio.sleep(td.total_seconds())
        try:
            reminder_em = info_embed("⏰ Reminder", message)
            await interaction.user.send(embed=reminder_em)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UtilityCog(bot))

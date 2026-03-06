"""Leveling cog — XP gain on message, rank card, admin set-xp."""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import level_service
from services.embed_service import info_embed, success_embed, error_embed
from utils.decorators import admin_only, guild_only
from utils.helpers import format_number

log = logging.getLogger("cogs.leveling")

# Per-user XP cooldown tracker: guild_user -> last_xp_time
_xp_cooldowns: dict[str, float] = defaultdict(float)


class LevelingCog(commands.Cog, name="Leveling"):
    """Message-based XP system with level-up notifications."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Passive XP on message ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        key = f"{message.guild.id}_{message.author.id}"
        now = time.monotonic()
        if now - _xp_cooldowns[key] < config.XP_COOLDOWN:
            return
        _xp_cooldowns[key] = now

        amount = random.randint(config.XP_PER_MESSAGE_MIN, config.XP_PER_MESSAGE_MAX)
        new_xp, new_level, leveled_up = await level_service.add_xp(
            message.guild.id, message.author.id, amount
        )

        if leveled_up:
            em = discord.Embed(
                title="🎉 Level Up!",
                description=f"Congratulations {message.author.mention}! You reached **Level {new_level}**!",
                colour=0xFFD700,
            )
            em.set_thumbnail(url=message.author.display_avatar.url)
            try:
                await message.channel.send(embed=em)
            except discord.HTTPException:
                pass

            # Assign milestone role if configured
            role_id = config.LEVEL_ROLES.get(new_level)
            if role_id:
                role = message.guild.get_role(role_id)
                if role and isinstance(message.author, discord.Member):
                    try:
                        await message.author.add_roles(role, reason=f"Reached level {new_level}")
                    except discord.HTTPException:
                        pass

    # ── /rank ─────────────────────────────────────────────────────────────
    @app_commands.command(name="rank", description="View your or another user's rank card.")
    @app_commands.describe(user="User to check (defaults to you)")
    @guild_only()
    async def rank(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        assert interaction.guild is not None
        target = user or interaction.user
        assert isinstance(target, discord.Member)

        stats = await level_service.get_stats(interaction.guild.id, target.id)
        xp = stats["xp"]
        level = stats["level"]
        next_lvl_xp = level_service.xp_for_level(level + 1)

        # ASCII progress bar
        progress = min(xp / max(next_lvl_xp, 1), 1.0)
        bar_filled = int(progress * 20)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)

        em = info_embed(f"{target.display_name}'s Rank", "")
        em.set_thumbnail(url=target.display_avatar.url)
        em.add_field(name="Level", value=str(level), inline=True)
        em.add_field(name="XP", value=f"{format_number(xp)} / {format_number(next_lvl_xp)}", inline=True)
        em.add_field(name="Progress", value=f"`{bar}` {int(progress * 100)}%", inline=False)
        await interaction.response.send_message(embed=em)

    # ── /setxp ────────────────────────────────────────────────────────────
    @app_commands.command(name="setxp", description="Set a user's XP (admin only).")
    @app_commands.describe(user="Target user", xp="XP value to set")
    @admin_only()
    @guild_only()
    async def setxp(self, interaction: discord.Interaction, user: discord.Member, xp: app_commands.Range[int, 0]) -> None:
        assert interaction.guild is not None
        new_level = await level_service.set_xp(interaction.guild.id, user.id, xp)
        em = success_embed("XP Set", f"**{user}** now has **{format_number(xp)}** XP (Level {new_level}).")
        await interaction.response.send_message(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))

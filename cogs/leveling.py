"""Leveling cog — XP gain on message, rank card, admin set-xp."""

from __future__ import annotations

from utils.interactions import acknowledged, respond

import logging
import random
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import level_service, economy_service
from services.embed_service import add_member_context, info_embed, success_embed, progress_bar
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
        amount *= await economy_service.get_xp_multiplier(message.guild.id, message.author.id)
        new_xp, new_level, leveled_up = await level_service.add_xp(
            message.guild.id, message.author.id, amount
        )

        if leveled_up:
            em = info_embed(
                f"🎉 Level {new_level} unlocked!",
                f"Congratulations {message.author.mention}, your activity paid off. Keep going!",
            )
            em.colour = discord.Colour.gold()
            em.set_thumbnail(url=message.author.display_avatar.url)
            em.add_field(name="NEW LEVEL", value=f"**{new_level:,}**", inline=True)
            em.add_field(name="TOTAL XP", value=f"**{format_number(new_xp)}**", inline=True)
            if isinstance(message.author, discord.Member):
                add_member_context(em, message.author)
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
    @acknowledged(ephemeral=False)
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        assert interaction.guild is not None
        target = user or interaction.user
        assert isinstance(target, discord.Member)

        stats = await level_service.get_stats(interaction.guild.id, target.id)
        xp = stats["xp"]
        level = stats["level"]
        next_lvl_xp = level_service.xp_for_level(level + 1)

        # Render a compact profile card using the shared UI primitives.
        current_floor = level_service.xp_for_level(level)
        progress = min(max(0, xp - current_floor) / max(next_lvl_xp - current_floor, 1), 1.0)
        em = info_embed(f"🏅 {target.display_name}'s Rank", "Your activity snapshot for this server.")
        add_member_context(em, target)
        em.add_field(name="LEVEL", value=f"**{level:,}**", inline=True)
        em.add_field(name="TOTAL XP", value=f"**{format_number(xp)}**", inline=True)
        em.add_field(name="NEXT LEVEL", value=f"{format_number(max(0, next_lvl_xp - xp))} XP to go", inline=True)
        em.add_field(name="Progress to next level", value=progress_bar(progress, 1, width=18), inline=False)
        await respond(interaction, embed=em)

    # ── /setxp ────────────────────────────────────────────────────────────
    @app_commands.command(name="setxp", description="Set a user's XP (admin only).")
    @app_commands.describe(user="Target user", xp="XP value to set")
    @admin_only()
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def setxp(self, interaction: discord.Interaction, user: discord.Member, xp: app_commands.Range[int, 0]) -> None:
        assert interaction.guild is not None
        new_level = await level_service.set_xp(interaction.guild.id, user.id, xp)
        em = success_embed("XP Set", f"**{user}** now has **{format_number(xp)}** XP (Level {new_level}).")
        await respond(interaction, embed=em, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))

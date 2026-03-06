"""AI chat cog — /ai, /ai_reset, /ai_persona slash commands."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from services import ai_service
from services.embed_service import error_embed, info_embed, success_embed
from utils.helpers import chunk_text

import config

log = logging.getLogger("cogs.ai")

# ── Per-user rate limiter ─────────────────────────────────────────────────
_user_calls: dict[int, list[float]] = defaultdict(list)


def _rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    window = config.AI_RATE_WINDOW
    calls = _user_calls[user_id]
    # Prune old entries
    _user_calls[user_id] = [t for t in calls if now - t < window]
    if len(_user_calls[user_id]) >= config.AI_RATE_LIMIT:
        return True
    _user_calls[user_id].append(now)
    return False


class AICog(commands.Cog, name="AI"):
    """AI chat with per-user memory and persona switching."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /ai ───────────────────────────────────────────────────────────────
    @app_commands.command(name="ai", description="Chat with the AI assistant.")
    @app_commands.describe(prompt="Your message to the AI")
    async def ai_chat(self, interaction: discord.Interaction, prompt: str) -> None:
        if _rate_limited(interaction.user.id):
            em = error_embed("Rate Limited", "You're sending messages too fast. Please wait a moment.")
            await interaction.response.send_message(embed=em, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        reply = await ai_service.get_ai_response(prompt, interaction.user.id)

        chunks = chunk_text(reply)
        await interaction.followup.send(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(content=chunk)

    # ── /ai_reset ─────────────────────────────────────────────────────────
    @app_commands.command(name="ai_reset", description="Clear your AI conversation memory.")
    async def ai_reset(self, interaction: discord.Interaction) -> None:
        await ai_service.reset_user_memory(interaction.user.id)
        em = success_embed("Memory Cleared", "Your conversation history has been reset.")
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ── /ai_persona ───────────────────────────────────────────────────────
    @app_commands.command(name="ai_persona", description="Switch the AI's personality.")
    @app_commands.describe(set="Persona name: default, mentor, sarcastic, professional, coder")
    @app_commands.choices(
        set=[
            app_commands.Choice(name="Default (helpful assistant)", value="default"),
            app_commands.Choice(name="Mentor (patient teacher)", value="mentor"),
            app_commands.Choice(name="Sarcastic (witty & dry)", value="sarcastic"),
            app_commands.Choice(name="Professional (formal)", value="professional"),
            app_commands.Choice(name="Coder (software engineer)", value="coder"),
        ]
    )
    async def ai_persona(self, interaction: discord.Interaction, set: app_commands.Choice[str]) -> None:
        await ai_service.set_user_persona(interaction.user.id, set.value)
        em = success_embed("Persona Updated", f"AI persona set to **{set.name}**.")
        await interaction.response.send_message(embed=em, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AICog(bot))

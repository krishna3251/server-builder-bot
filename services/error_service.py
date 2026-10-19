"""Correlate private user-facing errors with detailed operator logs."""

from __future__ import annotations

import logging
import uuid
from collections import deque

import discord

from services.embed_service import error_embed

log = logging.getLogger("bot.errors")
recent_errors: deque[dict] = deque(maxlen=20)


def report_error(error: Exception, *, context: str, guild_id: int | None = None) -> str:
    reference = uuid.uuid4().hex[:10]
    recent_errors.append({"reference": reference, "type": type(error).__name__, "context": context[:100], "guild_id": guild_id})
    log.error("Error %s in %s", reference, context, exc_info=(type(error), error, error.__traceback__))
    return reference


async def respond_error(interaction: discord.Interaction, error: Exception, *, context: str) -> None:
    reference = report_error(error, context=context, guild_id=interaction.guild_id)
    embed = error_embed(
        "Something needs attention",
        "The action could not finish. Ask an administrator to check `/diagnostics` "
        f"and the bot logs.\n**Error reference:** `{reference}`",
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver error reference %s: interaction expired", reference)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await respond_error(interaction, error, context=type(self).__name__)


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        await respond_error(interaction, error, context=type(self).__name__)

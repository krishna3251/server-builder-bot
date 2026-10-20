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
    if isinstance(error, discord.app_commands.MissingPermissions):
        description = 'You no longer have the permissions required for this action. Open a new result after your permissions are restored.'
    elif isinstance(error, discord.Forbidden):
        description = "Discord denied the action. Check the bot's channel permissions and role position."
    elif isinstance(error, discord.NotFound):
        description = "The channel, member, message, or interaction no longer exists. Refresh or run the command again."
    elif isinstance(error, (TimeoutError, ConnectionError)):
        description = "A service timed out or disconnected. Try again shortly; repeated failures need an administrator."
    else:
        description = "The action could not finish. Ask an administrator to check /diagnostics and the bot logs."
    embed = error_embed("Action needs attention", f"{description}\n**Error reference:** `{reference}`")
    try:
        if interaction.response.is_done():
            if interaction.response.type == discord.InteractionResponseType.deferred_channel_message:
                await interaction.edit_original_response(content="The action could not finish. Private error details are below.", embed=None, view=None)
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

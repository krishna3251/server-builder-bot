"""Entry point — initialises the bot, loads cogs, and starts the event loop."""

import asyncio
import datetime
import logging
import os
import pathlib
from threading import Thread

import discord
from discord.ext import commands
from flask import Flask

import config
from utils.logger import setup_logging

# ── Keep-alive server for Render free tier ────────────────────────────────
_keep_alive_app = Flask(__name__)


@_keep_alive_app.route("/")
def _health_check():
    return "Bot is alive!", 200


def _run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    _keep_alive_app.run(host="0.0.0.0", port=port, use_reloader=False)


Thread(target=_run_keep_alive, daemon=True).start()
# ─────────────────────────────────────────────────────────────────────────

# ── Ensure data directories exist ────────────────────────────────────────
_DATA_DIRS = [
    "data/memory",
    "data/economy",
    "data/levels",
    "data/tickets",
    "data/templates",
]
for _d in _DATA_DIRS:
    pathlib.Path(_d).mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
setup_logging()
log = logging.getLogger("bot")

# ── Intents ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = False


class ServerBot(commands.Bot):
    """Custom bot subclass with startup hook."""

    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="/help | v" + config.BOT_VERSION
            ),
        )
        self.start_time: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)

    async def setup_hook(self) -> None:
        """Load all cogs dynamically from the cogs/ directory."""
        cog_dir = pathlib.Path("cogs")
        for cog_file in cog_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                log.info("Loaded cog: %s", ext)
            except Exception as exc:
                log.error("Failed to load cog %s: %s", ext, exc)

        # Sync application commands globally
        synced = await self.tree.sync()
        log.info("Synced %d slash commands globally.", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)  # type: ignore[union-attr]
        log.info("Guilds: %d | Latency: %.0fms", len(self.guilds), self.latency * 1000)


bot = ServerBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
) -> None:
    """Global slash-command error handler."""
    from services.embed_service import error_embed

    if isinstance(error, discord.app_commands.CommandOnCooldown):
        em = error_embed("Cooldown", f"Try again in **{error.retry_after:.1f}s**.")
        await interaction.response.send_message(embed=em, ephemeral=True)
    elif isinstance(error, discord.app_commands.MissingPermissions):
        em = error_embed("Missing Permissions", "You lack the required permissions.")
        await interaction.response.send_message(embed=em, ephemeral=True)
    elif isinstance(error, discord.app_commands.CheckFailure):
        em = error_embed("Check Failed", str(error) or "You cannot use this command.")
        await interaction.response.send_message(embed=em, ephemeral=True)
    else:
        log.exception("Unhandled app-command error: %s", error)
        em = error_embed("Error", "An unexpected error occurred. Please try again later.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=em, ephemeral=True)
            else:
                await interaction.response.send_message(embed=em, ephemeral=True)
        except discord.HTTPException:
            pass


async def main() -> None:
    async with bot:
        await asyncio.sleep(3)  # small delay before login
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

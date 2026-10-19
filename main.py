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
from services.error_service import respond_error, report_error

# ── Keep-alive server for Render free tier ────────────────────────────────
_keep_alive_app = Flask(__name__)


@_keep_alive_app.route("/")
def _health_check():
    active_bot = globals().get("bot")
    ready = bool(active_bot and active_bot.is_ready())
    failures = sorted(getattr(active_bot, "extension_errors", {}))
    return {"ready": ready, "failed_extensions": failures}, 200 if ready and not failures else 503


def _run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    _keep_alive_app.run(host="0.0.0.0", port=port, use_reloader=False)



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
            allowed_mentions=discord.AllowedMentions(everyone=False),
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="/help | v" + config.BOT_VERSION
            ),
        )
        self.extension_errors: dict[str, str] = {}
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
                self.extension_errors[ext] = report_error(exc, context=f"Loading {ext}")

        # A sync outage should be visible in logs but must not hide the original
        # connection/startup error by crashing setup_hook without a traceback.
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally.", len(synced))
        except Exception:
            log.exception("Failed to sync application commands globally")

    async def on_error(self, event_method, *args, **kwargs):
        import sys
        error = sys.exc_info()[1]
        if error is not None:
            report_error(error, context=f"Event {event_method}")

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
        em = error_embed("Take a moment", f"Try again in **{error.retry_after:.1f}s**.")
    elif isinstance(error, (discord.app_commands.MissingPermissions, discord.app_commands.BotMissingPermissions)):
        who = "The bot needs" if isinstance(error, discord.app_commands.BotMissingPermissions) else "You need"
        em = error_embed("Permission required", who + ": **" + ", ".join(p.replace("_", " ") for p in error.missing_permissions) + "**.")
    elif isinstance(error, discord.app_commands.CheckFailure):
        em = error_embed("Action unavailable", "Check your server permissions and use this command inside a server.")
    else:
        await respond_error(interaction, getattr(error, "original", error), context="Slash command")
        return
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=em, ephemeral=True)
        else:
            await interaction.response.send_message(embed=em, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver a command permission/cooldown response")


async def main() -> None:
    if not config.DISCORD_TOKEN.strip():
        log.critical(
            "DISCORD_TOKEN is missing. Add it to the hosting environment variables; "
            "the bot will not attempt to log in."
        )
        raise RuntimeError("DISCORD_TOKEN is required to start the bot.")

    Thread(target=_run_keep_alive, daemon=True).start()
    try:
        async with bot:
            await asyncio.sleep(1)
            await bot.start(config.DISCORD_TOKEN.strip())
    except discord.LoginFailure:
        log.critical("Discord rejected DISCORD_TOKEN. Check the token and rotate it if exposed.")
        raise
    except Exception:
        log.exception("Fatal bot startup/runtime error")
        raise


if __name__ == "__main__":
    asyncio.run(main())

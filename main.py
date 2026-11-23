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
from core.startup import StartupManager, StartupState
from utils.logger import setup_logging
from services.error_service import respond_error, report_error

# ── Keep-alive server for Render free tier ────────────────────────────────
_keep_alive_app = Flask(__name__)


@_keep_alive_app.route("/")
@_keep_alive_app.route("/ready")
def _health_check():
    active_bot = globals().get("bot")
    ready = bool(active_bot and active_bot.is_ready())
    failures = sorted(getattr(active_bot, "extension_errors", {}))
    return {"ready": ready, "failed_extensions": failures}, 200 if ready and not failures and not getattr(active_bot, "command_sync_error", None) else 503


@_keep_alive_app.route("/health")
def _process_health():
    return {"status": "alive"}, 200


@_keep_alive_app.route("/status")
def _startup_status():
    status = startup_manager.get_health_status()
    status.pop("last_error", None)
    return status


def _run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    _keep_alive_app.run(host="0.0.0.0", port=port, use_reloader=False)



# ─────────────────────────────────────────────────────────────────────────

# ── Ensure data directories exist ────────────────────────────────────────
_DATA_DIRS = ("memory", "economy", "levels", "tickets", "templates")
for _d in _DATA_DIRS:
    (pathlib.Path(config.DATA_DIR) / _d).mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────
setup_logging()
log = logging.getLogger("bot")

# ── Intents ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = False
intents.moderation = True
intents.voice_states = True


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
        self.command_sync_error: str | None = None
        self.start_time: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)

    async def setup_hook(self) -> None:
        """Load all cogs dynamically from the cogs/ directory."""
        log.info("Release: %s v%s | source=krishna3251/server-builder-bot", config.BOT_NAME, config.BOT_VERSION)
        cog_dir = pathlib.Path("cogs")
        for cog_file in cog_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            if ext in self.extensions:
                continue
            try:
                await self.load_extension(ext)
                self.extension_errors.pop(ext, None)
                log.info("Loaded cog: %s", ext)
            except Exception as exc:
                self.extension_errors[ext] = report_error(exc, context=f"Loading {ext}")

        # A sync outage should be visible in logs but must not hide the original
        # connection/startup error by crashing setup_hook without a traceback.
        try:
            synced = await self.tree.sync()
            self.command_sync_error = None
            log.info("Synced %d slash commands globally.", len(synced))
        except Exception as exc:
            self.command_sync_error = report_error(exc, context="Synchronizing application commands")

    async def on_error(self, event_method, *args, **kwargs):
        import sys
        error = sys.exc_info()[1]
        if error is not None:
            report_error(error, context=f"Event {event_method}")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)  # type: ignore[union-attr]
        log.info("Guilds: %d | Latency: %.0fms", len(self.guilds), self.latency * 1000)


bot = ServerBot()
startup_manager = StartupManager(bot)


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
    token = config.normalize_token(config.DISCORD_TOKEN)
    config.validate_discord_token(token)
    Thread(target=_run_keep_alive, daemon=True).start()
    try:
        await startup_manager.run(token)
        state = startup_manager.state
    finally:
        await startup_manager.shutdown()
    if state in (StartupState.INVALID_TOKEN, StartupState.FATAL_ERROR):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

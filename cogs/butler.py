"""Discord gateway adapter for the server's private butler ledger."""

from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
import io
from pathlib import Path
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.butler_service import ButlerService, day_bounds
from utils.interactions import acknowledged, respond

log = logging.getLogger(__name__)


class ButlerCog(commands.Cog, name="Server Butler"):
    def __init__(self, bot):
        self.bot = bot
        self.service = ButlerService(bot, Path(config.DATA_DIR) / "butler.sqlite3")
        self.worker = None
        self.ready = asyncio.Event()
        self.boot_lock = asyncio.Lock()
        self.last_audit = 0.0
        self.last_heartbeat = 0.0

    async def cog_load(self):
        if self.service.settings.enabled:
            self.worker = asyncio.create_task(self.run(), name="server-butler")

    async def cog_unload(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.ready.is_set():
            await self.service.store.pause()
        self.ready.clear()

    async def boot(self):
        if (
            not self.service.settings.enabled
            or self.boot_lock.locked()
            or self.ready.is_set()
        ):
            return
        async with self.boot_lock:
            self.ready.clear()
            for guild in self.bot.guilds:
                if guild.unavailable:
                    continue
                try:
                    await self.service.begin(guild)
                except Exception as exc:
                    self.service.last_error = (
                        f"Collection initialization failed: {type(exc).__name__}"
                    )
                    log.warning(
                        "Butler initialization failed for guild %s: %s",
                        guild.id,
                        type(exc).__name__,
                    )
            self.ready.set()

    async def run(self):
        await self.bot.wait_until_ready()
        await self.boot()
        while True:
            try:
                if self.bot.is_ready() and self.ready.is_set():
                    now = self.service.clock()
                    if now - self.last_heartbeat >= 60:
                        for guild in self.bot.guilds:
                            if (
                                guild.id in self.service.guilds
                                and not guild.unavailable
                            ):
                                await self.service.store.heartbeat(
                                    guild.id, self.service.voices(guild)
                                )
                            elif (
                                self.service.enabled(guild.id) and not guild.unavailable
                            ):
                                await self.service.begin(guild)
                        self.last_heartbeat = now
                    if now - self.last_audit >= 900:
                        start, _ = day_bounds(self.service.today(), self.service.zone)
                        for guild in self.bot.guilds:
                            if (
                                guild.id in self.service.guilds
                                and not guild.unavailable
                            ):
                                await self.service.collect_audit(guild, start)
                        self.last_audit = now
                    await self.service.schedule()
                    await self.service.deliver()
            except Exception as exc:
                self.service.last_error = f"Butler worker failed: {type(exc).__name__}"
                log.warning("Butler worker failed: %s", type(exc).__name__)
            await asyncio.sleep(10)

    async def usable(self, guild_id):
        if not self.service.enabled(guild_id):
            return False
        await self.ready.wait()
        return guild_id in self.service.guilds and self.bot.is_ready()

    @commands.Cog.listener()
    async def on_message(self, message):
        # No ordinary messages or DMs enter storage.
        if (
            message.guild
            and message.mention_everyone
            and await self.usable(message.guild.id)
        ):
            await self.service.mention(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        content = payload.data.get("content", "")
        if not payload.guild_id or not ("@everyone" in content or "@here" in content):
            return
        if not await self.usable(payload.guild_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            await self.service.mention(message)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if await self.usable(member.guild.id):
            await self.service.store.member(
                member.guild.id,
                member.id,
                member.display_name,
                "join",
                member.joined_at.timestamp() if member.joined_at else None,
            )

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload):
        if await self.usable(payload.guild_id):
            user = payload.user
            await self.service.store.member(
                payload.guild_id, user.id, str(user), "leave"
            )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        old = getattr(before.channel, "id", None)
        new = getattr(after.channel, "id", None)
        if old != new and await self.usable(member.guild.id):
            await self.service.store.voice_change(
                member.guild.id, member.id, member.display_name, old, new
            )

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        if await self.usable(entry.guild.id):
            await self.service.audit(entry)

    @commands.Cog.listener()
    async def on_disconnect(self):
        if self.ready.is_set():
            self.ready.clear()
            await self.service.store.pause()

    @commands.Cog.listener()
    async def on_resumed(self):
        await self.boot()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.worker and not self.ready.is_set():
            await self.boot()

    @commands.Cog.listener()
    async def on_guild_available(self, guild):
        if self.ready.is_set() and self.service.enabled(guild.id):
            await self.service.begin(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if self.ready.is_set() and self.service.enabled(guild.id):
            await self.service.begin(guild)

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild):
        if guild.id in self.service.guilds:
            await self.service.store.pause(guild.id)
            self.service.guilds.discard(guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        if guild.id in self.service.guilds:
            await self.service.store.retire(guild.id)
            self.service.guilds.discard(guild.id)

    butler = app_commands.Group(
        name="butler",
        description="Server housekeeping, voice ledger and private owner reports",
    )

    @butler.command(
        name="status",
        description="Administrators: check collection, anti-ping policy and report delivery",
    )
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def status(self, interaction: discord.Interaction):
        if not interaction.guild or not (
            interaction.user.guild_permissions.administrator
            or await self.service.is_owner(interaction.user)
        ):
            return await respond(
                interaction,
                "Administrator or bot-owner access is required.",
                ephemeral=True,
            )
        settings = self.service.settings
        perms = (
            interaction.guild.me.guild_permissions
            if interaction.guild.me
            else discord.Permissions.none()
        )
        text = (
            f"Butler enabled here: {self.service.enabled(interaction.guild_id)}\n"
            f"Collector ready: {self.ready.is_set() and interaction.guild_id in self.service.guilds}\n"
            "Mass mentions: one message per member/server every three hours; administrators and the owner are included.\n"
            f"Daily ledger: {settings.report_hour:02}:{settings.report_minute:02} {settings.timezone_name} for the previous day.\n"
            f"Retention: {settings.retention_days} days. First-start report: today's available data.\n"
            f"Manage Messages: {perms.manage_messages} · View Audit Log: {perms.view_audit_log}\n"
            f"Members intent: {self.bot.intents.members} · Voice states intent: {self.bot.intents.voice_states}\n"
            f"Last issue: {self.service.last_error or 'none recorded'}\n"
            "Reports go privately to the bot owner. No normal message bodies or voice audio are collected."
        )
        await respond(interaction, text, ephemeral=True)

    @butler.command(
        name="report",
        description="Bot owner: privately export today or a retained date for this server",
    )
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def report(self, interaction: discord.Interaction, date: str | None = None):
        if not interaction.guild or not await self.service.is_owner(interaction.user):
            return await respond(
                interaction,
                "Only the bot owner can open the private activity ledger.",
                ephemeral=True,
            )
        if not self.service.enabled(interaction.guild_id):
            return await respond(
                interaction, "The butler is disabled for this server.", ephemeral=True
            )
        if not self.ready.is_set() or interaction.guild_id not in self.service.guilds:
            return await respond(
                interaction,
                "The collector is not ready for this server yet. Check /butler status.",
                ephemeral=True,
            )
        try:
            day = (
                datetime.strptime(date, "%Y-%m-%d").date()
                if date
                else self.service.today()
            )
        except ValueError:
            return await respond(
                interaction, "Use a date in YYYY-MM-DD format.", ephemeral=True
            )
        if (
            not self.service.today()
            - timedelta(days=self.service.settings.retention_days)
            <= day
            <= self.service.today()
        ):
            return await respond(
                interaction,
                "Choose today or a date inside the configured retention period.",
                ephemeral=True,
            )
        embed, text = await self.service.report(interaction.guild_id, day)
        await respond(
            interaction,
            embed=embed,
            file=discord.File(
                io.BytesIO(text.encode()),
                filename=f"server-{interaction.guild_id}-{day}.txt",
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(ButlerCog(bot))

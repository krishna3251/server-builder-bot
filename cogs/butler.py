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
from services.automatic_moderation import privileged_spam_mode
from utils.interactions import acknowledged, respond
from services.error_service import SafeView

log = logging.getLogger(__name__)


def can_configure(interaction):
    return bool(interaction.guild and (
        interaction.user.id == interaction.guild.owner_id
        or interaction.user.guild_permissions.administrator
    ))


class ClearExceptions(SafeView):
    def __init__(self, service, guild_id, actor_id):
        super().__init__(timeout=90)
        self.service, self.guild_id, self.actor_id = service, guild_id, actor_id
        self.used = False

    async def interaction_check(self, interaction):
        if interaction.guild_id == self.guild_id and interaction.user.id == self.actor_id and can_configure(interaction):
            return True
        await interaction.response.send_message("These controls require the initiating administrator.", ephemeral=True)
        return False

    @discord.ui.button(label="Clear exceptions", style=discord.ButtonStyle.danger)
    async def clear(self, interaction, button):
        if self.used:
            return await interaction.response.send_message("This confirmation was already used.", ephemeral=True)
        self.used = True
        await interaction.response.defer(ephemeral=True)
        await self.service.policy(self.guild_id, lambda policy: policy.update(exceptions=[]))
        self.stop()
        await interaction.edit_original_response(content="Butler channel exceptions cleared.", view=None)

    @discord.ui.button(label="Cancel")
    async def cancel(self, interaction, button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


class ButlerCog(commands.Cog, name="Server Butler"):
    def __init__(self, bot):
        self.bot = bot
        self.service = ButlerService(bot, Path(config.DATA_DIR) / "butler.sqlite3")
        self.worker = None
        self.ready = asyncio.Event()
        self.boot_lock = asyncio.Lock()
        self.ledger_ready = False
        self.last_audit = 0.0
        self.last_heartbeat = 0.0

    async def cog_load(self):
        if self.service.settings.enabled:
            self.worker = asyncio.create_task(self.run(), name="server-butler")

    async def cog_unload(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.ledger_ready and self.ready.is_set():
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
            self.ledger_ready = False
            try:
                database = await self.service.store.health()
            except Exception as exc:
                self.service.last_error = f"Ledger unavailable: {type(exc).__name__}"
                log.error("Butler startup ledger check failed: %s", type(exc).__name__)
                self.ready.set()
                return
            if database != "ok":
                self.service.last_error = "Ledger integrity check failed"
                log.error("Butler startup ledger integrity check failed")
                self.ready.set()
                return
            self.ledger_ready = True
            for guild in self.bot.guilds:
                if guild.unavailable:
                    continue
                try:
                    await self.service.begin(guild)
                    policy = await self.service.policy(guild.id)
                    health = await self.service.store.delivery_health(guild.id)
                    missing = sum(guild.get_channel(identifier) is None for identifier in policy["exceptions"])
                    log.info("Butler startup guild=%s mode=%s cooldown=%ss exceptions=%s missing=%s delivery=%s",
                             guild.id, policy["mode"], policy["seconds"], len(policy["exceptions"]), missing, health)
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
                if self.bot.is_ready() and self.ready.is_set() and self.ledger_ready:
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
        return self.ledger_ready and guild_id in self.service.guilds and self.bot.is_ready()

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
        flag = payload.data.get("mention_everyone")
        if not payload.guild_id or flag is False or (
            not isinstance(content, str) or not ("@everyone" in content or "@here" in content)
        ):
            return
        if not await self.usable(payload.guild_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        if await self.service.exempt(payload.guild_id, channel):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            await self.service.mention(message)
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            log.warning("Butler could not inspect edited message guild=%s message=%s HTTP=%s",
                        payload.guild_id, payload.message_id, exc.status)

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
            try:
                await self.service.audit(entry)
            except (AttributeError, ValueError, TypeError):
                log.warning("Skipped malformed Discord audit metadata in guild %s", entry.guild.id)

    @commands.Cog.listener()
    async def on_disconnect(self):
        if self.ready.is_set():
            self.ready.clear()
            if self.ledger_ready:
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
        if self.ledger_ready and self.ready.is_set() and self.service.enabled(guild.id):
            await self.service.begin(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if self.ledger_ready and self.ready.is_set() and self.service.enabled(guild.id):
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

    exception = app_commands.Group(name="exception", description="Manage exempt channels", parent=butler)
    antiping = app_commands.Group(name="antiping", description="Configure mass-mention limits", parent=butler)

    async def _change_exception(self, interaction, channel, add):
        def update(policy):
            ids = set(policy["exceptions"])
            if add:
                ids.add(channel.id)
                if len(ids) > 100:
                    raise ValueError("At most 100 channel exceptions are supported.")
            else:
                ids.discard(channel.id)
            policy["exceptions"] = sorted(ids)

        try:
            await self.service.policy(interaction.guild_id, update)
        except ValueError as exc:
            return await respond(interaction, str(exc), ephemeral=True)
        await respond(interaction, f"Exception {'enabled' if add else 'removed'} for <#{channel.id}>.",
                      allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)

    @exception.command(name="add", description="Exempt a text channel, announcement channel or category")
    @app_commands.check(can_configure)
    @acknowledged(ephemeral=True)
    async def exception_add(self, interaction: discord.Interaction, channel: discord.TextChannel | discord.CategoryChannel):
        await self._change_exception(interaction, channel, True)

    @exception.command(name="remove", description="Remove an exception, including a deleted channel by ID")
    @app_commands.check(can_configure)
    @acknowledged(ephemeral=True)
    async def exception_remove(self, interaction: discord.Interaction,
                               channel: discord.TextChannel | discord.CategoryChannel | None = None,
                               channel_id: str | None = None):
        if (channel is None) == (channel_id is None) or (
            channel_id is not None and (not channel_id.isascii() or not channel_id.isdigit() or len(channel_id) > 20)
        ):
            return await respond(interaction, "Choose a channel or provide its numeric ID.", ephemeral=True)
        from types import SimpleNamespace
        await self._change_exception(interaction, channel or SimpleNamespace(id=int(channel_id)), False)

    @exception.command(name="list", description="List saved Butler channel exceptions")
    @app_commands.check(can_configure)
    @acknowledged(ephemeral=True)
    async def exception_list(self, interaction: discord.Interaction):
        policy = await self.service.policy(interaction.guild_id)
        identifiers = policy["exceptions"]
        text = "\n".join(
            f"<#{identifier}> • {identifier}" if interaction.guild.get_channel(identifier) else f"{identifier} • missing here"
            for identifier in identifiers
        )
        if len(text) > 1800:
            return await respond(interaction, file=discord.File(io.BytesIO(text.encode()), filename="butler-exceptions.txt"),
                                 ephemeral=True)
        await respond(interaction, text or "No channel exceptions saved.", ephemeral=True,
                      allowed_mentions=discord.AllowedMentions.none())

    @exception.command(name="clear", description="Preview clearing all Butler exceptions")
    @app_commands.check(can_configure)
    @acknowledged(ephemeral=True)
    async def exception_clear(self, interaction: discord.Interaction):
        await respond(interaction, "Clear all Butler channel exceptions for this server?",
                      ephemeral=True, view=ClearExceptions(self.service, interaction.guild_id, interaction.user.id))

    @antiping.command(name="mode", description="Choose how Butler handles repeat mass mentions")
    @app_commands.check(can_configure)
    @app_commands.choices(mode=[app_commands.Choice(name=label, value=value) for label, value in (
        ("Delete", "delete"), ("Warn only", "warn"), ("Disabled", "disabled"),
    )])
    @acknowledged(ephemeral=True)
    async def antiping_mode(self, interaction: discord.Interaction, mode: str):
        await self.service.policy(interaction.guild_id, lambda policy: policy.update(mode=mode))
        await respond(interaction, f"Butler anti-ping mode set to {mode}.", ephemeral=True)

    @antiping.command(name="cooldown", description="Set the time between allowed mass mentions")
    @app_commands.check(can_configure)
    @acknowledged(ephemeral=True)
    async def antiping_cooldown(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440]):
        await self.service.policy(interaction.guild_id, lambda policy: policy.update(seconds=minutes * 60))
        await respond(interaction, f"Mass-mention cooldown set to {minutes} minutes.", ephemeral=True)

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
        if self.ready.is_set() and not self.ledger_ready:
            return await respond(interaction, self.service.last_error or "Butler ledger is unavailable.", ephemeral=True)
        policy = await self.service.policy(interaction.guild_id)
        health = await self.service.store.delivery_health(interaction.guild_id)
        deliveries = ", ".join(f"{status}/{reason or ('legacy unknown' if status == 'failed' else 'none')}: {total}"
                               for status, reason, total in health) or "empty"
        ids = policy["exceptions"]
        missing = [identifier for identifier in ids if interaction.guild.get_channel(identifier) is None]
        perms = (
            interaction.guild.me.guild_permissions
            if interaction.guild.me
            else discord.Permissions.none()
        )
        text = (
            f"Butler enabled here: {self.service.enabled(interaction.guild_id)}\n"
            f"Collector ready: {self.ready.is_set() and interaction.guild_id in self.service.guilds}\n"
            f"Mass mentions: {policy['mode']} • one per user/server every {policy['seconds'] // 60} minutes.\n"
            f"Exempt channels: {', '.join(str(identifier) for identifier in ids[:10]) or 'none'}"
            f"{' … See /butler exception list.' if len(ids) > 10 else ''} • Missing here: {len(missing)}\n"
            f"Delivery health: {deliveries[:520]}\n"
            f"Privileged spam mode: {privileged_spam_mode()} (alert keeps messages).\n"
            f"Daily ledger: {settings.report_hour:02}:{settings.report_minute:02} {settings.timezone_name} for the previous day.\n"
            f"Retention: {settings.retention_days} days. First-start report: today's available data.\n"
            f"Manage Messages: {perms.manage_messages} · View Audit Log: {perms.view_audit_log}\n"
            f"Members intent: {self.bot.intents.members} · Voice states intent: {self.bot.intents.voice_states}\n"
            f"Last issue: {self.service.last_error or 'none recorded'}\n"
            "Reports go privately to the server owner and bot application owner. "
            "No normal message bodies or voice audio are collected."
        )
        await respond(interaction, text, ephemeral=True)

    @butler.command(
        name="report",
        description="Owners: privately export today or a retained date for this server",
    )
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def report(self, interaction: discord.Interaction, date: str | None = None):
        if not interaction.guild or not await self.service.can_read_report(interaction.guild, interaction.user):
            return await respond(
                interaction,
                "This private activity report is available to the server owner, bot application owner or configured report operator.",
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

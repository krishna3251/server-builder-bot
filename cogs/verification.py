"""Advanced verification system with persistent buttons and server-builder hooks."""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.interaction import safe_defer, safe_followup, safe_send
from services.embed_service import error_embed, info_embed, success_embed, warning_embed
from services.guild_settings import GuildSettings
from services.quarantine_service import has_quarantine_record
from services.permission_service import bot_can_act, can_assign_role
from utils.interactions import acknowledged

log = logging.getLogger("cogs.verification")

VERIFY_CUSTOM_ID = "damu_verification:verify"
DEFAULT_TITLE = "Verify To Enter"
DEFAULT_DESCRIPTION = (
    "Welcome to **{server}**.\n"
    "Read the rules, then press the button below to unlock the community."
)
DEFAULT_BUTTON_TEXT = "Verify"
DEFAULT_ACCOUNT_AGE_DAYS = 7
VERIFY_COOLDOWN_SECONDS = 8


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "channel_id": 0,
        "verified_role_id": 0,
        "unverified_role_id": 0,
        "log_channel_id": 0,
        "embed_title": DEFAULT_TITLE,
        "embed_description": DEFAULT_DESCRIPTION,
        "button_text": DEFAULT_BUTTON_TEXT,
        "account_age_check": False,
        "min_account_age_days": DEFAULT_ACCOUNT_AGE_DAYS,
        "captcha_enabled": False,
        "message_id": 0,
    }


class VerificationView(discord.ui.View):
    def __init__(self, button_text: str = DEFAULT_BUTTON_TEXT) -> None:
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == VERIFY_CUSTOM_ID:
                item.label = button_text[:80] or DEFAULT_BUTTON_TEXT

    @discord.ui.button(
        label=DEFAULT_BUTTON_TEXT,
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=VERIFY_CUSTOM_ID,
    )
    async def verify(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("Verification")
        if not isinstance(cog, VerificationCog):
            await safe_send(
                interaction,
                "Verification is not ready yet. Please try again in a moment.",
                ephemeral=True,
            )
            return
        await cog.handle_verify(interaction)


class VerificationCog(commands.Cog, name="Verification"):
    """Professional member verification with persistent button state."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = GuildSettings(config.VERIFICATION_CONFIG_FILE)
        self._cooldowns: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        await self.settings.load()
        self.bot.add_view(VerificationView())

    def _guild_config(self, guild_id: int) -> dict[str, Any]:
        return {**_default_config(), **self.settings.get(guild_id)}

    def is_enabled(self, guild_id: int) -> bool:
        return self.settings.get(guild_id).get("enabled") is True

    async def _log_event(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        *,
        success: bool = True,
    ) -> None:
        guild_config = self._guild_config(guild.id)
        channel = guild.get_channel(guild_config.get("log_channel_id", 0))
        if not isinstance(channel, discord.TextChannel):
            return

        embed = success_embed(title, description) if success else warning_embed(title, description)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("Failed to send verification log in guild %s", guild.id)

    @staticmethod
    def _format_text(text: str, guild: discord.Guild) -> str:
        return text.replace("{server}", guild.name).replace("{count}", str(guild.member_count or 0))

    def _verification_embed(self, guild: discord.Guild, guild_config: dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"🛡️ {self._format_text(guild_config.get('embed_title', DEFAULT_TITLE), guild)}"[:256],
            description=self._format_text(
                guild_config.get("embed_description", DEFAULT_DESCRIPTION),
                guild,
            )[:4096],
            colour=config.BOT_COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="📌 Quick Rules",
            value="1. Respect everyone.\n2. No spam or harassment.\n3. Follow Discord Terms of Service.",
            inline=False,
        )
        embed.add_field(
            name="✨ Access",
            value=(
                f"Press **{guild_config.get('button_text', DEFAULT_BUTTON_TEXT)}** "
                "to receive the verified role and unlock the server."
            ),
            inline=False,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"{config.BOT_NAME} verification")
        return embed

    async def _send_or_update_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        guild_config: dict[str, Any],
    ) -> int:
        embed = self._verification_embed(guild, guild_config)
        view = VerificationView(guild_config.get("button_text", DEFAULT_BUTTON_TEXT))
        message_id = int(guild_config.get("message_id", 0) or 0)
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
                return message.id
            except discord.HTTPException:
                pass

        message = await channel.send(embed=embed, view=view)
        return message.id

    @staticmethod
    async def _ensure_role(
        guild: discord.Guild,
        role: discord.Role | None,
        name: str,
        colour: discord.Colour,
    ) -> discord.Role:
        if role:
            return role
        existing = discord.utils.get(guild.roles, name=name)
        if existing:
            return existing
        return await guild.create_role(name=name, colour=colour, reason="Verification auto setup")

    @staticmethod
    async def _ensure_category(guild: discord.Guild) -> discord.CategoryChannel:
        existing = discord.utils.get(guild.categories, name="Verification")
        if existing:
            return existing
        return await guild.create_category(name="Verification", reason="Verification auto setup")

    @staticmethod
    async def _ensure_channel(
        guild: discord.Guild,
        category: discord.CategoryChannel,
        channel: discord.TextChannel | None,
    ) -> discord.TextChannel:
        if channel:
            return channel
        existing = discord.utils.get(guild.text_channels, name="verify-here")
        if existing:
            return existing
        return await guild.create_text_channel(
            name="verify-here",
            category=category,
            topic="Verify here to unlock the server.",
            reason="Verification auto setup",
        )

    async def _apply_permissions(
        self,
        guild: discord.Guild,
        verification_channel: discord.TextChannel,
        verified_role: discord.Role,
        unverified_role: discord.Role,
    ) -> list[str]:
        logs: list[str] = []
        everyone = guild.default_role

        public_channels = [
            channel for channel in guild.channels
            if channel.id != verification_channel.id and channel.permissions_for(everyone).view_channel
        ]
        for channel in public_channels:
            targets = ((everyone, False), (unverified_role, False), (verified_role, True))
            try:
                for role, visible in targets:
                    overwrite = channel.overwrites_for(role)
                    overwrite.view_channel = visible
                    await channel.set_permissions(role, overwrite=overwrite, reason="Verification access")
            except discord.HTTPException:
                logs.append(f"Could not update verification access for {channel.name}")

        try:
            for role, visible in ((everyone, False), (unverified_role, True), (verified_role, False)):
                overwrite = verification_channel.overwrites_for(role)
                overwrite.view_channel = visible
                if role == unverified_role:
                    overwrite.read_message_history = True
                    overwrite.send_messages = False
                await verification_channel.set_permissions(role, overwrite=overwrite, reason="Verification channel")
        except discord.HTTPException:
            logs.append("Could not fully update verification channel permissions")

        return logs

    async def _mark_existing_members_verified(
        self, guild: discord.Guild,
        verified_role: discord.Role,
        unverified_role: discord.Role,
    ) -> tuple[int, int]:
        assigned = 0
        failed = 0
        for member in guild.members:
            if (member.bot or not bot_can_act(guild, member)
                    or verified_role in member.roles or unverified_role in member.roles
                    or await has_quarantine_record(self.bot, member)):
                continue
            try:
                await member.add_roles(verified_role, reason="Verification setup - existing member")
                assigned += 1
            except discord.HTTPException:
                failed += 1
        return assigned, failed

    async def setup_verification_system(
        self,
        guild: discord.Guild,
        *,
        verification_channel: discord.TextChannel | None = None,
        verified_role: discord.Role | None = None,
        unverified_role: discord.Role | None = None,
        log_channel: discord.TextChannel | None = None,
        auto_create: bool = True,
        embed_title: str = DEFAULT_TITLE,
        embed_description: str = DEFAULT_DESCRIPTION,
        button_text: str = DEFAULT_BUTTON_TEXT,
        account_age_check: bool = False,
        min_account_age_days: int = DEFAULT_ACCOUNT_AGE_DAYS,
        lock_public_channels: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        logs: list[str] = []
        if any(role is not None and not can_assign_role(guild, role, automated=True)
               for role in (verified_role, unverified_role)):
            raise ValueError("Choose assignable verification roles without staff permissions.")
        if auto_create:
            category = await self._ensure_category(guild)
            verified_role = await self._ensure_role(
                guild,
                verified_role,
                "Verified",
                discord.Colour.green(),
            )
            unverified_role = await self._ensure_role(
                guild,
                unverified_role,
                "Unverified",
                discord.Colour.dark_grey(),
            )
            verification_channel = await self._ensure_channel(guild, category, verification_channel)
            logs.append("Auto-created or reused verification category, channel, and roles.")

        if not verification_channel or not verified_role or not unverified_role:
            raise ValueError("Provide a verification channel, verified role, and unverified role, or enable auto setup.")

        if verified_role == unverified_role or any(not can_assign_role(guild, role, automated=True)
                for role in (verified_role, unverified_role)):
            raise ValueError("Choose two different assignable roles without staff permissions.")

        if lock_public_channels:
            assigned, failed = await self._mark_existing_members_verified(guild, verified_role, unverified_role)
            logs.append(f"Assigned Verified to {assigned} existing members. {failed} assignments failed.")
            logs.extend(await self._apply_permissions(guild, verification_channel, verified_role, unverified_role))

        guild_config = self._guild_config(guild.id)
        guild_config.update({
            "enabled": True,
            "channel_id": verification_channel.id,
            "verified_role_id": verified_role.id,
            "unverified_role_id": unverified_role.id,
            "log_channel_id": log_channel.id if log_channel else guild_config.get("log_channel_id", 0),
            "embed_title": embed_title or DEFAULT_TITLE,
            "embed_description": embed_description or DEFAULT_DESCRIPTION,
            "button_text": button_text or DEFAULT_BUTTON_TEXT,
            "account_age_check": account_age_check,
            "min_account_age_days": max(0, min_account_age_days),
            "captcha_enabled": False,
        })
        guild_config["message_id"] = await self._send_or_update_panel(
            guild,
            verification_channel,
            guild_config,
        )
        await self.settings.update(guild.id, **guild_config)

        await self._log_event(
            guild,
            "Verification Setup Updated",
            f"Channel: {verification_channel.mention}\nVerified: {verified_role.mention}\nUnverified: {unverified_role.mention}",
        )
        return guild_config, logs

    async def handle_verify(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await safe_send(interaction, "Use this inside a server.", ephemeral=True)
            return

        guild = interaction.guild
        member = interaction.user
        if member.bot:
            await safe_send(interaction, "Bots cannot verify through this panel.", ephemeral=True)
            return

        if await has_quarantine_record(self.bot, member):
            await safe_send(interaction, "Verification is paused while your quarantine record is under review.", ephemeral=True)
            return

        guild_config = self._guild_config(guild.id)
        if not guild_config.get("enabled"):
            await safe_send(interaction, "Verification is not enabled.", ephemeral=True)
            return

        now = time.monotonic()
        key = (guild.id, member.id)
        retry_at = self._cooldowns.get(key, 0)
        if retry_at > now:
            await safe_send(
                interaction,
                f"Please wait **{retry_at - now:.1f}s** before trying again.",
                ephemeral=True,
            )
            return
        self._cooldowns[key] = now + VERIFY_COOLDOWN_SECONDS

        verified_role = guild.get_role(int(guild_config.get("verified_role_id", 0) or 0))
        unverified_role = guild.get_role(int(guild_config.get("unverified_role_id", 0) or 0))
        if (not verified_role or not unverified_role or verified_role == unverified_role
                or not all(can_assign_role(guild, role, automated=True) for role in (verified_role, unverified_role))
                or not bot_can_act(guild, member)):
            await safe_send(
                interaction,
                "Verification roles are unavailable. Please contact staff.",
                ephemeral=True,
            )
            await self._log_event(
                guild,
                "Verification Failed",
                f"{member.mention} could not verify because a role is missing.",
                success=False,
            )
            return

        if verified_role in member.roles and unverified_role not in member.roles:
            await safe_send(interaction, "You are already verified.", ephemeral=True)
            return

        if guild_config.get("account_age_check"):
            min_days = int(guild_config.get("min_account_age_days", DEFAULT_ACCOUNT_AGE_DAYS) or 0)
            account_age = datetime.datetime.now(datetime.timezone.utc) - member.created_at
            if account_age.days < min_days:
                await safe_send(
                    interaction,
                    f"Your account must be at least **{min_days} days** old to verify.",
                    ephemeral=True,
                )
                await self._log_event(
                    guild,
                    "Verification Failed",
                    f"{member.mention} failed account age check. Account age: **{account_age.days} days**.",
                    success=False,
                )
                return

        if not await safe_defer(interaction, ephemeral=True, thinking=True):
            return
        try:
            if verified_role not in member.roles:
                await member.add_roles(verified_role, reason="Member verified")
            if unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason="Member verified")
        except discord.Forbidden:
            await safe_send(
                interaction,
                "I cannot update your roles. Staff must move my bot role higher.",
                ephemeral=True,
            )
            await self._log_event(
                guild,
                "Verification Role Error",
                f"Missing permission or role hierarchy blocked verification for {member.mention}.",
                success=False,
            )
            return
        except discord.HTTPException:
            await safe_send(
                interaction,
                "Verification failed because Discord rejected the role update. Try again shortly.",
                ephemeral=True,
            )
            await self._log_event(
                guild,
                "Verification Role Error",
                f"Discord API error while verifying {member.mention}.",
                success=False,
            )
            return

        await safe_send(
            interaction,
            "✅ Verified successfully. The server is unlocked for you now.",
            ephemeral=True,
        )
        dashboard = self.bot.get_cog("Dashboard")
        if dashboard:
            await dashboard.grant_configured_auto_role(member)
        await self._log_event(
            guild,
            "User Verified",
            f"{member.mention} (`{member.id}`) completed verification.",
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or await has_quarantine_record(self.bot, member):
            return
        guild_config = self._guild_config(member.guild.id)
        if not guild_config.get("enabled"):
            return
        unverified_role = member.guild.get_role(int(guild_config.get("unverified_role_id", 0) or 0))
        verified_role = member.guild.get_role(int(guild_config.get("verified_role_id", 0) or 0))
        if not unverified_role or not verified_role or verified_role in member.roles:
            return
        if not can_assign_role(member.guild, unverified_role, automated=True) or not bot_can_act(member.guild, member):
            await self._log_event(
                member.guild,
                "Verification Role Error",
                f"Could not give Unverified to {member.mention}; role is above my bot role.",
                success=False,
            )
            return
        try:
            await member.add_roles(unverified_role, reason="Verification gate")
        except discord.HTTPException:
            await self._log_event(
                member.guild,
                "Verification Role Error",
                f"Could not give Unverified to {member.mention}.",
                success=False,
            )

    @app_commands.command(name="setupverification", description="Setup or edit the verification system.")
    @app_commands.describe(
        verification_channel="Channel where members verify",
        verified_role="Role given after verification",
        unverified_role="Role given before verification",
        auto_create="Create/reuse Verification category, verify-here channel, and roles automatically",
        embed_title="Verification embed title",
        embed_description="Verification embed description. Supports {server} and {count}",
        button_text="Text to store for the verify button",
        verification_log_channel="Channel for verification logs",
        account_age_check="Block accounts younger than the minimum age",
        min_account_age_days="Minimum account age in days when account-age check is enabled",
        lock_public_channels="Require verification in currently public channels",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def setupverification(
        self,
        interaction: discord.Interaction,
        verification_channel: discord.TextChannel | None = None,
        verified_role: discord.Role | None = None,
        unverified_role: discord.Role | None = None,
        auto_create: bool = True,
        embed_title: str = DEFAULT_TITLE,
        embed_description: str = DEFAULT_DESCRIPTION,
        button_text: str = DEFAULT_BUTTON_TEXT,
        verification_log_channel: discord.TextChannel | None = None,
        account_age_check: bool = False,
        min_account_age_days: app_commands.Range[int, 0, 3650] = DEFAULT_ACCOUNT_AGE_DAYS,
        lock_public_channels: bool = False,
    ) -> None:
        if not interaction.guild:
            return

        if any(role is not None and not can_assign_role(interaction.guild, role, actor=interaction.user,
                                                        automated=True)
               for role in (verified_role, unverified_role)):
            return await safe_followup(interaction, "Choose assignable roles below your highest role.", ephemeral=True)
        try:
            guild_config, logs = await self.setup_verification_system(
                interaction.guild,
                verification_channel=verification_channel,
                verified_role=verified_role,
                unverified_role=unverified_role,
                log_channel=verification_log_channel,
                auto_create=auto_create,
                embed_title=embed_title,
                embed_description=embed_description,
                button_text=button_text,
                account_age_check=account_age_check,
                min_account_age_days=min_account_age_days,
                lock_public_channels=lock_public_channels,
            )
        except (discord.HTTPException, ValueError) as exc:
            detail = "Discord rejected a verification setting. Check the bot's permissions." if isinstance(exc, discord.HTTPException) else str(exc)
            await interaction.followup.send(
                embed=error_embed("Verification Setup Failed", detail),
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(guild_config["channel_id"])
        verified = interaction.guild.get_role(guild_config["verified_role_id"])
        unverified = interaction.guild.get_role(guild_config["unverified_role_id"])
        details = [
            f"Channel: {channel.mention if isinstance(channel, discord.TextChannel) else 'missing'}",
            f"Verified role: {verified.mention if verified else 'missing'}",
            f"Unverified role: {unverified.mention if unverified else 'missing'}",
            f"Account age check: **{'on' if account_age_check else 'off'}**",
        ]
        if logs:
            details.append("")
            details.extend(logs[:6])
        await interaction.followup.send(
            embed=success_embed("Verification System Ready", "\n".join(details)),
            ephemeral=True,
        )

    @app_commands.command(name="verification_status", description="Show verification system configuration.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    @acknowledged(ephemeral=True)
    async def verification_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return

        guild_config = self._guild_config(interaction.guild.id)
        channel = interaction.guild.get_channel(guild_config.get("channel_id", 0))
        verified = interaction.guild.get_role(guild_config.get("verified_role_id", 0))
        unverified = interaction.guild.get_role(guild_config.get("unverified_role_id", 0))
        log_channel = interaction.guild.get_channel(guild_config.get("log_channel_id", 0))
        await safe_followup(interaction,
            embed=info_embed(
                "Verification Status",
                "\n".join([
                    f"Enabled: **{guild_config.get('enabled', False)}**",
                    f"Channel: {channel.mention if isinstance(channel, discord.TextChannel) else 'not set'}",
                    f"Verified: {verified.mention if verified else 'not set'}",
                    f"Unverified: {unverified.mention if unverified else 'not set'}",
                    f"Logs: {log_channel.mention if isinstance(log_channel, discord.TextChannel) else 'not set'}",
                    f"Account age check: **{guild_config.get('account_age_check', False)}**",
                ]),
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))

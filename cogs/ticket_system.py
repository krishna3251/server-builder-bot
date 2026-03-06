# ===== ticket_system.py =====
# Production-ready Discord ticket system cog with persistent views,
# config persistence, blacklist, auto-delete, and staff notifications.

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# ── Constants ─────────────────────────────────────────────────────────────────
EMBED_COLOR: int = 0x5865F2
TICKET_COOLDOWN_SECONDS: int = 300  # 5 minutes
AUTO_DELETE_DELAY: int = 10  # seconds before ticket channel is deleted after close

log = logging.getLogger("cogs.ticket_system")


# ── Config Manager ────────────────────────────────────────────────────────────
class ConfigManager:
    """Handles all JSON-based config persistence for the ticket system."""

    FILE_PATH: str = "ticket_config.json"

    async def load_all(self) -> dict:
        try:
            with open(self.FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    async def save_all(self, data: dict) -> None:
        with open(self.FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    async def get_guild(self, guild_id: int) -> dict:
        data = await self.load_all()
        return data.get(str(guild_id), {})

    async def save_guild(self, guild_id: int, guild_data: dict) -> None:
        data = await self.load_all()
        data[str(guild_id)] = guild_data
        await self.save_all(data)

    async def get_key(self, guild_id: int, key: str, default=None):
        guild_data = await self.get_guild(guild_id)
        return guild_data.get(key, default)

    async def set_key(self, guild_id: int, key: str, value) -> None:
        guild_data = await self.get_guild(guild_id)
        guild_data[key] = value
        await self.save_guild(guild_id, guild_data)


# ── Auto-Delete Manager ──────────────────────────────────────────────────────
class AutoDeleteManager:
    """Manages scheduled auto-deletion tasks for closed ticket channels."""

    def __init__(self) -> None:
        self.pending_deletes: dict[int, asyncio.Task] = {}

    async def schedule_delete(
        self,
        channel: discord.TextChannel,
        delay: int,
        log_channel: Optional[discord.TextChannel],
        transcript_bytes: bytes,
        ticket_name: str,
    ) -> None:
        try:
            await channel.send(
                f"\u23f3 This ticket channel will be automatically deleted in **{delay} seconds**."
            )
            await asyncio.sleep(delay)
            await channel.delete(reason="Ticket closed \u2014 auto-deleted by ticket system.")
            log.info("Auto-deleted ticket channel: %s", ticket_name)
            if channel.id in self.pending_deletes:
                del self.pending_deletes[channel.id]
        except discord.NotFound:
            log.info("Channel %s was already deleted before auto-delete ran.", ticket_name)
        except discord.Forbidden:
            log.error("No permission to delete channel %s.", ticket_name)
        except asyncio.CancelledError:
            log.info("Auto-delete for %s was cancelled.", ticket_name)
            try:
                await channel.send("\U0001f6ab Auto-delete has been cancelled by a moderator.")
            except (discord.NotFound, discord.Forbidden):
                pass


# ── Close Ticket Modal ───────────────────────────────────────────────────────
class CloseTicketModal(discord.ui.Modal, title="Close Ticket"):
    """Modal that asks for a reason before closing a ticket."""

    reason_input = discord.ui.TextInput(
        label="Reason for closing",
        placeholder="Describe why this ticket is being closed...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, cog: "TicketSystem") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        assert isinstance(interaction.channel, discord.TextChannel)
        channel = interaction.channel
        reason = self.reason_input.value or "No reason provided."

        config_data = await self.cog.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "No ticket config found for this server.", ephemeral=True
            )

        # Find the original opener
        open_tickets: dict = config_data.get("open_tickets", {})
        opener_id: Optional[int] = None
        for uid_str, cid in open_tickets.items():
            if cid == channel.id:
                opener_id = int(uid_str)
                break

        # Determine ticket counter from channel name
        ticket_counter_str = channel.name.split("-")[1] if "-" in channel.name else "????"

        # Step 1 \u2014 Send closing embed in channel
        close_embed = discord.Embed(
            title="\U0001f512 Ticket Closed",
            color=0xFF4444,
            timestamp=discord.utils.utcnow(),
        )
        close_embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
        close_embed.add_field(name="Reason", value=reason, inline=False)
        close_embed.add_field(
            name="Closed at",
            value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
            inline=True,
        )
        close_embed.set_footer(
            text=f"This channel will be deleted in {AUTO_DELETE_DELAY} seconds."
        )
        await interaction.response.send_message(embed=close_embed)

        # Step 2 \u2014 Generate transcript
        messages = [msg async for msg in channel.history(limit=200, oldest_first=True)]
        transcript_lines: list[str] = []
        for msg in messages:
            transcript_lines.append(f"[{msg.created_at}] {msg.author}: {msg.content}")
        transcript_text = "\n".join(transcript_lines)
        transcript_bytes = transcript_text.encode("utf-8")

        # Step 3 \u2014 Post to log channel
        log_channel_id = config_data.get("log_channel_id")
        log_channel: Optional[discord.TextChannel] = None
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)  # type: ignore[assignment]

        if log_channel:
            opener_mention = f"<@{opener_id}>" if opener_id else "Unknown"
            log_embed = discord.Embed(
                title=f"\U0001f4c1 Ticket Closed \u2014 #{ticket_counter_str}",
                color=0xFF4444,
                timestamp=discord.utils.utcnow(),
            )
            log_embed.add_field(name="Ticket", value=channel.name, inline=True)
            log_embed.add_field(name="Opened by", value=opener_mention, inline=True)
            log_embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Reason", value=reason, inline=False)
            log_embed.add_field(
                name="Closed at",
                value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
                inline=True,
            )
            transcript_file = discord.File(
                io.BytesIO(transcript_bytes), filename=f"{channel.name}-transcript.txt"
            )
            try:
                await log_channel.send(embed=log_embed, file=transcript_file)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error("Failed to send transcript to log channel: %s", e)
        else:
            if log_channel_id:
                log.warning("Log channel %s not found in guild %s.", log_channel_id, interaction.guild.id)

        # Step 4 \u2014 Lock out the opener
        if opener_id:
            opener_member = interaction.guild.get_member(opener_id)
            if opener_member:
                try:
                    overwrites = channel.overwrites_for(opener_member)
                    overwrites.send_messages = False
                    overwrites.view_channel = True
                    await channel.set_permissions(opener_member, overwrite=overwrites)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.error("Failed to lock permissions for opener: %s", e)

        # Step 5 \u2014 Remove from config
        if opener_id and str(opener_id) in open_tickets:
            del open_tickets[str(opener_id)]
        claimed_tickets: dict = config_data.get("claimed_tickets", {})
        if str(channel.id) in claimed_tickets:
            del claimed_tickets[str(channel.id)]
        config_data["open_tickets"] = open_tickets
        config_data["claimed_tickets"] = claimed_tickets
        await self.cog.config_manager.save_guild(interaction.guild.id, config_data)

        # Step 6 \u2014 Schedule auto-delete
        delay = config_data.get("auto_delete_delay", AUTO_DELETE_DELAY)
        task = asyncio.create_task(
            self.cog.auto_delete_manager.schedule_delete(
                channel, delay, log_channel, transcript_bytes, channel.name
            )
        )
        self.cog.auto_delete_manager.pending_deletes[channel.id] = task


# ── Ticket Control View (Claim / Close) ──────────────────────────────────────
class TicketControlView(discord.ui.View):
    """Persistent view inside each ticket channel with Claim and Close buttons."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="\u2705 Claim Ticket",
        style=discord.ButtonStyle.success,
        custom_id="ticket_claim",
    )
    async def claim_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        assert interaction.guild is not None
        assert isinstance(interaction.channel, discord.TextChannel)

        cog: Optional[TicketSystem] = interaction.client.get_cog("TicketSystem")  # type: ignore[assignment]
        if cog is None:
            return await interaction.response.send_message(
                "Ticket system is not loaded.", ephemeral=True
            )

        config_data = await cog.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "No ticket config found.", ephemeral=True
            )

        # Permission check \u2014 must have support_role or mod_role
        support_role_id = config_data.get("support_role_id")
        mod_role_id = config_data.get("mod_role_id")
        member = interaction.user
        assert isinstance(member, discord.Member)

        has_support = any(r.id == support_role_id for r in member.roles)
        has_mod = any(r.id == mod_role_id for r in member.roles)

        if not has_support and not has_mod:
            return await interaction.response.send_message(
                "Only support staff can claim tickets.", ephemeral=True
            )

        # Already claimed check
        claimed_tickets: dict = config_data.get("claimed_tickets", {})
        channel_id_str = str(interaction.channel.id)
        if channel_id_str in claimed_tickets:
            claimer_id = claimed_tickets[channel_id_str]
            return await interaction.response.send_message(
                f"This ticket was already claimed by <@{claimer_id}>.", ephemeral=True
            )

        # Claim it
        claimed_tickets[channel_id_str] = interaction.user.id
        config_data["claimed_tickets"] = claimed_tickets
        await cog.config_manager.save_guild(interaction.guild.id, config_data)

        claim_embed = discord.Embed(
            title="\u2705 Ticket Claimed",
            color=0x00FF7F,
            timestamp=discord.utils.utcnow(),
        )
        claim_embed.add_field(name="Claimed by", value=interaction.user.mention, inline=True)
        claim_embed.add_field(
            name="Claimed at",
            value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
            inline=True,
        )
        await interaction.channel.send(embed=claim_embed)

        # Attempt to edit the original control embed to add the claimed-by field
        try:
            control_msg_id = config_data.get("control_messages", {}).get(
                str(interaction.channel.id)
            )
            if control_msg_id:
                control_msg = await interaction.channel.fetch_message(control_msg_id)
                if control_msg.embeds:
                    embed = control_msg.embeds[0].copy()
                    embed.add_field(
                        name="Claimed by", value=interaction.user.mention, inline=True
                    )
                    await control_msg.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning("Could not edit control message to add claim info: %s", e)

        await interaction.response.send_message("You have claimed this ticket.", ephemeral=True)
        log.info("Ticket %s claimed by %s", interaction.channel.name, interaction.user)

    @discord.ui.button(
        label="\U0001f512 Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        assert interaction.guild is not None
        assert isinstance(interaction.channel, discord.TextChannel)

        cog: Optional[TicketSystem] = interaction.client.get_cog("TicketSystem")  # type: ignore[assignment]
        if cog is None:
            return await interaction.response.send_message(
                "Ticket system is not loaded.", ephemeral=True
            )

        config_data = await cog.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "No ticket config found.", ephemeral=True
            )

        # Permission check \u2014 support_role, mod_role, or ticket opener
        support_role_id = config_data.get("support_role_id")
        mod_role_id = config_data.get("mod_role_id")
        member = interaction.user
        assert isinstance(member, discord.Member)

        has_support = any(r.id == support_role_id for r in member.roles)
        has_mod = any(r.id == mod_role_id for r in member.roles)

        # Check if the user is the ticket opener
        open_tickets: dict = config_data.get("open_tickets", {})
        is_opener = False
        for uid_str, cid in open_tickets.items():
            if cid == interaction.channel.id and int(uid_str) == member.id:
                is_opener = True
                break

        if not has_support and not has_mod and not is_opener:
            return await interaction.response.send_message(
                "You do not have permission to close this ticket.", ephemeral=True
            )

        # Open the close modal
        modal = CloseTicketModal(cog)
        await interaction.response.send_modal(modal)


# ── Ticket Panel View (Tech / Server / Mod) ──────────────────────────────────
class TicketPanelView(discord.ui.View):
    """Persistent panel view with three ticket type buttons."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="\U0001f527 Tech Support",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_panel_tech",
    )
    async def tech_support(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        cog: Optional[TicketSystem] = interaction.client.get_cog("TicketSystem")  # type: ignore[assignment]
        if cog:
            await cog.create_ticket(interaction, "Tech Support")

    @discord.ui.button(
        label="\U0001f6e1\ufe0f Server Support",
        style=discord.ButtonStyle.success,
        custom_id="ticket_panel_server",
    )
    async def server_support(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        cog: Optional[TicketSystem] = interaction.client.get_cog("TicketSystem")  # type: ignore[assignment]
        if cog:
            await cog.create_ticket(interaction, "Server Support")

    @discord.ui.button(
        label="\u2694\ufe0f Mod Support",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_panel_mod",
    )
    async def mod_support(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        cog: Optional[TicketSystem] = interaction.client.get_cog("TicketSystem")  # type: ignore[assignment]
        if cog:
            await cog.create_ticket(interaction, "Mod Support")


# ── Blacklist Command Group ────────────────────────────────────────────────────
class TicketBlacklist(app_commands.Group):
    """Manage the ticket blacklist."""

    def __init__(self, cog: "TicketSystem") -> None:
        super().__init__(name="ticket_blacklist", description="Manage ticket blacklist")
        self.cog = cog

    @app_commands.command(name="add", description="Blacklist a user from opening tickets.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(user="The user to blacklist")
    async def blacklist_add(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        assert interaction.guild is not None
        config_data = await self.cog.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "No ticket config found. Run /setup_tickets first.", ephemeral=True
            )

        blacklist: list = config_data.get("blacklisted_users", [])
        if user.id in blacklist:
            return await interaction.response.send_message(
                "User is already blacklisted.", ephemeral=True
            )

        blacklist.append(user.id)
        config_data["blacklisted_users"] = blacklist
        await self.cog.config_manager.save_guild(interaction.guild.id, config_data)

        embed = discord.Embed(title="\U0001f6ab User Blacklisted", color=0xFF4444)
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Added by", value=interaction.user.mention, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Remove a user from the ticket blacklist.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(user="The user to remove from the blacklist")
    async def blacklist_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        assert interaction.guild is not None
        config_data = await self.cog.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "No ticket config found. Run /setup_tickets first.", ephemeral=True
            )

        blacklist: list = config_data.get("blacklisted_users", [])
        if user.id not in blacklist:
            return await interaction.response.send_message(
                "User is not blacklisted.", ephemeral=True
            )

        blacklist.remove(user.id)
        config_data["blacklisted_users"] = blacklist
        await self.cog.config_manager.save_guild(interaction.guild.id, config_data)

        embed = discord.Embed(title="\u2705 User Removed from Blacklist", color=0x00FF7F)
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Removed by", value=interaction.user.mention, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="list", description="View all blacklisted users.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config_data = await self.cog.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "No ticket config found. Run /setup_tickets first.", ephemeral=True
            )

        blacklist: list = config_data.get("blacklisted_users", [])
        if not blacklist:
            return await interaction.response.send_message(
                "No users are blacklisted.", ephemeral=True
            )

        mentions = "\n".join(f"<@{uid}>" for uid in blacklist)
        embed = discord.Embed(
            title="\U0001f6ab Blacklisted Users",
            description=mentions,
            color=0xFF4444,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Main Cog ─────────────────────────────────────────────────────────────────
class TicketSystem(commands.Cog):
    """Full-featured ticket system with persistent views, blacklist, and auto-delete."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config_manager = ConfigManager()
        self.auto_delete_manager = AutoDeleteManager()
        self.ticket_cooldowns: dict[int, datetime] = {}

        # Register blacklist command group
        self.blacklist_group = TicketBlacklist(self)
        self.bot.tree.add_command(self.blacklist_group)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("ticket_blacklist", type=discord.AppCommandType.chat_input)
        # Cancel any pending deletion tasks
        for task in self.auto_delete_manager.pending_deletes.values():
            task.cancel()

    async def cog_load(self) -> None:
        # Register persistent views so buttons survive bot restarts
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_username(name: str) -> str:
        """Sanitize a username for use in channel names."""
        sanitized = name.lower().replace(" ", "-")
        return "".join(c for c in sanitized if c.isalnum() or c == "-")

    def _check_cooldown(self, user_id: int) -> Optional[int]:
        """Return remaining cooldown seconds, or None if no cooldown."""
        last = self.ticket_cooldowns.get(user_id)
        if last is None:
            return None
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        remaining = TICKET_COOLDOWN_SECONDS - elapsed
        if remaining > 0:
            return int(remaining)
        return None

    # ── /setup_tickets ────────────────────────────────────────────────────────

    @app_commands.command(
        name="setup_tickets",
        description="Set up the ticket system panel with roles, channels, and optional image.",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        title="Embed title for the ticket panel",
        description="Embed description text",
        support_role="Role pinged for tech/server tickets",
        mod_role="Role pinged for mod tickets",
        log_channel="Channel where ticket logs are posted",
        ticket_category="Category where ticket channels are created",
        ticket_image="Custom image used as the ticket panel banner",
    )
    async def setup_tickets(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        support_role: discord.Role,
        mod_role: discord.Role,
        log_channel: discord.TextChannel,
        ticket_category: discord.CategoryChannel,
        ticket_image: Optional[discord.Attachment] = None,
    ) -> None:
        assert interaction.guild is not None

        # Validate bot permissions
        bot_member = interaction.guild.me
        required_perms = {
            "manage_channels": bot_member.guild_permissions.manage_channels,
            "manage_threads": bot_member.guild_permissions.manage_threads,
            "send_messages": bot_member.guild_permissions.send_messages,
            "attach_files": bot_member.guild_permissions.attach_files,
        }
        missing = [name for name, has in required_perms.items() if not has]
        if missing:
            return await interaction.response.send_message(
                f"Missing required permissions: {', '.join(missing)}. Please fix and retry.",
                ephemeral=True,
            )

        # Validate image if provided
        ticket_image_url: Optional[str] = None
        if ticket_image is not None:
            if not ticket_image.content_type or not ticket_image.content_type.startswith("image/"):
                return await interaction.response.send_message(
                    "The uploaded file is not a valid image. Please upload a PNG, JPG, GIF, or WEBP file.",
                    ephemeral=True,
                )
            ticket_image_url = ticket_image.url

        # Load existing config or create new one
        config_data = await self.config_manager.get_guild(interaction.guild.id)

        # Build the panel embed
        panel_embed = discord.Embed(
            title=title,
            description=description,
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        panel_embed.set_footer(text="Click a button below to open a support ticket.")
        if ticket_image_url:
            panel_embed.set_image(url=ticket_image_url)

        # Send panel
        panel_view = TicketPanelView()
        panel_msg = await interaction.channel.send(embed=panel_embed, view=panel_view)  # type: ignore[union-attr]

        # Register the view for persistence
        self.bot.add_view(panel_view)

        # Save config
        config_data.update(
            {
                "support_role_id": support_role.id,
                "mod_role_id": mod_role.id,
                "log_channel_id": log_channel.id,
                "ticket_category_id": ticket_category.id,
                "panel_message_id": panel_msg.id,
                "panel_channel_id": interaction.channel.id,  # type: ignore[union-attr]
                "ticket_image_url": ticket_image_url,
            }
        )
        # Initialize defaults if not present
        config_data.setdefault("ticket_counter", 0)
        config_data.setdefault("open_tickets", {})
        config_data.setdefault("claimed_tickets", {})
        config_data.setdefault("blacklisted_users", [])
        config_data.setdefault("auto_delete_delay", AUTO_DELETE_DELAY)
        config_data.setdefault("control_messages", {})

        await self.config_manager.save_guild(interaction.guild.id, config_data)

        await interaction.response.send_message("Ticket panel created successfully! \u2705", ephemeral=True)
        log.info(
            "Ticket panel set up in guild %s by %s", interaction.guild.id, interaction.user
        )

    # ── Ticket Creation (called by panel buttons) ───────────────────────────

    async def create_ticket(
        self, interaction: discord.Interaction, ticket_type: str
    ) -> None:
        assert interaction.guild is not None

        # CHECK 1 \u2014 Config exists
        config_data = await self.config_manager.get_guild(interaction.guild.id)
        if not config_data:
            return await interaction.response.send_message(
                "This server has no ticket setup. Ask an admin to run /setup_tickets.",
                ephemeral=True,
            )

        # CHECK 2 \u2014 Blacklist
        blacklist: list = config_data.get("blacklisted_users", [])
        if interaction.user.id in blacklist:
            return await interaction.response.send_message(
                "You are not permitted to open tickets in this server.", ephemeral=True
            )

        # CHECK 3 \u2014 Existing open ticket
        open_tickets: dict = config_data.get("open_tickets", {})
        user_id_str = str(interaction.user.id)
        if user_id_str in open_tickets:
            existing_channel_id = open_tickets[user_id_str]
            existing_channel = interaction.guild.get_channel(existing_channel_id)
            if existing_channel:
                return await interaction.response.send_message(
                    f"You already have an open ticket: {existing_channel.mention}. "
                    "Please resolve it before opening a new one.",
                    ephemeral=True,
                )
            else:
                # Channel was deleted manually \u2014 clean up stale reference
                del open_tickets[user_id_str]
                config_data["open_tickets"] = open_tickets
                await self.config_manager.save_guild(interaction.guild.id, config_data)

        # CHECK 4 \u2014 Cooldown
        remaining = self._check_cooldown(interaction.user.id)
        if remaining is not None:
            return await interaction.response.send_message(
                f"Please wait {remaining} seconds before opening another ticket.",
                ephemeral=True,
            )

        # Defer the response since channel creation may take a moment
        await interaction.response.defer(ephemeral=True)

        # \u2500\u2500 Ticket ID Format \u2500\u2500
        counter = config_data.get("ticket_counter", 0) + 1
        sanitized_name = self._sanitize_username(interaction.user.name)
        ticket_id = f"ticket-{counter:04d}-{sanitized_name}"

        # \u2500\u2500 Resolve roles \u2500\u2500
        support_role = interaction.guild.get_role(config_data.get("support_role_id", 0))
        mod_role = interaction.guild.get_role(config_data.get("mod_role_id", 0))
        category = interaction.guild.get_channel(config_data.get("ticket_category_id", 0))

        if not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send(
                "Ticket category not found. Please ask an admin to reconfigure with /setup_tickets.",
                ephemeral=True,
            )

        # \u2500\u2500 Permission Overwrites \u2500\u2500
        overwrites: dict[
            discord.Role | discord.Member, discord.PermissionOverwrite
        ] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(  # type: ignore[dict-item]
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                attach_files=True,
            )
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                attach_files=True,
            )

        # \u2500\u2500 Create Channel \u2500\u2500
        try:
            ticket_channel = await interaction.guild.create_text_channel(
                name=ticket_id,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user} ({ticket_type})",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error("Failed to create ticket channel for %s: %s", interaction.user, e)
            return await interaction.followup.send(
                "Failed to create ticket channel. Please check my permissions and try again.",
                ephemeral=True,
            )

        # \u2500\u2500 Opening Embed \u2500\u2500
        ticket_embed = discord.Embed(
            title=f"Ticket #{counter:04d}",
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        ticket_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        ticket_image_url = config_data.get("ticket_image_url")
        if ticket_image_url:
            ticket_embed.set_image(url=ticket_image_url)
        ticket_embed.add_field(name="Opened by", value=interaction.user.mention, inline=True)
        ticket_embed.add_field(name="Ticket Type", value=ticket_type, inline=True)
        ticket_embed.add_field(
            name="Opened at",
            value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
            inline=True,
        )
        ticket_embed.set_footer(text="Use the buttons below to manage this ticket.")

        # \u2500\u2500 Role mention text \u2500\u2500
        mention_parts: list[str] = []
        if support_role:
            mention_parts.append(support_role.mention)
        if mod_role:
            mention_parts.append(mod_role.mention)
        mention_text = " ".join(mention_parts) + " \u2014 a new ticket has been opened." if mention_parts else "A new ticket has been opened."

        # \u2500\u2500 Send control view \u2500\u2500
        control_view = TicketControlView()
        control_msg = await ticket_channel.send(
            content=mention_text,
            embed=ticket_embed,
            view=control_view,
        )

        # Save control message ID for later editing (claim)
        control_messages: dict = config_data.get("control_messages", {})
        control_messages[str(ticket_channel.id)] = control_msg.id
        config_data["control_messages"] = control_messages

        # \u2500\u2500 Post-creation \u2500\u2500
        open_tickets[user_id_str] = ticket_channel.id
        config_data["open_tickets"] = open_tickets
        config_data["ticket_counter"] = counter
        await self.config_manager.save_guild(interaction.guild.id, config_data)

        self.ticket_cooldowns[interaction.user.id] = datetime.now(timezone.utc)

        log.info(
            "Ticket %s created by %s in guild %s",
            ticket_id,
            interaction.user,
            interaction.guild.id,
        )

        # Respond to the user
        await interaction.followup.send(
            f"Your ticket has been created: {ticket_channel.mention}", ephemeral=True
        )

        # Notify staff (non-blocking)
        asyncio.create_task(
            self.notify_staff(
                interaction.guild, ticket_channel, interaction.user, ticket_type, config_data
            )
        )

    # ── Staff Notification ─────────────────────────────────────────────────────

    async def notify_staff(
        self,
        guild: discord.Guild,
        ticket_channel: discord.TextChannel,
        opener: discord.User | discord.Member,
        ticket_type: str,
        config_data: dict,
    ) -> None:
        """Send DM notifications to staff and post to the log channel."""

        dm_embed = discord.Embed(
            title="\U0001f4ec New Ticket Opened",
            color=0xFFA500,
            timestamp=discord.utils.utcnow(),
        )
        dm_embed.add_field(
            name="Opened by", value=f"{opener.mention} ({opener.name})", inline=True
        )
        dm_embed.add_field(name="Ticket Type", value=ticket_type, inline=True)
        dm_embed.add_field(name="Channel", value=ticket_channel.mention, inline=True)
        dm_embed.add_field(name="Jump Link", value=ticket_channel.jump_url, inline=False)

        # Build target list: server owner + all members with mod_role
        targets: set[discord.Member] = set()
        if guild.owner:
            targets.add(guild.owner)

        mod_role_id = config_data.get("mod_role_id")
        if mod_role_id:
            mod_role = guild.get_role(mod_role_id)
            if mod_role:
                for member in mod_role.members:
                    targets.add(member)

        # METHOD 1 \u2014 DM Notifications
        for target in targets:
            try:
                await target.send(embed=dm_embed)
            except discord.Forbidden:
                log.warning("Could not DM %s \u2014 DMs are closed.", target)
            except discord.HTTPException as e:
                log.error("HTTP error DMing %s: %s", target, e)

        # METHOD 2 \u2014 Log Channel
        log_channel_id = config_data.get("log_channel_id")
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel and isinstance(log_channel, discord.TextChannel):
                try:
                    await log_channel.send(embed=dm_embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.error("Failed to send to log channel: %s", e)
            else:
                log.warning("Log channel %s not found in guild %s.", log_channel_id, guild.id)

    # ── /cancel_delete ────────────────────────────────────────────────────────

    @app_commands.command(
        name="cancel_delete",
        description="Cancel the auto-delete timer for the current ticket channel.",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cancel_delete(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        assert interaction.channel is not None

        channel_id = interaction.channel.id
        if channel_id in self.auto_delete_manager.pending_deletes:
            task = self.auto_delete_manager.pending_deletes.pop(channel_id)
            task.cancel()
            await interaction.response.send_message(
                "Auto-delete cancelled for this channel.", ephemeral=False
            )
            log.info("Auto-delete cancelled for channel %s by %s", channel_id, interaction.user)
        else:
            await interaction.response.send_message(
                "No auto-delete is scheduled for this channel.", ephemeral=True
            )


# ── Setup Function ────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketSystem(bot))

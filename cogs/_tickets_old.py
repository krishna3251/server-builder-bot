"""Ticket cog — support ticket system with persistent buttons."""

from __future__ import annotations

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import ticket_service as ts
from services.embed_service import error_embed, info_embed, success_embed
from utils.decorators import guild_only, mod_only

log = logging.getLogger("cogs.tickets")


# ── Persistent button view (survives restart) ────────────────────────────
class TicketPanelView(discord.ui.View):
    """A persistent view with a single 'Open Ticket' button."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="\U0001f4e9 Open Ticket", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[type-arg]
        assert interaction.guild is not None
        if await ts.has_open_ticket(interaction.guild.id, interaction.user.id):
            return await interaction.response.send_message(
                embed=error_embed("Error", "You already have an open ticket."), ephemeral=True
            )

        # Create a private channel
        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),  # type: ignore[dict-item]
        }
        # Add support role if configured
        if config.SUPPORT_ROLE_ID:
            support_role = interaction.guild.get_role(config.SUPPORT_ROLE_ID)
            if support_role:
                overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await interaction.guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            overwrites=overwrites,
            reason=f"Ticket opened by {interaction.user}",
        )

        await ts.open_ticket(interaction.guild.id, interaction.user.id, channel.id)

        em = info_embed("Ticket Opened", f"Welcome {interaction.user.mention}!\nDescribe your issue and a staff member will assist you.\n\nUse `/ticket close` when resolved.")
        await channel.send(embed=em)

        await interaction.response.send_message(
            embed=success_embed("Ticket Created", f"Your ticket: {channel.mention}"), ephemeral=True
        )


class TicketGroup(app_commands.Group):
    """Ticket management commands."""

    def __init__(self) -> None:
        super().__init__(name="ticket", description="Support ticket commands")

    # ── /ticket setup ─────────────────────────────────────────────────────────
    @app_commands.command(name="setup", description="Set up the ticket panel in a channel.")
    @app_commands.describe(panel_channel="Channel to send the ticket panel to")
    async def ticket_setup(self, interaction: discord.Interaction, panel_channel: discord.TextChannel) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(embed=error_embed("Error", "Admin only."), ephemeral=True)

        em = info_embed("\U0001f3ab Support Tickets", "Click the button below to open a support ticket.\nA private channel will be created for you.")
        await panel_channel.send(embed=em, view=TicketPanelView())
        await interaction.response.send_message(embed=success_embed("Done", f"Ticket panel sent to {panel_channel.mention}."), ephemeral=True)

    # ── /ticket close ─────────────────────────────────────────────────────────
    @app_commands.command(name="close", description="Close the current ticket.")
    @app_commands.describe(reason="Reason for closing")
    async def ticket_close(self, interaction: discord.Interaction, reason: str = "No reason provided") -> None:
        assert interaction.guild is not None

        # Find whose ticket this channel belongs to
        ticket_data = None
        ticket_owner_id = None
        # Search through open tickets
        for key in list(ts._tickets.keys()):
            data = ts._tickets[key]
            if data.get("open") and data.get("channel_id") == interaction.channel_id:
                ticket_data = data
                ticket_owner_id = data["user_id"]
                break

        if not ticket_data or ticket_owner_id is None:
            return await interaction.response.send_message(embed=error_embed("Error", "This is not a ticket channel."), ephemeral=True)

        await interaction.response.defer()

        # Generate transcript
        assert isinstance(interaction.channel, discord.TextChannel)
        transcript = await ts.generate_transcript(interaction.channel)

        # Close in data
        await ts.close_ticket(interaction.guild.id, ticket_owner_id, reason)

        # Send transcript to log channel
        if config.TICKET_LOG_CHANNEL_ID:
            log_ch = interaction.guild.get_channel(config.TICKET_LOG_CHANNEL_ID)
            if log_ch and isinstance(log_ch, discord.TextChannel):
                file = discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-{ticket_owner_id}.txt")
                em = info_embed("Ticket Closed", f"**Opener:** <@{ticket_owner_id}>\n**Closed by:** {interaction.user.mention}\n**Reason:** {reason}")
                await log_ch.send(embed=em, file=file)

        # DM the opener
        try:
            opener = await interaction.guild.fetch_member(ticket_owner_id)
            file_dm = discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-transcript.txt")
            dm_em = info_embed("Ticket Closed", f"Your ticket in **{interaction.guild.name}** was closed.\nReason: {reason}")
            await opener.send(embed=dm_em, file=file_dm)
        except Exception:
            pass

        # Delete the channel
        em = success_embed("Closing", "This ticket will be deleted in 5 seconds\u2026")
        await interaction.followup.send(embed=em)
        import asyncio
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed: {reason}")
        except discord.HTTPException:
            pass

    # ── /ticket add ───────────────────────────────────────────────────────────
    @app_commands.command(name="add", description="Add a user to the current ticket.")
    @app_commands.describe(user="User to add")
    async def ticket_add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert isinstance(interaction.channel, discord.TextChannel)
        await interaction.channel.set_permissions(user, view_channel=True, send_messages=True)
        em = success_embed("User Added", f"{user.mention} has been added to this ticket.")
        await interaction.response.send_message(embed=em)

    # ── /ticket remove ────────────────────────────────────────────────────────
    @app_commands.command(name="remove", description="Remove a user from the current ticket.")
    @app_commands.describe(user="User to remove")
    async def ticket_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert isinstance(interaction.channel, discord.TextChannel)
        await interaction.channel.set_permissions(user, overwrite=None)
        em = success_embed("User Removed", f"{user.mention} has been removed from this ticket.")
        await interaction.response.send_message(embed=em)

    # ── /ticket transcript ────────────────────────────────────────────────────
    @app_commands.command(name="transcript", description="Generate a transcript of this ticket.")
    async def ticket_transcript(self, interaction: discord.Interaction) -> None:
        assert isinstance(interaction.channel, discord.TextChannel)
        await interaction.response.defer(ephemeral=True)
        transcript = await ts.generate_transcript(interaction.channel)
        file = discord.File(io.BytesIO(transcript.encode()), filename="transcript.txt")
        await interaction.followup.send(file=file, ephemeral=True)


class TicketCog(commands.Cog, name="Tickets"):
    """Support ticket system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(TicketGroup())
        # Register persistent view on load
        self.bot.add_view(TicketPanelView())

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("ticket")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketCog(bot))

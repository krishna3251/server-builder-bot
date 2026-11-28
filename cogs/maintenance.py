"""Read-only permission explanations and private owner operations."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from services.access_explainer import AccessAction, explain_access
from services.butler_service import delivery_failure
from services.embed_service import info_embed
from utils.interactions import acknowledged, respond


class MaintenanceCog(commands.Cog, name="Maintenance"):
    maintenance = app_commands.Group(name="maintenance", description="Server access and private owner operations", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def _owner_service(self, interaction):
        butler = self.bot.get_cog("Server Butler")
        if not butler or not interaction.guild or not butler.service.enabled(interaction.guild_id):
            await respond(interaction, "The activity ledger is unavailable for this server.")
            return None
        if not await butler.service.is_guild_or_app_owner(interaction.guild, interaction.user):
            await respond(interaction, "Only the server owner and Discord application owner can use this tool.")
            return None
        return butler.service

    @maintenance.command(name="access", description="Explain a member's and the bot's effective channel access")
    @app_commands.describe(channel="Channel or thread to inspect", member="Defaults to you", action="Permission to explain")
    @acknowledged(ephemeral=True)
    async def access(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.ForumChannel | discord.Thread,
        member: discord.Member | None = None,
        action: AccessAction = "view",
    ):
        guild = interaction.guild
        viewer = interaction.user
        if guild is None or channel.guild.id != guild.id or not isinstance(viewer, discord.Member) or not (
            viewer.id == guild.owner_id or viewer.guild_permissions.administrator
        ):
            return await respond(interaction, "This tool requires Administrator in the selected server.")
        target = member or viewer
        if target.guild.id != guild.id:
            return await respond(interaction, "Select a member of this server.")

        async def check(subject):
            if subject is None:
                return "Unknown", "The bot's server member is unavailable."
            return await explain_access(channel, subject, action)

        member_result, bot_result = await asyncio.gather(check(target), check(guild.me))
        name = discord.utils.escape_markdown(channel.name)
        embed = info_embed(f"Access check · {action.replace('_', ' ')}", f"Channel: **{name}** (`{channel.id}`)")
        for title, result in ((f"Member · {target.display_name}", member_result), ("Bot", bot_result)):
            status, explanation = result
            embed.add_field(name=f"{title} · {status}", value=explanation[:1024], inline=False)
        await respond(interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @maintenance.command(name="delivery", description="Inspect, test, or retry your private owner reports")
    @app_commands.describe(action="Choose status, test, or retry", reference="Failed report reference shown by status")
    @acknowledged(ephemeral=True)
    async def delivery(
        self,
        interaction: discord.Interaction,
        action: Literal["status", "test", "retry"] = "status",
        reference: app_commands.Range[str, 1, 128] | None = None,
    ):
        service = await self._owner_service(interaction)
        if service is None:
            return
        guild_id, recipient = interaction.guild_id, interaction.user.id
        if action == "test":
            if not await service.store.allow_delivery_test(recipient):
                return await respond(interaction, "You can send one inbox test per minute.")
            try:
                await asyncio.wait_for(interaction.user.send(
                    f"Owner report delivery test for server `{guild_id}`. This DM contains no activity details.",
                    allowed_mentions=discord.AllowedMentions.none(),
                ), timeout=5)
            except (discord.HTTPException, discord.RateLimited, TimeoutError, OSError) as exc:
                reason, _, _ = delivery_failure(exc)
                return await respond(interaction, f"The test DM failed: {reason.replace('_', ' ')}.")
            return await respond(interaction, "Test DM delivered to your account.")
        if action == "retry":
            if not reference:
                return await respond(interaction, "Enter a failed report reference from `/maintenance delivery`.")
            if await service.store.retry_owner_report(guild_id, recipient, reference.strip().strip('`')):
                return await respond(interaction, "Report queued for retry to your inbox. Check delivery status for the outcome.")
            return await respond(interaction, "This report is unavailable for retry. Reports can be retried for up to 48 hours. Use `/butler report` to open retained reports.")

        last_sent, failures = await service.store.owner_report_deliveries(guild_id, recipient)
        last = f"<t:{int(last_sent)}:F>" if last_sent else "No successful reports recorded for your account."
        embed = info_embed("Your report delivery", f"Last successful DM: {last}\nUse `test` to check your inbox or `retry` with a failed report reference.")
        for row in failures:
            day = json.loads(row["payload"])["day"]
            reason = (row["failure"] or "unknown").replace('_', ' ')
            embed.add_field(name=day, value=f"`{row['id']}` · {reason}", inline=False)
        await respond(interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @maintenance.command(name="inbox", description="Review or update private owner incidents")
    @app_commands.describe(reference="Incident reference from an owner alert or this inbox", state="Filter the list or update the selected incident")
    @acknowledged(ephemeral=True)
    async def inbox(
        self,
        interaction: discord.Interaction,
        reference: app_commands.Range[str, 1, 128] | None = None,
        state: Literal["needs_review", "acknowledged", "resolved"] | None = None,
    ):
        service = await self._owner_service(interaction)
        if service is None:
            return
        guild_id = interaction.guild_id
        reference = reference.strip().strip('`') if reference else None
        if reference and state:
            if not await service.store.set_inbox_state(guild_id, reference, state, interaction.user.id):
                return await respond(interaction, "That incident was not found in this server's retained inbox.")
        entries = await service.store.inbox_entries(guild_id, reference=reference, state=state if not reference else None)
        if not entries:
            return await respond(interaction, "No matching incidents in this server's retained inbox.")
        embed = info_embed("Owner inbox", "High-priority audit and repeat-ping incidents. Use the reference and a state to update an incident for both owners.")
        for entry in entries:
            payload = json.loads(entry["payload"])
            label = entry['state'].replace('_', ' ').title()
            if entry["kind"] == "incident":
                event = f"Repeat mass mention by `{payload['user']}` · {payload['delete_result']}"
            else:
                action = discord.utils.escape_markdown(payload['action'].replace('_', ' '))
                event = f"Audit · {action} · actor `{payload['user']}` · target `{payload['target']}`"
                if payload['flags']:
                    event += "\nReview: " + ", ".join(map(discord.utils.escape_markdown, payload['flags']))
            detail = f"`{entry['id']}` · <t:{int(entry['created'])}:R>\n{event}"
            if entry["updated"]:
                detail += f"\nUpdated by `{entry['actor']}` · <t:{int(entry['updated'])}:R>"
            embed.add_field(name=label, value=detail[:600], inline=False)
        await respond(interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(MaintenanceCog(bot))

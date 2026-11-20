"""Private controls for exemptions on Discord AutoMod rules."""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.automod_service import AutoModService, AutoModSettingsError, accepts_channels, has_timeout, recent_timeouts
from services.error_service import respond_error
from utils.interactions import acknowledged, defer_response, respond


def rule_status(rule: discord.AutoModRule) -> str:
    state = "Enabled" if rule.enabled else "Disabled"
    return f"{state} · {'Timeout' if has_timeout(rule) else 'No timeout action'}"


class AutoModExceptionsView(discord.ui.LayoutView):
    def __init__(self, service: AutoModService, interaction: discord.Interaction, rules: list[discord.AutoModRule]):
        super().__init__(timeout=180)
        self.service = service
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.rules = {rule.id: rule for rule in rules}
        self.selected_id: int | None = None
        self.remove = False
        self.busy = False
        self.message: discord.InteractionMessage | None = None
        self.render()

    def render(self, notice: str = "") -> None:
        self.clear_items()
        rule = self.rules.get(self.selected_id)
        card = discord.ui.Container(accent_colour=config.BOT_COLOR)
        card.add_item(discord.ui.TextDisplay(
            "## AutoMod exceptions\nChoose the rule that acted. Exceptions apply separately to each rule."
        ))
        picker = discord.ui.Select(placeholder="Choose an AutoMod rule", options=[
            discord.SelectOption(label=item.name[:100], value=str(item.id), description=rule_status(item),
                                 default=item.id == self.selected_id)
            for item in list(self.rules.values())[:25]
        ])

        async def select_rule(interaction: discord.Interaction) -> None:
            if self.busy:
                return await interaction.response.send_message("Wait for the current update.", ephemeral=True)
            self.selected_id = int(picker.values[0])
            self.remove = False
            self.render()
            await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

        picker.callback = select_rule
        card.add_item(discord.ui.ActionRow(picker))
        if rule:
            roles = " ".join(f"<@&{role_id}>" for role_id in sorted(rule.exempt_role_ids)) or "None"
            channels = " ".join(f"<#{channel_id}>" for channel_id in sorted(rule.exempt_channel_ids)) or "None"
            detail = (f"### {discord.utils.escape_markdown(rule.name)}\n"
                      f"{rule_status(rule)} · Rule ID `{rule.id}`\n"
                      f"**Exempt roles ({len(rule.exempt_role_ids)}/20)**\n{roles}")
            if accepts_channels(rule):
                detail += f"\n**Exempt channels ({len(rule.exempt_channel_ids)}/50)**\n{channels}"
            card.add_item(discord.ui.TextDisplay(detail))
            remove = self.remove
            mode = "Remove" if remove else "Add"
            toggle = discord.ui.Button(label="Switch to Add" if self.remove else "Switch to Remove")
            toggle.callback = self.toggle_mode
            card.add_item(discord.ui.ActionRow(toggle))
            role_picker = discord.ui.RoleSelect(placeholder=f"{mode} roles", max_values=20)

            async def select_roles(interaction: discord.Interaction) -> None:
                await self.save(
                    interaction, rule.id, remove, role_ids={role.id for role in role_picker.values}, channel_ids=set(),
                )

            role_picker.callback = select_roles
            card.add_item(discord.ui.ActionRow(role_picker))
            if accepts_channels(rule):
                channel_picker = discord.ui.ChannelSelect(placeholder=f"{mode} channels", max_values=25)

                async def select_channels(interaction: discord.Interaction) -> None:
                    await self.save(
                        interaction, rule.id, remove, role_ids=set(),
                        channel_ids={channel.id for channel in channel_picker.values},
                    )

                channel_picker.callback = select_channels
                card.add_item(discord.ui.ActionRow(channel_picker))
        if notice:
            card.add_item(discord.ui.TextDisplay(notice))
        self.add_item(card)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message("Open your own AutoMod panel.", ephemeral=True)
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server to use this panel.", ephemeral=True)
            return False
        return True

    async def toggle_mode(self, interaction: discord.Interaction) -> None:
        if self.busy:
            return await interaction.response.send_message("Wait for the current update.", ephemeral=True)
        self.remove = not self.remove
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def save(
        self, interaction: discord.Interaction, rule_id: int, remove: bool, *,
        role_ids: set[int], channel_ids: set[int],
    ) -> None:
        if self.busy:
            return await interaction.response.send_message("Wait for the current update.", ephemeral=True)
        if rule_id != self.selected_id or remove != self.remove:
            return await interaction.response.send_message("Use the current rule and mode shown in the panel.", ephemeral=True)
        self.busy = True
        try:
            await defer_response(interaction)
            rule, changed = await self.service.change(
                interaction.guild, interaction.user, rule_id,
                role_ids=role_ids, channel_ids=channel_ids, remove=remove,
            )
            self.rules[rule.id] = rule
            self.render("Exemptions saved." if changed else "Those selections already match the rule.")
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
        except (AutoModSettingsError, discord.NotFound, discord.Forbidden) as error:
            await interaction.followup.send(_settings_message(error), ephemeral=True)
        finally:
            self.busy = False

    async def on_timeout(self) -> None:
        if self.message is not None:
            for item in self.walk_children():
                if hasattr(item, "disabled"):
                    item.disabled = True
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await respond_error(interaction, error, context="AutoMod exception panel")


def _settings_message(error: Exception) -> str:
    if isinstance(error, discord.NotFound):
        return "Discord could not find that rule. Reopen /automod exception to see the current rules."
    if isinstance(error, discord.Forbidden):
        return "Discord denied this change. Check the bot's Manage Server permission and Moderate Members for timeout rules."
    return str(error)


class AutoModCog(commands.Cog, name="AutoMod"):
    automod = app_commands.Group(
        name="automod", description="Manage Discord AutoMod exceptions and look up timeouts.",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self) -> None:
        self.service = AutoModService()

    @automod.command(name="exception", description="Open AutoMod exceptions or change a rule using a role or channel mention.")
    @app_commands.describe(
        rule="Select the Discord AutoMod rule, or paste its ID", role="Choose an @role",
        channel="Choose a #channel or thread", remove="Remove the selected exception instead of adding it",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_guild=True)
    @acknowledged(ephemeral=True)
    async def exception(
        self, interaction: discord.Interaction, rule: str | None = None, role: discord.Role | None = None,
        channel: app_commands.AppCommandChannel | app_commands.AppCommandThread | None = None, remove: bool = False,
    ) -> None:
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        try:
            if role or channel or remove:
                if not rule or not rule.isascii() or not rule.isdecimal() or not 1 <= len(rule) <= 20 or int(rule) >= 2**64:
                    return await respond(interaction, "Select the AutoMod rule you want to change.", ephemeral=True)
                if not (role or channel):
                    return await respond(interaction, "Select a role or channel to remove.", ephemeral=True)
                saved, changed = await self.service.change(
                    guild, interaction.user, int(rule), role_ids={role.id} if role else set(),
                    channel_ids={channel.id} if channel else set(), remove=remove,
                )
                message = "Exemptions saved" if changed else "Rule already matches those selections"
                return await respond(interaction, f"{message} for **{discord.utils.escape_markdown(saved.name)}**.",
                                     ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

            rules = await guild.fetch_automod_rules()
            if rule:
                rules = [item for item in rules if str(item.id) == rule]
                if not rules:
                    return await respond(interaction, "Discord could not find that rule.", ephemeral=True)
            if not rules:
                return await respond(interaction, "Set up a rule in Server Settings → AutoMod first.", ephemeral=True)
            view = AutoModExceptionsView(self.service, interaction, rules)
            if len(rules) == 1:
                view.selected_id = rules[0].id
                view.render()
            view.message = await respond(interaction, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except (AutoModSettingsError, discord.NotFound, discord.Forbidden) as error:
            await respond(interaction, _settings_message(error), ephemeral=True)

    @exception.autocomplete("rule")
    async def rule_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if (
            guild is None or guild.me is None or not guild.me.guild_permissions.manage_guild
            or not interaction.user.guild_permissions.manage_guild
        ):
            return []
        try:
            rules = await asyncio.wait_for(guild.fetch_automod_rules(), timeout=2)
        except (discord.HTTPException, TimeoutError):
            return []
        query = current.strip().casefold()
        return [
            app_commands.Choice(name=f"{rule.name[:75]} · {rule_status(rule)}"[:100], value=str(rule.id))
            for rule in rules
            if query in rule.name.casefold() or query in str(rule.id)
        ][:25]

    @automod.command(name="recent-timeouts", description="Check which Discord rule, bot, or moderator timed out a member.")
    @app_commands.checks.has_permissions(manage_guild=True, view_audit_log=True)
    @app_commands.checks.bot_has_permissions(view_audit_log=True)
    @acknowledged(ephemeral=True)
    async def timeout_history(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        entries = await recent_timeouts(guild, member.id)
        if not entries:
            return await respond(interaction, "No matching timeouts in the 100 most recent entries of each audit type.", ephemeral=True)
        lines = []
        for entry in entries:
            when = discord.utils.format_dt(entry.created_at, style="R")
            if entry.action == discord.AuditLogAction.automod_timeout_member:
                name = discord.utils.escape_markdown(getattr(entry.extra, "automod_rule_name", "Unknown rule"))
                channel_id = getattr(getattr(entry.extra, "channel", None), "id", None)
                source = f"Discord AutoMod · **{name}**" + (f" · <#{channel_id}>" if channel_id else "")
            else:
                actor = getattr(entry.user, "id", None) or entry.user_id
                source = f"<@{actor}> · ID `{actor}`" if actor else "Unknown moderator or app"
            reason = discord.utils.escape_markdown((entry.reason or "No reason recorded").replace("\n", " ")[:160])
            lines.append(f"• {when} · {source}\n  {reason}")
        await respond(interaction, "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoModCog())

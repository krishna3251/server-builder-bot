"""Utility cog — ping, serverinfo, userinfo, avatar, poll, remind, and more."""

from __future__ import annotations

from utils.interactions import acknowledged, respond

import datetime
import logging
import platform
import math
from services.reminder_service import ReminderService
from utils.polls import PollView

import discord
from services.error_service import SafeView
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import add_member_context, add_usage_footer, info_embed, success_embed, error_embed
from utils.decorators import guild_only
from utils.helpers import format_number, parse_duration

_HELP_PAGES: dict[str, tuple[str, str]] = {
    "overview": (
        "🤖 Command center",
        "Choose a category from the menu to explore the bot. Admin tools are permission-gated, "
        "and some commands only work inside a server.",
    ),
    "builder": (
        "🧱 Server builder",
        "**Quick start:** `/builder_panel` · **JSON reference:** `/json_example`\n"
        "**Commands:** `/setup_server`, `/setup_custom`, `/server_json`, `/json_example`, `/json_validate`, `/generate_server`, `/channel_summaries`\n\n"
        "Review previews before applying changes. Cleaning an existing server is destructive.",
    ),
    "tickets": (
        "🎫 Tickets",
        "**Setup:** `/setup_tickets`\n"
        "**Controls:** use the ticket panel buttons to open a category, claim, or close a ticket.\n"
        "**Blacklist:** `/ticket_blacklist add`, `/ticket_blacklist remove`, `/ticket_blacklist list`\n"
        "**Other:** `/cancel_delete` · `/diagnostics` (admin)",
    ),
    "moderation": (
        "🛡️ Moderation",
        "`/kick` · `/ban` · `/unban` · `/mute` · `/unmute`\n"
        "`/warn` · `/warnings` · `/clearwarnings` · `/clear`\n"
        "`/slowmode` · `/lock` · `/unlock`\n\n"
        "Moderation commands require the relevant server permissions and respect role hierarchy.",
    ),
    "economy": (
        "💰 Economy & leveling",
        "`/balance` · `/daily` · `/work` · `/pay`\n"
        "`/leaderboard` · `/shop` · `/buy` · `/inventory`\n"
        "`/rank` · `/setxp`\n\n"
        "The shop has an interactive catalogue. Purchases are recorded in your server inventory.",
    ),
    "ai": (
        "✨ AI assistant",
        "`/ai` — private chat, with separate memory per server\n"
        "`/ai_persona` — choose its response style\n"
        "`/ai_reset` — clear your conversation memory",
    ),
    "utility": (
        "🔧 Utility & analytics",
        "`/ping` · `/serverinfo` · `/userinfo` · `/roleinfo`\n"
        "`/avatar` · `/banner` · `/invite` · `/uptime` · `/botinfo`\n"
        "`/poll` · `/remind` · `/reminders` · `/reminder_cancel`\n"
        "`/analytics messages` · `/analytics members` · `/analytics commands`",
    ),
}


class HelpCategorySelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Overview", value="overview", emoji="🤖", default=True),
            discord.SelectOption(label="Server Builder", value="builder", emoji="🧱"),
            discord.SelectOption(label="Tickets", value="tickets", emoji="🎫"),
            discord.SelectOption(label="Moderation", value="moderation", emoji="🛡️"),
            discord.SelectOption(label="Economy & Leveling", value="economy", emoji="💰"),
            discord.SelectOption(label="AI Assistant", value="ai", emoji="✨"),
            discord.SelectOption(label="Utilities & Analytics", value="utility", emoji="🔧"),
        ]
        super().__init__(
            placeholder="Browse command categories…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CommandHelpView):
            return
        if interaction.user.id != view.owner_id:
            return await respond(interaction,
                "This command guide belongs to the person who opened it.",
                ephemeral=True,
            )
        key = self.values[0]
        for option in self.options:
            option.default = option.value == key
        title, description = _HELP_PAGES[key]
        em = info_embed(title, description)
        add_usage_footer(em, "Command guide")
        await interaction.response.edit_message(embed=em, view=view)


class CommandHelpView(SafeView):
    """Private dropdown help navigation so the command list stays readable."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self.add_item(HelpCategorySelect())

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


log = logging.getLogger("cogs.utility")


class UtilityCog(commands.Cog, name="Utility"):
    """General-purpose utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminders_service = ReminderService(bot)

    async def cog_load(self):
        self.reminders_service.start()

    async def cog_unload(self):
        await self.reminders_service.close()

    @app_commands.command(name="help", description="Open the interactive command guide.")
    @acknowledged(ephemeral=True)
    async def help(self, interaction: discord.Interaction) -> None:
        title, description = _HELP_PAGES["overview"]
        em = info_embed(title, description)
        em.add_field(
            name="Featured commands",
            value="`/builder_panel` · `/shop` · `/rank` · `/ai`",
            inline=False,
        )
        add_usage_footer(em, "Select a category below")
        view = CommandHelpView(interaction.user.id)
        await respond(interaction, embed=em, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @app_commands.command(name="diagnostics", description="Check modules, server permissions, and recent error references.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def diagnostics(self, interaction: discord.Interaction):
        from services.error_service import recent_errors
        guild = interaction.guild
        em = info_embed("🩺 Bot diagnostics", "Configuration and runtime checks for administrators. Error references match the bot logs.")
        em.add_field(name="Connection", value=f"{'✅ Ready' if self.bot.is_ready() else '⏳ Connecting'} · {round(self.bot.latency * 1000) if math.isfinite(self.bot.latency) else 'N/A'} ms", inline=True)
        em.add_field(name="AI configuration", value="✅ Key configured" if config.OPENROUTER_API_KEY else "⚠️ Set OPENROUTER_API_KEY", inline=True)
        em.add_field(name="Ticket configuration", value="MongoDB configured (connection not verified)" if config.MONGO_URI else "Local JSON mode (persistent storage required)", inline=True)
        sync_error = getattr(self.bot, 'command_sync_error', None)
        em.add_field(name='Slash command sync', value=f'⚠️ Failed: `{sync_error}` · developer can run /sync_commands' if sync_error else 'No sync failure recorded (check hosting logs for startup details)', inline=False)
        loaded = sorted(self.bot.extensions)
        em.add_field(name="Loaded modules", value="\n".join(f"✅ {name.removeprefix('cogs.')}" for name in loaded) or "None", inline=True)
        failed = getattr(self.bot, "extension_errors", {})
        em.add_field(name="Modules needing attention", value="\n".join(f"⚠️ {name}: `{reference}`" for name, reference in failed.items())[:1000] or "✅ No startup failures recorded", inline=True)
        required = ("manage_channels", "manage_roles", "manage_guild", "send_messages", "embed_links", "attach_files", "read_message_history")
        missing = [p.replace("_", " ") for p in required if not getattr(guild.me.guild_permissions, p, False)]
        em.add_field(name="Server permissions", value=", ".join(missing) if missing else "✅ Core permissions available", inline=False)
        current_errors = [item for item in recent_errors if item.get("guild_id") in (None, guild.id)]
        em.add_field(name="Recent errors · this process", value="\n".join(f"`{item['reference']}` · {item['type']}" for item in current_errors[-5:]) or "✅ No captured errors", inline=False)
        em.add_field(name="Data storage", value="Economy, XP, analytics, and AI memory use local JSON. Keep the data directory on persistent storage and run one bot process. Tickets use MongoDB when configured, otherwise local JSON. Reminder deadlines use local JSON.", inline=False)
        await respond(interaction, embed=em, ephemeral=True)

    @app_commands.command(name="sync_commands", description="Retry global slash-command sync (bot developer/owner only).")
    @acknowledged(ephemeral=True)
    async def sync_commands(self, interaction: discord.Interaction):
        if interaction.user.id not in config.DEV_IDS and not await self.bot.is_owner(interaction.user):
            return await respond(interaction, embed=error_embed("Developer access required", "Only the bot owner or configured DEV_IDS can sync commands."), ephemeral=True)
        synced = await self.bot.tree.sync()
        self.bot.command_sync_error = None
        await respond(interaction, embed=success_embed("Commands synchronized", f"Published {len(synced)} top-level commands. Refresh Discord if a new command is not visible yet."), ephemeral=True)

    # ── /ping ─────────────────────────────────────────────────────────────
    @app_commands.command(name="ping", description="Check bot latency.")
    @acknowledged(ephemeral=False)
    async def ping(self, interaction: discord.Interaction) -> None:
        ws_latency = round(self.bot.latency * 1000, 1)
        em = info_embed("🏓 Pong!", f"WebSocket: **{ws_latency}ms**")
        start = datetime.datetime.now(datetime.timezone.utc)
        await respond(interaction, embed=em)
        end = datetime.datetime.now(datetime.timezone.utc)
        api_latency = round((end - start).total_seconds() * 1000, 1)
        em.description += f"\nAPI: **{api_latency}ms**"
        await interaction.edit_original_response(embed=em)

    # ── /serverinfo ───────────────────────────────────────────────────────
    @app_commands.command(name="serverinfo", description="Display server information.")
    @guild_only()
    @acknowledged(ephemeral=False)
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        g = interaction.guild
        assert g is not None
        em = info_embed(g.name, "")
        if g.icon:
            em.set_thumbnail(url=g.icon.url)
        em.add_field(name="OWNER", value=str(g.owner or "Unknown"), inline=True)
        em.add_field(name="MEMBERS", value=format_number(g.member_count or 0), inline=True)
        em.add_field(name="ROLES", value=format_number(len(g.roles)), inline=True)
        em.add_field(name="TEXT CHANNELS", value=format_number(len(g.text_channels)), inline=True)
        em.add_field(name="VOICE CHANNELS", value=format_number(len(g.voice_channels)), inline=True)
        em.add_field(name="BOOSTS", value=format_number(g.premium_subscription_count or 0), inline=True)
        em.add_field(name="CREATED", value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
        em.add_field(name="SERVER ID", value=f"`{g.id}`", inline=True)
        await respond(interaction, embed=em)

    # ── /userinfo ─────────────────────────────────────────────────────────
    @app_commands.command(name="userinfo", description="Display information about a user.")
    @app_commands.describe(user="User to inspect")
    @guild_only()
    @acknowledged(ephemeral=False)
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        target = user or interaction.user
        assert isinstance(target, discord.Member)
        em = info_embed(f"👤 {target.display_name}", "Member profile for this server.")
        add_member_context(em, target)
        em.add_field(name="USER ID", value=f"`{target.id}`", inline=True)
        em.add_field(name="NICKNAME", value=target.nick or "Not set", inline=True)
        em.add_field(name="TOP ROLE", value=target.top_role.mention, inline=True)
        em.add_field(name="JOINED SERVER", value=f"<t:{int(target.joined_at.timestamp())}:R>" if target.joined_at else "Unknown", inline=True)
        em.add_field(name="ACCOUNT CREATED", value=f"<t:{int(target.created_at.timestamp())}:D>", inline=True)
        em.add_field(name="ROLE COUNT", value=format_number(max(0, len(target.roles) - 1)), inline=True)
        await respond(interaction, embed=em)

    # ── /roleinfo ─────────────────────────────────────────────────────────
    @app_commands.command(name="roleinfo", description="Display information about a role.")
    @app_commands.describe(role="Role to inspect")
    @guild_only()
    @acknowledged(ephemeral=False)
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role) -> None:
        em = info_embed(role.name, "")
        em.colour = role.colour
        em.add_field(name="ID", value=str(role.id), inline=True)
        em.add_field(name="Colour", value=str(role.colour), inline=True)
        em.add_field(name="Members", value=str(len(role.members)), inline=True)
        em.add_field(name="Hoisted", value=str(role.hoist), inline=True)
        em.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
        em.add_field(name="Position", value=str(role.position), inline=True)
        em.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
        await respond(interaction, embed=em)

    # ── /avatar ───────────────────────────────────────────────────────────
    @app_commands.command(name="avatar", description="Get a user's avatar.")
    @app_commands.describe(user="User whose avatar to show")
    @acknowledged(ephemeral=False)
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        em = info_embed(f"{target}'s Avatar", "")
        em.set_image(url=target.display_avatar.with_size(1024).url)
        await respond(interaction, embed=em)

    # ── /banner ───────────────────────────────────────────────────────────
    @app_commands.command(name="banner", description="Get a user's banner.")
    @app_commands.describe(user="User whose banner to show")
    @acknowledged(ephemeral=False)
    async def banner(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        fetched = await self.bot.fetch_user(target.id)
        if fetched.banner:
            em = info_embed(f"{target}'s Banner", "")
            em.set_image(url=fetched.banner.with_size(1024).url)
            await respond(interaction, embed=em)
        else:
            await respond(interaction, embed=error_embed("No Banner", "This user has no banner."), ephemeral=True)

    # ── /invite ───────────────────────────────────────────────────────────
    @app_commands.command(name="invite", description="Get the bot's invite link.")
    @acknowledged(ephemeral=True)
    async def invite(self, interaction: discord.Interaction) -> None:
        assert self.bot.user is not None
        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            attach_files=True,
            use_application_commands=True,
            manage_channels=True,
            manage_roles=True,
            manage_messages=True,
            manage_guild=True,
            moderate_members=True,
            kick_members=True,
            ban_members=True,
        )
        url = discord.utils.oauth_url(self.bot.user.id, permissions=permissions)
        em = info_embed(
            "🤖 Add the bot",
            f"[**Invite {config.BOT_NAME} to your server**]({url})\n\n"
            "The invite requests permissions used by its setup, moderation, and ticket features. "
            "Review the permission list on Discord before approving it; Administrator is not requested.",
        )
        em.add_field(name="Requested access", value="Channels · Roles · Messages · Moderation · Application commands", inline=False)
        await respond(interaction, embed=em, ephemeral=True)

    # ── /uptime ───────────────────────────────────────────────────────────
    @app_commands.command(name="uptime", description="Show how long the bot has been running.")
    @acknowledged(ephemeral=False)
    async def uptime(self, interaction: discord.Interaction) -> None:
        delta = datetime.datetime.now(datetime.timezone.utc) - self.bot.start_time  # type: ignore
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        em = info_embed("Uptime", f"**{delta.days}d {hours % 24}h {minutes}m {seconds}s**")
        await respond(interaction, embed=em)

    # ── /botinfo ──────────────────────────────────────────────────────────
    @app_commands.command(name="botinfo", description="Show bot statistics.")
    @acknowledged(ephemeral=False)
    async def botinfo(self, interaction: discord.Interaction) -> None:
        em = info_embed(config.BOT_NAME, "")
        em.add_field(name="Version", value=config.BOT_VERSION, inline=True)
        em.add_field(name="Guilds", value=str(len(self.bot.guilds)), inline=True)
        cmd_count = len(self.bot.tree.get_commands())
        em.add_field(name="Commands", value=str(cmd_count), inline=True)
        em.add_field(name="Python", value=platform.python_version(), inline=True)
        em.add_field(name="discord.py", value=discord.__version__, inline=True)
        await respond(interaction, embed=em)

    # ── /poll ─────────────────────────────────────────────────────────────
    @app_commands.command(name="poll", description="Create a reaction poll.")
    @app_commands.describe(question="The poll question", options="Comma-separated options (max 10)")
    @acknowledged(ephemeral=False)
    async def poll(self, interaction: discord.Interaction, question: str, options: str) -> None:
        items = [o.strip() for o in options.split(",") if o.strip()]
        if len(items) < 2:
            return await respond(interaction, embed=error_embed("Error", "Provide at least 2 options."), ephemeral=True)
        if len(items) > 10:
            return await respond(interaction, embed=error_embed("Error", "Maximum 10 options."), ephemeral=True)

        if len(question) > 220 or any(len(item) > 100 for item in items) or len(set(item.casefold() for item in items)) != len(items):
            return await respond(interaction, embed=error_embed("Poll needs an edit", "Use a question up to 220 characters, unique options, and at most 100 characters per option."), ephemeral=True)
        view = PollView(question, items, interaction.user.id)
        await respond(interaction, embed=view.card(), view=view)
        view.message = await interaction.original_response()

    # ── /remind ───────────────────────────────────────────────────────────
    @app_commands.command(name="remind", description="Set a DM reminder.")
    @app_commands.describe(time="When to remind (e.g. 10m, 2h, 1d)", message="Reminder message")
    @acknowledged(ephemeral=True)
    async def remind(self, interaction: discord.Interaction, time: str, message: str) -> None:
        try:
            td = parse_duration(time)
        except ValueError:
            return await respond(interaction, embed=error_embed("Error", "Invalid time format."), ephemeral=True)

        target_dt = datetime.datetime.now(datetime.timezone.utc) + td
        try:
            identifier = await self.reminders_service.add(interaction.user.id, target_dt, message)
        except ValueError as exc:
            return await respond(interaction, embed=error_embed("Reminder limit", str(exc)), ephemeral=True)
        em = success_embed("Reminder saved", f"I'll DM you <t:{int(target_dt.timestamp())}:R>.\nReference: `{identifier}` · Cancel using `/reminder_cancel`.\nDM delivery requires your DMs to be open.")
        await respond(interaction, embed=em, ephemeral=True)

    @app_commands.command(name="reminders", description="List your saved reminders and delivery status.")
    @acknowledged(ephemeral=True)
    async def reminders(self, interaction: discord.Interaction):
        items = await self.reminders_service.list_for(interaction.user.id)
        lines = [f"`{key}` · {'Delivery failed; check DMs' if value.get('status') == 'failed' else '<t:' + str(int(value['due'])) + ':R>'} · {discord.utils.escape_markdown(value['message'][:100])}" for key, value in items.items()]
        await respond(interaction, embed=info_embed("⏰ Your reminders", "\n\n".join(lines) or "No saved reminders. Use /remind to create one."), ephemeral=True)

    @app_commands.command(name="reminder_cancel", description="Cancel one of your saved reminders by reference ID.")
    @acknowledged(ephemeral=True)
    async def reminder_cancel(self, interaction: discord.Interaction, reference: str):
        deleted = await self.reminders_service.cancel(interaction.user.id, reference.strip())
        await respond(interaction, embed=success_embed("Reminder cancelled", "The reminder was removed.") if deleted else error_embed("Reminder not found", "Use /reminders to find your own reminder IDs."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UtilityCog(bot))

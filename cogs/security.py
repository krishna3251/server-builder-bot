"""Private security console, quarantine commands, channel creation and bot profile."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services.error_service import respond_error
from services.embed_service import error_embed, success_embed
from services.moderation_notices import notify_action
from cogs._quarantine_ui import (
    QuarantineConfirmation, QuarantineDashboard, audit_file, is_quarantine_admin, status_text,
)
from services.quarantine_resources import QuarantineResources
from services.automatic_moderation import private_mod_log
from services.quarantine_service import QuarantineService, DENIED, overwrite_dict
from services.permission_service import is_above
from utils.interactions import acknowledged, respond

log = logging.getLogger(__name__)


class ChannelCreateModal(discord.ui.Modal, title='Create a channel'):
    name_input = discord.ui.TextInput(label='Channel name', min_length=1, max_length=100)
    kind_input = discord.ui.TextInput(label='Type — text, voice or category', default='text', max_length=8)

    def __init__(self, cog, owner_id):
        super().__init__(timeout=180)
        self.cog, self.owner_id = cog, owner_id

    async def on_submit(self, interaction):
        if (interaction.user.id != self.owner_id or not interaction.guild
                or not interaction.user.guild_permissions.manage_channels):
            return await respond(interaction, 'You need Manage Channels in this server.', ephemeral=True, view=None)
        kind = self.kind_input.value.strip().lower()
        if kind not in {'text', 'voice', 'category'}:
            return await respond(interaction, 'Choose text, voice or category.', ephemeral=True, view=None)
        for predicate in self.cog.create_channel.checks:
            if not await discord.utils.maybe_coroutine(predicate, interaction):
                raise app_commands.CheckFailure('Channel creation permission check failed')
        await self.cog.create_channel.callback(self.cog, interaction, self.name_input.value, kind)

    async def on_error(self, interaction, error):
        await respond_error(interaction, error, context='Channel creation')


class SecurityConsole(discord.ui.LayoutView):
    def __init__(self, cog, user_id, text):
        super().__init__(timeout=180)
        self.cog, self.user_id = cog, user_id
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay('## Security console\n' + text),
            discord.ui.Separator(),
            discord.ui.TextDisplay('### Quarantine\n`/quarantine_setup` prepares the private review room.\n'
                                   '`/sudo_ban` or `/quarantine apply` suspends access.\n'
                                   '`/quarantine_release` restores saved access.'),
            discord.ui.TextDisplay('### Server tools\n`/create_channel` creates a text channel, voice channel or category.\n'
                                   '`/butler report` opens your private daily ledger.\n'
                                   '`/bot_profile` refreshes the public bot description.'),
            accent_colour=config.BOT_COLOR))
        button = discord.ui.Button(label='Refresh status', style=discord.ButtonStyle.primary)
        button.callback = self.refresh
        self.add_item(discord.ui.ActionRow(button))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not interaction.guild or not interaction.user.guild_permissions.administrator:
            await respond(interaction, 'Only the administrator who opened this console can use it.', ephemeral=True, view=None)
            return False
        return True

    async def refresh(self, interaction):
        await interaction.response.defer()
        text = await self.cog.status_text(interaction.guild)
        await interaction.edit_original_response(view=SecurityConsole(self.cog, self.user_id, text))
        self.stop()

    async def on_error(self, interaction, error, item):
        await respond_error(interaction, error, context='Security console')


class SecurityCog(commands.Cog, name='Security'):
    def __init__(self, bot):
        self.bot = bot
        self.quarantine_service = QuarantineService(Path(config.DATA_DIR) / 'quarantine.json')
        self.quarantine_resources = QuarantineResources(self.quarantine_service)
        self.application = None
        self.last_profile_error = None
        self.last_channel_error = None
        self.recovering = False
        self._last_recovery = 0.0
        self._manual_release_checks: set[tuple[int, int]] = set()

    async def cog_load(self):
        self.profile_worker.start().set_name("security-profile")

    async def cog_unload(self):
        self.profile_worker.cancel()
        if self.profile_worker.get_task():
            import asyncio
            await asyncio.gather(self.profile_worker.get_task(), return_exceptions=True)

    async def refresh_profile(self):
        creator = config.BOT_CREATOR_NAME
        description = (f'Created by {creator} • Server Builder v{config.BOT_VERSION}\n'
                       f'{len(self.bot.guilds)} servers • Builder, moderation and community tools.\n'
                       'Open /help to get started.')
        if self.application is None:
            self.application = await self.bot.application_info()
        if self.application.description != description:
            self.application = await self.application.edit(description=description)
        await self.bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name=f'/help • Created by {creator} • {len(self.bot.guilds)} servers'))
        self.last_profile_error = None

    @tasks.loop(minutes=15)
    async def profile_worker(self):
        try:
            await self.refresh_profile()
        except Exception as exc:
            self.last_profile_error = type(exc).__name__
            log.warning('Dynamic bot profile update failed: %s', type(exc).__name__)

    @profile_worker.before_loop
    async def before_profile(self):
        await self.bot.wait_until_ready()

    async def status_text(self, guild):
        async with self.quarantine_service.lock:
            state = await self.quarantine_service.load()
            settings = state.get(str(guild.id), {})
            members = settings.get('members', {})
            partial = sum(record['phase'] != 'active' for record in members.values())
        permissions = guild.me.guild_permissions
        requirements = ['view_channel', 'send_messages', 'read_message_history', 'manage_channels', 'manage_roles', 'manage_messages', 'view_audit_log', 'moderate_members', 'kick_members', 'ban_members']
        missing = [name.replace('_', ' ') for name in requirements if not getattr(permissions, name)]
        return (f'Quarantine setup: {"ready" if settings.get("configured") else "run /quarantine_setup"}\n'
                f'Quarantined accounts: {len(members)} • Pending recovery: {partial}\n'
                f'Channels needing setup retry: {len(settings.get("failed_channels", []))}\n'
                f'Features lacking permissions: {", ".join(missing) or "none"}\n'
                f'Last channel reconciliation issue: {self.last_channel_error or "none"}\n'
                f'Profile update issue: {self.last_profile_error or "none"}\n'
                'Moderation receipts and reports are private. Discord owners and Administrators can see private channels.')

    @app_commands.command(name='security', description='Open your private security console.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def security(self, interaction: discord.Interaction):
        await respond(interaction, view=SecurityConsole(self, interaction.user.id, await self.status_text(interaction.guild)), ephemeral=True)

    @app_commands.command(name='quarantine_setup', description='Create private quarantine resources and safely apply channel restrictions.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @acknowledged(ephemeral=True)
    async def quarantine_setup(self, interaction: discord.Interaction, staff_role: discord.Role | None = None):
        if staff_role and (staff_role.is_default() or staff_role.managed or not (
            staff_role.permissions.administrator or staff_role.permissions.moderate_members or staff_role.permissions.manage_guild
        )):
            return await respond(interaction, 'Choose an existing staff role with Moderate Members, Manage Server or Administrator.', ephemeral=True)
        try:
            settings = await self.quarantine_service.setup(interaction.guild, staff_role)
        except ValueError as exc:
            return await respond(interaction, str(exc), ephemeral=True)
        failures = settings['failed_channels']
        text = (f'Private room <#{settings["room"]}> and quarantine role are configured.\n'
                + (f'{len(failures)} channels need permission fixes. Run this command again to retry.' if failures else 'Channel restrictions are ready.'))
        await respond(interaction, embed=success_embed('Quarantine setup', text), ephemeral=True)

    async def apply_quarantine(self, interaction, member, reason):
        try:
            record = await self.quarantine_service.quarantine(interaction.guild, interaction.user, member, reason)
        except ValueError as exc:
            return await respond(interaction, str(exc), ephemeral=True)
        partial = record['phase'] != 'active'
        await notify_action(self.bot, interaction.guild, interaction.user, member,
                            'partial quarantine' if partial else 'quarantine', reason, interaction.id)
        text = f'Account ID {member.id} • Previous access was recorded.'
        if partial:
            text += f'\n{len(record["failed_channels"])} restrictions could not be confirmed. Fix permissions and repeat this command.'
        else:
            text += '\nAccess is restricted to the private review room. Use /quarantine_release to restore access.'
        await respond(interaction, embed=(error_embed if partial else success_embed)('Quarantine needs review' if partial else 'Member quarantined', text), ephemeral=True)

    quarantine_group = app_commands.Group(name='quarantine', description='Manage reversible member quarantine', guild_only=True)

    @quarantine_group.command(name='apply', description='Suspend a member’s access and save it for a reversible release.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True, manage_roles=True)
    @acknowledged(ephemeral=True)
    async def quarantine(self, interaction: discord.Interaction, member: discord.Member, reason: str = 'Staff review'):
        await self.apply_quarantine(interaction, member, reason)

    @app_commands.command(name='sudo_ban', description='Quarantine a member for staff review while keeping them in the server.')
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True, manage_roles=True)
    @app_commands.checks.has_permissions(moderate_members=True, manage_roles=True)
    @acknowledged(ephemeral=True)
    async def sudo_ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = 'Staff review'):
        await self.apply_quarantine(interaction, member, reason)

    @app_commands.command(name='quarantine_release', description='Restore a member’s saved access after quarantine review.')
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True, manage_roles=True)
    @app_commands.checks.has_permissions(moderate_members=True, manage_roles=True)
    @acknowledged(ephemeral=True)
    async def quarantine_release(self, interaction: discord.Interaction, member: discord.Member):
        try:
            failures = await self.quarantine_service.release(interaction.guild, interaction.user, member)
        except ValueError as exc:
            return await respond(interaction, str(exc), ephemeral=True)
        await notify_action(self.bot, interaction.guild, interaction.user, member,
                            'partial quarantine release' if failures else 'quarantine release', 'Staff review completed', interaction.id)
        await respond(interaction, f'{len(failures)} channels still need restoration. Fix permissions and retry.' if failures else f'Saved access restored for account {member.id}.', ephemeral=True)

    async def quarantine_protected_resources(self, guild):
        protected = {config.TICKET_LOG_CHANNEL_ID, config.SUPPORT_ROLE_ID, *config.LEVEL_ROLES.values()}
        trap = self.bot.get_cog('Soft Banned Diddy')
        if trap is not None:
            try:
                await trap._load()
            except (OSError, ValueError) as exc:
                raise ValueError('Trap configuration is unreadable. Cleanup cannot verify protected resources.') from exc
            cfg = trap._cfg(guild.id)
            protected.update(cfg.get(key) for key in ('channel_id', 'log_channel_id', 'mod_role_id'))
        for name in ('Verification', 'Dashboard'):
            cog = self.bot.get_cog(name)
            if cog is None:
                continue
            cfg = cog.settings.get(guild.id)
            protected.update(value for key, value in cfg.items() if key.endswith('_id') and isinstance(value, int))
            for section in ('auto_roles', 'welcome'):
                protected.update(value for key, value in cfg.get(section, {}).items()
                                 if key.endswith('_id') and isinstance(value, int))
        tickets = self.bot.get_cog('TicketSystem')
        if tickets is not None:
            cfg = await tickets.config_manager.get_guild(guild.id)
            protected.update(value for key, value in cfg.items() if key.endswith('_id') and isinstance(value, int))
        return protected - {0, None}

    async def quarantine_report(self, interaction, section):
        report = await self.quarantine_resources.discovery(
            interaction.guild, history=section in {'audit', 'permissions', 'resources'},
        )
        trap = self.bot.get_cog('Soft Banned Diddy')
        report['trap_resources'] = []
        if trap is not None:
            try:
                await trap._load()
                cfg = trap._cfg(interaction.guild_id)
                report['trap_resources'] = sorted(cfg[key] for key in ('channel_id', 'log_channel_id', 'mod_role_id')
                                                  if cfg.get(key))
            except (OSError, ValueError):
                report['issues'].append('Trap configuration could not be loaded.')
        if section == 'permissions':
            data = {key: report[key] for key in ('canonical', 'permissions', 'issues', 'failures', 'member_failures')}
        elif section == 'resources':
            data = {key: report[key] for key in ('canonical', 'managed', 'uncertain', 'holders', 'missing',
                                                'previous', 'pending_creations', 'log_channel', 'trap_resources')}
        else:
            data = report
        await respond(interaction, status_text(report), file=audit_file(section, data), ephemeral=True,
                      allowed_mentions=discord.AllowedMentions.none())

    async def quarantine_preview(self, interaction, operation):
        guild = interaction.guild
        try:
            if operation == 'cleanup':
                protected = await self.quarantine_protected_resources(guild)
                preview = await self.quarantine_resources.cleanup_plan(guild, protected)
                count = len(preview['candidates'])
                text = (f'{count} verified obsolete resources eligible for deletion. The report lists '
                        'affected overwrites and resources kept because they are assigned or protected.' if count
                        else 'No verified obsolete resources are eligible for cleanup.')
                view = QuarantineConfirmation(interaction, self, operation, preview) if count else None
                audit = await self.quarantine_resources.discovery(guild)
                file = audit_file(operation, {**preview, 'uncertain_preserved': audit['uncertain'],
                                               'assigned_roles': audit['holders']})
            else:
                preview = await self.quarantine_resources.discovery(guild)
                summary = ('Repair canonical resources and channel permissions?' if operation == 'repair'
                           else f"Move {len(preview['pending'])} tracked members to the current marker?")
                text = summary + '\n' + status_text(preview)
                view = QuarantineConfirmation(interaction, self, operation, preview)
                file = audit_file(operation, preview)
        except (ValueError, OSError, discord.HTTPException) as exc:
            return await respond(interaction, str(exc) if isinstance(exc, ValueError)
                                 else 'Could not verify resources. Check bot access and saved data.', ephemeral=True)
        await respond(interaction, text, view=view, file=file, ephemeral=True,
                      allowed_mentions=discord.AllowedMentions.none())

    async def run_quarantine_operation(self, interaction, operation, preview=None):
        guild = interaction.guild
        if not is_quarantine_admin(interaction):
            raise ValueError('Administrator access is required.')
        if operation == 'cleanup':
            protected = await self.quarantine_protected_resources(guild)
            deleted, snapshot = await self.quarantine_resources.cleanup(guild, interaction.user, preview, protected)
            planned = len(preview['candidates'])
            text = f'Deleted {len(deleted)} of {planned} resources.'
            if len(deleted) < planned:
                text += ' Cleanup stopped after a state or permission change. Re-audit before proceeding.'
            return f'{text} Snapshot: {snapshot or "none"}.', audit_file('cleanup-result', deleted)
        if preview is not None:
            current = await self.quarantine_resources.discovery(guild)
            if current != preview:
                raise ValueError('Quarantine state changed. Open a fresh preview before continuing.')
        if operation == 'migrate':
            members, failed = await self.quarantine_resources.migrate(guild)
            return (f'Migrated {len(members)} members. {len(failed)} require review.',
                    audit_file('migration-result', {'migrated': members, 'failed': failed}))
        if operation == 'sync':
            async with self.quarantine_service.lock:
                state = await self.quarantine_service.load()
                if not state.get(str(guild.id), {}).get('configured'):
                    raise ValueError('Run /quarantine_setup first.')
        await self.quarantine_service.setup(guild, create=operation == 'repair')
        report = await self.quarantine_resources.discovery(guild)
        return status_text(report), audit_file(operation, report)

    async def quarantine_write(self, interaction, operation):
        try:
            text, file = await self.run_quarantine_operation(interaction, operation)
        except ValueError as exc:
            return await respond(interaction, str(exc), ephemeral=True)
        await respond(interaction, text, file=file, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @quarantine_group.command(name='audit', description='Audit resources, assignments and effective permissions')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_audit(self, interaction: discord.Interaction):
        await self.quarantine_report(interaction, 'audit')

    @quarantine_group.command(name='status', description='Show quarantine health and active members')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_status(self, interaction: discord.Interaction):
        await self.quarantine_report(interaction, 'status')

    @quarantine_group.command(name='permissions', description='Inspect quarantine permissions and access conflicts')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_permissions(self, interaction: discord.Interaction):
        await self.quarantine_report(interaction, 'permissions')

    @quarantine_group.command(name='resources', description='List canonical and bot-managed resources')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_resources_command(self, interaction: discord.Interaction):
        await self.quarantine_report(interaction, 'resources')

    @quarantine_group.command(name='repair', description='Repair or replace missing canonical resources')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_repair(self, interaction: discord.Interaction):
        await self.quarantine_write(interaction, 'repair')

    @quarantine_group.command(name='sync', description='Reconcile permissions for existing quarantine resources')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_sync(self, interaction: discord.Interaction):
        await self.quarantine_write(interaction, 'sync')

    @quarantine_group.command(name='migrate', description='Preview and confirm active quarantine migrations')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_migrate(self, interaction: discord.Interaction):
        await self.quarantine_preview(interaction, 'migrate')

    @quarantine_group.command(name='cleanup', description='Preview and confirm obsolete resource cleanup')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_cleanup(self, interaction: discord.Interaction):
        await self.quarantine_preview(interaction, 'cleanup')

    @quarantine_group.command(name='dashboard', description='Open private quarantine management controls')
    @app_commands.check(is_quarantine_admin)
    @acknowledged(ephemeral=True)
    async def quarantine_dashboard(self, interaction: discord.Interaction):
        report = await self.quarantine_resources.discovery(interaction.guild)
        await respond(interaction, status_text(report), view=QuarantineDashboard(interaction, self), ephemeral=True)

    @app_commands.command(name='create_channel', description='Create a text channel, voice channel or category.')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.cooldown(2, 15, key=lambda i: (i.guild_id, i.user.id))
    @acknowledged(ephemeral=True)
    @app_commands.choices(kind=[app_commands.Choice(name='Text', value='text'), app_commands.Choice(name='Voice', value='voice'), app_commands.Choice(name='Category', value='category')])
    async def create_channel(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100],
                             kind: str = 'text', category: discord.CategoryChannel | None = None, private: bool = False):
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_channels:
            return await respond(interaction, 'The bot needs Manage Channels.', ephemeral=True)
        if private and not guild.me.guild_permissions.manage_roles:
            return await respond(interaction, 'Private channel overwrites also require Manage Roles.', ephemeral=True)
        if not name.strip() or (category and category.guild.id != guild.id):
            return await respond(interaction, 'Enter a channel name and select a category from this server.', ephemeral=True)
        if category and not category.permissions_for(interaction.user).manage_channels:
            return await respond(interaction, 'You need Manage Channels in the selected category.', ephemeral=True)
        if kind == 'category' and category is not None:
            return await respond(interaction, 'Categories cannot be nested. Clear the category option.', ephemeral=True)
        async with self.quarantine_service.lock:
            await self.quarantine_service.load()
            settings = self.quarantine_service.state.get(str(guild.id), {})
            overwrites = dict(category.overwrites) if category else {}
            if private:
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
                overwrites[interaction.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                # A private channel starts with only the creator, bot and admins.
                overwrites = {target: value for target, value in overwrites.items()
                              if target in (guild.default_role, interaction.user, guild.me)}
            member_before = {}
            if settings.get('configured'):
                if not guild.me.guild_permissions.manage_roles:
                    return await respond(interaction, 'Channel creation needs Manage Roles to preserve configured quarantine protections.', ephemeral=True)
                role = guild.get_role(settings['role'])
                if role:
                    overwrites[role] = discord.PermissionOverwrite(**DENIED)
                if kind != 'category':
                    for user_id, record in settings['members'].items():
                        if record['phase'] != 'releasing' and guild.get_member(int(user_id)):
                            member = guild.get_member(int(user_id))
                            member_before[user_id] = overwrite_dict(overwrites.get(member, discord.PermissionOverwrite()))
                            overwrites[member] = discord.PermissionOverwrite(**DENIED)
            kwargs = dict(name=name.strip(), reason=f'Channel command by {interaction.user.id}')
            if overwrites:
                kwargs['overwrites'] = overwrites
            if kind == 'category':
                channel = await guild.create_category(**kwargs)
            else:
                kwargs['category'] = category
                method = guild.create_voice_channel if kind == 'voice' else guild.create_text_channel
                channel = await method(**kwargs)
            if settings.get('configured'):
                if kind != 'category':
                    for user_id, before in member_before.items():
                        settings['members'][user_id]['overwrites'][str(channel.id)] = {
                            'before': before, 'applied': overwrite_dict(discord.PermissionOverwrite(**DENIED))}
                    await self.quarantine_service.save()
                await self.quarantine_service.reconcile_channel(guild, channel, settings)
        await respond(interaction, embed=success_embed('Channel created', f'{channel.mention} • {kind}'), ephemeral=True)

    @app_commands.command(name='bot_profile', description='Bot owner: refresh the dynamic description and presence.')
    @acknowledged(ephemeral=True)
    async def profile_command(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            return await respond(interaction, 'Only the bot application owner can update its profile.', ephemeral=True)
        await self.refresh_profile()
        await respond(interaction, f'Profile refreshed. Created by {config.BOT_CREATOR_NAME}. Automatic updates run every 15 minutes.', ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        try:
            await self.quarantine_service.channel_changed(channel)
        except Exception as exc:
            self.last_channel_error = f'Channel {channel.id}: {type(exc).__name__}'
            log.warning('Quarantine channel protection failed for %s: %s', channel.id, type(exc).__name__)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if before.overwrites != after.overwrites or getattr(before, 'category_id', None) != getattr(after, 'category_id', None):
            await self.on_guild_channel_create(after)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.recovering or time.monotonic() - self._last_recovery < 30:
            return
        self.recovering = True
        try:
            await self.recover()
        finally:
            self.recovering = False
            self._last_recovery = time.monotonic()

    @commands.Cog.listener()
    async def on_resumed(self):
        await self.on_ready()

    async def recover(self):
        for guild in self.bot.guilds:
            if guild.unavailable:
                continue
            try:
                health = await self.quarantine_resources.discovery(guild)
                log.info('Quarantine startup guild=%s configured=%s missing=%s conflicts=%s members=%s migrations=%s errors=%s',
                         guild.id, health['configured'], health['missing'], len(health['permissions']),
                         len(health['members']), len(health['pending']), health['issues'])
                if not health['configured'] or health['missing']:
                    continue
                await self.quarantine_service.setup(guild, create=False)
                async with self.quarantine_service.lock:
                    state = await self.quarantine_service.load()
                    settings = state.get(str(guild.id), {})
                    records = dict(settings.get('members', {}))
                for identifier, record in records.items():
                    member = guild.get_member(int(identifier))
                    if not member or record['phase'] == 'releasing' or identifier in health['pending']:
                        continue
                    marker = record.get('marker', health['canonical']['role'])
                    if record['phase'] == 'active' and marker not in {role.id for role in member.roles}:
                        actor = await self._authorized_manual_release_actor(member, marker, settings, since=record.get('confirmed_at'))
                        if actor:
                            await self.quarantine_service.release(guild, actor, member, expected_marker=marker)
                            continue
                    if record['phase'] != 'active' or marker not in {role.id for role in member.roles}:
                        await self.on_member_join(member)
            except (ValueError, OSError, discord.HTTPException) as exc:
                self.last_channel_error = f'Guild {guild.id}: {type(exc).__name__}'
                log.warning('Quarantine startup review failed for guild %s: %s', guild.id, type(exc).__name__)

    async def _authorized_manual_release_actor(self, member, marker_role_id, settings, *, since=None):
        """Resolve a marker removal to a verified administrator/configured moderator.

        A gateway event has no actor. Trust only a recent Discord audit entry that
        explicitly lists the removed quarantine marker, never a role change alone.
        """
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            log.warning(
                'Cannot verify manual quarantine release for guild %s member %s: '
                'bot needs View Audit Log. Staff can use /quarantine_release.',
                guild.id, member.id,
            )
            return None

        staff_roles = {settings.get('staff')}
        trap = self.bot.get_cog('Soft Banned Diddy')
        if trap is not None:
            try:
                await trap._load()
                staff_roles.add(trap._cfg(guild.id).get('mod_role_id'))
            except (ValueError, OSError):
                log.warning('Could not read configured trap moderator role in guild %s', guild.id)
        staff_roles.discard(None)
        staff_roles.discard(0)
        observed = discord.utils.utcnow()

        for attempt in range(3):
            try:
                async for entry in guild.audit_logs(
                    limit=100 if since else 12, action=discord.AuditLogAction.member_role_update
                ):
                    if getattr(getattr(entry, 'target', None), 'id', None) != member.id:
                        continue
                    when = getattr(entry, 'created_at', None)
                    if when is None or (when.timestamp() < since if since else not (
                        -5 <= (observed - when).total_seconds() <= 20
                    )):
                        continue
                    removed = getattr(getattr(entry, 'before', None), 'roles', None) or []
                    added = getattr(getattr(entry, 'after', None), 'roles', None) or []
                    if any(getattr(role, 'id', None) == marker_role_id for role in added):
                        if since:
                            return None
                        continue
                    if not any(getattr(role, 'id', None) == marker_role_id for role in removed):
                        continue
                    user_id = getattr(entry, 'user_id', None) or getattr(getattr(entry, 'user', None), 'id', None)
                    if user_id is None or user_id == me.id or user_id == member.id:
                        return None
                    actor = guild.get_member(user_id)
                    if actor is None:
                        try:
                            actor = await guild.fetch_member(user_id)
                        except (discord.NotFound, discord.Forbidden):
                            return None
                    if not is_above(actor, member):
                        return None
                    perms = actor.guild_permissions
                    is_admin = actor.id == guild.owner_id or perms.administrator
                    is_configured_mod = (
                        any(role.id in staff_roles for role in actor.roles)
                        and perms.manage_roles
                    )
                    return actor if is_admin or is_configured_mod else None
            except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
                log.warning(
                    'Cannot inspect quarantine role-removal audit in guild %s: %s',
                    guild.id, type(exc).__name__,
                )
                return None
            if attempt < 2:
                # Audit entries can arrive after the member-update gateway event.
                await asyncio.sleep(0.75)
        return None

    async def _handle_quarantine_role_change(self, before, after):
        guild = after.guild
        key = (guild.id, after.id)
        if key in self._manual_release_checks:
            return
        self._manual_release_checks.add(key)
        try:
            async with self.quarantine_service.lock:
                state = await self.quarantine_service.load()
                settings = state.get(str(guild.id), {})
                record = settings.get('members', {}).get(str(after.id))
                if not record or record.get('phase') == 'releasing':
                    return
                marker_id = record.get('marker', settings.get('role'))
                if record.get('replacement_marker'):
                    return
                # Snapshot only the configured values, never the mutable journal.
                configured = {'staff': settings.get('staff')}
            before_ids = {role.id for role in before.roles}
            after_ids = {role.id for role in after.roles}
            if marker_id in before_ids and marker_id not in after_ids:
                actor = await self._authorized_manual_release_actor(after, marker_id, configured)
                if actor is not None:
                    try:
                        failures = await self.quarantine_service.release(guild, actor, after, expected_marker=marker_id)
                    except (ValueError, discord.HTTPException, OSError) as exc:
                        log.warning(
                            'Verified staff removal by %s could not finish quarantine release '
                            'for member %s in guild %s: %s',
                            actor.id, after.id, guild.id, type(exc).__name__,
                        )
                        # Existing journal remains for explicit staff recovery.
                        return
                    await notify_action(
                        self.bot, guild, actor, after,
                        'partial manual quarantine release' if failures else 'manual quarantine release',
                        'Verified moderator removed the Quarantined marker.',
                        f'manual-release:{guild.id}:{after.id}:{int(time.time())}',
                    )
                    return
                log.warning(
                    'Quarantine marker removed for member %s in guild %s without '
                    'verified staff approval; preserving the quarantine. '
                    'Use /quarantine_release to authorize a release.',
                    after.id, guild.id,
                )
                embed = discord.Embed(
                    title='Unverified quarantine marker removal',
                    description=f'Account <@{after.id}> • ID {after.id}\nMarker {marker_id} • Guild {guild.id}',
                    timestamp=discord.utils.utcnow(),
                )
                await private_mod_log(guild, embed)
            if marker_id not in after_ids or after_ids - before_ids:
                await self.on_member_join(after)
        finally:
            self._manual_release_checks.discard(key)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if before.permissions == after.permissions:
            return
        async with self.quarantine_service.lock:
            state = await self.quarantine_service.load()
            settings = state.get(str(after.guild.id), {})
            tracked = settings.get('members', {})
            if after.id == settings.get('role'):
                if not self.quarantine_service.valid_marker(after.guild, after):
                    log.warning('Unsafe canonical quarantine marker guild=%s role=%s', after.guild.id, after.id)
                return
            affected = [member for member in after.members if str(member.id) in tracked]
        for member in affected:
            await self.on_member_join(member)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            try:
                await self._handle_quarantine_role_change(before, after)
            except Exception as exc:
                self.last_channel_error = f'Member {after.id}: {type(exc).__name__}'
                log.exception('Quarantine role-change review failed for member %s', after.id)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            await self.quarantine_service.rejoin(member)
        except Exception as exc:
            self.last_channel_error = f'Member {member.id}: {type(exc).__name__}'
            log.warning('Quarantine rejoin protection failed: %s', type(exc).__name__)
            await notify_action(self.bot, member.guild, member.guild.me, member,
                                'quarantine protection needs review',
                                'Review role privileges, bot permissions and the saved quarantine record.',
                                f'quarantine-reconcile:{member.guild.id}:{member.id}:{int(time.time() // 300)}')


async def setup(bot):
    await bot.add_cog(SecurityCog(bot))

"""Restart-safe quarantine journal and channel permission reconciliation."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

import discord

from services.permission_service import bot_can_act, can_assign_role, is_above
from utils.storage import write_json_atomic

log = logging.getLogger(__name__)

# View Channel masks ordinary member access to text, voice and child threads.
# Keeping the overwrite small avoids requiring unrelated moderation permissions.
DENIED = dict(view_channel=False)
ROOM = dict(view_channel=True, read_message_history=True, send_messages=True)


class ChannelAccessError(ValueError):
    """A safe permission snapshot requires visibility into the channel."""


DANGEROUS = ('administrator', 'manage_roles', 'manage_guild', 'manage_channels',
             'ban_members', 'kick_members', 'moderate_members', 'manage_webhooks',
             'manage_events', 'manage_expressions', 'view_audit_log', 'create_events',
             'create_expressions', 'view_guild_insights')


def overwrite_dict(overwrite):
    # discord.py yields the historical read_messages alias when iterated.
    return {('view_channel' if name == 'read_messages' else name): value
            for name, value in overwrite if value is not None}


def normalize_overwrite(data):
    return overwrite_dict(discord.PermissionOverwrite(**data))


async def resource_ownership(guild, settings, history):
    managed = {int(identifier): kind for identifier, kind in settings.get('managed', {}).items()}
    if not history or guild.me is None or not guild.me.guild_permissions.view_audit_log:
        return managed
    for action in (discord.AuditLogAction.role_create, discord.AuditLogAction.channel_create):
        try:
            async for entry in guild.audit_logs(limit=100, action=action):
                if entry.user_id != guild.me.id or not entry.target or not entry.reason:
                    continue
                if entry.reason != 'Private quarantine setup' and not entry.reason.startswith('Private quarantine setup '):
                    continue
                identifier = entry.target.id
                if action == discord.AuditLogAction.role_create:
                    managed[identifier] = 'role'
                else:
                    channel = guild.get_channel(identifier)
                    if isinstance(channel, discord.CategoryChannel):
                        managed[identifier] = 'category'
                    elif isinstance(channel, discord.TextChannel):
                        managed[identifier] = 'room'
        except discord.HTTPException as exc:
            log.warning('Quarantine ownership audit unavailable guild=%s HTTP=%s', guild.id, exc.status)
    return managed

async def has_quarantine_record(bot, member):
    get_cog = getattr(bot, 'get_cog', lambda name: None)
    service = getattr(get_cog('Security'), 'quarantine_service', None)
    if not isinstance(service, QuarantineService):
        return False
    async with service.lock:
        try:
            state = await service.load()
        except (OSError, ValueError):
            log.warning('Automatic role assignment paused for guild %s: quarantine journal unreadable', member.guild.id)
            return True
        return str(member.id) in state.get(str(member.guild.id), {}).get('members', {})


class QuarantineService:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        self.state = None

    async def load(self):
        if self.state is None:
            def read():
                if not self.path.exists():
                    return {}
                result = json.loads(self.path.read_text())
                if not isinstance(result, dict):
                    raise ValueError('Invalid quarantine journal')
                for settings in result.values():
                    if (not isinstance(settings, dict) or not isinstance(settings.get('members', {}), dict)
                            or any(type(settings.get(key, 0)) is not int for key in ('role', 'room', 'category', 'staff'))):
                        raise ValueError('Invalid quarantine guild settings')
                    for identifier, record in settings.get('members', {}).items():
                        if (not identifier.isascii() or not identifier.isdigit() or not isinstance(record, dict)
                                or not isinstance(record.get('overwrites'), dict)
                                or not isinstance(record.get('roles'), list)
                                or record.get('phase') not in {'applying', 'active', 'partial', 'releasing'}):
                            raise ValueError('Invalid quarantine member record')
                return result
            self.state = await asyncio.to_thread(read)
        return self.state

    async def save(self):
        try:
            await write_json_atomic(str(self.path), self.state)
        except OSError:
            # Subsequent recovery must use the last durable snapshot.
            self.state = None
            raise

    def settings(self, guild):
        return self.state.setdefault(str(guild.id), {'members': {}})

    def resources(self, guild, settings):
        role = guild.get_role(settings.get('role', 0))
        category = guild.get_channel(settings.get('category', 0))
        room = guild.get_channel(settings.get('room', 0))
        return role, category, room

    @staticmethod
    def check_bot(guild):
        if not guild.me or not guild.me.guild_permissions.manage_channels or not guild.me.guild_permissions.manage_roles:
            raise ValueError('The bot needs Manage Channels and Manage Roles. Its role must outrank the quarantine role and target.')

    @staticmethod
    def valid_marker(guild, role):
        return role is not None and role.permissions.value == 0 and can_assign_role(guild, role)

    @staticmethod
    def channel_write_allowed(channel, guild):
        if not channel.permissions_for(guild.me).view_channel:
            raise ChannelAccessError('The bot needs View Channel before snapshotting permissions.')

    @staticmethod
    async def set_overwrite(channel, target, before, after, reason):
        if before == after:
            return
        if not channel.permissions_for(channel.guild.me).manage_roles:
            raise ChannelAccessError('The bot needs Manage Roles in this channel to change access.')
        await channel.set_permissions(
            target, overwrite=discord.PermissionOverwrite(**after) if after else None, reason=reason,
        )

    async def create_resource(self, guild, settings, kind, create, *args, **kwargs):
        pending = settings.setdefault('pending_creations', {})
        entry = pending.get(kind)
        if entry is not None:
            # If the previous process stopped between Discord and the journal,
            # identify the exact request by its persisted audit reason.
            if not guild.me.guild_permissions.view_audit_log:
                raise ValueError(f'Previous {kind} creation is unverified. Grant View Audit Log and retry.')
            action = discord.AuditLogAction.role_create if kind == 'role' else discord.AuditLogAction.channel_create
            async for item in guild.audit_logs(action=action, limit=100, after=discord.Object(entry['after'])):
                if item.user_id == guild.me.id and item.reason == entry['reason'] and item.target:
                    resource = (await guild.fetch_roles() if kind == 'role' else await guild.fetch_channels())
                    existing = discord.utils.get(resource, id=item.target.id)
                    if existing is not None:
                        break
            else:
                raise ValueError(f'Previous {kind} creation has no matching Discord audit entry. Review it before retrying.')
        else:
            entry = pending[kind] = {
                'reason': f'Private quarantine setup {kind} {uuid4().hex}',
                'after': discord.utils.time_snowflake(discord.utils.utcnow()),
            }
            await self.save()
            try:
                existing = await create(*args, reason=entry['reason'], **kwargs)
            except discord.HTTPException as exc:
                if 400 <= exc.status < 500 and exc.status != 429:
                    del pending[kind]
                    await self.save()
                raise
        settings.setdefault('managed', {})[str(existing.id)] = kind
        settings[kind] = existing.id
        del pending[kind]
        await self.save()
        return existing

    async def reuse_missing_resources(self, guild, settings, role, category, room):
        if not isinstance(guild, discord.Guild) or all((role, category, room)):
            return role, category, room
        known = await resource_ownership(guild, settings, history=True)
        pending = settings.get('pending_creations', {})
        if role is None and 'role' not in pending:
            candidates = [
                candidate for candidate in guild.roles if known.get(candidate.id) == 'role'
                and self.valid_marker(guild, candidate)
            ]
            if candidates:
                tracked = {int(identifier) for identifier in settings['members']}
                candidate_ids = {candidate.id for candidate in candidates}
                async for member in guild.fetch_members(limit=None):
                    if member.id not in tracked:
                        candidate_ids.difference_update(member_role.id for member_role in member.roles)
                    if not candidate_ids:
                        break
                # Retain the newest verified, unoccupied marker when a saved ID is gone.
                role = max((candidate for candidate in candidates if candidate.id in candidate_ids),
                           key=lambda candidate: candidate.id, default=None)
        if category is None and 'category' not in pending:
            prior_category = guild.get_channel(getattr(room, 'category_id', 0) or 0)
            if prior_category and known.get(prior_category.id) == 'category':
                category = prior_category
            else:
                category = max(
                    (candidate for candidate in guild.categories if known.get(candidate.id) == 'category'
                     and not candidate.permissions_for(guild.default_role).view_channel),
                    key=lambda candidate: candidate.id, default=None,
                )
        if room is None and category is not None and 'room' not in pending:
            room = max(
                (candidate for candidate in category.text_channels if known.get(candidate.id) == 'room'
                 and not candidate.last_message_id and not candidate.threads
                 and not candidate.permissions_for(guild.default_role).view_channel),
                key=lambda candidate: candidate.id, default=None,
            )

        for kind, resource in (('role', role), ('category', category), ('room', room)):
            if resource is None or resource.id == settings.get(kind):
                continue
            old = settings.get(kind)
            if old:
                settings.setdefault('previous', {})[str(old)] = kind
                if kind == 'role':
                    for record in settings['members'].values():
                        record.setdefault('marker', old)
            settings[kind] = resource.id
            settings.setdefault('managed', {})[str(resource.id)] = kind
            await self.save()
        return role, category, room

    async def setup(self, guild, staff_role=None, *, create=True):
        async with self.lock:
            await self.load()
            self.check_bot(guild)
            settings = self.settings(guild)
            role, category, room = self.resources(guild, settings)
            if isinstance(guild, discord.Guild):
                if role is None and settings.get('role'):
                    role = discord.utils.get(await guild.fetch_roles(), id=settings['role'])
                if (category is None and settings.get('category')) or (room is None and settings.get('room')):
                    live_channels = await guild.fetch_channels()
                    category = category or discord.utils.get(live_channels, id=settings.get('category'))
                    room = room or discord.utils.get(live_channels, id=settings.get('room'))
                if category is not None and not isinstance(category, discord.CategoryChannel):
                    category = None
                if room is not None and not isinstance(room, discord.TextChannel):
                    room = None
            if create:
                role, category, room = await self.reuse_missing_resources(guild, settings, role, category, room)
            if not create and not all((role, category, room)):
                raise ValueError('A canonical resource is missing. Use /quarantine repair to replace it.')
            if role is not None and not self.valid_marker(guild, role) and not can_assign_role(guild, role):
                raise ValueError('The stored quarantine role is protected or outranks the bot. Correct its hierarchy before repair.')
            if not self.valid_marker(guild, role):
                if not create:
                    raise ValueError('The quarantine marker needs repair.')
                old_marker = settings.get('role')
                for record in settings['members'].values():
                    record.setdefault('marker', old_marker)
                if old_marker:
                    settings.setdefault('previous', {})[str(old_marker)] = 'role'
                role = await self.create_resource(
                    guild, settings, 'role', guild.create_role, name='Quarantined',
                    permissions=discord.Permissions.none(), mentionable=False, hoist=False,
                )
            if staff_role is not None:
                if not can_assign_role(guild, staff_role) or not any(
                    getattr(staff_role.permissions, permission)
                    for permission in ('administrator', 'moderate_members', 'manage_guild')
                ):
                    raise ValueError('Choose a staff role below the bot with Administrator, Moderate Members or Manage Server.')
            elif settings.get('staff'):
                staff_role = guild.get_role(settings['staff'])
                if staff_role is None:
                    raise ValueError('The saved staff role is missing. Select a replacement with /quarantine_setup.')
                if not any(getattr(staff_role.permissions, name) for name in
                           ('administrator', 'moderate_members', 'manage_guild')):
                    raise ValueError('The saved staff role lost moderation permissions. Select a replacement with /quarantine_setup.')
            old_staff_id = settings.get('staff')
            private = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                       manage_channels=True, read_message_history=True),
            }
            if staff_role is not None:
                private[staff_role] = discord.PermissionOverwrite(**ROOM)
                settings['staff'] = staff_role.id
            if category is None:
                if settings.get('category'):
                    settings.setdefault('previous', {})[str(settings['category'])] = 'category'
                category = await self.create_resource(
                    guild, settings, 'category', guild.create_category, 'Private moderation', overwrites=private,
                )
            if room is None:
                if settings.get('room'):
                    settings.setdefault('previous', {})[str(settings['room'])] = 'room'
                room = await self.create_resource(
                    guild, settings, 'room', guild.create_text_channel, 'quarantine-review',
                    category=category, overwrites=private,
                )
            if isinstance(room, discord.TextChannel) and room.category_id != category.id:
                if not create:
                    raise ValueError('The review room is outside its category. Use /quarantine repair.')
                self.channel_write_allowed(room, guild)
                if not room.permissions_for(guild.me).manage_channels:
                    raise ValueError('The bot needs Manage Channels to move the existing review room.')
                room = await room.edit(category=category, sync_permissions=False, reason='Quarantine category repair')
            for channel in (category, room):
                self.channel_write_allowed(channel, guild)
                for target, overwrite in private.items():
                    before = overwrite_dict(channel.overwrites_for(target))
                    await self.set_overwrite(channel, target, before, {**before, **overwrite_dict(overwrite)},
                                             'Private quarantine access')
                if old_staff_id and old_staff_id != settings.get('staff'):
                    old_staff = guild.get_role(old_staff_id)
                    if old_staff is not None:
                        before = overwrite_dict(channel.overwrites_for(old_staff))
                        if before == ROOM:
                            await self.set_overwrite(channel, old_staff, before, {}, 'Previous quarantine staff access')
            settings['configured'] = True
            await self.save()
            failures = await self.reconcile_guild(guild, settings, extra=(category, room), marker=role)
            return {**settings, 'failed_channels': failures}

    async def reconcile_guild(self, guild, settings, *, extra=(), marker=None):
        channels = {channel.id: channel for channel in (*guild.channels, *extra)}
        failures = []
        for channel in sorted(channels.values(), key=lambda item: not isinstance(item, discord.CategoryChannel)):
            try:
                await self.reconcile_channel(guild, channel, settings, marker=marker)
            except (discord.HTTPException, ChannelAccessError) as exc:
                failures.append(channel.id)
                log.warning('Quarantine channel repair guild=%s channel=%s error=%s',
                            guild.id, channel.id, type(exc).__name__)
        if settings.get('failed_channels') != failures:
            settings['failed_channels'] = failures
            await self.save()
        return failures

    async def reconcile_channel(self, guild, channel, settings, *, marker=None):
        self.channel_write_allowed(channel, guild)
        role = marker or guild.get_role(settings.get('role', 0))
        if not self.valid_marker(guild, role):
            raise ChannelAccessError('The canonical quarantine marker is missing or unsafe.')
        is_private = channel.id in (settings.get('category'), settings.get('room'))
        if is_private:
            public = channel.overwrites_for(guild.default_role)
            if public.view_channel is not False:
                before = overwrite_dict(public)
                await self.set_overwrite(channel, guild.default_role, before, {**before, **DENIED},
                                         'Private quarantine boundary')
            for target, overwrite in list(channel.overwrites.items()):
                if overwrite.view_channel is not True or target.id in (guild.me.id, guild.owner_id):
                    continue
                if channel.id == settings.get('room') and str(target.id) in settings['members']:
                    continue
                permissions = getattr(target, 'guild_permissions', getattr(target, 'permissions', discord.Permissions.none()))
                if permissions.administrator or permissions.moderate_members or permissions.manage_guild:
                    continue
                before = overwrite_dict(overwrite)
                await self.set_overwrite(channel, target, before, {**before, **DENIED}, 'Private quarantine boundary')
        if not getattr(channel, 'permissions_synced', False):
            before = overwrite_dict(channel.overwrites_for(role))
            if before.get('view_channel') is not False:
                await self.set_overwrite(channel, role, before, {**before, **DENIED}, 'Quarantine channel safety')
        if isinstance(channel, discord.CategoryChannel):
            return
        for user_id, record in settings['members'].items():
            if record['phase'] == 'releasing':
                continue
            member = guild.get_member(int(user_id))
            if member is not None:
                await self.restrict_channel(channel, member, record, channel.id == settings.get('room'))

    async def restrict_channel(self, channel, member, record, private):
        self.channel_write_allowed(channel, member.guild)
        key = str(channel.id)
        current = overwrite_dict(channel.overwrites_for(member))
        entry = record['overwrites'].get(key)
        controlled = ROOM if private else DENIED
        desired = {**current, **controlled}
        if entry is None:
            entry = record['overwrites'][key] = {'before': current, 'applied': controlled}
            await self.save()
        else:
            entry['before'] = normalize_overwrite(entry['before'])
            applied = normalize_overwrite(entry['applied'])
            # When a former review room is retired, undo only its previous
            # managed send/history grants and keep its View Channel deny.
            for name in ROOM.keys() - controlled.keys():
                if (applied.get(name) is True and current.get(name) is True
                        and entry['before'].get(name) != current.get(name)):
                    if name in entry['before']:
                        desired[name] = entry['before'][name]
                    else:
                        desired.pop(name, None)
            if entry['applied'] != controlled:
                entry['applied'] = controlled
                await self.save()
        await self.set_overwrite(channel, member, current, desired, 'Member quarantine')

    async def quarantine(self, guild, actor, member, reason):
        async with self.lock:
            await self.load()
            self.check_bot(guild)
            if member.bot or not is_above(actor, member) or not bot_can_act(guild, member):
                raise ValueError('You and the bot must outrank a human target. The server owner cannot be quarantined.')
            settings = self.settings(guild)
            role, category, room = self.resources(guild, settings)
            if not settings.get('configured') or not all((role, category, room)):
                raise ValueError('Run /quarantine_setup first.')
            if not self.valid_marker(guild, role):
                raise ValueError('The quarantine marker must be editable, below the bot and have no guild permissions.')
            retained = [r for r in member.roles if not r.is_default() and (r.managed or r >= guild.me.top_role)]
            if any(any(getattr(r.permissions, permission, False) for permission in DANGEROUS) for r in [guild.default_role, *retained]):
                raise ValueError('The everyone role or an uneditable role grants privileged guild access. Remove that access before quarantine.')
            records = settings['members']
            record = records.get(str(member.id))
            if record is not None and record['phase'] == 'releasing':
                raise ValueError('Finish /quarantine_release for this member before applying another quarantine.')
            if record is None:
                record = records[str(member.id)] = {
                    'phase': 'applying', 'roles': [r.id for r in member.roles if not r.is_default() and r != role and r not in retained],
                    'had_marker': role in member.roles, 'marker': role.id, 'overwrites': {}, 'reason': reason[:500], 'actor': actor.id,
                    'role_permissions': {str(r.id): r.permissions.value for r in member.roles},
                }
                await self.save()
            if record.get('marker', role.id) != role.id:
                raise ValueError('This member has a pending marker migration. Use /quarantine migrate.')
            record.setdefault('marker', role.id)
            record['phase'] = 'applying'
            await self.save()
            removable = [r for r in member.roles if not r.is_default() and r != role and r not in retained]
            if removable:
                await member.remove_roles(*removable, reason='Quarantine access suspension', atomic=True)
            if role not in member.roles:
                await member.add_roles(role, reason='Quarantine marker', atomic=True)
            failures = []
            for channel in guild.channels:
                if isinstance(channel, discord.CategoryChannel):
                    continue
                try:
                    await self.restrict_channel(channel, member, record, channel.id == room.id)
                except (discord.HTTPException, ChannelAccessError):
                    failures.append(channel.id)
            if member.voice and member.voice.channel:
                if guild.me.guild_permissions.move_members:
                    try:
                        await member.move_to(None, reason='Quarantine voice isolation')
                    except discord.HTTPException:
                        failures.append(member.voice.channel.id)
                else:
                    failures.append(member.voice.channel.id)
            record['phase'] = 'partial' if failures else 'active'
            if not failures:
                record['confirmed_at'] = discord.utils.utcnow().timestamp()
            record['failed_channels'] = failures
            await self.save()
            return record.copy()

    async def release(self, guild, actor, member, *, expected_marker=None):
        async with self.lock:
            await self.load()
            self.check_bot(guild)
            if not is_above(actor, member) or not bot_can_act(guild, member):
                raise ValueError('You and the bot must outrank this member.')
            settings = self.settings(guild)
            record = settings['members'].get(str(member.id))
            if record is None:
                raise ValueError('This member has no saved quarantine record.')
            marker_id = record.get('marker', settings.get('role', 0))
            if expected_marker is not None and marker_id != expected_marker:
                raise ValueError('Quarantine marker changed while staff release was being verified.')
            if expected_marker is not None:
                record.update(phase='releasing', release_actor=actor.id)
                await self.save()
            markers = {marker_id, record.get('replacement_marker')}
            for marker in markers:
                marker_role = guild.get_role(marker) if marker else None
                if (marker_role in member.roles and (marker != marker_id or not record['had_marker'])
                        and not can_assign_role(guild, marker_role, actor=actor)):
                    raise ValueError('The quarantine marker is beyond current role hierarchy.')
            for identifier in record['roles']:
                role = guild.get_role(identifier)
                if role is None:
                    continue
                if role.managed or role >= guild.me.top_role or (actor.id != guild.owner_id and role >= actor.top_role):
                    raise ValueError('A saved role now outranks the moderator or bot. The server owner must review this release.')
                if role.permissions.value & ~record.get('role_permissions', {}).get(str(identifier), 0):
                    raise ValueError('A saved role gained permissions during quarantine. Review that role before release.')
            record['phase'] = 'releasing'
            record['release_actor'] = actor.id
            await self.save()
            failures = []
            for identifier, entry in list(record['overwrites'].items()):
                channel = guild.get_channel(int(identifier))
                if channel is not None:
                    if not channel.permissions_for(guild.me).view_channel:
                        failures.append(int(identifier))
                        continue
                    before = overwrite_dict(channel.overwrites_for(member))
                    current = dict(before)
                    # Restore only fields still matching our last write, preserving
                    # independent permission edits made by staff during quarantine.
                    prior = normalize_overwrite(entry['before'])
                    for name, applied in normalize_overwrite(entry['applied']).items():
                        if name not in ROOM:
                            continue
                        if current.get(name) == applied:
                            if name in prior:
                                current[name] = prior[name]
                            else:
                                current.pop(name, None)
                    try:
                        await self.set_overwrite(channel, member, before, current, 'Quarantine release')
                    except (discord.HTTPException, ChannelAccessError):
                        failures.append(int(identifier))
                        continue
                del record['overwrites'][identifier]
                await self.save()
            # Keep the marker until all channel restrictions have been restored.
            if failures:
                return failures
            for identifier in list(record['roles']):
                role = guild.get_role(identifier)
                if role is not None:
                    if role.managed or role >= guild.me.top_role or (actor.id != guild.owner_id and role >= actor.top_role):
                        raise ValueError('A saved role now outranks the moderator or bot. Ask the server owner to review the partial release.')
                    if role.permissions.value & ~record.get('role_permissions', {}).get(str(identifier), 0):
                        raise ValueError('A saved role gained permissions during quarantine. Review that role before completing release.')
                    await member.add_roles(role, reason='Restore quarantine role snapshot', atomic=True)
                record['roles'].remove(identifier)
                await self.save()
            for marker in markers:
                role = guild.get_role(marker) if marker else None
                if role is None or role not in member.roles or (marker == marker_id and record['had_marker']):
                    continue
                if not can_assign_role(guild, role, actor=actor):
                    raise ValueError('The quarantine marker is beyond current role hierarchy.')
                await member.remove_roles(role, reason='Quarantine released', atomic=True)
            releases = settings.setdefault('releases', {})
            releases[str(member.id)] = {'actor': actor.id, 'at': discord.utils.utcnow().isoformat()}
            if len(releases) > 500:
                del releases[next(iter(releases))]
            log.info("Quarantine release confirmed guild=%s member=%s actor=%s", guild.id, member.id, actor.id)
            del settings['members'][str(member.id)]
            await self.save()
            return []

    async def channel_changed(self, channel):
        async with self.lock:
            await self.load()
            settings = self.state.get(str(channel.guild.id))
            if settings and settings.get('configured'):
                await self.reconcile_channel(channel.guild, channel, settings)

    async def rejoin(self, member):
        async with self.lock:
            await self.load()
            settings = self.state.get(str(member.guild.id), {})
            record = settings.get('members', {}).get(str(member.id))
            if not record or record['phase'] == 'releasing':
                return
            record['phase'] = 'partial'
            await self.save()
        # Use the owner as actor only for restoring a previously authorized record.
        if member.guild.owner:
            await self.quarantine(member.guild, member.guild.owner, member, record['reason'])

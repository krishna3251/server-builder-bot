"""Inspect, migrate and retire quarantine resources from the existing journal."""
from __future__ import annotations

from copy import deepcopy
import logging
from uuid import uuid4

import discord

import config
from services.permission_service import bot_can_act, can_assign_role
from services.quarantine_service import DENIED, overwrite_dict, resource_ownership
from utils.storage import write_json_atomic

log = logging.getLogger(__name__)


def channel_data(channel):
    return {
        'id': channel.id, 'name': getattr(channel, 'name', str(channel.id)),
        'category': getattr(channel, 'category_id', None),
        'overwrites': {str(target.id): overwrite_dict(overwrite)
                       for target, overwrite in channel.overwrites.items()},
    }


class QuarantineResources:
    def __init__(self, journal):
        self.journal = journal

    async def discovery(self, guild, *, history=False):
        async with self.journal.lock:
            state = await self.journal.load()
            settings = deepcopy(state.get(str(guild.id), {}))
            found = await resource_ownership(guild, settings, history)
            roles = {role.id: role for role in guild.roles}
            channels = {channel.id: channel for channel in guild.channels}
            canonical = {kind: settings.get(kind) for kind in ('role', 'category', 'room', 'staff')}
            missing = [kind for kind, identifier in canonical.items()
                       if (not identifier and kind != 'staff') or
                       (identifier and identifier not in (roles if kind in ('role', 'staff') else channels))]
            canonical_role = guild.get_role(canonical['role'] or 0)
            issues = []
            if canonical_role and not self.journal.valid_marker(guild, canonical_role):
                issues.append('Canonical role is privileged, protected or above the bot.')
            if guild.me is None:
                issues.append('The bot member is unavailable.')
            else:
                for permission in ('manage_roles', 'manage_channels', 'view_audit_log', 'move_members'):
                    if not getattr(guild.me.guild_permissions, permission):
                        issues.append(f'Bot lacks {permission.replace("_", " ")}.')
            staff = guild.get_role(canonical['staff'] or 0)
            if staff and not any(getattr(staff.permissions, flag) for flag in (
                'administrator', 'manage_guild', 'moderate_members',
            )):
                issues.append('Saved staff role lacks moderation permissions.')
            if isinstance(guild, discord.Guild):
                for kind, cls in (('category', discord.CategoryChannel), ('room', discord.TextChannel)):
                    resource = channels.get(canonical[kind])
                    if resource is not None and not isinstance(resource, cls):
                        issues.append(f'Canonical {kind} has an invalid channel type.')
                room = channels.get(canonical['room'])
                if room is not None and getattr(room, 'category_id', None) != canonical['category']:
                    issues.append('Review room is outside the canonical category.')
            tracked = settings.get('members', {})
            permissions = []
            for channel in channels.values():
                if not canonical_role or guild.me is None:
                    break
                private = channel.id in (canonical['category'], canonical['room'])
                perms = channel.permissions_for(guild.me)
                if not perms.view_channel or not perms.manage_roles:
                    permissions.append(f'{channel.id}: bot lacks View Channel or Manage Roles')
                if private and channel.permissions_for(guild.default_role).view_channel:
                    permissions.append(f'{channel.id}: visible to @everyone')
                if not getattr(channel, 'permissions_synced', False) and (
                    channel.overwrites_for(canonical_role).view_channel is not False
                ):
                    permissions.append(f'{channel.id}: missing marker View Channel deny')
                if not history or not isinstance(guild, discord.Guild) or isinstance(channel, discord.CategoryChannel):
                    continue
                for identifier, record in tracked.items():
                    member = guild.get_member(int(identifier))
                    if member and record.get('phase') != 'releasing':
                        visible = channel.permissions_for(member).view_channel
                        if visible != (channel.id == canonical['room']):
                            permissions.append(f'{channel.id}: unexpected effective access for {member.id}')
            uncertain = {
                role.id: 'role' for role in roles.values()
                if 'quarantin' in getattr(role, 'name', '').lower()
                and role.id not in found and role.id not in canonical.values()
            }
            uncertain.update({
                channel.id: 'channel' for channel in channels.values()
                if any(word in getattr(channel, 'name', '').lower()
                       for word in ('quarantin', 'private moderation'))
                and channel.id not in found and channel.id not in canonical.values()
            })
            occupied = {
                identifier: sorted(member.id for member in getattr(roles[identifier], 'members', ()))
                for identifier, kind in found.items() if kind == 'role' and identifier in roles
            }
            return {
                'configured': bool(settings.get('configured')),
                'canonical': canonical, 'missing': missing, 'issues': issues,
                'permissions': permissions, 'managed': found, 'uncertain': uncertain, 'holders': occupied,
                'previous': settings.get('previous', {}),
                'members': {identifier: record.get('phase', 'unknown') for identifier, record in tracked.items()},
                'pending': [identifier for identifier, record in tracked.items()
                            if record.get('marker', canonical['role']) != canonical['role']
                            or record.get('replacement_marker')],
                'pending_creations': sorted(settings.get('pending_creations', {})),
                'failures': settings.get('failed_channels', []),
                'member_failures': {identifier: record['failed_channels']
                                    for identifier, record in tracked.items() if record.get('failed_channels')},
                'log_channel': config.MOD_LOG_CHANNEL_ID,
            }

    async def migrate(self, guild):
        async with self.journal.lock:
            await self.journal.load()
            self.journal.check_bot(guild)
            settings = self.journal.settings(guild)
            role, category, room = self.journal.resources(guild, settings)
            if not category or not room or not self.journal.valid_marker(guild, role):
                raise ValueError('Repair the canonical resources before migrating.')
            if await self.journal.reconcile_guild(guild, settings, extra=(category, room), marker=role):
                raise ValueError('Fix channel permission failures before migrating members.')
            live = isinstance(guild, discord.Guild)
            channels = await guild.fetch_channels() if live else guild.channels
            migrated, failed = [], {}
            for identifier, record in settings['members'].items():
                old_id = record.get('marker', role.id)
                if record.get('phase') == 'releasing' or (
                    old_id == role.id and not record.get('replacement_marker')
                ):
                    continue
                try:
                    member = await guild.fetch_member(int(identifier)) if live else guild.get_member(int(identifier))
                    if member is None or not bot_can_act(guild, member):
                        raise ValueError('Member absent or above the bot.')
                    old_role = guild.get_role(old_id) if old_id else None
                    if old_role in member.roles and not can_assign_role(guild, old_role):
                        raise ValueError('Previous role is beyond current bot hierarchy.')
                    if any(
                        channel.overwrites_for(member).view_channel is not (channel.id == room.id)
                        for channel in channels if not isinstance(channel, discord.CategoryChannel)
                    ):
                        raise ValueError('Member channel permissions could not be verified.')
                    record['replacement_marker'] = role.id
                    await self.journal.save()
                    if role not in member.roles:
                        await member.add_roles(role, reason='Quarantine marker migration', atomic=True)
                    member = await guild.fetch_member(member.id) if live else member
                    if role not in member.roles:
                        raise ValueError('Replacement role assignment could not be verified.')
                    if old_role in member.roles and old_role != role:
                        await member.remove_roles(old_role, reason='Verified quarantine marker migration', atomic=True)
                    member = await guild.fetch_member(member.id) if live else member
                    if role not in member.roles or (old_role != role and old_role in member.roles):
                        raise ValueError('Final role migration could not be verified.')
                    if live and any(
                        channel.permissions_for(member).view_channel != (channel.id == room.id)
                        for channel in channels if not isinstance(channel, discord.CategoryChannel)
                    ):
                        raise ValueError('Final effective channel permissions need repair.')
                    record.update(marker=role.id, had_marker=False, phase='active',
                                  confirmed_at=discord.utils.utcnow().timestamp())
                    record.pop('replacement_marker', None)
                    record.pop('failed_channels', None)
                    await self.journal.save()
                    migrated.append(identifier)
                except (ValueError, discord.HTTPException) as exc:
                    failed[identifier] = str(exc) if isinstance(exc, ValueError) else f'Discord HTTP {exc.status}'
                    if record.get('replacement_marker'):
                        record['phase'] = 'partial'
                        await self.journal.save()
            return migrated, failed

    async def cleanup_plan(self, guild, protected=()):
        async with self.journal.lock:
            await self.journal.load()
            return await self._cleanup_plan(guild, protected)

    async def _cleanup_plan(self, guild, protected):
        settings = self.journal.settings(guild)
        candidates, skipped = {}, {}
        self.journal.check_bot(guild)
        ownership = await resource_ownership(guild, settings, history=True)
        live = isinstance(guild, discord.Guild)
        roles = await guild.fetch_roles() if live else guild.roles
        channels = await guild.fetch_channels() if live else guild.channels
        role_map = {role.id: role for role in roles}
        channel_map = {channel.id: channel for channel in channels}
        reserved = set(protected) | {
            settings.get('role'), settings.get('room'), settings.get('category'), settings.get('staff'),
            config.MOD_LOG_CHANNEL_ID, guild.id, getattr(guild, 'system_channel_id', None),
            getattr(guild, 'rules_channel_id', None), getattr(guild, 'public_updates_channel_id', None),
            getattr(guild, 'safety_alerts_channel_id', None), getattr(guild, 'afk_channel_id', None),
        }
        records = settings.get('members', {})
        for record in records.values():
            reserved.update(record.get('roles', ()))
            reserved.update((record.get('marker'), record.get('replacement_marker')))
            reserved.update(int(identifier) for identifier in record.get('overwrites', {}))
        old_roles = {identifier for identifier, kind in ownership.items()
                     if kind == 'role' and identifier not in reserved and identifier in role_map}
        holders = {identifier: [] for identifier in old_roles}
        if old_roles:
            members = [member async for member in guild.fetch_members(limit=None)] if live else guild.members
            for member in members:
                for role in member.roles:
                    if role.id in holders:
                        holders[role.id].append(member.id)
        for identifier, kind in sorted(ownership.items()):
            if identifier in reserved:
                skipped[identifier] = 'canonical, protected, or referenced by live records'
                continue
            if kind == 'role':
                role = role_map.get(identifier)
                if role is None:
                    continue
                if holders[identifier]:
                    skipped[identifier] = 'assigned to ' + ', '.join(map(str, sorted(holders[identifier])))
                    continue
                if not self.journal.valid_marker(guild, role):
                    skipped[identifier] = 'privileged, integration-managed or above the bot'
                    continue
                overrides = {str(channel.id): overwrite_dict(channel.overwrites_for(role))
                             for channel in channels if not channel.overwrites_for(role).is_empty()}
                if any(overwrite != DENIED for overwrite in overrides.values()):
                    skipped[identifier] = 'has permission overrides beyond the quarantine deny'
                    continue
                candidates[identifier] = {
                    'kind': 'role', 'name': role.name, 'permissions': role.permissions.value,
                    'position': role.position, 'overwrites': overrides, 'members': [],
                }
                continue
            channel = channel_map.get(identifier)
            if channel is None:
                continue
            if not await self._empty_channel(guild, channel, kind, channels):
                skipped[identifier] = 'channel is populated, inaccessible, protected or has children'
                continue
            candidates[identifier] = {'kind': kind, **channel_data(channel)}
        return {'candidates': candidates, 'skipped': skipped}

    @staticmethod
    async def _empty_channel(guild, channel, kind, channels):
        perms = channel.permissions_for(guild.me)
        if not perms.view_channel or not perms.manage_channels:
            return False
        if kind == 'category':
            return isinstance(channel, discord.CategoryChannel) and not any(
                getattr(child, 'category_id', None) == channel.id for child in channels
            )
        if (not isinstance(channel, discord.TextChannel) or channel.is_news()
                or not perms.read_message_history or not perms.manage_threads
                or channel.last_message_id or channel.threads):
            return False
        try:
            async for _ in channel.history(limit=1):
                return False
            for private in (False, True):
                async for _ in channel.archived_threads(limit=1, private=private):
                    return False
        except discord.HTTPException:
            return False
        return True

    async def cleanup(self, guild, actor, preview, protected=()):
        if actor.id != guild.owner_id and not actor.guild_permissions.administrator:
            raise ValueError('Administrator access is required for cleanup.')
        async with self.journal.lock:
            await self.journal.load()
            current = await self._cleanup_plan(guild, protected)
            if current != preview:
                raise ValueError('Resources changed after the preview. Run /quarantine cleanup again.')
            if not current['candidates']:
                return [], None
            operation = uuid4().hex
            settings = self.journal.settings(guild)
            snapshot = self.journal.path.with_name(f'quarantine-cleanup-{guild.id}-{operation}.json')
            await write_json_atomic(str(snapshot), {
                'actor': actor.id, 'timestamp': discord.utils.utcnow().isoformat(),
                'settings': settings, 'candidates': current['candidates'],
            })
            deleted = []
            for identifier, candidate in current['candidates'].items():
                if candidate['kind'] == 'role':
                    resource = guild.get_role(identifier)
                    if (not self.journal.valid_marker(guild, resource)
                            or getattr(resource, 'members', [])
                            or not can_assign_role(guild, resource, actor=actor)):
                        break
                else:
                    resource = guild.get_channel(identifier)
                    if resource is None or channel_data(resource) != {
                        k: v for k, v in candidate.items() if k != 'kind'
                    } or not await self._empty_channel(guild, resource, candidate['kind'], guild.channels):
                        break
                try:
                    await resource.delete(reason=f'Confirmed quarantine cleanup {operation} by {actor.id}')
                except discord.HTTPException as exc:
                    log.warning('Quarantine cleanup %s stopped on resource %s HTTP=%s', operation, identifier, exc.status)
                    break
                settings.get('managed', {}).pop(str(identifier), None)
                settings.get('previous', {}).pop(str(identifier), None)
                settings.get('marker_overwrites', {}).pop(str(identifier), None)
                deleted.append(identifier)
                await self.journal.save()
            log.info('Quarantine cleanup guild=%s actor=%s operation=%s deleted=%s',
                     guild.id, actor.id, operation, deleted)
            return deleted, snapshot.name

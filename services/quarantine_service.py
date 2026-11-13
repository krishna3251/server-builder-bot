"""Restart-safe quarantine journal and channel permission reconciliation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import discord

from services.permission_service import bot_can_act, is_above
from utils.storage import write_json_atomic

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
    return {name: value for name, value in overwrite if value is not None}


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
                return result
            self.state = await asyncio.to_thread(read)
        return self.state

    async def save(self):
        await write_json_atomic(str(self.path), self.state)

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

    async def setup(self, guild, staff_role=None):
        async with self.lock:
            await self.load()
            self.check_bot(guild)
            settings = self.settings(guild)
            role, category, room = self.resources(guild, settings)
            if role is None:
                role = await guild.create_role(name='Quarantined', permissions=discord.Permissions.none(),
                                               mentionable=False, hoist=False, reason='Private quarantine setup')
                settings['role'] = role.id
                await self.save()
            if role.managed or role >= guild.me.top_role or role.permissions.value != 0:
                raise ValueError('The stored quarantine role must be editable by the bot and have no guild permissions.')
            private = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            }
            if staff_role is not None:
                private[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                settings['staff'] = staff_role.id
            elif settings.get('staff') and guild.get_role(settings['staff']):
                private[guild.get_role(settings['staff'])] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            if category is None:
                category = await guild.create_category('Private moderation', overwrites=private, reason='Private quarantine setup')
                settings['category'] = category.id
                await self.save()
            if room is None:
                room = await guild.create_text_channel('quarantine-review', category=category, overwrites=private,
                                                       reason='Private quarantine setup')
                settings['room'] = room.id
                await self.save()
            # Explicit member access is granted only while quarantined. The marker
            # role itself cannot reveal every quarantine review to its holders.
            for channel in (category, room):
                for target, overwrite in private.items():
                    await channel.set_permissions(target, overwrite=overwrite, reason='Private quarantine access')
            settings['configured'] = True
            await self.save()
            failures = []
            for channel in sorted(guild.channels, key=lambda item: not isinstance(item, discord.CategoryChannel)):
                try:
                    await self.reconcile_channel(guild, channel, settings)
                except (discord.HTTPException, ChannelAccessError):
                    failures.append(channel.id)
            settings['failed_channels'] = failures
            await self.save()
            return settings.copy()

    async def reconcile_channel(self, guild, channel, settings):
        if not channel.permissions_for(guild.me).view_channel:
            raise ChannelAccessError('The bot needs View Channel before snapshotting permissions.')
        role, _, _ = self.resources(guild, settings)
        if role is None:
            return
        is_private = channel.id in (settings.get('category'), settings.get('room'))
        if is_private:
            public = channel.overwrites_for(guild.default_role)
            if public.view_channel is not False:
                public.view_channel = False
                await channel.set_permissions(guild.default_role, overwrite=public, reason='Private quarantine boundary')
            for target, overwrite in list(channel.overwrites.items()):
                if overwrite.view_channel is not True or target.id in (guild.me.id, guild.owner_id):
                    continue
                if channel.id == settings.get('room') and str(target.id) in settings['members']:
                    continue
                permissions = getattr(target, 'guild_permissions', getattr(target, 'permissions', discord.Permissions.none()))
                if permissions.administrator or permissions.moderate_members or permissions.manage_guild:
                    continue
                overwrite.view_channel = False
                await channel.set_permissions(target, overwrite=overwrite, reason='Private quarantine boundary')
        if not getattr(channel, 'permissions_synced', False):
            overwrite = channel.overwrites_for(role)
            desired = overwrite_dict(discord.PermissionOverwrite(**dict(overwrite_dict(overwrite), **DENIED)))
            if overwrite_dict(overwrite) != desired:
                await channel.set_permissions(role, overwrite=discord.PermissionOverwrite(**desired), reason='Quarantine channel safety')
        if isinstance(channel, discord.CategoryChannel):
            return
        for user_id, record in settings['members'].items():
            if record['phase'] == 'releasing':
                continue
            member = guild.get_member(int(user_id))
            if member is not None:
                await self.restrict_channel(channel, member, record, is_private)

    async def restrict_channel(self, channel, member, record, private):
        if not channel.permissions_for(member.guild.me).view_channel:
            raise ChannelAccessError('The bot needs View Channel before snapshotting permissions.')
        key = str(channel.id)
        current = overwrite_dict(channel.overwrites_for(member))
        entry = record['overwrites'].get(key)
        desired = overwrite_dict(discord.PermissionOverwrite(**dict(current, **(ROOM if private else DENIED))))
        if entry is None:
            entry = record['overwrites'][key] = {'before': current, 'applied': desired}
            await self.save()  # Journal before the remote write so a retry can recover it.
        else:
            if entry['applied'] != desired:
                entry['applied'] = desired
                await self.save()
        if current != desired:
            await channel.set_permissions(member, overwrite=discord.PermissionOverwrite(**desired), reason='Member quarantine')

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
            if role.managed or role >= guild.me.top_role or role.permissions.value != 0:
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
                    'had_marker': role in member.roles, 'overwrites': {}, 'reason': reason[:500], 'actor': actor.id,
                    'role_permissions': {str(r.id): r.permissions.value for r in member.roles},
                }
                await self.save()
            record['phase'] = 'applying'
            await self.save()
            removable = [r for r in member.roles if not r.is_default() and r != role and r not in retained]
            if removable:
                await member.remove_roles(*removable, reason='Quarantine access suspension', atomic=True)
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
            record['failed_channels'] = failures
            await self.save()
            return record.copy()

    async def release(self, guild, actor, member):
        async with self.lock:
            await self.load()
            self.check_bot(guild)
            if not is_above(actor, member) or not bot_can_act(guild, member):
                raise ValueError('You and the bot must outrank this member.')
            settings = self.settings(guild)
            record = settings['members'].get(str(member.id))
            if record is None:
                raise ValueError('This member has no saved quarantine record.')
            for identifier in record['roles']:
                role = guild.get_role(identifier)
                if role is None:
                    continue
                if role.managed or role >= guild.me.top_role or (actor.id != guild.owner_id and role >= actor.top_role):
                    raise ValueError('A saved role now outranks the moderator or bot. The server owner must review this release.')
                if role.permissions.value & ~record.get('role_permissions', {}).get(str(identifier), 0):
                    raise ValueError('A saved role gained permissions during quarantine. Review that role before release.')
            record['phase'] = 'releasing'
            await self.save()
            failures = []
            for identifier, entry in list(record['overwrites'].items()):
                channel = guild.get_channel(int(identifier))
                if channel is not None:
                    if not channel.permissions_for(guild.me).view_channel:
                        failures.append(int(identifier))
                        continue
                    current = overwrite_dict(channel.overwrites_for(member))
                    # Restore only fields still matching our last write, preserving
                    # independent permission edits made by staff during quarantine.
                    for name, applied in entry['applied'].items():
                        if current.get(name) == applied:
                            if name in entry['before']:
                                current[name] = entry['before'][name]
                            else:
                                current.pop(name, None)
                    try:
                        await channel.set_permissions(member, overwrite=discord.PermissionOverwrite(**current) if current else None,
                                                      reason='Quarantine release')
                    except discord.HTTPException:
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
            role = guild.get_role(settings.get('role', 0))
            if role and not record['had_marker']:
                await member.remove_roles(role, reason='Quarantine released', atomic=True)
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

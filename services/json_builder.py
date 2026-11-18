"""JSON server builder — creates roles, categories, channels from a schema dict."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import discord

import config
from services.permission_service import AUTOMATIC_ROLE_RESTRICTED
from utils.storage import write_json_atomic

log = logging.getLogger("services.json_builder")
_BUILD_LOCKS: dict[int, asyncio.Lock] = {}


class BuildInProgress(RuntimeError):
    """A build or cleanup already owns this guild's reservation."""


@asynccontextmanager
async def build_session(guild_id: int):
    """Cover cleanup and creation with one per-server reservation."""
    lock = _BUILD_LOCKS.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        raise BuildInProgress("A build or cleanup is already running in this server.")
    async with lock:
        try:
            yield
        finally:
            _BUILD_LOCKS.pop(guild_id, None)


class BuildJournal:
    """Remember the Discord resources created for one plan on one server."""

    def __init__(self, path, state):
        self.path = path
        self.state = state
        self.resources = {}

    @classmethod
    async def load(cls, guild_id, schema):
        encoded = json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        return await cls.read(Path(config.DATA_DIR) / 'builds' / str(guild_id) / f'{digest}.json')

    @classmethod
    async def read(cls, path):
        try:
            state = json.loads(await asyncio.to_thread(path.read_text, encoding='utf-8'))
        except FileNotFoundError:
            state = {'resources': {}, 'summaries': []}
        return cls(path, state)

    async def save(self):
        await write_json_atomic(str(self.path), self.state)

    async def load_resources(self, guild):
        fetched_roles = None
        for key, identifier in self.state['resources'].items():
            if key.startswith('role:'):
                resource = guild.get_role(identifier)
                if resource is None:
                    if fetched_roles is None:
                        fetched_roles = {role.id: role for role in await guild.fetch_roles()}
                    resource = fetched_roles.get(identifier)
            else:
                resource = guild.get_channel_or_thread(identifier)
                if resource is None:
                    try:
                        resource = await guild.fetch_channel(identifier)
                    except discord.NotFound:
                        pass
            if resource is not None:
                self.resources[key] = resource

    async def get_or_create(self, key, create, **kwargs):
        if key in self.resources:
            return self.resources[key]
        resource = await create(**kwargs)
        identifiers = self.state['resources']
        before = identifiers.get(key)
        identifiers[key] = resource.id
        try:
            await self.save()
        except Exception:
            if before is None:
                identifiers.pop(key, None)
            else:
                identifiers[key] = before
            try:
                await resource.delete(reason='Could not record server builder resource')
            except discord.HTTPException:
                log.exception('Could not remove unrecorded builder resource %s', resource.id)
            raise
        self.resources[key] = resource
        return resource

    async def prepare_cleanup(self, guild, safe_channel_id, protected_channels, protected_roles):
        safe_channel = guild.get_channel(safe_channel_id)
        if safe_channel is None:
            raise ValueError('The command channel must exist before cleanup can be prepared.')
        resource_ids = set(self.state['resources'].values())
        protected_channels = protected_channels | resource_ids | {safe_channel_id}
        protected_channels |= {channel.category_id for channel in guild.channels if channel.id in protected_channels}
        protected_roles = protected_roles | resource_ids | {role.id for role in guild.me.roles}
        if 'cleanup' not in self.state:
            channels = sorted(guild.channels, key=lambda channel: isinstance(channel, discord.CategoryChannel))
            self.state['cleanup'] = {
                'channels': [[channel.id, channel.name] for channel in channels],
                'roles': [[role.id, role.name] for role in guild.roles
                          if not role.is_default() and not role.managed and role < guild.me.top_role],
                'completed': False,
            }
        plan = self.state['cleanup']
        plan['channels'] = [item for item in plan['channels'] if item[0] not in protected_channels]
        plan['roles'] = [item for item in plan['roles'] if item[0] not in protected_roles]
        await self.save()


async def apply_cleanup(guild, actor, journal_path, snapshot, protected_channels=frozenset(), protected_roles=frozenset()):
    """Remove only the resources specifically listed for this owner's approval."""
    if actor.id != guild.owner_id:
        raise ValueError('Only the server owner can approve cleanup.')
    async with build_session(guild.id):
        journal = await BuildJournal.read(journal_path)
        plan = journal.state.get('cleanup')
        if not journal.state.get('complete') or plan is None:
            raise ValueError('Finish the replacement build before approving cleanup.')
        if plan['completed']:
            return 0, []
        if any(plan[key] != snapshot[key] for key in ('channels', 'roles')):
            raise ValueError('The deletion list changed. Open a new cleanup preview before approving it.')
        if protected_channels.intersection(item[0] for item in plan['channels']) or protected_roles.intersection(item[0] for item in plan['roles']):
            raise ValueError('The deletion list contains resources now used by tickets or moderation. Reapply the plan to refresh the list.')
        await journal.load_resources(guild)
        if set(journal.resources) != set(journal.state['resources']):
            raise ValueError('The replacement layout is missing resources. Reapply the same build plan first.')
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles or not me.guild_permissions.manage_channels:
            raise ValueError('The bot needs Manage Roles and Manage Channels before cleanup.')
        channels = [(guild.get_channel(identifier), name) for identifier, name in plan['channels']]
        roles = [(guild.get_role(identifier), name) for identifier, name in plan['roles']]
        if any(channel and not channel.permissions_for(me).manage_channels for channel, _ in channels):
            raise ValueError('The bot cannot manage every listed channel. Check its channel permissions before retrying.')
        if any(role and (role in me.roles or role.is_default() or role.managed or role >= me.top_role) for role, _ in roles):
            raise ValueError('The bot cannot manage every listed role. Check role order before retrying.')

        deleted = 0
        failures = []
        for resource, name in channels + roles:
            if resource is None:
                continue
            try:
                await resource.delete(reason=f'Server builder cleanup approved by owner {actor.id}')
                deleted += 1
            except discord.NotFound:
                pass
            except discord.HTTPException:
                failures.append(name)
        if not failures:
            plan['completed'] = True
            await journal.save()
        return deleted, failures

# ── Permission name → discord.Permissions flag mapping ────────────────────
_PERM_MAP: dict[str, int] = {
    "administrator": discord.Permissions.administrator.flag,
    "manage_guild": discord.Permissions.manage_guild.flag,
    "manage_channels": discord.Permissions.manage_channels.flag,
    "manage_roles": discord.Permissions.manage_roles.flag,
    "manage_messages": discord.Permissions.manage_messages.flag,
    "kick_members": discord.Permissions.kick_members.flag,
    "ban_members": discord.Permissions.ban_members.flag,
    "send_messages": discord.Permissions.send_messages.flag,
    "read_messages": discord.Permissions.read_messages.flag,
    "connect": discord.Permissions.connect.flag,
    "speak": discord.Permissions.speak.flag,
    "mute_members": discord.Permissions.mute_members.flag,
    "deafen_members": discord.Permissions.deafen_members.flag,
    "move_members": discord.Permissions.move_members.flag,
    "mention_everyone": discord.Permissions.mention_everyone.flag,
    "embed_links": discord.Permissions.embed_links.flag,
    "attach_files": discord.Permissions.attach_files.flag,
    "manage_webhooks": discord.Permissions.manage_webhooks.flag,
    "view_audit_log": discord.Permissions.view_audit_log.flag,
    "view_channel": discord.Permissions.view_channel.flag,
    "read_message_history": discord.Permissions.read_message_history.flag,
    "use_application_commands": discord.Permissions.use_application_commands.flag,
    "create_public_threads": discord.Permissions.create_public_threads.flag,
    "send_messages_in_threads": discord.Permissions.send_messages_in_threads.flag,
}

def _resolve_permissions(perm_names: list[str]) -> discord.Permissions:
    """Convert a list of permission name strings to a ``discord.Permissions`` object."""
    value = 0
    for name in perm_names:
        flag = _PERM_MAP.get(name.lower())
        if flag:
            value |= flag
        else:
            log.warning("Unknown permission name: %s", name)
    return discord.Permissions(value)


# ── Named colour presets ──────────────────────────────────────────────────────
_NAMED_COLOURS: dict[str, int] = {
    "red": 0xE74C3C, "dark_red": 0x992D22, "crimson": 0xDC143C,
    "orange": 0xE67E22, "dark_orange": 0xA84300,
    "yellow": 0xF1C40F, "gold": 0xFFD700, "amber": 0xFFBF00,
    "green": 0x2ECC71, "dark_green": 0x1F8B4C, "lime": 0x00FF00, "emerald": 0x50C878,
    "teal": 0x1ABC9C, "dark_teal": 0x11806A, "cyan": 0x00FFFF, "aqua": 0x00FFFF,
    "blue": 0x3498DB, "dark_blue": 0x206694, "navy": 0x000080, "royal_blue": 0x4169E1,
    "purple": 0x9B59B6, "dark_purple": 0x71368A, "violet": 0x8B00FF, "indigo": 0x4B0082,
    "magenta": 0xE91E63, "pink": 0xFFC0CB, "hot_pink": 0xFF69B4, "fuchsia": 0xFF00FF,
    "white": 0xFFFFFF, "light_grey": 0x95A5A6, "grey": 0x7F8C8D, "dark_grey": 0x546E7A,
    "black": 0x010101, "blurple": 0x5865F2, "greyple": 0x99AAB5,
}


def _parse_colour(colour_str: str | None) -> discord.Colour:
    """Parse a colour from hex (#FF0000) or named colour (red, gold, blurple, etc.)."""
    if not colour_str:
        return discord.Colour.default()
    cleaned = colour_str.strip().lower().replace(" ", "_").replace("-", "_")
    # Check named colours first
    if cleaned in _NAMED_COLOURS:
        return discord.Colour(_NAMED_COLOURS[cleaned])
    # Then try hex
    try:
        return discord.Colour(int(cleaned.lstrip("#"), 16))
    except ValueError:
        log.warning("Unknown colour: %s — using default", colour_str)
        return discord.Colour.default()


class BuildProgress:
    """Simple progress tracker that can update an embed message."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.last_update = 0.0

    def advance(self) -> None:
        self.done += 1

    @property
    def bar(self) -> str:
        filled = int((self.done / max(self.total, 1)) * 20)
        return "\u2588" * filled + "\u2591" * (20 - filled) + f" {self.done}/{self.total}"


async def _build_server_unlocked(
    guild: discord.Guild,
    schema: dict[str, Any],
    progress_msg: discord.Message | None = None,
    created_channel_ids: set[int] | None = None,
    journal: BuildJournal | None = None,
) -> tuple[list[str], dict[str, discord.Role]]:
    """Build roles, categories, and channels in *guild* from *schema*.

    Returns ``(logs, role_map)`` where *logs* is a list of description lines
    and *role_map* maps role-name \u2192 created :class:`discord.Role`.
    Raises on fatal errors after rolling-back partially created objects.
    """
    created_roles: list[discord.Role] = []
    created_channels: list[discord.abc.GuildChannel] = []
    logs: list[str] = []
    original_guild_name = guild.name
    renamed = False

    async def create(key, method, **kwargs):
        if journal is not None:
            return await journal.get_or_create(key, method, **kwargs)
        return await method(**kwargs)

    # Count total items for progress
    total = len(schema.get("roles", [])) + sum(
        1 + len(cat.get("channels", [])) for cat in schema.get("categories", [])
    )
    progress = BuildProgress(total)

    try:
        # ── Rename server ────────────────────────────────────────────────────
        if journal is None and schema.get("server_name") and schema["server_name"] != guild.name:
            await guild.edit(name=schema["server_name"])
            renamed = True
            logs.append(f"Renamed server to **{schema['server_name']}**")

        # ── Create roles ─────────────────────────────────────────────────────
        role_map: dict[str, discord.Role] = {}
        for index, role_data in enumerate(schema.get("roles", [])):
            perms = _resolve_permissions(role_data.get("permissions", []))
            colour = _parse_colour(role_data.get("color"))
            role = await create(f"role:{index}", guild.create_role,
                name=role_data["name"],
                colour=colour,
                hoist=role_data.get("hoist", False),
                mentionable=role_data.get("mentionable", False),
                permissions=perms,
            )
            created_roles.append(role)
            role_map[role_data["name"]] = role
            logs.append(f"Role: **{role.name}**")
            progress.advance()
            if progress_msg:
                await _update_progress(progress_msg, progress)

        # ── Create categories + channels ─────────────────────────────────────
        for cat_index, cat_data in enumerate(schema.get("categories", [])):
            # Build permission overwrites for category
            overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
            for ow in cat_data.get("permission_overwrites", []):
                role_name = ow.get("role", "")
                target_role = role_map.get(role_name)
                if not target_role and role_name.lower() == "@everyone":
                    target_role = guild.default_role
                if target_role:
                    allow = _resolve_permissions(ow.get("allow", []))
                    deny = _resolve_permissions(ow.get("deny", []))
                    overwrites[target_role] = discord.PermissionOverwrite.from_pair(allow, deny)

            if guild.me is not None:
                overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
            category = await create(f"category:{cat_index}", guild.create_category, name=cat_data["name"], overwrites=overwrites)
            created_channels.append(category)
            logs.append(f"Category: **{cat_data['name']}**")
            progress.advance()
            if progress_msg:
                await _update_progress(progress_msg, progress)

            for ch_index, ch_data in enumerate(cat_data.get("channels", [])):
                channel_key = f"channel:{cat_index}:{ch_index}"
                # ── Per-channel permission overwrites ────────────────────────
                # Explicit channel overwrites must retain the category's privacy rules.
                ch_overwrites = dict(overwrites)
                for ow in ch_data.get("permission_overwrites", []):
                    target = role_map.get(ow.get("role", ""))
                    if not target and ow.get("role", "").lower() == "@everyone":
                        target = guild.default_role
                    if target:
                        allow = _resolve_permissions(ow.get("allow", []))
                        deny = _resolve_permissions(ow.get("deny", []))
                        explicit = discord.PermissionOverwrite.from_pair(allow, deny)
                        inherited = ch_overwrites.get(target, discord.PermissionOverwrite())
                        combined = discord.PermissionOverwrite.from_pair(*inherited.pair())
                        for permission, value in explicit:
                            if value is not None:
                                setattr(combined, permission, value)
                        ch_overwrites[target] = combined

                ch_type = ch_data.get("type", "text").lower()
                if ch_type == "voice":
                    # Clamp bitrate to guild's max (96000 for unboosted servers)
                    max_bitrate = guild.bitrate_limit
                    bitrate = min(ch_data.get("bitrate", 64000), max_bitrate)
                    kwargs: dict[str, Any] = {
                        "name": ch_data["name"],
                        "category": category,
                        "bitrate": bitrate,
                        "user_limit": ch_data.get("user_limit", 0),
                    }
                    if ch_overwrites:
                        kwargs["overwrites"] = ch_overwrites
                    vc = await create(channel_key, guild.create_voice_channel, **kwargs)
                    created_channels.append(vc)
                    perm_note = f" (perms: {', '.join(o.get('role','') for o in ch_data.get('permission_overwrites', []))})" if ch_overwrites else ""
                    logs.append(f"Voice channel: **{ch_data['name']}**{perm_note}")
                else:
                    kwargs: dict[str, Any] = {
                        "name": ch_data["name"],
                        "category": category,
                        "topic": ch_data.get("topic", ""),
                        "slowmode_delay": ch_data.get("slowmode", 0),
                        "nsfw": ch_data.get("nsfw", False),
                    }
                    if ch_overwrites:
                        kwargs["overwrites"] = ch_overwrites
                    tc = await create(channel_key, guild.create_text_channel, **kwargs)
                    created_channels.append(tc)
                    if created_channel_ids is not None:
                        created_channel_ids.add(tc.id)
                    perm_note = f" (perms: {', '.join(o.get('role','') for o in ch_data.get('permission_overwrites', []))})" if ch_overwrites else ""
                    logs.append(f"Text channel: **#{ch_data['name']}**{perm_note}")

                    # Optional thread creation
                    for thread_index, thread_data in enumerate(ch_data.get("threads", [])):
                        await create(f"thread:{cat_index}:{ch_index}:{thread_index}", tc.create_thread,
                            name=thread_data["name"],
                            type=discord.ChannelType.public_thread,
                            auto_archive_duration=thread_data.get("auto_archive", 1440),
                        )
                        logs.append(f"  \u2514 Created thread: **{thread_data['name']}**")

                progress.advance()
                if progress_msg:
                    await _update_progress(progress_msg, progress)

        if journal is not None and schema.get("server_name") and not journal.state.get('renamed'):
            if guild.name != schema["server_name"]:
                await guild.edit(name=schema["server_name"])
                logs.append(f"Renamed server to **{schema['server_name']}**")
            journal.state['renamed'] = True
            await journal.save()

    except (Exception, asyncio.CancelledError) as exc:
        if journal is not None:
            raise
        log.exception("Build failed; rolling back newly created resources: %s", exc)
        # Restore the server name if this build changed it.
        if renamed:
            try:
                await guild.edit(name=original_guild_name, reason="Server build rollback")
            except discord.HTTPException:
                log.exception("Could not restore the original server name during rollback")
        # Rollback newly created channels/categories and roles.
        for ch in reversed(created_channels):
            try:
                await ch.delete(reason="Server build rollback")
            except Exception:
                log.exception("Could not roll back channel %s", ch.id)
        for role in reversed(created_roles):
            try:
                await role.delete(reason="Server build rollback")
            except Exception:
                log.exception("Could not roll back role %s", role.id)
        raise

    return logs, role_map


def _validate_schema(schema: dict[str, Any]) -> None:
    """Fully validate untrusted plans before any resource is changed."""
    def name(value, label, maximum=100):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{label} must contain 1–{maximum} characters.")

    def permissions(value, label):
        if not isinstance(value, list) or any(not isinstance(p, str) or p.lower() not in _PERM_MAP for p in value):
            raise ValueError(f"{label} must contain supported permission names.")

    def integer(value, label, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{label} must be an integer between {low} and {high}.")

    if not isinstance(schema, dict):
        raise ValueError("Server template must be a JSON object.")
    if "server_name" in schema and schema["server_name"] is not None:
        name(schema["server_name"], "Server name")
        if len(schema["server_name"].strip()) < 2:
            raise ValueError("Server name must contain at least 2 characters.")
    roles, categories = schema.get("roles", []), schema.get("categories", [])
    if not isinstance(roles, list) or not isinstance(categories, list):
        raise ValueError("Roles and categories must be arrays.")
    if len(roles) > 40 or len(categories) > 30:
        raise ValueError("A build supports at most 40 roles and 30 categories.")
    role_names = set()
    for role in roles:
        if not isinstance(role, dict):
            raise ValueError("Every role must be an object.")
        name(role.get("name"), "Role name")
        if role["name"].lower() == "@everyone" or role["name"] in role_names:
            raise ValueError("Role names must be unique and cannot be @everyone.")
        role_names.add(role["name"])
        permissions(role.get("permissions", []), "Role permissions")
        for key in ("hoist", "mentionable"):
            if key in role and type(role[key]) is not bool:
                raise ValueError(f"Role {key} must be true or false.")
        color = role.get("color")
        if color is not None:
            if not isinstance(color, str):
                raise ValueError("Role color must be a named color or hex string.")
            cleaned = color.strip().lower().replace(" ", "_").replace("-", "_")
            if cleaned not in _NAMED_COLOURS:
                try:
                    if not 0 <= int(cleaned.lstrip("#"), 16) <= 0xFFFFFF:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"Invalid role color: {color}") from None

    auto_assign = schema.get("auto_assign")
    if auto_assign is not None:
        if not isinstance(auto_assign, str) or auto_assign not in role_names:
            raise ValueError("auto_assign must name a role in this plan.")
        role = next(r for r in roles if r["name"] == auto_assign)
        if AUTOMATIC_ROLE_RESTRICTED.intersection(p.lower() for p in role.get("permissions", [])):
            raise ValueError("Privileged roles cannot be automatically assigned to members.")

    def overwrites(value):
        if not isinstance(value, list):
            raise ValueError("Permission overwrites must be an array.")
        seen = set()
        for ow in value:
            if not isinstance(ow, dict) or not isinstance(ow.get("role"), str):
                raise ValueError("Every overwrite must be an object with a role name.")
            target = ow["role"]
            if target.lower() != "@everyone" and target not in role_names:
                raise ValueError(f"Unknown overwrite role: {target}")
            target_key = "@everyone" if target.lower() == "@everyone" else target
            if target_key in seen:
                raise ValueError(f"Duplicate overwrite for role: {target}")
            seen.add(target_key)
            permissions(ow.get("allow", []), "Allowed permissions")
            permissions(ow.get("deny", []), "Denied permissions")
            if (target == auto_assign or target.lower() == "@everyone") and AUTOMATIC_ROLE_RESTRICTED.intersection(p.lower() for p in ow.get("allow", [])):
                raise ValueError("Privileged channel permissions cannot be granted to everyone or the automatic member role.")
            if _resolve_permissions(ow.get("allow", [])).value & _resolve_permissions(ow.get("deny", [])).value:
                raise ValueError("A permission cannot be both allowed and denied.")

    channel_count = thread_count = 0
    for category in categories:
        if not isinstance(category, dict):
            raise ValueError("Every category must be an object.")
        name(category.get("name"), "Category name")
        overwrites(category.get("permission_overwrites", []))
        channels = category.get("channels", [])
        if not isinstance(channels, list) or len(channels) > 50:
            raise ValueError("Each category supports at most 50 channels.")
        channel_count += len(channels)
        for channel in channels:
            if not isinstance(channel, dict):
                raise ValueError("Every channel must be an object.")
            name(channel.get("name"), "Channel name")
            if channel.get("type", "text") not in ("text", "voice"):
                raise ValueError("Channel type must be text or voice.")
            overwrites(channel.get("permission_overwrites", []))
            if not isinstance(channel.get("topic", ""), str) or len(channel.get("topic", "")) > 1024:
                raise ValueError("Channel topics must be strings of at most 1,024 characters.")
            integer(channel.get("slowmode", 0), "Slowmode", 0, 21600)
            integer(channel.get("bitrate", 64000), "Bitrate", 8000, 384000)
            integer(channel.get("user_limit", 0), "Voice user limit", 0, 99)
            if type(channel.get("nsfw", False)) is not bool:
                raise ValueError("NSFW must be true or false.")
            threads = channel.get("threads", [])
            if not isinstance(threads, list):
                raise ValueError("Threads must be an array.")
            if threads and channel.get("type", "text") != "text":
                raise ValueError("Threads can only be created in text channels.")
            thread_count += len(threads)
            for thread in threads:
                if not isinstance(thread, dict):
                    raise ValueError("Every thread must be an object.")
                name(thread.get("name"), "Thread name")
                if type(thread.get("auto_archive", 1440)) is not int or thread.get("auto_archive", 1440) not in (60, 1440, 4320, 10080):
                    raise ValueError("Thread archive duration must be 60, 1440, 4320, or 10080 minutes.")
    if channel_count > 100 or thread_count > 100:
        raise ValueError("A build supports at most 100 channels and 100 threads.")


def preflight_build(guild: discord.Guild, schema: dict, *, existing=()) -> None:
    """Check permissions and capacity for resources still needed by this plan."""
    _validate_schema(schema)
    me = guild.me
    if me is None:
        raise ValueError("The bot's server membership could not be resolved.")
    required = {"manage_channels", "manage_roles"}
    if schema.get("server_name"):
        required.add("manage_guild")
    if any(c.get("threads") for cat in schema.get("categories", []) for c in cat.get("channels", [])):
        required.update(("create_public_threads", "send_messages"))
    missing = sorted(p for p in required if not getattr(me.guild_permissions, p, False))
    if missing:
        raise ValueError("Bot needs these server permissions: " + ", ".join(missing))
    for index, role in enumerate(schema.get("roles", [])):
        requested = _resolve_permissions(role.get("permissions", []))
        if f"role:{index}" not in existing and requested.value & ~me.guild_permissions.value and not me.guild_permissions.administrator:
            raise ValueError(f"Bot cannot grant all permissions requested by role {role['name']}.")
    roles = sum(f"role:{i}" not in existing for i in range(len(schema.get("roles", []))))
    channels = 0
    for cat_index, category in enumerate(schema.get("categories", [])):
        channels += f"category:{cat_index}" not in existing
        channels += sum(f"channel:{cat_index}:{i}" not in existing for i in range(len(category.get("channels", []))))
    if len(guild.channels) + channels > 500 or len(guild.roles) + roles > 250:
        raise ValueError("The replacement must fit alongside existing channels and roles.")


async def build_server(
    guild: discord.Guild,
    schema: dict[str, Any],
    progress_msg: discord.Message | None = None,
    *,
    clean_existing: bool = False,
    safe_channel_id: int | None = None,
    created_channel_ids: set[int] | None = None,
    finalize=None,
    protected_channels=frozenset(),
    protected_roles=frozenset(),
) -> tuple[list[str], dict[str, discord.Role]]:
    """Create the replacement, with optional durable progress and separate cleanup."""
    async with build_session(guild.id):
        journal = await BuildJournal.load(guild.id, schema) if finalize is not None else None
        if journal is not None:
            await journal.load_resources(guild)
            missing = journal.state['resources'].keys() - journal.resources.keys()
            has_saved_channels = any(key.startswith(('channel:', 'category:')) for key in journal.resources)
            if has_saved_channels and any(key.startswith(('role:', 'category:')) for key in missing):
                raise ValueError('A recorded role or category was deleted while other build resources still exist. Review the current layout and submit a revised plan.')
        preflight_build(guild, schema, existing=() if journal is None else journal.resources)
        if journal is not None and journal.state.get('complete'):
            journal.state['complete'] = False
            await journal.save()
        if clean_existing:
            if journal is None:
                raise ValueError('Cleanup requires a recorded replacement build and a separate owner approval.')
            await journal.prepare_cleanup(guild, safe_channel_id, protected_channels, protected_roles)
        logs, role_map = await _build_server_unlocked(guild, schema, progress_msg, created_channel_ids, journal)
        if journal is not None:
            await finalize(role_map, journal)
            journal.state['complete'] = True
            await journal.save()
        return logs, role_map


async def _update_progress(msg: discord.Message, progress: BuildProgress) -> None:
    """Edit the progress message embed, throttled to once every 2s to avoid rate limits."""
    import time
    import discord as _d
    now = time.monotonic()
    # Only update every 2 seconds or on completion to avoid Discord rate limits
    if now - progress.last_update < 2.0 and progress.done < progress.total:
        return
    progress.last_update = now
    em = _d.Embed(title="🔨 Building your server", description="Step 3 of 3 · Creating roles and channels\n\n" + progress.bar, colour=0x5865F2)
    try:
        await msg.edit(embed=em)
    except discord.HTTPException:
        pass

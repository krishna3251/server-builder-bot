"""JSON server builder — creates roles, categories, channels from a schema dict."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import discord

log = logging.getLogger("services.json_builder")
_BUILD_LOCKS: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def build_session(guild_id: int):
    """Cover cleanup and creation with one per-server reservation."""
    lock = _BUILD_LOCKS.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        raise RuntimeError("A build is already running in this server. Wait for it to finish.")
    async with lock:
        try:
            yield
        finally:
            _BUILD_LOCKS.pop(guild_id, None)

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

_PRIVILEGED = {
    "administrator", "manage_guild", "manage_channels", "manage_roles",
    "manage_messages", "kick_members", "ban_members", "manage_webhooks",
    "mute_members", "deafen_members", "move_members", "mention_everyone",
    "view_audit_log",
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

    # Count total items for progress
    total = len(schema.get("roles", [])) + sum(
        1 + len(cat.get("channels", [])) for cat in schema.get("categories", [])
    )
    progress = BuildProgress(total)

    try:
        # ── Rename server ────────────────────────────────────────────────────
        if schema.get("server_name"):
            await guild.edit(name=schema["server_name"])
            logs.append(f"Renamed server to **{schema['server_name']}**")

        # ── Create roles ─────────────────────────────────────────────────────
        role_map: dict[str, discord.Role] = {}
        for role_data in schema.get("roles", []):
            perms = _resolve_permissions(role_data.get("permissions", []))
            colour = _parse_colour(role_data.get("color"))
            role = await guild.create_role(
                name=role_data["name"],
                colour=colour,
                hoist=role_data.get("hoist", False),
                mentionable=role_data.get("mentionable", False),
                permissions=perms,
            )
            created_roles.append(role)
            role_map[role_data["name"]] = role
            logs.append(f"Created role: **{role.name}**")
            progress.advance()
            if progress_msg:
                await _update_progress(progress_msg, progress)

        # ── Create categories + channels ─────────────────────────────────────
        for cat_data in schema.get("categories", []):
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

            category = await guild.create_category(name=cat_data["name"], overwrites=overwrites)
            created_channels.append(category)
            logs.append(f"Created category: **{cat_data['name']}**")
            progress.advance()
            if progress_msg:
                await _update_progress(progress_msg, progress)

            for ch_data in cat_data.get("channels", []):
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
                    vc = await guild.create_voice_channel(**kwargs)
                    created_channels.append(vc)
                    perm_note = f" (perms: {', '.join(o.get('role','') for o in ch_data.get('permission_overwrites', []))})" if ch_overwrites else ""
                    logs.append(f"Created voice channel: **{ch_data['name']}**{perm_note}")
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
                    tc = await guild.create_text_channel(**kwargs)
                    created_channels.append(tc)
                    if created_channel_ids is not None:
                        created_channel_ids.add(tc.id)
                    perm_note = f" (perms: {', '.join(o.get('role','') for o in ch_data.get('permission_overwrites', []))})" if ch_overwrites else ""
                    logs.append(f"Created text channel: **#{ch_data['name']}**{perm_note}")

                    # Optional thread creation
                    for thread_data in ch_data.get("threads", []):
                        await tc.create_thread(
                            name=thread_data["name"],
                            type=discord.ChannelType.public_thread,
                            auto_archive_duration=thread_data.get("auto_archive", 1440),
                        )
                        logs.append(f"  \u2514 Created thread: **{thread_data['name']}**")

                progress.advance()
                if progress_msg:
                    await _update_progress(progress_msg, progress)

    except (Exception, asyncio.CancelledError) as exc:
        log.exception("Build failed; rolling back newly created resources: %s", exc)
        # Restore the server name if this build changed it.
        if guild.name != original_guild_name:
            try:
                await guild.edit(name=original_guild_name, reason="Server build rollback")
            except discord.HTTPException:
                log.exception("Could not restore the original server name during rollback")
        # Rollback newly created channels/categories and roles.
        for ch in reversed(created_channels):
            try:
                await ch.delete(reason="Server build rollback")
            except Exception:
                pass
        for role in reversed(created_roles):
            try:
                await role.delete(reason="Server build rollback")
            except Exception:
                pass
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
        if _PRIVILEGED.intersection(p.lower() for p in role.get("permissions", [])):
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
            if target in seen:
                raise ValueError(f"Duplicate overwrite for role: {target}")
            seen.add(target)
            permissions(ow.get("allow", []), "Allowed permissions")
            permissions(ow.get("deny", []), "Denied permissions")
            if (target == auto_assign or target.lower() == "@everyone") and _PRIVILEGED.intersection(p.lower() for p in ow.get("allow", [])):
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


def preflight_build(guild: discord.Guild, schema: dict, *, clean_existing: bool = False) -> None:
    """Check permissions and remaining server capacity before cleanup starts."""
    _validate_schema(schema)
    me = guild.me
    if me is None:
        raise ValueError("The bot's server membership could not be resolved.")
    required = {"manage_channels", "manage_roles"}
    if schema.get("server_name"):
        required.add("manage_guild")
    if any(c.get("threads") for cat in schema.get("categories", []) for c in cat.get("channels", [])):
        required.add("create_public_threads")
    missing = sorted(p for p in required if not getattr(me.guild_permissions, p, False))
    if missing:
        raise ValueError("Bot needs these server permissions: " + ", ".join(missing))
    for role in schema.get("roles", []):
        requested = _resolve_permissions(role.get("permissions", []))
        if requested.value & ~me.guild_permissions.value and not me.guild_permissions.administrator:
            raise ValueError(f"Bot cannot grant all permissions requested by role {role['name']}.")
    channels = len(schema.get("categories", [])) + sum(len(c.get("channels", [])) for c in schema.get("categories", []))
    existing_channels = 1 if clean_existing else len(guild.channels)
    existing_roles = sum(r.is_default() or r.managed or r >= me.top_role for r in guild.roles) if clean_existing else len(guild.roles)
    if existing_channels + channels > 500 or existing_roles + len(schema.get("roles", [])) > 250:
        raise ValueError("This plan exceeds the server's channel or role capacity.")


async def build_server(
    guild: discord.Guild,
    schema: dict[str, Any],
    progress_msg: discord.Message | None = None,
    *,
    clean_existing: bool = False,
    safe_channel_id: int | None = None,
    created_channel_ids: set[int] | None = None,
) -> tuple[list[str], dict[str, discord.Role]]:
    """Validate, reserve this server, then perform cleanup and creation."""
    async with build_session(guild.id):
        preflight_build(guild, schema, clean_existing=clean_existing)
        cleanup_logs = []
        if clean_existing:
            if safe_channel_id is None or guild.get_channel(safe_channel_id) is None:
                raise ValueError("The command channel must exist before cleanup can start.")
            for channel in list(guild.channels):
                if channel.id != safe_channel_id:
                    await channel.delete(reason="Confirmed server builder cleanup")
            for role in list(guild.roles):
                if not (role.is_default() or role.managed or role >= guild.me.top_role):
                    await role.delete(reason="Confirmed server builder cleanup")
            cleanup_logs.append("Removed existing channels and eligible roles as confirmed.")
        logs, role_map = await _build_server_unlocked(guild, schema, progress_msg, created_channel_ids)
        return cleanup_logs + logs, role_map


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

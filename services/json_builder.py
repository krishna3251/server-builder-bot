"""JSON server builder — creates roles, categories, channels from a schema dict."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

log = logging.getLogger("services.json_builder")
_BUILD_LOCK = asyncio.Lock()

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
                ch_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
                for ow in ch_data.get("permission_overwrites", []):
                    target = role_map.get(ow.get("role", ""))
                    if not target and ow.get("role", "").lower() == "@everyone":
                        target = guild.default_role
                    if target:
                        allow = _resolve_permissions(ow.get("allow", []))
                        deny = _resolve_permissions(ow.get("deny", []))
                        ch_overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)

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
                    perm_note = f" (perms: {', '.join(o.get('role','') for o in ch_data.get('permission_overwrites', []))})" if ch_overwrites else ""
                    logs.append(f"Created text channel: **#{ch_data['name']}**{perm_note}")

                    # Optional thread creation
                    for thread_data in ch_data.get("threads", []):
                        await tc.create_thread(
                            name=thread_data["name"],
                            auto_archive_duration=thread_data.get("auto_archive", 1440),
                        )
                        logs.append(f"  \u2514 Created thread: **{thread_data['name']}**")

                progress.advance()
                if progress_msg:
                    await _update_progress(progress_msg, progress)

    except Exception as exc:
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
    """Reject malformed or oversized plans before making any Discord API changes."""
    if not isinstance(schema, dict):
        raise ValueError("Server template must be a JSON object.")

    server_name = schema.get("server_name")
    if server_name is not None and (
        not isinstance(server_name, str) or len(server_name.strip()) > 100
    ):
        raise ValueError("server_name must be a string of at most 100 characters.")

    roles = schema.get("roles", [])
    categories = schema.get("categories", [])
    if not isinstance(roles, list) or not isinstance(categories, list):
        raise ValueError("'roles' and 'categories' must be lists.")
    if len(roles) > 40:
        raise ValueError("A single build may create at most 40 roles.")
    if len(categories) > 30:
        raise ValueError("A single build may create at most 30 categories.")

    role_names: set[str] = set()
    for role in roles:
        if not isinstance(role, dict):
            raise ValueError("Every role entry must be an object.")
        name = role.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Every role needs a non-empty name of at most 100 characters.")
        if name in role_names:
            raise ValueError(f"Duplicate role name: {name}")
        role_names.add(name)
        permissions = role.get("permissions", [])
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
            raise ValueError(f"Permissions for role '{name}' must be a list of strings.")

    channel_count = 0
    for category in categories:
        if not isinstance(category, dict):
            raise ValueError("Every category entry must be an object.")
        category_name = category.get("name")
        if not isinstance(category_name, str) or not category_name.strip() or len(category_name) > 100:
            raise ValueError("Every category needs a non-empty name of at most 100 characters.")
        if not isinstance(category.get("permission_overwrites", []), list):
            raise ValueError(f"Permission overwrites for category '{category_name}' must be a list.")
        channels = category.get("channels", [])
        if not isinstance(channels, list):
            raise ValueError(f"Channels for category '{category_name}' must be a list.")
        channel_count += len(channels)
        for channel in channels:
            if not isinstance(channel, dict):
                raise ValueError("Every channel entry must be an object.")
            channel_name = channel.get("name")
            if not isinstance(channel_name, str) or not channel_name.strip() or len(channel_name) > 100:
                raise ValueError("Every channel needs a non-empty name of at most 100 characters.")
            if not isinstance(channel.get("permission_overwrites", []), list):
                raise ValueError(f"Permission overwrites for channel '{channel_name}' must be a list.")
            channel_type = channel.get("type", "text")
            if channel_type not in ("text", "voice"):
                raise ValueError(f"Unsupported channel type '{channel_type}'. Use 'text' or 'voice'.")
            if not isinstance(channel.get("threads", []), list):
                raise ValueError(f"Threads for channel '{channel_name}' must be a list.")
    if channel_count > 100:
        raise ValueError("A single build may create at most 100 channels.")


async def build_server(
    guild: discord.Guild,
    schema: dict[str, Any],
    progress_msg: discord.Message | None = None,
) -> tuple[list[str], dict[str, discord.Role]]:
    """Validate and serialize server builds to prevent malformed concurrent operations."""
    _validate_schema(schema)
    if _BUILD_LOCK.locked():
        raise RuntimeError("Another server build is already running. Wait for it to finish.")
    async with _BUILD_LOCK:
        return await _build_server_unlocked(guild, schema, progress_msg)


_last_progress_update: float = 0.0


async def _update_progress(msg: discord.Message, progress: BuildProgress) -> None:
    """Edit the progress message embed, throttled to once every 2s to avoid rate limits."""
    import time
    import discord as _d
    global _last_progress_update
    now = time.monotonic()
    # Only update every 2 seconds or on completion to avoid Discord rate limits
    if now - _last_progress_update < 2.0 and progress.done < progress.total:
        return
    _last_progress_update = now
    em = _d.Embed(title="\U0001f528 Building Server\u2026", description=progress.bar, colour=0x5865F2)
    try:
        await msg.edit(embed=em)
    except discord.HTTPException:
        pass

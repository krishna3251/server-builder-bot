"""Channel Engine — Comprehensive management of text, voice, forum, and stage channels."""

from __future__ import annotations

import logging
from typing import Any

import discord

from core.errors import PermissionError, ResourceNotFoundError, ValidationError
from engines.guild.permission_engine import PermissionPreset, permission_engine
from services.json_builder import _parse_colour, _resolve_permissions

log = logging.getLogger("engines.guild.channel_engine")


class ChannelEngine:
    """Manages channel creation, modification, deletion, cloning, and permissions."""

    async def create_channel(self, guild: discord.Guild, params: dict[str, Any]) -> discord.abc.GuildChannel:
        """Validate and create a channel in the guild."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission to create channels.", required_permission="Manage Channels")

        raw_name = params.get("name")
        if not raw_name or not str(raw_name).strip():
            raise ValidationError("Channel name cannot be empty.", field="name")

        name = str(raw_name).strip()[:100]
        ch_type = str(params.get("type", "text")).lower()

        # Resolve category if specified
        category: discord.CategoryChannel | None = None
        category_param = params.get("category") or params.get("category_id")
        if category_param:
            if isinstance(category_param, discord.CategoryChannel):
                category = category_param
            elif isinstance(category_param, int) or (isinstance(category_param, str) and category_param.isdigit()):
                category = guild.get_channel(int(category_param))  # type: ignore[assignment]
            else:
                cat_name = str(category_param).lower()
                category = discord.utils.find(lambda c: c.name.lower() == cat_name, guild.categories)

        if category_param and not isinstance(category, discord.CategoryChannel):
            raise ValidationError("Select an existing category in this server.", field="category")

        # Build overwrites: either from preset or direct list
        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        preset = params.get("permission_preset")
        if preset:
            overwrites = permission_engine.build_overwrites_for_preset(guild, preset)
        elif "permissions" in params and isinstance(params["permissions"], dict):
            # Convert raw permissions dictionary if supplied
            for role_name, rules in params["permissions"].items():
                role = discord.utils.get(guild.roles, name=role_name) or (guild.default_role if role_name == "@everyone" else None)
                if role and isinstance(rules, dict):
                    overwrites[role] = discord.PermissionOverwrite(**rules)

        topic = str(params.get("topic", ""))[:1024]
        slowmode = max(0, min(int(params.get("slowmode", 0)), 21600))
        nsfw = bool(params.get("nsfw", False))

        if ch_type == "voice":
            max_bitrate = guild.bitrate_limit
            bitrate = min(int(params.get("bitrate", 64000)), max_bitrate)
            user_limit = max(0, min(int(params.get("user_limit", 0)), 99))
            kwargs: dict[str, Any] = {
                "name": name,
                "category": category,
                "bitrate": bitrate,
                "user_limit": user_limit,
            }
            if overwrites:
                kwargs["overwrites"] = overwrites
            return await guild.create_voice_channel(**kwargs)

        elif ch_type == "forum":
            kwargs = {
                "name": name,
                "category": category,
                "topic": topic,
                "slowmode_delay": slowmode,
                "nsfw": nsfw,
            }
            if overwrites:
                kwargs["overwrites"] = overwrites
            return await guild.create_forum(**kwargs)

        elif ch_type == "stage":
            # Supported in discord.py 2.0+
            kwargs = {
                "name": name,
                "category": category,
                "topic": topic,
            }
            if overwrites:
                kwargs["overwrites"] = overwrites
            return await guild.create_stage_channel(**kwargs)

        else:
            # Default to text channel
            kwargs = {
                "name": name,
                "category": category,
                "topic": topic,
                "slowmode_delay": slowmode,
                "nsfw": nsfw,
            }
            if overwrites:
                kwargs["overwrites"] = overwrites
            return await guild.create_text_channel(**kwargs)

    async def edit_channel(self, guild: discord.Guild, params: dict[str, Any]) -> discord.abc.GuildChannel:
        """Edit an existing channel's properties."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        channel_id = params.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            raise ResourceNotFoundError("Channel", channel_id or "unknown")

        edit_kwargs: dict[str, Any] = {}
        if "name" in params:
            edit_kwargs["name"] = str(params["name"]).strip()[:100]
        if "topic" in params and hasattr(channel, "topic"):
            edit_kwargs["topic"] = str(params["topic"])[:1024]
        if "slowmode" in params and hasattr(channel, "slowmode_delay"):
            edit_kwargs["slowmode_delay"] = max(0, min(int(params["slowmode"]), 21600))
        if "nsfw" in params and hasattr(channel, "nsfw"):
            edit_kwargs["nsfw"] = bool(params["nsfw"])
        if "category_id" in params:
            cat = guild.get_channel(int(params["category_id"]))
            if isinstance(cat, discord.CategoryChannel):
                edit_kwargs["category"] = cat

        if edit_kwargs:
            await channel.edit(**edit_kwargs)
        return channel

    async def delete_channel(self, guild: discord.Guild, params: dict[str, Any]) -> bool:
        """Delete an existing channel safely."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        channel_id = params.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            raise ResourceNotFoundError("Channel", channel_id or "unknown")

        reason = params.get("reason", "Deleted via DAMU Channel Engine")
        await channel.delete(reason=reason)
        return True

    async def move_channel(self, guild: discord.Guild, params: dict[str, Any]) -> bool:
        """Move a channel's position or category."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        channel_id = params.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            raise ResourceNotFoundError("Channel", channel_id or "unknown")

        position = params.get("position")
        category_id = params.get("category_id")

        kwargs: dict[str, Any] = {}
        if position is not None:
            kwargs["position"] = int(position)
        if category_id is not None:
            cat = guild.get_channel(int(category_id))
            if isinstance(cat, discord.CategoryChannel) or cat is None:
                kwargs["category"] = cat

        if kwargs:
            await channel.edit(**kwargs)
        return True

    async def clone_channel(self, guild: discord.Guild, params: dict[str, Any]) -> discord.abc.GuildChannel:
        """Clone an existing channel with its permissions."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        channel_id = params.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not channel or not hasattr(channel, "clone"):
            raise ResourceNotFoundError("Channel", channel_id or "unknown")

        new_name = params.get("name") or f"{channel.name}-copy"
        return await channel.clone(name=new_name, reason="Cloned via DAMU Channel Engine")

    async def update_permissions(self, guild: discord.Guild, params: dict[str, Any]) -> bool:
        """Update permissions for a specific role or member on a channel."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        channel_id = params.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            raise ResourceNotFoundError("Channel", channel_id or "unknown")

        preset = params.get("permission_preset")
        if preset:
            overwrites = permission_engine.build_overwrites_for_preset(guild, preset)
            await channel.edit(overwrites=overwrites)
            return True

        target_id = params.get("target_id")
        target: discord.Role | discord.Member | None = guild.get_role(int(target_id)) if target_id else None
        if not target and target_id:
            target = guild.get_member(int(target_id))

        if not target and params.get("target_name") == "@everyone":
            target = guild.default_role

        if not target:
            raise ResourceNotFoundError("Role/Member target", target_id or "unknown")

        overwrite_rules = params.get("overwrites", {})
        overwrite = discord.PermissionOverwrite(**overwrite_rules)
        await channel.set_permissions(target, overwrite=overwrite)
        return True

    def analyze_channel(self, channel: discord.abc.GuildChannel) -> dict[str, Any]:
        """Perform read-only diagnostic analysis of a channel."""
        return {
            "id": channel.id,
            "name": channel.name,
            "type": channel.type.name,
            "category": channel.category.name if channel.category else None,
            "position": channel.position,
            "overwrites_count": len(channel.overwrites),
            "synced_with_category": channel.permissions_synced if hasattr(channel, "permissions_synced") else False,
            "audit_warnings": permission_engine.audit_channel_permissions(channel),
        }

    @staticmethod
    async def delete_channel_safe(channel: discord.abc.GuildChannel, reason: str = "") -> bool:
        """Safely delete a channel during transaction rollback or cleanup without throwing unhandled exceptions."""
        try:
            await channel.delete(reason=reason)
            return True
        except discord.NotFound:
            return True
        except Exception as exc:
            log.warning("[CHANNEL_ENGINE] Failed to safely delete channel %s: %s", getattr(channel, "name", "unknown"), exc)
            return False

    @staticmethod
    async def create_text_channel(
        guild: discord.Guild,
        name: str,
        category: Optional[discord.CategoryChannel] = None,
        overwrites: Optional[dict] = None,
        topic: Optional[str] = None,
        reason: str = "",
    ) -> Optional[discord.TextChannel]:
        """Create a text channel coordinated with API guard."""
        from core.discord_api.guard import api_guard
        allowed, api_state, retry_at = api_guard.can_execute()
        if not allowed:
            log.warning("[CHANNEL_ENGINE] create_text_channel blocked by API guard (state=%s)", api_state.value)
            return None

        kwargs: dict[str, Any] = {
            "name": name,
            "category": category,
            "reason": reason,
        }
        if overwrites:
            kwargs["overwrites"] = overwrites
        if topic:
            kwargs["topic"] = topic

        try:
            ch = await guild.create_text_channel(**kwargs)
            api_guard.record_success()
            return ch
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                api_guard.record_failure(status=429, body=getattr(exc, "text", ""), error=exc)
            log.error("[CHANNEL_ENGINE] Failed to create channel: %s", exc)
            return None
        except Exception as exc:
            log.exception("[CHANNEL_ENGINE] Error creating channel: %s", exc)
            return None

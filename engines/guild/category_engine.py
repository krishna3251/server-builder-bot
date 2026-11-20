"""Category Engine — Create, edit, delete, inspect categories, and identify orphan channels."""

from __future__ import annotations

import logging
from typing import Any

import discord

from core.errors import PermissionError, ResourceNotFoundError, ValidationError
from engines.guild.permission_engine import permission_engine

log = logging.getLogger("engines.guild.category_engine")


def _is_category(ch: Any) -> bool:
    return isinstance(ch, discord.CategoryChannel) or getattr(ch, "type", None) == discord.ChannelType.category


class CategoryEngine:
    """Manages categories, inherited permissions, and orphan channel detection."""

    async def create_category(self, guild: discord.Guild, params: dict[str, Any]) -> discord.CategoryChannel:
        """Create a new category with optional preset or overwrites."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        name = params.get("name")
        if not name or not str(name).strip():
            raise ValidationError("Category name cannot be empty.", field="name")

        category_name = str(name).strip()[:100]

        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        preset = params.get("permission_preset")
        if preset:
            overwrites = permission_engine.build_overwrites_for_preset(guild, preset)

        position = params.get("position")
        kwargs: dict[str, Any] = {"name": category_name}
        if overwrites:
            kwargs["overwrites"] = overwrites
        if position is not None:
            kwargs["position"] = int(position)

        return await guild.create_category(**kwargs)

    async def rename_category(self, guild: discord.Guild, category_id: int, new_name: str) -> discord.CategoryChannel:
        """Rename an existing category."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        cat = guild.get_channel(category_id)
        if not _is_category(cat):
            raise ResourceNotFoundError("Category", category_id)

        clean_name = new_name.strip()[:100]
        if not clean_name:
            raise ValidationError("Category name cannot be empty.", field="name")

        await cat.edit(name=clean_name)
        return cat

    async def delete_category(
        self,
        guild: discord.Guild,
        category_id: int,
        delete_channels_inside: bool = False,
        reason: str = "Deleted via DAMU Category Engine",
    ) -> bool:
        """Delete category, with option to delete or keep child channels as orphans."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        cat = guild.get_channel(category_id)
        if not _is_category(cat):
            raise ResourceNotFoundError("Category", category_id)

        if delete_channels_inside:
            for ch in list(cat.channels):
                try:
                    await ch.delete(reason=reason)
                except Exception as e:
                    log.warning("Could not delete child channel %s: %s", ch.name, e)

        await cat.delete(reason=reason)
        return True

    async def move_category(self, guild: discord.Guild, category_id: int, position: int) -> bool:
        """Change vertical position of category in the channel list."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        cat = guild.get_channel(category_id)
        if not _is_category(cat):
            raise ResourceNotFoundError("Category", category_id)

        await cat.edit(position=max(0, position))
        return True

    async def configure_permissions(self, guild: discord.Guild, category_id: int, preset: str) -> bool:
        """Apply a permission preset to the category."""
        bot = guild.me
        if not bot or not bot.guild_permissions.manage_channels:
            raise PermissionError("Bot lacks 'Manage Channels' permission.", required_permission="Manage Channels")

        cat = guild.get_channel(category_id)
        if not _is_category(cat):
            raise ResourceNotFoundError("Category", category_id)

        overwrites = permission_engine.build_overwrites_for_preset(guild, preset)
        await cat.edit(overwrites=overwrites)
        return True

    def list_channels_in_category(self, category: discord.CategoryChannel) -> list[discord.abc.GuildChannel]:
        """List all channels assigned to this category."""
        return list(category.channels)

    def detect_orphan_channels(self, guild: discord.Guild) -> list[discord.abc.GuildChannel]:
        """Detect channels that do not belong to any category."""
        orphans: list[discord.abc.GuildChannel] = []
        for channel in guild.channels:
            if not isinstance(channel, discord.CategoryChannel) and channel.category is None:
                orphans.append(channel)
        return orphans

    def inspect_category(self, category: discord.CategoryChannel) -> dict[str, Any]:
        """Return diagnostic metrics for a category and its channels."""
        channels = list(category.channels)
        synced_count = sum(1 for c in channels if getattr(c, "permissions_synced", False))
        return {
            "id": category.id,
            "name": category.name,
            "position": category.position,
            "total_channels": len(channels),
            "synced_channels": synced_count,
            "unsynced_channels": len(channels) - synced_count,
            "overwrites_count": len(category.overwrites),
        }

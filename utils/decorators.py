"""Reusable slash-command check decorators."""

from __future__ import annotations

import discord
from discord import app_commands

import config


def admin_only() -> app_commands.check:
    """Allow only users with **Administrator** permission."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        assert isinstance(interaction.user, discord.Member)
        if not interaction.user.guild_permissions.administrator:
            raise app_commands.MissingPermissions(["administrator"])
        return True

    return app_commands.check(predicate)


def mod_only() -> app_commands.check:
    """Allow users with **Kick**, **Ban**, or **Manage Messages** permissions."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        assert isinstance(interaction.user, discord.Member)
        perms = interaction.user.guild_permissions
        if not (perms.kick_members or perms.ban_members or perms.manage_messages):
            raise app_commands.MissingPermissions(["kick_members", "ban_members", "manage_messages"])
        return True

    return app_commands.check(predicate)


def guild_only() -> app_commands.check:
    """Prevent the command from running in DMs."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        return True

    return app_commands.check(predicate)


def dev_only() -> app_commands.check:
    """Restrict the command to developer user IDs listed in ``config.DEV_IDS``."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in config.DEV_IDS:
            raise app_commands.CheckFailure("This command is restricted to bot developers.")
        return True

    return app_commands.check(predicate)

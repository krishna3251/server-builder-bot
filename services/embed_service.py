"""Shared embed design system for consistent, accessible Discord responses."""

from __future__ import annotations

import datetime
from typing import Any

import discord

import config

# Discord-inspired palette. Keep meanings consistent across every cog.
_GREEN = 0x57F287
_RED = 0xED4245
_BLUE = 0x5865F2
_ORANGE = 0xFEE75C
_NEUTRAL = 0x2B2D31
_MUTED = 0x949BA4


def _base(title: str, description: str, colour: int) -> discord.Embed:
    """Create a branded embed with one consistent timestamp and footer."""
    em = discord.Embed(
        title=title[:256],
        description=(description[:4096] if description else None),
        colour=colour,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    em.set_footer(text=f"{config.BOT_NAME} • v{config.BOT_VERSION}")
    return em


def success_embed(title: str, description: str) -> discord.Embed:
    """Use for completed actions and successful configuration changes."""
    return _base(f"✅  {title}", description, _GREEN)


def error_embed(title: str, description: str) -> discord.Embed:
    """Use for actionable user-facing errors, never raw tracebacks."""
    return _base(f"⚠️  {title}", description, _RED)


def info_embed(title: str, description: str) -> discord.Embed:
    """Use for neutral information, help, profiles, and status pages."""
    return _base(title, description, _BLUE)


def warning_embed(title: str, description: str) -> discord.Embed:
    """Use for cooldowns, moderation actions, and non-fatal warnings."""
    return _base(f"🟠  {title}", description, _ORANGE)


def neutral_embed(title: str, description: str) -> discord.Embed:
    """Use when an informational card should not imply success or failure."""
    return _base(title, description, _NEUTRAL)


def progress_bar(current: int | float, total: int | float, *, width: int = 12) -> str:
    """Return a compact text progress bar with safe bounds for total=0."""
    width = max(1, min(int(width), 30))
    ratio = 0.0 if total <= 0 else max(0.0, min(float(current) / float(total), 1.0))
    filled = round(ratio * width)
    return f"{'▰' * filled}{'▱' * (width - filled)}  {round(ratio * 100)}%"


def add_member_context(embed: discord.Embed, member: discord.Member) -> discord.Embed:
    """Add consistent avatar/author context to user-related embeds."""
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    return embed


def add_usage_footer(embed: discord.Embed, usage: str) -> discord.Embed:
    """Append a short command hint while preserving the standard bot footer."""
    footer = embed.footer.text or f"{config.BOT_NAME} v{config.BOT_VERSION}"
    embed.set_footer(text=f"{footer} • {usage}"[:2048])
    return embed

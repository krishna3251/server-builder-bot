"""Embed factory — consistent colour-coded embeds for every response."""

from __future__ import annotations

import datetime

import discord

import config

# ── Colour palette ────────────────────────────────────────────────────────────────
_GREEN = 0x2ECC71
_RED = 0xE74C3C
_BLUE = 0x3498DB
_ORANGE = 0xE67E22


def _base(title: str, description: str, colour: int) -> discord.Embed:
    em = discord.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    em.set_footer(text=f"{config.BOT_NAME} v{config.BOT_VERSION}")
    return em


def success_embed(title: str, description: str) -> discord.Embed:
    return _base(title, description, _GREEN)


def error_embed(title: str, description: str) -> discord.Embed:
    return _base(title, description, _RED)


def info_embed(title: str, description: str) -> discord.Embed:
    return _base(title, description, _BLUE)


def warning_embed(title: str, description: str) -> discord.Embed:
    return _base(title, description, _ORANGE)

"""Analytics cog — tracks messages, commands, member joins/leaves."""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import aiofiles
import discord
from discord import app_commands
from discord.ext import commands

import config
from services.embed_service import info_embed
from utils.decorators import guild_only

log = logging.getLogger("cogs.analytics")

_data: dict[str, Any] = {}
_loaded: bool = False


async def _ensure_loaded() -> None:
    global _data, _loaded
    if _loaded:
        return
    if os.path.exists(config.ANALYTICS_FILE):
        try:
            async with aiofiles.open(config.ANALYTICS_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
                _data = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            log.warning("Could not load analytics: %s", exc)
            _data = {}
    _loaded = True


async def _save() -> None:
    os.makedirs(os.path.dirname(config.ANALYTICS_FILE), exist_ok=True)
    async with aiofiles.open(config.ANALYTICS_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_data, indent=2))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _guild_key(guild_id: int) -> str:
    return str(guild_id)


async def _inc(guild_id: int, section: str, key: str, amount: int = 1) -> None:
    await _ensure_loaded()
    gk = _guild_key(guild_id)
    if gk not in _data:
        _data[gk] = {}
    if section not in _data[gk]:
        _data[gk][section] = {}
    _data[gk][section][key] = _data[gk][section].get(key, 0) + amount
    await _save()


def _ascii_bar(value: int, max_value: int, width: int = 15) -> str:
    if max_value == 0:
        return "\u2591" * width
    filled = int((value / max_value) * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


class AnalyticsCog(commands.Cog, name="Analytics"):
    """Server analytics with message, member, and command tracking."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Event listeners ───────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        await _inc(message.guild.id, "messages", _today())

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await _inc(member.guild.id, "joins", _today())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await _inc(member.guild.id, "leaves", _today())

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: app_commands.Command  # type: ignore[type-arg]
    ) -> None:
        if interaction.guild:
            await _inc(interaction.guild.id, "commands", command.name)

    # ── /analytics ────────────────────────────────────────────────────────
    analytics_group = app_commands.Group(name="analytics", description="Server analytics")

    @analytics_group.command(name="messages", description="Messages sent per day (last 7 days).")
    @guild_only()
    async def analytics_messages(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await _ensure_loaded()
        gk = _guild_key(interaction.guild.id)
        msgs = _data.get(gk, {}).get("messages", {})

        # Last 7 days
        from datetime import timedelta
        days: list[str] = []
        today = datetime.now(timezone.utc).date()
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(d)

        values = [msgs.get(d, 0) for d in days]
        max_val = max(values) if values else 1

        lines: list[str] = []
        for d, v in zip(days, values):
            bar = _ascii_bar(v, max_val)
            lines.append(f"`{d[5:]}` {bar} **{v}**")

        em = info_embed("\U0001f4ca Messages (7 days)", "\n".join(lines) or "No data yet.")
        await interaction.response.send_message(embed=em)

    @analytics_group.command(name="members", description="Join/leave trend (last 7 days).")
    @guild_only()
    async def analytics_members(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await _ensure_loaded()
        gk = _guild_key(interaction.guild.id)
        joins = _data.get(gk, {}).get("joins", {})
        leaves = _data.get(gk, {}).get("leaves", {})

        from datetime import timedelta
        today = datetime.now(timezone.utc).date()
        lines: list[str] = []
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            j = joins.get(d, 0)
            l = leaves.get(d, 0)
            lines.append(f"`{d[5:]}` \U0001f4e5 **{j}** joined | \U0001f4e4 **{l}** left")

        em = info_embed("\U0001f4ca Member Trend (7 days)", "\n".join(lines) or "No data yet.")
        await interaction.response.send_message(embed=em)

    @analytics_group.command(name="commands", description="Most used commands.")
    @guild_only()
    async def analytics_commands(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await _ensure_loaded()
        gk = _guild_key(interaction.guild.id)
        cmds: dict[str, int] = _data.get(gk, {}).get("commands", {})

        if not cmds:
            em = info_embed("\U0001f4ca Command Usage", "No data yet.")
            return await interaction.response.send_message(embed=em)

        sorted_cmds = sorted(cmds.items(), key=lambda x: x[1], reverse=True)[:10]
        max_val = sorted_cmds[0][1] if sorted_cmds else 1

        lines = [f"`/{name:15s}` {_ascii_bar(count, max_val)} **{count}**" for name, count in sorted_cmds]
        em = info_embed("\U0001f4ca Command Usage (Top 10)", "\n".join(lines))
        await interaction.response.send_message(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AnalyticsCog(bot))

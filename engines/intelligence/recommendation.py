"""Recommendation Engine — Formats smart fixes discovered by the Server Analyzer."""

from __future__ import annotations

import logging
from typing import Any

import discord

from engines.guild.analyzer import SuggestedFix

log = logging.getLogger("engines.intelligence.recommendation")


class RecommendationEngine:
    """Translates diagnostic findings into user-confirmable repair actions."""

    def format_fix_embed(self, fix: SuggestedFix, current_index: int, total_fixes: int) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔧 SMART REPAIR SUGGESTION ({current_index}/{total_fixes})",
            description=f"**Issue:** {fix.title}\n\n**Details:** {fix.description}",
            colour=0xF1C40F,
        )

        action_summary = f"Action: `{fix.action_type}` on target `{fix.target_id}`"
        embed.add_field(name="Proposed Action", value=action_summary, inline=False)

        if "overwrites" in fix.parameters:
            lines = [f"`{k}`: {v}" for k, v in fix.parameters["overwrites"].items()]
            embed.add_field(name="Permission Changes", value="\n".join(lines), inline=False)

        embed.set_footer(text="Requires confirmation before any changes are made.")
        return embed


recommendation_engine = RecommendationEngine()

"""Result formatting and embed generators for DAMU Core Engine."""

from __future__ import annotations

import discord

from core.models import ActionStatus, BuildPlan, EngineResult, RiskLevel


def format_preview_embed(plan: BuildPlan) -> discord.Embed:
    """Format a structured BuildPlan into a user-facing Change Preview embed."""
    risk_emojis = {
        RiskLevel.LOW: "🟢 LOW",
        RiskLevel.MEDIUM: "🟡 MEDIUM",
        RiskLevel.HIGH: "🔴 HIGH",
        RiskLevel.CRITICAL: "🛑 CRITICAL (Explicit Confirmation Required)",
    }
    risk_colors = {
        RiskLevel.LOW: 0x2ECC71,
        RiskLevel.MEDIUM: 0xF1C40F,
        RiskLevel.HIGH: 0xE74C3C,
        RiskLevel.CRITICAL: 0x992D22,
    }

    embed = discord.Embed(
        title="🔧 DAMU CHANGE PREVIEW",
        description=f"**Operation:** {plan.operation}\n**Transaction ID:** `{plan.build_id}`",
        colour=risk_colors.get(plan.risk_level, 0x5865F2),
    )

    embed.add_field(name="Risk Assessment", value=risk_emojis.get(plan.risk_level, "⚪ UNKNOWN"), inline=True)
    embed.add_field(name="Total Planned Actions", value=str(len(plan.actions)), inline=True)

    # Summarize actions
    action_lines: list[str] = []
    for idx, action in enumerate(plan.actions[:12], 1):
        action_type = action.type.value.replace("_", " ").title()
        action_lines.append(f"`{idx}.` **{action_type}**: {action.target}")
    if len(plan.actions) > 12:
        action_lines.append(f"...and **{len(plan.actions) - 12}** more actions")

    if action_lines:
        embed.add_field(name="Planned Changes", value="\n".join(action_lines), inline=False)

    if plan.warnings:
        warn_text = "\n".join(f"⚠️ {w}" for w in plan.warnings[:6])
        embed.add_field(name="Security & Policy Warnings", value=warn_text, inline=False)

    embed.set_footer(text="Review the changes above before confirming execution.")
    return embed


def format_result_embed(result: EngineResult) -> discord.Embed:
    """Format an EngineResult into a final outcome embed."""
    if result.success:
        embed = discord.Embed(
            title="✅ Operation Completed Successfully",
            description=result.message,
            colour=0x2ECC71,
        )
    else:
        embed = discord.Embed(
            title="❌ Operation Failed",
            description=result.message,
            colour=0xE74C3C,
        )
        if result.error_code:
            embed.add_field(name="Error Code", value=f"`{result.error_code}`", inline=True)
        if result.recovery:
            embed.add_field(name="Recovery Action", value=result.recovery, inline=True)

    if result.build_id:
        embed.set_footer(text=f"Transaction ID: {result.build_id} | DAMU Core Engine")

    # Add executed action summaries
    status_icons = {
        ActionStatus.COMPLETED: "✅",
        ActionStatus.FAILED: "❌",
        ActionStatus.ROLLED_BACK: "↩️",
        ActionStatus.SKIPPED: "⏭️",
        ActionStatus.PENDING: "⏳",
        ActionStatus.RUNNING: "🔄",
    }
    if result.actions:
        lines: list[str] = []
        for action in result.actions[:10]:
            icon = status_icons.get(action.status, "•")
            lines.append(f"{icon} {action.type.value.replace('_', ' ').title()}: **{action.target}**")
        if len(result.actions) > 10:
            lines.append(f"...and {len(result.actions) - 10} more actions")
        embed.add_field(name="Execution Audit", value="\n".join(lines), inline=False)

    if result.warnings:
        embed.add_field(name="Warnings", value="\n".join(f"⚠️ {w}" for w in result.warnings[:4]), inline=False)

    return embed

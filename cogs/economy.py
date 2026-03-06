"""Economy cog — balance, daily, work, pay, leaderboard, shop, buy, inventory."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import economy_service as eco
from services.embed_service import error_embed, info_embed, success_embed
from utils.decorators import guild_only
from utils.helpers import format_number

log = logging.getLogger("cogs.economy")


class EconomyCog(commands.Cog, name="Economy"):
    """Server economy — earn, spend, and transfer coins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /balance ──────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your coin balance.")
    @guild_only()
    async def balance(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        bal = await eco.get_balance(interaction.guild.id, interaction.user.id)
        em = info_embed("Balance", f"{config.CURRENCY_SYMBOL} **{format_number(bal)}**")
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ── /daily ────────────────────────────────────────────────────────────
    @app_commands.command(name="daily", description="Claim your daily reward.")
    @guild_only()
    async def daily(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        last = await eco.get_last_daily(interaction.guild.id, interaction.user.id)
        now = datetime.now(timezone.utc)
        if last:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=24):
                remaining = last_dt + timedelta(hours=24) - now
                h, m = divmod(int(remaining.total_seconds()), 3600)
                mins = m // 60
                em = error_embed("Cooldown", f"Come back in **{h}h {mins}m**.")
                return await interaction.response.send_message(embed=em, ephemeral=True)

        new_bal = await eco.add_balance(interaction.guild.id, interaction.user.id, config.DAILY_REWARD)
        await eco.set_last_daily(interaction.guild.id, interaction.user.id, now.isoformat())
        em = success_embed("Daily Reward!", f"You received {config.CURRENCY_SYMBOL} **{format_number(config.DAILY_REWARD)}**!\nNew balance: {config.CURRENCY_SYMBOL} **{format_number(new_bal)}**")
        await interaction.response.send_message(embed=em)

    # ── /work ─────────────────────────────────────────────────────────────
    @app_commands.command(name="work", description="Work for a random reward.")
    @guild_only()
    async def work(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        last = await eco.get_last_work(interaction.guild.id, interaction.user.id)
        now = datetime.now(timezone.utc)
        if last:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=1):
                remaining = last_dt + timedelta(hours=1) - now
                mins = int(remaining.total_seconds()) // 60
                em = error_embed("Cooldown", f"You can work again in **{mins}m**.")
                return await interaction.response.send_message(embed=em, ephemeral=True)

        amount = random.randint(config.WORK_MIN, config.WORK_MAX)
        new_bal = await eco.add_balance(interaction.guild.id, interaction.user.id, amount)
        await eco.set_last_work(interaction.guild.id, interaction.user.id, now.isoformat())

        jobs = ["flipped burgers", "walked dogs", "coded a website", "delivered packages", "tutored students", "streamed on Twitch"]
        job = random.choice(jobs)
        em = success_embed("Work Complete!", f"You {job} and earned {config.CURRENCY_SYMBOL} **{format_number(amount)}**!\nBalance: {config.CURRENCY_SYMBOL} **{format_number(new_bal)}**")
        await interaction.response.send_message(embed=em)

    # ── /pay ──────────────────────────────────────────────────────────────
    @app_commands.command(name="pay", description="Transfer coins to another user.")
    @app_commands.describe(user="User to pay", amount="Amount to send")
    @guild_only()
    async def pay(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]) -> None:
        assert interaction.guild is not None
        if user.id == interaction.user.id:
            return await interaction.response.send_message(embed=error_embed("Error", "You can't pay yourself."), ephemeral=True)
        if user.bot:
            return await interaction.response.send_message(embed=error_embed("Error", "You can't pay a bot."), ephemeral=True)

        ok = await eco.transfer(interaction.guild.id, interaction.user.id, user.id, amount)
        if not ok:
            return await interaction.response.send_message(embed=error_embed("Insufficient Funds", "You don't have enough coins."), ephemeral=True)
        em = success_embed("Transfer Complete", f"Sent {config.CURRENCY_SYMBOL} **{format_number(amount)}** to {user.mention}.")
        await interaction.response.send_message(embed=em)

    # ── /leaderboard (economy) ──────────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="View the server leaderboard.")
    @app_commands.describe(type="Leaderboard type")
    @app_commands.choices(type=[
        app_commands.Choice(name="Coins", value="coins"),
        app_commands.Choice(name="XP", value="xp"),
    ])
    @guild_only()
    async def leaderboard(self, interaction: discord.Interaction, type: app_commands.Choice[str] = None) -> None:  # type: ignore[assignment]
        assert interaction.guild is not None
        lb_type = type.value if type else "coins"

        if lb_type == "xp":
            from services import level_service
            entries = await level_service.get_leaderboard(interaction.guild.id)
            lines: list[str] = []
            for i, (uid, xp, level) in enumerate(entries, 1):
                member = interaction.guild.get_member(uid)
                name = member.display_name if member else f"User#{uid}"
                lines.append(f"**{i}.** {name} — Level {level} ({format_number(xp)} XP)")
            em = info_embed("🏆 XP Leaderboard", "\n".join(lines) or "No data yet.")
        else:
            entries_eco = await eco.get_leaderboard(interaction.guild.id)
            lines = []
            for i, (uid, bal) in enumerate(entries_eco, 1):
                member = interaction.guild.get_member(uid)
                name = member.display_name if member else f"User#{uid}"
                lines.append(f"**{i}.** {name} — {config.CURRENCY_SYMBOL} {format_number(bal)}")
            em = info_embed("🏆 Coin Leaderboard", "\n".join(lines) or "No data yet.")
        await interaction.response.send_message(embed=em)

    # ── /shop ─────────────────────────────────────────────────────────────
    @app_commands.command(name="shop", description="Browse the item shop.")
    @guild_only()
    async def shop(self, interaction: discord.Interaction) -> None:
        items = await eco.get_shop_items()
        lines = [f"**{it['name']}** — {config.CURRENCY_SYMBOL} {format_number(it['price'])}\n> {it['description']}" for it in items]
        em = info_embed("🛒 Shop", "\n\n".join(lines) or "Shop is empty.")
        await interaction.response.send_message(embed=em)

    # ── /buy ──────────────────────────────────────────────────────────────
    @app_commands.command(name="buy", description="Purchase an item from the shop.")
    @app_commands.describe(item="Name of the item to buy")
    @guild_only()
    async def buy(self, interaction: discord.Interaction, item: str) -> None:
        assert interaction.guild is not None
        ok, msg = await eco.buy_item(interaction.guild.id, interaction.user.id, item)
        if ok:
            em = success_embed("Purchase", msg)
        else:
            em = error_embed("Purchase Failed", msg)
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ── /inventory ─────────────────────────────────────────────────────────
    @app_commands.command(name="inventory", description="View your purchased items.")
    @guild_only()
    async def inventory(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        inv = await eco.get_inventory(interaction.guild.id, interaction.user.id)
        if not inv:
            em = info_embed("Inventory", "Your inventory is empty.")
        else:
            from collections import Counter
            counts = Counter(inv)
            lines = [f"• **{name}** ×{count}" for name, count in counts.items()]
            em = info_embed("🎒 Inventory", "\n".join(lines))
        await interaction.response.send_message(embed=em, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))

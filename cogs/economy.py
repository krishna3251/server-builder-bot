"""Economy cog — balance, daily, work, pay, leaderboard, shop, buy, inventory."""

from __future__ import annotations

from utils.interactions import acknowledged, respond, defer_response

import logging
import random

import discord
from services.error_service import SafeView
from discord import app_commands
from discord.ext import commands

import config
from services import economy_service as eco
from services.embed_service import add_member_context, error_embed, info_embed, success_embed
from utils.decorators import guild_only
from utils.helpers import format_number

log = logging.getLogger("cogs.economy")


class ShopItemSelect(discord.ui.Select):
    """Page-aware item selector for the private shop interface."""

    def __init__(self, shop_view: "ShopView") -> None:
        self.shop_view = shop_view
        start = shop_view.page * shop_view.page_size
        end = min(start + shop_view.page_size, len(shop_view.items))
        options = []
        for index in range(start, end):
            item = shop_view.items[index]
            name = str(item.get("name", "Unknown item"))
            description = str(item.get("description", "No description") or "No description")
            price = int(item.get("price", 0))
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    description=f"{config.CURRENCY_SYMBOL} {format_number(price)} • {description}"[:100],
                    value=str(index),
                )
            )
        super().__init__(
            placeholder="Choose an item to view details…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.shop_view
        if interaction.user.id != view.owner_id:
            return await respond(interaction,
                "This shop session belongs to another member.", ephemeral=True
            )
        view.selected_index = int(self.values[0])
        view.buy_button.disabled = False
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class ShopPageButton(discord.ui.Button):
    def __init__(self, direction: int) -> None:
        self.direction = direction
        label = "Previous" if direction < 0 else "Next"
        emoji = "◀" if direction < 0 else "▶"
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ShopView):
            return
        if interaction.user.id != view.owner_id:
            return await respond(interaction,
                "This shop session belongs to another member.", ephemeral=True
            )
        view.page = max(0, min(view.page + self.direction, view.page_count - 1))
        view.selected_index = None
        view.buy_button.disabled = True
        view._replace_selector()
        view._update_nav()
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class ShopBuyButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Purchase selected",
            emoji="🛒",
            style=discord.ButtonStyle.success,
            disabled=True,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ShopView):
            return
        if interaction.user.id != view.owner_id:
            return await respond(interaction,
                "This shop session belongs to another member.", ephemeral=True
            )
        if view.selected_index is None:
            return await respond(interaction,
                "Choose an item from the menu first.", ephemeral=True
            )

        item = view.items[view.selected_index]
        # Acknowledge immediately: the purchase and balance reads may hit the database.
        await defer_response(interaction, )
        ok, message = await eco.buy_item(view.guild_id, view.owner_id, str(item.get("name", "")))
        if ok:
            receipt = success_embed("Purchase complete", message)
            receipt.add_field(
                name="Remaining balance",
                value=f"{config.CURRENCY_SYMBOL} {format_number(await eco.get_balance(view.guild_id, view.owner_id))}",
                inline=True,
            )
            await interaction.edit_original_response(embed=receipt, view=None)
            view.stop()
        else:
            await interaction.edit_original_response(
                embed=error_embed("Purchase unavailable", message),
                view=view,
            )


class ShopCloseButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Close", emoji="✕", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ShopView):
            return
        if interaction.user.id != view.owner_id:
            return await respond(interaction,
                "This shop session belongs to another member.", ephemeral=True
            )
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(view=view)
        view.stop()


class ShopView(SafeView):
    """Owner-locked shop with item details, buy action, and catalogue paging."""

    page_size = 25

    def __init__(self, items: list[dict], *, guild_id: int, owner_id: int) -> None:
        super().__init__(timeout=120)
        self.items = items
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.page = 0
        self.selected_index: int | None = None
        self.message: discord.Message | None = None
        self.selector = ShopItemSelect(self)
        self.buy_button = ShopBuyButton()
        self.prev_button = ShopPageButton(-1)
        self.next_button = ShopPageButton(1)
        self.close_button = ShopCloseButton()
        self.add_item(self.selector)
        self.add_item(self.prev_button)
        self.add_item(self.buy_button)
        self.add_item(self.next_button)
        self.add_item(self.close_button)
        self._update_nav()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.items) + self.page_size - 1) // self.page_size)

    def _replace_selector(self) -> None:
        self.remove_item(self.selector)
        self.selector = ShopItemSelect(self)
        self.add_item(self.selector)

    def _update_nav(self) -> None:
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1

    def make_embed(self) -> discord.Embed:
        em = info_embed(
            "🛒 Server Shop",
            "Select an item to inspect it, then choose **Purchase selected**. "
            "This private shop session can only be used by you.",
        )
        start = self.page * self.page_size
        stop = min(start + self.page_size, len(self.items))
        em.set_author(name=f"Catalogue • Page {self.page + 1}/{self.page_count}")
        if self.selected_index is not None:
            item = self.items[self.selected_index]
            em.add_field(name=str(item.get("name", "Unknown item"))[:256], value=str(item.get("description", "No description provided."))[:1024], inline=False)
            em.add_field(
                name="Price",
                value=f"**{config.CURRENCY_SYMBOL} {format_number(int(item.get('price', 0)))}**",
                inline=True,
            )
            em.set_footer(text="Purchases are added to your inventory • Use /inventory to review items")
        else:
            lines = []
            for index in range(start, stop):
                item = self.items[index]
                lines.append(
                    f"**{index + 1}. {discord.utils.escape_markdown(str(item.get('name', 'Unknown item')))}**"
                    f" · {config.CURRENCY_SYMBOL} {format_number(int(item.get('price', 0)))}"
                )
            em.description = "Select an item to inspect it, then choose **Purchase selected**.\n\n" + "\n".join(lines)
            em.set_footer(text="Use /buy if you prefer purchasing by name")
        return em

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class EconomyCog(commands.Cog, name="Economy"):
    """Server economy — earn, spend, and transfer coins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /balance ──────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your coin balance.")
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def balance(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        bal = await eco.get_balance(interaction.guild.id, interaction.user.id)
        em = info_embed("💰 Wallet", "Your current balance in this server.")
        em.add_field(name="Available coins", value=f"**{config.CURRENCY_SYMBOL} {format_number(bal)}**", inline=False)
        if isinstance(interaction.user, discord.Member):
            add_member_context(em, interaction.user)
        await respond(interaction, embed=em, ephemeral=True)

    # ── /daily ────────────────────────────────────────────────────────────
    @app_commands.command(name="daily", description="Claim your daily reward.")
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await defer_response(interaction, ephemeral=True)
        claimed, new_bal, ready = await eco.claim_reward(interaction.guild.id, interaction.user.id, "daily", config.DAILY_REWARD)
        if not claimed:
            return await interaction.edit_original_response(embed=info_embed("Daily reward claimed", f"Your next reward is available <t:{int(ready.timestamp())}:R>."))
        em = success_embed("Daily Reward!", f"You received {config.CURRENCY_SYMBOL} **{format_number(config.DAILY_REWARD)}**!\nNew balance: {config.CURRENCY_SYMBOL} **{format_number(new_bal)}**")
        await interaction.edit_original_response(embed=em)

    # ── /work ─────────────────────────────────────────────────────────────
    @app_commands.command(name="work", description="Work for a random reward.")
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await defer_response(interaction, ephemeral=True)
        amount = random.randint(config.WORK_MIN, config.WORK_MAX)
        claimed, new_bal, ready = await eco.claim_reward(interaction.guild.id, interaction.user.id, "work", amount)
        if not claimed:
            return await interaction.edit_original_response(embed=info_embed("Take a break", f"Your next shift starts <t:{int(ready.timestamp())}:R>."))

        jobs = ["flipped burgers", "walked dogs", "coded a website", "delivered packages", "tutored students", "streamed on Twitch"]
        job = random.choice(jobs)
        em = success_embed("Work Complete!", f"You {job} and earned {config.CURRENCY_SYMBOL} **{format_number(amount)}**!\nBalance: {config.CURRENCY_SYMBOL} **{format_number(new_bal)}**")
        await interaction.edit_original_response(embed=em)

    # ── /pay ──────────────────────────────────────────────────────────────
    @app_commands.command(name="pay", description="Transfer coins to another user.")
    @app_commands.describe(user="User to pay", amount="Amount to send")
    @guild_only()
    @acknowledged(ephemeral=False)
    @app_commands.guild_only()
    async def pay(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]) -> None:
        assert interaction.guild is not None
        if user.id == interaction.user.id:
            return await respond(interaction, embed=error_embed("Error", "You can't pay yourself."), ephemeral=True)
        if user.bot:
            return await respond(interaction, embed=error_embed("Error", "You can't pay a bot."), ephemeral=True)

        ok = await eco.transfer(interaction.guild.id, interaction.user.id, user.id, amount)
        if not ok:
            return await respond(interaction, embed=error_embed("Insufficient Funds", "You don't have enough coins."), ephemeral=True)
        em = success_embed("Transfer Complete", f"Sent {config.CURRENCY_SYMBOL} **{format_number(amount)}** to {user.mention}.")
        await respond(interaction, embed=em)

    # ── /leaderboard (economy) ──────────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="View the server leaderboard.")
    @app_commands.describe(type="Leaderboard type")
    @app_commands.choices(type=[
        app_commands.Choice(name="Coins", value="coins"),
        app_commands.Choice(name="XP", value="xp"),
    ])
    @guild_only()
    @acknowledged(ephemeral=False)
    @app_commands.guild_only()
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
                place = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**#{i}**")
                lines.append(f"{place} **{discord.utils.escape_markdown(name)}** · Level **{level}** · {format_number(xp)} XP")
            em = info_embed("🏆 XP Leaderboard", "\n".join(lines) or "No data yet.")
        else:
            entries_eco = await eco.get_leaderboard(interaction.guild.id)
            lines = []
            for i, (uid, bal) in enumerate(entries_eco, 1):
                member = interaction.guild.get_member(uid)
                name = member.display_name if member else f"User#{uid}"
                place = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**#{i}**")
                lines.append(f"{place} **{discord.utils.escape_markdown(name)}** · {config.CURRENCY_SYMBOL} {format_number(bal)}")
            em = info_embed("🏆 Coin Leaderboard", "\n".join(lines) or "No data yet.")
        await respond(interaction, embed=em)

    # ── /shop ─────────────────────────────────────────────────────────────
    @app_commands.command(name="shop", description="Browse and purchase items from the server shop.")
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await defer_response(interaction, ephemeral=True, thinking=True)
        items = await eco.get_shop_items()
        if not items:
            return await interaction.edit_original_response(
                embed=info_embed("🛒 Server Shop", "The catalogue is empty right now."),
                view=None,
            )

        view = ShopView(items, guild_id=interaction.guild.id, owner_id=interaction.user.id)
        await interaction.edit_original_response(embed=view.make_embed(), view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    # ── /buy ──────────────────────────────────────────────────────────────
    @app_commands.command(name="buy", description="Purchase an item from the shop.")
    @app_commands.describe(item="Name of the item to buy")
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
    async def buy(self, interaction: discord.Interaction, item: str) -> None:
        assert interaction.guild is not None
        ok, msg = await eco.buy_item(interaction.guild.id, interaction.user.id, item)
        if ok:
            em = success_embed("Purchase", msg)
        else:
            em = error_embed("Purchase Failed", msg)
        await respond(interaction, embed=em, ephemeral=True)

    # ── /inventory ─────────────────────────────────────────────────────────
    @app_commands.command(name="inventory", description="View your purchased items.")
    @guild_only()
    @acknowledged(ephemeral=True)
    @app_commands.guild_only()
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
        await respond(interaction, embed=em, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))

"""Reusable, owner-locked paginator for embeds and long command output."""

from __future__ import annotations

import discord
from services.error_service import SafeView


class Paginator(SafeView):
    """A tidy paginator with page count, guarded controls, and timeout cleanup."""

    def __init__(
        self,
        pages: list[discord.Embed],
        *,
        author_id: int,
        timeout: float = 90.0,
    ) -> None:
        if not pages:
            raise ValueError("Paginator requires at least one embed page.")
        super().__init__(timeout=timeout)
        self.pages = [discord.Embed.from_dict(page.to_dict()) for page in pages]
        self.author_id = author_id
        self.current = 0
        self.message: discord.Message | None = None
        self._apply_page_footer()
        self._update_buttons()

    def _apply_page_footer(self) -> None:
        total = len(self.pages)
        for index, page in enumerate(self.pages, start=1):
            existing = page.footer.text or ""
            # Strip an older paginator footer if the same embed is paginated again.
            existing = existing.split(" • Page ")[0]
            footer = f"{existing} • Page {index}/{total}" if existing else f"Page {index}/{total}"
            page.set_footer(text=footer[:2048], icon_url=page.footer.icon_url)

    def _update_buttons(self) -> None:
        self.btn_prev.disabled = self.current == 0
        self.btn_next.disabled = self.current >= len(self.pages) - 1

    async def _interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "These page controls belong to the person who opened this list.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary)
    async def btn_prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._interaction_check(interaction):
            return
        self.current = max(0, self.current - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="Close", emoji="✕", style=discord.ButtonStyle.danger)
    async def btn_stop(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._interaction_check(interaction):
            return
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Next", emoji="▶", style=discord.ButtonStyle.secondary)
    async def btn_next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._interaction_check(interaction):
            return
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

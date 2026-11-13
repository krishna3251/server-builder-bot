"""Discord Components V2 cards for command results and private navigation."""
import discord

import config
from services.error_service import respond_error


def embed_text(embed):
    parts = []
    if embed.author.name:
        parts.append(str(embed.author.name))
    if embed.title:
        parts.append('## ' + embed.title)
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        parts.append(f'### {field.name}\n{field.value}')
    if embed.footer.text:
        parts.append('-# ' + embed.footer.text)
    return '\n\n'.join(parts)


class ModernResultView(discord.ui.LayoutView):
    def __init__(self, owner_id, embed, *, refresh=None):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.refresh_callback = refresh
        self.message = None
        self.busy = False
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(embed_text(embed)),
                                          accent_colour=embed.colour or config.BOT_COLOR))
        self.refresh = discord.ui.Button(label='Refresh', style=discord.ButtonStyle.primary)
        self.refresh.callback = self.refresh_result
        self.guide = discord.ui.Button(label='Command center', style=discord.ButtonStyle.secondary)
        self.guide.callback = self.open_guide
        self.close = discord.ui.Button(label='Close', style=discord.ButtonStyle.secondary)
        self.close.callback = self.close_result
        controls = [self.guide, self.close]
        if refresh:
            controls.insert(0, self.refresh)
        self.add_item(discord.ui.ActionRow(*controls))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('These controls belong to the person who opened them.', ephemeral=True)
            return False
        return True

    async def refresh_result(self, interaction):
        from utils.interactions import defer_response
        if self.busy:
            return await interaction.response.send_message('A refresh is already running.', ephemeral=True)
        self.busy = True
        try:
            interaction.extras['result_modern'] = True
            await defer_response(interaction)
            await self.refresh_callback(interaction)
            if not interaction.extras.get('result_followup'):
                self.stop()
        finally:
            self.busy = False

    async def open_guide(self, interaction):
        from cogs.utility import CommandHelpView
        await interaction.response.send_message(view=CommandHelpView(interaction.user.id), ephemeral=True)

    async def close_result(self, interaction):
        for child in self.walk_children():
            if hasattr(child, 'disabled'):
                child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.walk_children():
            if hasattr(child, 'disabled'):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        await respond_error(interaction, error, context='Command result')

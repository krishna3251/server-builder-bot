"""Button-era single-choice polls: one changeable vote per Discord user."""
import discord
from services.error_service import SafeView
from services.embed_service import info_embed
from utils.interactions import respond


class VoteSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder='Cast or change your vote…', options=[discord.SelectOption(label=option[:100], value=str(index)) for index, option in enumerate(options)])

    async def callback(self, interaction):
        view = self.view
        if view.closed:
            return await respond(interaction, 'This poll has closed.', ephemeral=True)
        view.votes[interaction.user.id] = int(self.values[0])
        await interaction.response.edit_message(embed=view.card(), view=view)


class PollView(SafeView):
    def __init__(self, question, options, owner_id):
        super().__init__(timeout=3600)
        self.question = question
        self.options = options
        self.owner_id = owner_id
        self.votes = {}
        self.closed = False
        self.message = None
        self.add_item(VoteSelect(options))

    def card(self):
        lines = [f"**{index + 1}. {option}** — {sum(v == index for v in self.votes.values())} votes" for index, option in enumerate(self.options)]
        card = info_embed('📊 ' + self.question, '\n\n'.join(lines))
        card.set_footer(text=f"{len(self.votes)} voters • {'Closed' if self.closed else 'One vote each; you can change it • Closes in one hour or on restart'}")
        return card

    @discord.ui.button(label='Close poll', style=discord.ButtonStyle.secondary, row=1)
    async def close_poll(self, interaction, button):
        if interaction.user.id != self.owner_id and not getattr(getattr(interaction.user, 'guild_permissions', None), 'manage_messages', False):
            return await respond(interaction, 'Only the poll creator or a moderator can close this poll.', ephemeral=True)
        self.closed = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=self.card(), view=self)

    async def on_timeout(self):
        self.closed = True
        for child in self.children:
            child.disabled = True
        if self.message:
            try: await self.message.edit(embed=self.card(), view=self)
            except discord.HTTPException: pass

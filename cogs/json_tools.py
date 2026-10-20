"""Interactive schema examples, documentation, and validation without mutation."""
from __future__ import annotations

from asyncio import TimeoutError

import io
import json
import discord
from discord import app_commands
from discord.ext import commands
from services.embed_service import info_embed, error_embed
from services.error_service import SafeView, SafeModal
from services.template_service import REFERENCE, example_plan, parse_plan, read_plan_attachment, generate_plan
from utils.interactions import acknowledged, respond, defer_response


def plan_file(schema, filename='server-example.json'):
    return discord.File(io.BytesIO(json.dumps(schema, indent=2, ensure_ascii=False).encode()), filename=filename)


def plan_card(schema, label='Server JSON reference'):
    roles = schema.get('roles', [])
    categories = schema.get('categories', [])
    card = info_embed('📄 ' + label, 'Download → edit → validate → preview → build. Nothing is created by this reference panel.')
    card.add_field(name='Roles', value=str(len(roles)))
    card.add_field(name='Categories', value=str(len(categories)))
    card.add_field(name='Channels', value=str(sum(len(c.get('channels', [])) for c in categories)))
    card.add_field(name='Structure', value='\n'.join(f"**{c['name']}** · {len(c.get('channels', []))} channels" for c in categories)[:1000] or 'Roles only', inline=False)
    card.set_footer(text='/json_validate checks files • /setup_custom applies them after confirmation')
    return card


class ExampleSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='Choose a JSON example…', row=0, options=[
            discord.SelectOption(label=label, value=value) for value, label in (
                ('minimal', 'Minimal starter'), ('creator', 'Thumbnail & video creators'),
                ('gaming', 'Gaming'), ('community', 'Community'), ('study', 'Study group'), ('business', 'Business team'))])

    async def callback(self, interaction):
        self.view.schema = example_plan(self.values[0])
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.edit_message(embed=plan_card(self.view.schema), view=self.view, attachments=[])


class ValidateModal(SafeModal, title='Validate server JSON'):
    text = discord.ui.TextInput(label='Paste JSON (use /json_validate for larger files)', style=discord.TextStyle.paragraph, max_length=4000)

    async def on_submit(self, interaction):
        await defer_response(interaction, ephemeral=True, thinking=True)
        try:
            schema = parse_plan(self.text.value)
        except ValueError as exc:
            return await respond(interaction, embed=error_embed('JSON needs a correction', str(exc)), ephemeral=True)
        view = JsonReferenceView(schema, interaction.user.id)
        await respond(interaction, embed=plan_card(schema, 'Schema validation passed'), view=view, ephemeral=True)
        view.message = await interaction.original_response()


class DraftModal(SafeModal, title='Describe your server'):
    theme = discord.ui.TextInput(label='Theme, audience, roles, and channels', style=discord.TextStyle.paragraph, min_length=3, max_length=500)

    async def on_submit(self, interaction):
        await defer_response(interaction, ephemeral=True, thinking=True)
        try:
            schema = await generate_plan(self.theme.value, interaction.user.id)
        except (ValueError, TimeoutError) as exc:
            return await respond(interaction, embed=error_embed('Draft unavailable', str(exc) or 'AI timed out. Try again.'), ephemeral=True)
        view = JsonReferenceView(schema, interaction.user.id)
        await respond(interaction, embed=plan_card(schema, 'Validated AI draft — review permissions'), view=view, ephemeral=True)
        view.message = await interaction.original_response()


class JsonReferenceView(SafeView):
    def __init__(self, schema, owner_id):
        super().__init__(timeout=300)
        self.schema = schema
        self.owner_id = owner_id
        self.message = None
        self.add_item(ExampleSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await respond(interaction, 'Open /json_example for your own reference panel.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Download JSON', emoji='📥', row=1)
    async def download(self, interaction, button):
        await respond(interaction, file=plan_file(self.schema), ephemeral=True)

    @discord.ui.button(label='Format guide', emoji='📚', row=1)
    async def guide(self, interaction, button):
        await respond(interaction, file=discord.File(io.BytesIO(REFERENCE.encode()), filename='server-json-reference.md'), ephemeral=True)

    @discord.ui.button(label='Validate JSON', emoji='✅', row=1)
    async def validate(self, interaction, button):
        await interaction.response.send_modal(ValidateModal())

    @discord.ui.button(label='Preview build', emoji='🧱', style=discord.ButtonStyle.primary, row=2)
    async def preview(self, interaction, button):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            return await respond(interaction, 'Administrator permission in a server is required to preview a build.', ephemeral=True)
        from cogs.server_builder import CustomBuildConfirmView
        cog = interaction.client.get_cog('Server Builder')
        if cog is None:
            return await respond(interaction, 'Server builder is unavailable. Check /diagnostics.', ephemeral=True)
        view = CustomBuildConfirmView(cog, self.schema, owner_id=interaction.user.id, command_channel_id=cog._safe_command_channel_id(interaction))
        await respond(interaction, embed=view.review_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @discord.ui.button(label='AI draft', emoji='✨', row=2)
    async def ai_draft(self, interaction, button):
        # Rate-limit shared paid API access, including repeated modal opens.
        from cogs.ai import _rate_limited
        if _rate_limited(interaction.user.id):
            return await respond(interaction, 'AI request limit reached. Try again in a minute.', ephemeral=True)
        await interaction.response.send_modal(DraftModal())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try: await self.message.edit(view=self)
            except discord.HTTPException: pass


class JsonTools(commands.Cog, name='JSON Tools'):
    @app_commands.command(name='json_example', description='Browse downloadable JSON examples, the format guide, and validation tools.')
    @acknowledged(ephemeral=True)
    async def json_example(self, interaction: discord.Interaction):
        schema = example_plan()
        view = JsonReferenceView(schema, interaction.user.id)
        await respond(interaction, embed=plan_card(schema), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name='json_validate', description='Validate a JSON file or pasted plan without changing the server.')
    @acknowledged(ephemeral=True)
    async def json_validate(self, interaction: discord.Interaction, json_file: discord.Attachment | None = None, json_text: str | None = None):
        if (json_file is None) == (json_text is None):
            return await respond(interaction, embed=error_embed('Choose one input', 'Upload json_file OR paste json_text.'), ephemeral=True)
        try:
            schema = await read_plan_attachment(json_file) if json_file is not None else parse_plan(json_text)
        except ValueError as exc:
            return await respond(interaction, embed=error_embed('JSON needs a correction', str(exc)), ephemeral=True)
        view = JsonReferenceView(schema, interaction.user.id)
        await respond(interaction, embed=plan_card(schema, 'Schema validation passed'), view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(JsonTools())

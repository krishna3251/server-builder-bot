"""One acknowledgment path and useful, guarded controls for command results."""
from __future__ import annotations

from functools import wraps
import inspect
from typing import get_type_hints
import discord

from services.embed_service import info_embed
from services.error_service import SafeView

READ_COMMANDS = {
    'ping', 'serverinfo', 'userinfo', 'roleinfo', 'avatar', 'banner', 'uptime',
    'botinfo', 'diagnostics', 'balance', 'leaderboard', 'inventory', 'rank',
    'messages', 'members', 'commands', 'warnings', 'list', 'reminders',
}


async def defer_response(interaction, *, ephemeral=True, thinking=False):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)


def acknowledged(*, ephemeral=True):
    """Defer slash commands before file/network I/O, after Discord checks run."""
    def decorate(function):
        @wraps(function)
        async def wrapped(self, interaction, *args, **kwargs):
            private = kwargs.get('preview', True) if function.__name__ == 'generate_server' else ephemeral
            await defer_response(interaction, ephemeral=private, thinking=True)
            extras = getattr(interaction, 'extras', None)
            if not isinstance(extras, dict):
                extras = interaction.extras = {}
            command = getattr(interaction, 'command', None)
            if getattr(command, 'name', '') in READ_COMMANDS:
                async def refresh(current):
                    for predicate in command.checks:
                        if not await discord.utils.maybe_coroutine(predicate, current):
                            raise discord.app_commands.CheckFailure('Permission check failed.')
                    return await wrapped(self, current, *args, **kwargs)
                extras['result_refresh'] = refresh
            return await function(self, interaction, *args, **kwargs)
        # discord.py resolves callback annotations in the wrapper module. Keep
        # the original signature with concrete types across module boundaries.
        signature = inspect.signature(function)
        hints = get_type_hints(inspect.unwrap(function))
        wrapped.__acknowledged__ = True
        wrapped.__signature__ = signature.replace(
            parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in signature.parameters.values()],
            return_annotation=hints.get('return', signature.return_annotation),
        )
        return wrapped
    return decorate


async def respond(interaction, content=None, **kwargs):
    """Finish a deferred command, or answer a component without double responding."""
    extras = getattr(interaction, 'extras', {})
    if not isinstance(extras, dict):
        extras = {}
    command = getattr(interaction, 'command', None)
    if 'view' not in kwargs and 'file' not in kwargs and 'files' not in kwargs and command is not None:
        name = getattr(command, 'qualified_name', '')
        if isinstance(name, str) and name:
            kwargs['view'] = ResultView(interaction.user.id, refresh=extras.get('result_refresh'))
    view = kwargs.get('view')
    if not interaction.response.is_done():
        result = await interaction.response.send_message(content=content, **kwargs)
    elif interaction.response.type in (discord.InteractionResponseType.deferred_channel_message, discord.InteractionResponseType.deferred_message_update):
        kwargs.pop('ephemeral', None)
        files = kwargs.pop('files', [])
        single = kwargs.pop('file', None)
        if single is not None:
            files = [single] + list(files)
        if files:
            kwargs['attachments'] = files
        result = await interaction.edit_original_response(content=content, **kwargs)
    else:
        result = await interaction.followup.send(content=content, **kwargs)
    if isinstance(view, ResultView):
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass
    return result


class ResultView(SafeView):
    def __init__(self, owner_id, *, refresh=None):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.refresh_callback = refresh
        self.message = None
        self.busy = False
        if refresh is None:
            self.remove_item(self.refresh)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await respond(interaction, 'These controls belong to the person who opened this result.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Refresh', emoji='🔄', style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        if self.busy:
            return await respond(interaction, 'A refresh is already running.', ephemeral=True)
        self.busy = True
        try:
            await defer_response(interaction)
            await self.refresh_callback(interaction)
        finally:
            self.busy = False

    @discord.ui.button(label='Command guide', emoji='📚', style=discord.ButtonStyle.secondary)
    async def guide(self, interaction, button):
        from cogs.utility import CommandHelpView, _HELP_PAGES
        title, description = _HELP_PAGES['overview']
        view = CommandHelpView(interaction.user.id)
        await respond(interaction, embed=info_embed(title, description), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def close(self, interaction, button):
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

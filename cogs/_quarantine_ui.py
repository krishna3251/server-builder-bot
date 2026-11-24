"""Private Discord views for the Security cog's quarantine controls."""
from __future__ import annotations

import io
import json

import discord

from services.error_service import SafeView
from utils.interactions import respond


def is_quarantine_admin(interaction):
    return bool(interaction.guild and (
        interaction.user.id == interaction.guild.owner_id
        or interaction.user.guild_permissions.administrator
    ))


def status_text(report):
    canonical = report['canonical']
    unresolved = (report['missing'] or report['issues'] or report['permissions'] or report['pending']
                  or report['pending_creations'] or report['failures'] or report['member_failures']
                  or any(phase != 'active' for phase in report['members'].values()))
    current = set(canonical.values())
    duplicates = (set(report['managed']) - current) | set(report['uncertain'])
    members = ', '.join(f'{identifier} ({phase})' for identifier, phase in list(report['members'].items())[:8])
    return (
        f"Quarantine health: {'not configured' if not report['configured'] else ('needs review' if unresolved else 'ready')}\n"
        f"Role: {canonical['role'] or 'missing'} • Room: {canonical['room'] or 'missing'} • "
        f"Category: {canonical['category'] or 'missing'} • Staff role: {canonical['staff'] or 'none'}\n"
        f"Tracked members ({len(report['members'])}): {members or 'none'}\n"
        f"Possible duplicates: {len(duplicates)} • Missing: {', '.join(report['missing']) or 'none'}\n"
        f"Permission conflicts: {len(report['permissions'])} • Pending migrations: {len(report['pending'])}\n"
        f"Failed channel operations: {len(report['failures'])} • Member failures: {len(report['member_failures'])}\n"
        f"Pending creations: {', '.join(report['pending_creations']) or 'none'}\n"
        f"Configuration issues: {' '.join(report['issues']) or 'none'}"
    )


def audit_file(name, data):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return discord.File(io.BytesIO(text.encode()), filename=f'quarantine-{name}.json')


class QuarantineView(SafeView):
    def __init__(self, interaction):
        super().__init__(timeout=150)
        self.actor_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.used = False

    async def interaction_check(self, interaction):
        if (interaction.guild_id == self.guild_id and interaction.user.id == self.actor_id
                and is_quarantine_admin(interaction) and not self.used):
            return True
        await interaction.response.send_message('These controls require the initiating administrator and an active preview.',
                                                ephemeral=True)
        return False

    @discord.ui.button(label='Cancel', row=2)
    async def cancel(self, interaction, button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content='Cancelled.', view=None, attachments=[])


class QuarantineConfirmation(QuarantineView):
    def __init__(self, interaction, cog, operation, preview=None):
        super().__init__(interaction)
        self.cog, self.operation, self.preview = cog, operation, preview

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.danger, row=2)
    async def confirm(self, interaction, button):
        if self.used:
            return await interaction.response.send_message('This preview has already been used.', ephemeral=True)
        self.used = True
        self.stop()
        await interaction.response.defer()
        try:
            result, file = await self.cog.run_quarantine_operation(interaction, self.operation, self.preview)
        except ValueError as exc:
            result, file = str(exc), None
        await interaction.edit_original_response(content=result, view=None, attachments=[file] if file else [],
                                                 allowed_mentions=discord.AllowedMentions.none())


class QuarantineDashboard(QuarantineView):
    def __init__(self, interaction, cog):
        super().__init__(interaction)
        self.cog = cog

    @discord.ui.button(label='Refresh', style=discord.ButtonStyle.primary, row=0)
    async def refresh(self, interaction, button):
        await interaction.response.defer()
        report = await self.cog.quarantine_resources.discovery(interaction.guild)
        await interaction.edit_original_response(content=status_text(report), view=self)

    @discord.ui.button(label='Run Audit', row=0)
    async def audit(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.quarantine_report(interaction, 'audit')

    @discord.ui.button(label='View Permissions', row=0)
    async def permissions(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.quarantine_report(interaction, 'permissions')

    @discord.ui.button(label='Preview Repair', row=1)
    async def repair(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.quarantine_preview(interaction, 'repair')

    @discord.ui.button(label='Preview Cleanup', row=1)
    async def cleanup(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.quarantine_preview(interaction, 'cleanup')

    @discord.ui.button(label='Confirm Migration', row=1)
    async def migrate(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.quarantine_preview(interaction, 'migrate')

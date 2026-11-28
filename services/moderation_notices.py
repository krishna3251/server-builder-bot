"""Deliver bot actions to the application owner and the affected guild owner."""
import logging
import asyncio

import discord

log = logging.getLogger(__name__)


async def record_action(bot, guild, actor_id, target_id, action, detail, identifier):
    """Include routine enforcement outcomes in the owners' digest without an immediate DM."""
    try:
        cog = getattr(bot, 'get_cog', lambda _: None)('Server Butler')
        service = getattr(cog, 'service', None)
        if service is not None and service.enabled(guild.id):
            await service.store.moderation(identifier, guild.id, actor_id, target_id, action, detail, ())
    except Exception:
        log.exception('Could not record moderation outcome: guild=%s action=%s', guild.id, action)


async def notify_action(bot, guild, actor, target, action, reason, identifier):
    summary = (f'Private moderation notice • {guild.name} ({guild.id})\n'
               f'Action: {action}\nModerator ID: {actor.id}\nMember ID: {target.id}\n'
               f'Reason: {reason[:700]}')
    try:
        cog = bot.get_cog('Server Butler')
        if cog and cog.service.enabled(guild.id):
            try:
                owner = await asyncio.wait_for(cog.service.resolve_application_owner(), timeout=5)
            except Exception:
                owner = None  # Resolve the application owner when the delivery worker retries.
            await cog.service.store.moderation(identifier, guild.id, actor.id, target.id,
                                               action, summary, {owner, guild.owner_id})
            return
        app = await bot.application_info()
        owner = app.team.owner_id if app.team else app.owner.id
        for recipient in {owner, guild.owner_id}:
            try:
                user = bot.get_user(recipient) or await bot.fetch_user(recipient)
                await user.send(summary, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning('Moderation DM blocked: guild=%s recipient=%s action=%s', guild.id, recipient, action)
    except Exception:
        log.exception('Could not queue moderation notice: guild=%s action=%s', guild.id, action)

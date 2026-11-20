"""Read and update exemptions on Discord's existing AutoMod rules."""
from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

import discord


class AutoModSettingsError(ValueError):
    """A rule or selection needs a change from the moderator."""


def has_timeout(rule: discord.AutoModRule) -> bool:
    return any(action.type == discord.AutoModRuleActionType.timeout for action in rule.actions)


def accepts_channels(rule: discord.AutoModRule) -> bool:
    return rule.event_type == discord.AutoModRuleEventType.message_send


class AutoModService:
    def __init__(self) -> None:
        self.locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()

    async def change(
        self, guild: discord.Guild, actor: discord.Member, rule_id: int, *,
        role_ids: set[int], channel_ids: set[int], remove: bool = False,
    ) -> tuple[discord.AutoModRule, bool]:
        # Fetch inside the lock so another panel cannot overwrite a more recent bot edit.
        lock = self.locks.setdefault((guild.id, rule_id), asyncio.Lock())
        async with lock:
            rule = await guild.fetch_automod_rule(rule_id)
            perms = actor.guild_permissions
            bot_perms = guild.me.guild_permissions if guild.me else discord.Permissions.none()
            if not (perms.manage_guild and bot_perms.manage_guild):
                raise AutoModSettingsError("You and the bot both need Manage Server.")
            if has_timeout(rule) and not (perms.moderate_members and bot_perms.moderate_members):
                raise AutoModSettingsError("You and the bot both need Moderate Members to change a timeout rule.")
            if channel_ids and not accepts_channels(rule):
                raise AutoModSettingsError("Choose a message rule to exempt channels.")
            if not remove and guild.id in role_ids:
                raise AutoModSettingsError("Choose a specific role. @everyone would exempt the whole server.")

            changes = {}
            for field, previous, selected, limit in (
                ("exempt_roles", rule.exempt_role_ids, role_ids, 20),
                ("exempt_channels", rule.exempt_channel_ids, channel_ids, 50),
            ):
                if not selected:
                    continue
                updated = previous - selected if remove else previous | selected
                if updated == previous:
                    continue
                if len(updated) > limit:
                    label = "roles" if field == "exempt_roles" else "channels"
                    raise AutoModSettingsError(f"Discord allows up to {limit} exempt {label} per rule.")
                changes[field] = [discord.Object(id=identifier) for identifier in sorted(updated)]
            if not changes:
                return rule, False

            action = "Remove" if remove else "Add"
            rule = await rule.edit(**changes, reason=f"{action} AutoMod exemptions by moderator {actor.id}")
            return rule, True


async def recent_timeouts(guild: discord.Guild, member_id: int) -> list[discord.AuditLogEntry]:
    """Find applied timeouts by either AutoMod or a person/app in recent audit entries."""
    async def find(action):
        matches = []
        async for entry in guild.audit_logs(action=action, limit=100):
            if getattr(entry.target, "id", None) != member_id:
                continue
            if action == discord.AuditLogAction.member_update:
                until = getattr(entry.after, "timed_out_until", None)
                if until is None or until <= entry.created_at:
                    continue
            matches.append(entry)
            if len(matches) == 5:
                break
        return matches

    native, manual = await asyncio.gather(
        find(discord.AuditLogAction.automod_timeout_member),
        find(discord.AuditLogAction.member_update),
    )
    return sorted(native + manual, key=lambda entry: entry.id, reverse=True)[:5]

# Soft Banned Diddy: top no-write channel

This feature is part of **krishna3251/server-builder-bot**, not the older DAMU repository. The cog is automatically loaded at startup by main.py.

## Setup (server owner or Administrator)

1. Run /quarantine_setup first. It creates/validates the **existing Security cog's reversible quarantine journal**, marker role and private review room. If you already set it up, the feature reuses that setup. Do not create a competing quarantine role.
2. Create or choose an existing **private** logging channel, e.g. #moderation-logs. Hide it from @everyone, allow your moderator role and the bot to view/send messages. The moderator role must have moderation permissions. The command rejects publicly accessible log destinations.
3. Run /soft_banned_diddy and select **log_channel** and **mod_role**. Optionally set **channel** if a #dont-write-there channel already exists and you want to reuse it.

The bot creates #dont-write-there (or uses the explicitly selected one), places it at the top of the uncategorized text channels, posts and pins a warning, and saves the selected settings. Re-running the command updates the configuration without creating a duplicate warning.

**Regular members:** messages are deleted, then the existing Security cog's reversible quarantine is attempted. The result is logged. If permissions, role hierarchy, setup or a Discord API call prevent enforcement, staff get an error/partial-action record rather than a false success message.

**Owner and Administrators:** are exempt from deletion and quarantine. The bot replies "Malik, kyun likh rahe ho? 👀" at most once per administrator per 20 seconds to avoid a flood; each exempt post still gets a private action log. Other bots/webhooks are ignored.

## Commands

- /soft_banned_diddy log_channel:<private-text-channel> mod_role:<staff-role> [channel:<existing-text-channel>] — configure or update
- /soft_banned_diddy_status — show configured IDs and quarantine setup status, privately to an Administrator
- /soft_banned_diddy_disable — stop enforcement without deleting channels, messages, or saved quarantine records
- /quarantine_release member:<member> — restore a quarantined member after staff review

## Required access

The bot needs Manage Channels, Manage Roles, Manage Messages, View Channel, Send Messages and Read Message History in the relevant channels. Its highest role must outrank the quarantine role and the member being quarantined. Audit the live bot permissions and channel position after enabling.

Logs contain guild, member, and message IDs and an action/result **but never message text**. If the configured log channel becomes unavailable or public, the cog attempts to notify the guild owner by DM instead of exposing logs publicly.

The persistent settings file is data/soft_banned_diddy.json. Keep this file with the deployment's normal persistent storage if the host wipes local files. The quarantine journal is managed separately by /quarantine_setup.

### Safety limits

Do not use the no-write channel to collect secret information. The feature is a moderation trap, not a substitute for Discord permissions or anti-raid protections. Members with privileged or uneditable roles may not be eligible for quarantine; these cases need manual staff review. Existing trap channels are not auto-adopted unless an Administrator explicitly selects them.

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


## Wispbyte: keep the same data directory after changing repositories

The old DAMU startup ran in /home/container; the new checkout runs in
/home/container/server-builder-bot. Without configuration, they save different
data/ directories, so quarantine/AutoMod/trap-channel state does not transfer.

**Before switching:** Back up /home/container/data and .env (if present).
In the Wispbyte environment, set BOT_DATA_DIR=/home/container/data. Alternatively,
export it in the startup command immediately before launching Python. Do not place
the Discord token directly into the startup command or commit it to GitHub.

The override applies to the moderation/quarantine journal, trap-channel settings,
AutoMod, and other features using config's data paths. Existing data is reused;
the app does not automatically import data from the old repository's Git checkout.

After reboot, look for the runtime log
source=krishna3251/server-builder-bot, a Discord Logged in as ... line and
successful slash-command sync. "Server marked as running" or a successful
git clone does **not** establish that Discord login succeeded.

Run /soft_banned_diddy_status to verify the feature is enabled on this guild.
If no configuration exists, run /quarantine_setup then /soft_banned_diddy
with an existing private log channel and moderator role. Check that the bot's role
outranks the target member, and that Members and Message Content privileged intents
are enabled in the Discord Developer Portal.

The default path remains the repository-local data folder unless
BOT_DATA_DIR is set. On an ephemeral host, /home/container/data must itself
be backed by persistent storage; setting an environment variable does not make
an ephemeral volume persistent.


## After every restart (automatic restoration diagnostics)

You do **not** have to run setup commands again when saved state survives. On
Discord ready, the bot now loads the existing trap configuration and checks its
saved enabled flag, channel permissions and quarantine readiness. Look in Wispbyte
console for:

- `Runtime state: data_dir=... | trap_config_exists=True | quarantine_journal_exists=True`
- `Trap restored automatically for guild ... No setup command required.`

If a file is missing, the bot logs an explicit warning rather than silently
activating a similarly named channel. Use the Wispbyte **File Manager** to check
`/home/container/data/soft_banned_diddy.json` and
`/home/container/data/quarantine.json` after a restart. A server folder being
present does **not** guarantee those JSON files exist.

If `trap_config_exists=False`, ensure `BOT_DATA_DIR=/home/container/data`
is set before launch and that Wispbyte actually preserves that directory; if
files were deleted, restore your own backup. If the quarantine journal is missing,
restore its backup before attempting to release previously quarantined users,
because their original role snapshots cannot safely be recreated from scratch.

Run `/soft_banned_diddy_status` to inspect the saved path, enabled setting,
channel permissions and quarantine readiness. The bot will **not** auto-enable a
trap that was explicitly disabled, and it will not auto-adopt an existing
`#dont-write-there` channel without a saved configuration.

The startup check is non-destructive. It reports lost data or missing permissions;
it cannot repair a Wispbyte disk that wipes files on restart. Permanent retention
requires storage that survives process/container restarts.


## Removing the Quarantined role manually

A member's old roles and channel permissions are stored in the quarantine journal.
Simply removing the Quarantined marker manually is **not** a complete release.
The recommended way is still /quarantine_release member:<user>.

If an Administrator or a moderator selected in /quarantine_setup or
/soft_banned_diddy **manually removes** the Quarantined role, Security now checks
the Discord audit log for a recent matching role-removal entry. Only when the
audit log identifies the authorized staff member (and their hierarchy allows
moderation) does the bot run the regular reversible release: restore the saved
original roles, restore compatible prior channel overwrites, and clear the
journal. A partial release retains the journal so staff can retry.

If the actor cannot be verified, is not authorized, the audit entry doesn't
identify that precise role removal, or the bot lacks View Audit Log,
the bot maintains quarantine rather than treating an unknown role change as
staff permission. Use /quarantine_release instead.

For automatic manual release the bot needs View Audit Log, Manage Roles, and
the usual hierarchy and channel permissions. The moderator requires Manage
Roles and must hold the configured staff/moderator role. Server owners and
Administrators are permitted, subject to the normal safe restoration checks.

A manual release never happens merely because the user lacks the marker,
and nobody is automatically granted a role that has gained new privileges
since quarantine. Discord audit-log entries may arrive slightly after the
member-change event; the bot briefly retries verification.

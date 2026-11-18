# Server Builder Bot

A full-featured Discord bot built with **discord.py 2.x** featuring:

- **AI Chat** — OpenRouter-powered AI assistant with per-user memory and persona switching
- **Server Builder** — Generate server layouts from templates or AI-generated JSON schemas
- **Ticket System** — Production-grade support ticket system with persistent buttons, blacklist, auto-delete, and staff notifications
- **Economy** — Coin-based economy with daily rewards, work, shop, and leaderboards
- **Leveling** — Message-based XP system with level-up notifications and role rewards
- **Moderation** — Kick, ban, mute, warn, clear, lock/unlock, slowmode
- **Analytics** — Track messages, commands, and member activity
- **Utility** — Ping, server/user info, polls, reminders, and more

## Setup

1. Clone the repo
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your tokens:
   ```env
   DISCORD_TOKEN=your_bot_token
   OPENROUTER_API_KEY=your_openrouter_key
   MONGO_URI=mongodb+srv://your_mongodb_connection
   ```
4. Run the bot:
   ```bash
   python main.py
   ```
   Or use `start_bot.bat` on Windows.

## Admin and owner spam protection

The moderation cog automatically watches new messages from the server owner and human members with Administrator. It checks each account across channels and threads in the same server. It selects three copies of the same nonempty text within 30 seconds (ignoring case and extra whitespace), three actual `@everyone` or `@here` pings within 15 seconds, or eight messages within eight seconds. It deletes messages matching those windows, including the first copies. Fast legitimate conversations can also meet the flood threshold.

Grant the bot **View Channel** and **Manage Messages** wherever cleanup is needed, and enable the Message Content Intent. Its role can stay in its current position. Detection uses recent events in memory and starts fresh on restart. The bot can only clean up while connected and permitted to delete messages. Already delivered notifications may still be visible.

The bot alerts the affected account and the server owner by DM, and posts incident counts to the configured `MOD_LOG_CHANNEL_ID`. Alerts are limited to one per account per server per minute. Failed removals appear in the hosting logs even when DMs are closed.

## Requirements

- Python 3.10+
- discord.py >= 2.3.0
- See [requirements.txt](requirements.txt) for full list

## Project Structure

```
main.py              # Entry point
config.py            # Configuration from .env
cogs/                # All bot modules (slash commands)
  ai.py              # AI chat commands
  analytics.py       # Server analytics
  economy.py         # Economy system
  leveling.py        # XP & leveling
  moderation.py      # Moderation commands
  server_builder.py  # Server template builder
  ticket_system.py   # Advanced ticket system
  _tickets_old.py    # Archived; not loaded
  utility.py         # Utility commands
services/            # Business logic layer
  ai_service.py      # OpenRouter API integration
  economy_service.py # Economy data management
  embed_service.py   # Consistent embed factory
  json_builder.py    # Server schema builder
  level_service.py   # XP/level calculations
  permission_service.py # Permission helpers
  ticket_service.py  # Ticket data management
utils/               # Shared utilities
  decorators.py      # Slash command check decorators
  helpers.py         # General helper functions
  logger.py          # Logging setup
  paginator.py       # Embed pagination view
data/                # Runtime data (JSON storage)
```

## License

MIT


## Discord UI (v2.2)

- `/builder_panel` offers presets, custom JSON and AI themes. Review roles and channel access in the section menu before confirming. Export the JSON to reuse the exact plan.
- `/help`: private category menu for the command guide.
- `/diagnostics`: administrator-only module status, configuration checks, server permissions, and recent error references.
- Buttons and modals report unexpected errors privately with a reference ID. Operators can look up that ID in `logs/error.log` or hosting logs.

## Operations and validation

Run **one bot process** with persistent storage for `data/`. Builder progress, economy, XP, AI memory and quarantine depend on that directory. Ticket configuration and deletion deadlines use MongoDB when configured and local JSON otherwise.

Tickets use MongoDB when MONGO_URI is configured and atomic local JSON otherwise. A configured but unavailable database never silently falls back. An OpenRouter key is required for AI requests; AI_MODEL can override the default model. `/diagnostics` distinguishes configuration from verified connectivity. Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal. The bot must also have the relevant permissions and role position. Presets that create Administrator roles require the bot to be able to grant Administrator.

The HTTP health endpoint returns 503 until Discord and slash commands are ready. Check `/diagnostics` and the hosting logs for startup errors.

With `clean_existing:true`, the server owner can build the replacement alongside the old layout. After the replacement is ready, the owner receives a separate approval with the exact channel and role IDs eligible for removal. Approval permanently deletes the listed channels, their messages and threads, and the listed role assignments. The builder preserves the command channel and the resources configured for tickets and moderation. The replacement needs enough room to coexist with the old layout until approval.

If a build stops partway, confirm the same plan again to reuse recorded resources. For an AI plan, take the JSON attachment from the build result and submit it to `/setup_custom`. Keep `data/builds/` across restarts.

Ticket setup requires dedicated staff roles and a staff-only log channel. When a ticket closes, the bot checks the log channel again and uploads a ZIP with the channel's message history and attachment copies before scheduling deletion. Tickets stay open when Discord rejects the archive or when the archive exceeds the upload limit.

Offline checks (no credentials needed):

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

See [COMMAND_AUDIT.md](COMMAND_AUDIT.md) for the original 57-command audit, verified checks and limitations; the server butler adds two commands documented below. [UPDATE_REPORT.md](UPDATE_REPORT.md) preserves the earlier review.


### JSON examples and command recovery

Use `/json_example` to browse and download examples, view the format guide, validate pasted JSON or get an AI draft. `/json_validate` checks a file or pasted JSON, and `/server_json` exports the examples. AI plans receive schema validation and at most one repair request. Applying a plan requires administrator access and explicit confirmation. [Examples and format guide](examples/JSON_REFERENCE.md)

Commands acknowledge before slow work. Read-only results provide Refresh/Command guide/Close controls; existing specialized menus are retained. If commands are missing, inspect `/diagnostics`, hosting logs, intents, and permissions. Only the bot owner or configured DEV_IDS can run `/sync_commands` to retry global registration. Use slash commands; the `!` prefix is not an alias for them.

`/ai` replies privately and keeps memory separate per server; `/ai_reset` affects the current scope. AI cannot inspect live state or execute bot actions. `/poll` uses single-choice dropdown votes for one hour (or until restart). `/remind` persists its deadline, `/reminders` lists saved reminders, and `/reminder_cancel` removes one of your own references. Reminder DMs may fail if blocked; delivery retries are bounded and may repeat after a crash between delivery and recording completion.

Shop XP Boost purchases now enable double message XP for one hour, stacking time on repeat purchases. Badge/colour entries remain collectibles or staff-fulfilled vouchers, not automatic Discord role grants.

## Server butler

Repeat `@everyone`/`@here` messages within three hours are removed, with a polite,
rate-limited DM asking the sender to stop. Administrators and the server owner
follow the same rule. The bot owner receives private incident/audit notices,
today's first-start snapshot and a daily ledger of joins, departures and each
person's observed voice joins/time. Ordinary message text and voice audio are
not collected. Missing historical activity is clearly marked as unknown.

Use `/butler status` (administrator/owner) and `/butler report` (bot owner).
Reports default to **00:05 Asia/Kolkata** for the previous day. Keep `data/`
persistent, allow owner DMs, and grant Manage Messages and View Audit Log.
See [BUTLER_UPDATE.md](BUTLER_UPDATE.md) and [.env.example](.env.example) for
setup, retention, first-report behavior and limitations. The first real report
requires the updated bot to be deployed and connected to Discord.

## Private security and quarantine

The bot uses Discord Components V2 for the command center, main builder panel, security console and compact command results. Existing ticket, shop, JSON and detailed build controls keep their specialized workflows. Install `discord.py>=2.6.0,<3.0` from `requirements.txt`.

| Command | Purpose |
| --- | --- |
| `/create_channel name kind category private` | Create a text channel, voice channel or category. A private channel initially admits its creator, the bot and administrators. |
| `/builder_panel` | Open the new builder panel, including channel creation. |
| `/security` | Open the administrator's private permission and recovery status. |
| `/quarantine_setup staff_role` | Create a hidden review category and marker role, then add restrictions to existing channels. The staff role is optional. Repeat to repair a partial setup. |
| `/quarantine member reason` | Suspend editable roles and channel access, create a private review room for the member and save a recovery record. |
| `/sudo_ban member reason` | Apply the same reversible quarantine. The member stays in the guild. |
| `/quarantine_release member` | Restore saved roles and channel access. Repeat after fixing permissions if release is partial. |
| `/butler status` | Check private reporting and activity collection. |
| `/butler report [date]` | Bot owner only, privately open the activity ledger. |
| `/bot_profile` | Bot application owner only, refresh the dynamic public description and presence. |

Quarantine setup requires Administrator on the caller. Apply/release requires Moderate Members and Manage Roles on the caller, plus the normal actor/target hierarchy checks. The bot needs View Channel, Send Messages, Read Message History, Manage Channels and Manage Roles and must outrank every role it suspends or restores. Existing role order is preserved. The owner is protected. Targets with privileged everyone/managed/uneditable roles are refused. The quarantine marker has zero guild permissions and is neither hoisted nor mentionable. Deleted resources can be recreated by rerunning setup.

Per-member channel permissions enforce quarantine even where other roles explicitly allow access. Each active case has its own room. Release removes the case member's private access grant and retains the history for staff. On startup, the bot makes the legacy shared review room staff-only and restores active cases to individual rooms. Voice disconnection needs Move Members. Partial operations can be retried. Saved roles that gained permissions require staff review before restoration.

Timeouts, automatic warning timeouts, kicks, bans, quarantines and releases generate private receipts to both the bot application owner and the server owner. Successful bot actions are recorded in the daily ledger. The original `/ban` remains available for an actual Discord ban. Use `/sudo_ban` for quarantine. Daily bot-owner reports run at **00:05 Asia/Kolkata** by default, with retryable delivery and the first available today's report on startup. Configure `BUTLER_OWNER_ID` for the report recipient and `BUTLER_REPORT_HOUR`/`BUTLER_REPORT_MINUTE` for another local time. DMs must be open.

Moderation command replies are ephemeral. The configured moderation log is skipped when ordinary members can view it. Quarantine spaces are hidden from ordinary members. Server owners and Administrators retain Discord's visibility bypass, and members can still observe role/profile changes. The bot cannot guarantee invisible moderation or stop an owner removing its permissions. New channels created by other clients are protected when Discord delivers their creation events.

`BOT_CREATOR_NAME=luffyanna` supplies the public creator credit. The application description and presence refresh every 15 minutes with the version and server count. Private incident counts stay in private reports. These updates use the application description API, and require a running bot with a valid token.

Keep `data/quarantine.json` and `data/butler.sqlite3` on persistent storage and run one bot process for these local stores. After restart, active records and channel restrictions are reconciled. Partial releases remain recorded for explicit staff completion. Hidden channels are marked incomplete and never used for permission snapshots. Discord API writes span multiple requests, so an interruption can leave a partial operation. Review `/security`, `/butler status` and hosting logs before treating a partial result as complete.

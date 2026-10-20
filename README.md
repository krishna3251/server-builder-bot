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

- `/builder_panel`: choose a preset, custom JSON, or AI theme. Review the plan with the section menu, Previous/Next controls, and JSON export before confirming.
- `/help`: private category menu for the command guide.
- `/diagnostics`: administrator-only module status, configuration checks, server permissions, and recent error references.
- Buttons and modals report unexpected errors privately with a reference ID. Operators can look up that ID in `logs/error.log` or hosting logs.

## Operations and validation

Run **one bot process**. Economy and ticket transition locks are process-local. Keep the `data/` directory on persistent storage; setting `MONGO_URI` persists ticket state only, not economy/XP/AI data. Ticket deletion deadlines are stored in MongoDB and restored after a normal restart. Do not run two deployments against the same data at once.

Tickets use MongoDB when MONGO_URI is configured and atomic local JSON otherwise. A configured but unavailable database never silently falls back. An OpenRouter key is required for AI requests; AI_MODEL can override the default model. `/diagnostics` distinguishes configuration from verified connectivity. Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal. The bot must also have the relevant permissions and role position. Presets that create Administrator roles require the bot to be able to grant Administrator.

The HTTP health endpoint returns 503 until Discord is ready or if a cog failed to load. It does not verify every remote dependency. Missing `DISCORD_TOKEN` exits with an error. Cleaning an existing server permanently deletes content; rollback only covers newly created resources.

Offline checks (no credentials needed):

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

See [COMMAND_AUDIT.md](COMMAND_AUDIT.md) for the current issue register, all 57 executable commands, verified checks, and remaining limitations. [UPDATE_REPORT.md](UPDATE_REPORT.md) preserves the earlier review.


### JSON examples and command recovery

Use `/json_example` to browse and download examples, a format guide, paste validation, or an AI draft. `/json_validate` validates an uploaded file or JSON text without changing the server. `/server_json` exports the same canonical examples. The source-controlled copies live in [examples/](examples/) with [the format guide](examples/JSON_REFERENCE.md). AI plans receive schema validation and at most one repair request; building still uses administrator checks and confirmation unless you explicitly disable AI preview.

Commands acknowledge before slow work. Read-only results provide Refresh/Command guide/Close controls; existing specialized menus are retained. If commands are missing, inspect `/diagnostics`, hosting logs, intents, and permissions. Only the bot owner or configured DEV_IDS can run `/sync_commands` to retry global registration. Use slash commands; the `!` prefix is not an alias for them.

`/ai` replies privately and keeps memory separate per server; `/ai_reset` affects the current scope. AI cannot inspect live state or execute bot actions. `/poll` uses single-choice dropdown votes for one hour (or until restart). `/remind` persists its deadline, `/reminders` lists saved reminders, and `/reminder_cancel` removes one of your own references. Reminder DMs may fail if blocked; delivery retries are bounded and may repeat after a crash between delivery and recording completion.

Shop XP Boost purchases now enable double message XP for one hour, stacking time on repeat purchases. Badge/colour entries remain collectibles or staff-fulfilled vouchers, not automatic Discord role grants.

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
  tickets.py         # Basic ticket commands
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

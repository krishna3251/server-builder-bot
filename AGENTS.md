# AGENTS.md — Server Builder Bot

## Project map
- Python 3.10+ Discord bot built with `discord.py` 2.x.
- `main.py` starts the bot; `cogs/` contains Discord command modules; `services/` contains business logic for AI, tickets, economy, embeds, level tracking, permissions, and server-schema building; `utils/` contains shared helpers and logging.
- `config.py` centralizes environment-based configuration. Read the relevant service and cog before making changes.

## Architecture and behavior
- Keep cogs thin where practical: validate Discord interaction input and permissions, then delegate reusable work to services.
- Preserve existing moderation, tickets, AI chat, server-builder, economy, leveling, analytics, and utility functionality.
- Treat AI-produced server layouts/JSON as untrusted input. Validate schemas, allowed operations, bounds, and permissions before creating channels, roles, permissions, or other server resources.
- Ensure persistent ticket buttons and state remain valid across restarts; consider duplicate interactions, missing channels, deleted messages, and partial failures.
- For economy/leveling, preserve data consistency and prevent duplicate reward processing or negative/invalid balances.
- Keep async command handlers non-blocking and handle Discord API rate limits and permission failures gracefully.

## Security
- Read credentials from environment variables. Never hardcode, print, or commit `DISCORD_TOKEN`, `OPENROUTER_API_KEY`, database secrets, or other credentials.
- Authorize privileged operations using Discord permissions and bot hierarchy checks, not AI output or client-side assumptions.
- Do not expose internal exceptions or sensitive user/server data in public messages.

## Validation
- Inspect `requirements.txt` and the actual test setup before choosing commands.
- Verify imports/syntax and test the touched service/cog. Run available broader checks if feasible.
- Report exactly what was tested and any service credentials needed for full integration tests. Avoid unrelated rewrites or dependency upgrades.
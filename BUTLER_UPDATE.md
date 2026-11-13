# Server butler update

The upgrade is specific to `krishna3251/server-builder-bot`, baseline `a4c46532331d66c9ada9a42de6639f5bb4ea6208`. The existing nine cogs, server-builder checks, moderation hierarchy, tickets, economy, XP, AI, analytics and reminders remain. The baseline passed 56 offline tests.

## Behavior

- One actual mass-mention **message** per author/server in a rolling three hours. `@everyone` and `@here` share the limit across channels. Discord's parsed `mention_everyone` flag determines whether a message contains a real mass mention; ordinary literals do not count. Multiple tokens in one message still cause one notification/message. A duplicate gateway event or edit of the same message does not spend another allowance.
- The second and later messages are deleted. There is no administrator, developer, bot-owner or server-owner exemption. The bot ignores its own messages; other bots/webhooks are subject to removal but do not receive courtesy DMs.
- A polite butler-style DM asks the member to stop. At most one courtesy/incident notice per author/server per rolling three-hour cooldown is queued; further violations still appear in the daily ledger. Failed deletion is recorded and the DM does not falsely claim it succeeded. Deletion cannot undo a notification Discord already sent.
- The bot owner receives a separate private incident notice, bounded audit-review alerts, a first-start **today** snapshot, and daily ledgers for the preceding local day. No incident reports are posted publicly if DMs fail.
- Joins and departures list member IDs/names/times. Raw member-removal events cover uncached members. On startup, up to 2,000 currently cached members' join timestamps from today can be imported and deduplicated.
- Per-person voice/stage reports count actual voice joins, channel moves, and observed channel-presence duration. Mute/deafen changes do not count as new joins. Sessions are clipped at each reporting-day boundary. Mute, AFK, deafness and stage audience time still count as **presence**, not speaking time, attendance quality or audio.
- A member already connected when tracking starts is marked as observed present with zero new joins; previous join time is unknown. Reconnect/heartbeat recovery does not fabricate joins. A crash closes old intervals at the last heartbeat, excluding unobserved downtime. A normal disconnect/shutdown closes observation windows explicitly.

Example courtesy tone (operational text, no model required): “A small housekeeping request from your server: please keep @everyone/@here to once every three hours. I removed the repeat-mention message. Thank you for helping keep the server calm.”

## Audit review

The bot listens for audit entries and backfills at most 500 **recent** entries on startup. It checks again every 15 minutes, with an overlapping cursor and audit-ID deduplication. Missing permission, API failure, malformed entries and history caps are reported as coverage limits.

Ledgers show actor/action counts and recent target IDs. Configured review flags include newly enabled powerful permissions (Administrator, Mention Everyone, Manage Roles/Server/Webhooks, Kick/Ban), new integrations, and three or more same-actor channel deletions, role deletions, bans or kicks within ten minutes. Alerts are limited to one per actor/action/hour. Startup backfills do not generate a flood of historical alerts. These are deterministic review signals: legitimate server-building or moderation can trigger them. The bot does not infer motive, automatically punish staff, change roles, reverse changes or execute model-generated actions.

Audit reasons, webhook secrets, permission-object dumps and normal message text are not stored or sent to an AI provider. Gateway audit actors can be uncached; the stable `user_id` is used rather than guessing a name.

## First report and historical limits

After this code is merged **and the updated process is deployed**, the ready collector automatically queues today's first ledger for each enabled server. It includes available audit records, current-member join timestamps and observations since tracking began. It is deduplicated per server/local date, so reconnecting does not repeatedly DM the same first report. A restart on another date can provide a new today snapshot. The regular previous-day ledger has its own delivery reference.

Discord does not supply a complete historical list of ordinary departures or past voice sessions. Existing analytics only contains totals. Those missing identities/durations cannot be reconstructed truthfully. Every ledger states the tracking start, observed online coverage, imported-data source and audit availability. The first report may therefore be partial. Nothing here claims that a real report has already reached the owner before deployment or without Discord credentials.

## Setup and permissions

1. Retain the `data/` directory on durable storage. Restart a single updated bot process and allow command sync to finish.
2. Enable the existing **Server Members** and **Message Content** privileged intents in the Discord Developer Portal. Voice-state and moderation intents are enabled in code. No presence intent or voice connection is needed.
3. Grant the bot **View Channel**, **Manage Messages** in channels to enforce the rule, and **View Audit Log** for audit collection. Message edits additionally need Read Message History for fetching an uncached edited message. Role hierarchy does not exempt administrator/owner messages from deletion; channel permissions still apply.
4. Open the bot owner's DMs. By default the recipient is the Discord application's owner, or its team owner. Set `BUTLER_OWNER_ID` if a specific trusted owner account should receive the ledgers.
5. `/butler status` is private and available to server administrators/the bot owner. It explains permissions, intents, schedule and recent delivery issues. `/butler report [date:YYYY-MM-DD]` privately exports the current server's ledger for the bot owner only; a server administrator alone cannot open it. Callback guards apply even to direct invocation.
6. To cover only selected servers, set `BUTLER_GUILD_IDS` to their comma-separated IDs. `BUTLER_ENABLED=false` disables collection/enforcement/delivery. Ordinary existing commands continue to work.

| Setting | Default | Meaning |
| --- | --- | --- |
| `BUTLER_ENABLED` | `true` | Enable the requested feature |
| `BUTLER_OWNER_ID` | empty | Default to application/team owner |
| `BUTLER_GUILD_IDS` | empty | All connected guilds; otherwise explicit IDs |
| `BUTLER_TIMEZONE` | `Asia/Kolkata` | Named reporting timezone, also used for “today” |
| `BUTLER_REPORT_HOUR` | `0` | Daily hour, 0–23 |
| `BUTLER_REPORT_MINUTE` | `5` | Daily minute, 0–59 |
| `BUTLER_RETENTION_DAYS` | `30` | Event/voice/delivery retention, clamped to 7–90 days |

See `.env.example`. On Windows without an IANA timezone database, install `tzdata` for named timezones; unavailable timezone names fall back to UTC with a warning and UTC labels in reports. There is no new required runtime package.

## Storage and reliability

A dedicated stdlib SQLite ledger at `data/butler.sqlite3` stores structured event metadata, voice intervals, ping allowances/cooldowns and a durable DM outbox. The existing project has JSON counters but no general transactional event database; rewriting a complete JSON file for each voice/audit event would lose consistency and scale poorly. This ledger does not replace or compete with ticket MongoDB or modify existing JSON state.

All SQLite work runs in serialized, cancellation-drained workers with WAL and a bounded busy timeout. Discord I/O remains asynchronous and uses SDK rate limits. The outbox has a 1,000-pending-job cap, report priority, three bounded delivery attempts for transient errors and no retries for blocked/deleted DM recipients. Notices older than three hours expire. Departed-guild jobs are retired. Catch-up daily scheduling is bounded to seven days; unknown offline activity is not invented. The single-process requirement already applies to this bot.

Reports export at most 1,000 join/leave details, 500 voice rows, 200 audit actor/action groups and 200 incident/audit details; total file output is capped at 256 KB, with visible truncation. Aggregates may exceed detail counts. The raw event ledger is retained by time, not a claim of constant disk use across unlimited servers/activity.

Expiry cleanup runs lazily while the store is used. Guild IDs/names and first-observed timestamps remain as minimal ledger references; event/voice/completed delivery records expire. Active voice rows stay until closed. SQLite deletion does not securely erase backups. Back up the SQLite file with SQLite's backup API or while the bot is stopped; when copying a live WAL database, its WAL/SHM state matters. Existing JSON/Mongo backups do not automatically include this new file.

A crash after Discord accepted a DM but before its sent receipt commits can duplicate that DM after restart. No exactly-once external-delivery claim is made. If persistence fails, the butler reports degradation rather than pretending deletion/reporting succeeded. Existing features remain separate. A blocked DM cannot be forced; status/manual report are the recovery paths.

## Validation

- Baseline: **56 tests passed** before changes.
- Updated suite: **100 tests passed** on Python 3.12; compile checks and `git diff --check` passed.
- No-network startup comparison: **9 → 10 cogs**, all **57 existing executable commands preserved**, **59 total** after adding `butler status` and `butler report`. All cogs and the new worker closed cleanly. See `BUTLER_VALIDATION.json`.
- The added suite covers three-hour boundaries, restart/deduplication, owner/admin enforcement, guild/user isolation, deletion/DM failures, notice limits, report privacy, scheduling/timezones, voice joins/moves/midnight/downtime, audit review/deduplication, metadata privacy and cancellation-drained storage.
- Existing command-registration checks load the tenth cog and serialize its commands. A voice-snapshot test uses the real `discord.Guild` public interface and channel-level voice-state mappings.
- Tests use fake Discord events and mocked delivery. Real live permission handling, DM delivery, audit coverage and gateway reconnect behavior require a test-guild deployment check. No production token, real server log or private message was accessed during development.

Run `python -m unittest discover -s tests -v`, then `python -m compileall -q cogs services tests main.py` and `git diff --check`.

Files: added `cogs/butler.py`, `services/butler_service.py`, `services/butler_store.py`, `tests/test_butler.py`, `.env.example`, `BUTLER_VALIDATION.json`, and this report. Updated `main.py` intents, command help, README, development ignores and the extension-count regression. No existing feature modules or records were removed.

Protocol reference: [discord.py gateway events, audit logs and voice states](https://discordpy.readthedocs.io/en/stable/api.html). These interfaces were also checked against the installed SDK; they are not substitutes for a live gateway test.

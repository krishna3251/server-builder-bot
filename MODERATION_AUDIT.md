# Server Builder Bot moderation and quarantine audit

Reviewed `main` at `49f0e6f12ba5b2dab82ff47e1bae7b640cbe4847`. This report covers the loaded cogs, shared services, permission engines, state stores and startup paths in that checkout. The [command inventory](COMMAND_AUDIT.md#command-inventory) lists executable slash paths and parameters.

## Feature inventory

| Cog | Responsibilities and interactions |
| --- | --- |
| AI | Private OpenRouter chat, persona and reset. Uses server-scoped memory. Builder drafts use stateless requests and schema validation. |
| Analytics | Aggregates messages, commands and joins/leaves with batched JSON writes. Counters can include messages subsequently moderated or removed. |
| AutoMod | Manages Discord-native rules and exceptions and reads timeout history. Observes native enforcement for private receipts and timeout conflict suppression. |
| Builder Tools | Previews saved builder plans and audits channel access against a template. Uses guild settings and the shared builder. |
| Server Butler | SQLite activity ledger, voice presence, audit review, anti-ping policy and private notification queue. Observes existing moderation actions without changing roles. |
| Dashboard | Welcome cards, optional Giphy, auto-role, command channel, bulk role and rules controls. Automatic role grants defer to verification and quarantine. |
| Economy | Wallets, rewards, transfers, shop and inventory. Uses serialized JSON updates. Purchased XP boosts feed Leveling. |
| JSON Tools | Validates and exports plans, opens a draft or build preview. Calls the same schema and builder as Server Builder. |
| Leveling | Earns XP from messages, reports rank and leaderboard and announces level-ups. Reads the economy boost multiplier. |
| Local AutoMod | Opt-in link and image burst removal, local exceptions, offense tracking, warnings and repeat timeouts. Shares message arbitration and observes existing or recently observed native timeouts. |
| Moderation | Manual warnings, timeout, kick, ban, purge and channel controls. Privileged Spam Guard detects mass pings, repeated text and bursts. Uses channel, hierarchy and private logging checks. |
| Security | Quarantine records, repairs, audit, migration, cleanup, role removal review, channel creation and profile worker. Owns the only quarantine writer and shared recovery journal. |
| Server Builder | Template, AI and custom layout builds with validation and rollback of newly created content. Destructive rebuild refuses to run while quarantine is configured. |
| Server Manager | Interactive guild health and channel, category, role, position and permission changes. Security listens for channel changes and reapplies only required quarantine access. |
| Soft Banned Diddy | Explicitly configured protected channel, pinned warning and private receipts. For regular members it owns the matching message incident and delegates quarantine to Security. |
| Ticket System | Persistent ticket controls, staff access, transcripts, blacklist and restored deletion deadlines. Uses MongoDB when configured or atomic JSON on local disk. |
| Utility | Command guide, server and user info, polls, durable reminders and startup diagnostics. Reminders use a separate delivery worker. |
| Verification | Persistent verification buttons, optional account age gate and verified/unverified roles. Its automatic access grants respect active quarantine records. |

The underscore-prefixed legacy ticket cog is archived and omitted from startup. The quarantine dashboard view uses the same convention and is imported by Security, so it does not register a competing cog.

## State, workers and inputs

| Component | Review |
| --- | --- |
| Startup and Discord API guard | Extension registration, command sync, Flask health, authentication retry and rate-limit classification. Registration failures are logged per extension. Startup emits command and named-worker diagnostics. |
| Builder and guild engines | JSON schema, planning, channel/category/role operations, access and hierarchy validation, rollback and permission audit. Both schema and interactive server management remain subject to Discord permissions. |
| Butler SQLite | WAL, transactional receipt and cooldown writes, bounded queue, retention, boot integrity check and classified failures. Existing rows are migrated in place. |
| Quarantine JSON | Atomic journal, canonical and previous resource IDs, ownership, member role/permission snapshots, release actor, pending creations/migrations and per-operation cleanup snapshots. |
| Other JSON | Warnings, local AutoMod, trap settings, dashboard, verification, economy, levels, reminders, AI history, analytics, templates and local tickets. Atomic writes and process-local locks are used where those stores mutate shared state. |
| Memory and external services | Message deduplication, privileged-spam and local image windows, interactive views and model rate limits. Credentials remain environment settings. Discord is the authority for role and channel state. |
| Background work | Butler delivery, voice heartbeat and bounded audit backfill. Security profile refresh. Batched analytics persistence, reminders and ticket-deletion recovery. Quarantine runs on ready/resume, scoped gateway changes and operator requests. |

## Findings and changes

| Finding | Root cause | Change |
| --- | --- | --- |
| Staff pings bypassed Butler while deletions could still appear from other modules | Earlier change added owner/administrator exemptions and left four deletion listeners independent | Uniform Butler rules, persistent exceptions and a shared message-deletion claim. Regular trap traffic takes precedence. Butler violations take precedence over the other bot-run message rules that evaluate the same mass mention. |
| Exemptions missed categories and differed by module | IDs were environment-only and ancestry resolution only looked one level up | Guild-level SQLite policy, parent and category matching, and privileged spam guards that consult current Butler exclusions. Local and native AutoMod keep their own policies. |
| Rejected mass mentions could continually extend the wait | All attempts counted toward the next allowance | Store the last accepted message separately from violations, with persistent message-ID deduplication. |
| Old Butler ledger showed 38 failures and 5 successes without causes | Delivery queue persisted status and attempts only | Classify closed DMs, missing users, permission failures, API errors, rate limits, expired jobs, connection failures and invalid settings. Permanent jobs stop and transient jobs have three attempts. The 43 historical outcomes cannot be reclassified from the exported ledger. |
| Repeated quarantine setup issued redundant permission writes | Setup applied complete overwrites on every run and treated saved IDs as the only available evidence | Compare targeted fields, reuse saved or verified resources, retain previous IDs and recover uncertain creates using a request identifier in the Discord audit reason. |
| Resource replacement and cleanup could leave active members untracked | No resource ownership and staged transfer between old and new marker roles | Journal the transition, apply access restrictions and verify replacement membership before removing the old marker. Clean up only verified obsolete resources, after confirmation and a snapshot. |
| A reused category or role could conflict with access grants | Role allows and member-specific overrides interact. Discord administrators bypass channel overrides | Validate marker role and hierarchy, inspect effective permissions, and maintain a member-specific view boundary for active quarantines. Automated verification and auto-role paths skip tracked members. |
| Authorized role removal and reconnect were sensitive to event ordering | Member update events have no actor, and previous recovery reran all tracked member work | Verify a matching audit action and current actor permissions. Journal verified releases, reconcile on resume, and retry unresolved protection without inventing actor attribution. |
| Native timeout and bot timeout could overlap | Discord acts independently of bot listeners and each rule has its own scope | Honor current timeouts and recent native timeout events. Keep native and local configurations separate and identify the origin in private logs. |
| Runtime failures could affect unrelated features | Environment Discord IDs were parsed with unchecked integer conversion | Ignore invalid IDs with a setting-specific log. Unhealthy ledgers and quarantine journals are reported per feature. |
| Some transient state could grow with users or repeated actions | Idle image windows were only pruned when the same user returned. Spam accounts lacked a hard bound | Bound the shared message claims and active spam/image windows, and expire short-lived evidence. |

## Maintenance procedure

1. Use `/quarantine dashboard` or `/quarantine audit`. Check the canonical role, review room, category, permissions and unfinished member operations. Use `/quarantine resources` for ownership evidence. Resource names only identify candidates for review.
2. Use `/quarantine sync` to repair drift with existing resources. Use `/quarantine repair` or `/quarantine_setup` for missing resources. Setup preserves valid canonical resources and can select an existing verified idle marker. When changing the staff role, supply the replacement to `/quarantine_setup`.
3. Open `/quarantine migrate` and confirm the preview. The service restricts access, journals the replacement, adds and verifies it, and only then removes the old marker. A failed member remains in the journal for a later retry. Repair and migration preserve original restore snapshots.
4. Open `/quarantine cleanup`, download and inspect the preview, then confirm as the initiating administrator. The bot rechecks the plan, current assignments, permissions and resource state. Assigned roles, active record dependencies, integration roles, system channels, populated rooms, categories with children and uncertain resources are preserved. A cleanup snapshot records candidate permissions and settings on disk before deletion. Deleting a confirmed obsolete role also removes its Discord overwrites. A snapshot can help reconstruct settings and permissions, but Discord will assign a new ID to any recreated resource.
5. Use `/quarantine_release` for an explicit release. A moderator removing the marker in Discord must be identifiable in the audit log and authorized at verification time. Otherwise the bot flags the change and preserves the quarantine. If Discord history is unavailable, use the explicit command to establish the actor.

## Access and configuration

| Operation | Required access |
| --- | --- |
| Butler policy | Administrator or server owner. Message deletion requires the bot to have View Channel and Manage Messages in the channel. Edited-message lookup needs Read Message History. |
| Quarantine resource setup and dashboard | Administrator or owner. Bot needs Manage Roles and Manage Channels, channel access and effective Manage Roles where it writes overwrites. |
| Apply or release | Staff need Moderate Members and Manage Roles for the slash paths and must outrank the target. Bot must outrank the target and relevant roles. Quarantining the server owner is blocked. |
| Manual marker removal review | Bot needs View Audit Log. It verifies owner/administrator or the configured moderator role with Manage Roles. |
| Voice isolation | Bot needs Move Members to disconnect an already connected quarantined member. |
| Cleanup | Effective Manage Channels and applicable hierarchy. Text-channel cleanup additionally checks Read Message History and Manage Threads to verify there are no messages or threads. Members intent is required to check complete role membership. |
| Private moderation receipts | Set `MOD_LOG_CHANNEL_ID` to a staff-only channel. Bot needs View Channel, Send Messages and Embed Links. Trap keeps its own private log selection. |
| Discord Gateway | Enable Server Members and Message Content in the Discord Developer Portal. The code enables moderation, AutoMod execution and voice-state events. |

`BUTLER_ANTI_PING_MODE` sets the default `delete`, `warn` or `disabled` behavior. The legacy `BUTLER_ANTI_PING_DELETE=false` applies when the mode is unset. `AUTO_MOD_TRUSTED_CHANNEL_IDS` sets initial Butler exceptions and baseline privileged-spam exclusions. Saved Butler policy takes precedence per guild. Keep `PRIVILEGED_SPAM_MODE=alert` unless you intend to enable deletion.

## Wispbyte deployment

1. Stop the single running process. Download a backup of the existing runtime directory, including `quarantine.json`, `soft_banned_diddy.json` and `butler.sqlite3`. A stopped process gives a consistent SQLite backup.
2. In Wispbyte **Startup**, choose Python 3.12 and keep `BOT_DATA_DIR` pointed at the existing persistent location. Earlier deployments used `/home/container/data` or `/home/container/server-builder-bot/data`. Read the last runtime-state log to identify which is in use. Keep `DISCORD_TOKEN` and optional provider credentials in the startup environment.
3. In the existing checkout, use a fast-forward pull from `main`, install `requirements.txt` and launch `python -u main.py`. A startup command for the usual checkout is below.

   ```sh
   cd /home/container/server-builder-bot &&
   git pull --ff-only origin main &&
   python -m pip install -r requirements.txt &&
   exec python -u main.py
   ```

4. Verify the running revision, successful Discord login, slash command sync, Butler ledger check and quarantine startup report in the console. Inspect `/butler status`, `/quarantine audit` and `/soft_banned_diddy_status`. Check with a test channel and test guild before any production cleanup. A normal restart reuses saved state.
5. For rollback, stop the process, restore the last known good code and matching data backup, then start once. Reconcile any Discord actions taken after that backup with staff before further mutations.

[Wispbyte startup settings](https://wispbyte.com/kb/startup-settings)

## Verification and limits

- Run `python -m compileall -q .` and `python -m unittest discover -s tests -v`. The suite includes mocked Discord requests. GitHub CI runs the same suite on Python 3.11 and 3.12.
- Discord actions and local persistence are separate systems. A crash after a DM is accepted but before its receipt commits can produce a duplicate DM. Gateway ordering can also prevent the bot from observing a native timeout before it decides whether to act.
- Discord permission, hierarchy, audit-log latency, command propagation and reconnect behavior need verification in a test guild. Quarantine with insufficient permissions remains marked partial. Audit-log history is bounded and names alone do not establish ownership.
- Local JSON and SQLite require one bot process per data directory. Cleanup snapshots require periodic operator review for disk use. Existing ticket transcripts and historical activity use their existing retention and coverage limits.
- This audit leaves the legacy ticket implementation archived, MongoDB and AI integrations on their existing libraries, and Discord-native rules on their existing server settings. The main changes target moderation, permission coordination and recovery.

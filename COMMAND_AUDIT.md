# Command reliability and JSON workbench audit — v2.2.0

Base: `d2566e1320a77ba870bbb8c18ccbea1a580e0de3` (`main`, after PR #4 merged).

This update builds on the [previous issue register](UPDATE_REPORT.md). The table below records newly verified problems, changes, and evidence. “Registered” means an offline Discord command tree accepted the command; it does not mean a live Discord API operation was performed.

## What changed

- All slash-command callbacks acknowledge Discord before storage/network work. A shared response helper completes the deferred response, supports file uploads, and avoids double responses. Concrete callback signatures preserve discord.py's parameter parsing.
- Read-only results offer owner-locked Refresh, Command guide, and Close controls. Refresh reruns permission predicates. Mutating results get guide/close controls, not a repeat-action button. Existing specialized builder, shop, ticket, and moderation controls remain in place.
- `/json_example` browses six canonical examples, exports JSON and a format guide, validates pasted JSON, opens an AI draft modal, and opens an administrator-only build preview. `/json_validate` checks files or text without making changes. `/server_json` now exports a real minimal reference by default instead of the old misleading “blank” administrator-heavy layout.
- AI drafts go through strict parsing and schema validation, with at most one repair request. Personal chat is private, scoped by server (DMs use a separate scope), has bounded response tokens and request time, and explicitly cannot claim to execute bot actions.
- Tickets use MongoDB if configured and atomic local JSON otherwise. A configured but unreachable database never silently switches backends. Persistent storage and one process are still required for local state.
- Polls use one changeable vote per user and owner/moderator close controls. Reminders are saved before acknowledgment, restored by the worker after restart, have ownership checks, listing/cancellation commands, and bounded delivery retries.

## Newly identified problems and fixes

| Confirmed issue | Fix | Evidence |
| --- | --- | --- |
| Multiple commands did I/O before their initial Discord acknowledgment | Shared command acknowledgment and deferred-response completion | All 57 executable callbacks register with wrapped signatures; balance test asserts acknowledgment occurs before storage |
| Warning reset's Cancel button referenced `info_embed` without importing it | Correct import and working cancellation card | Actual button callback regression |
| Ticket cog failed to load without `MONGO_URI`, making its commands disappear | Explicit local JSON ticket backend when no URI is configured | All nine cogs load without a URI; separate-store reload and concurrent saves tested |
| Ticket setup required Manage Threads even though tickets are text channels | Remove unused permission requirement; check embed/history permissions used by the feature | Actual setup callback succeeds with Manage Threads disabled |
| Warning reset confirmation did not recheck moderation permissions | Recheck permissions on button interaction | Source/static check; live permission removal still requires test server |
| Warning writes could truncate or overwrite unreadable state, and concurrent mutations had no lock | Atomic writes, refusal to overwrite unreadable files, locked mutations with rollback on save failure | Static/source check; file failure tests in broader suite cover the shared atomic writer |
| `/warn` could target a member the moderator did not outrank | Apply owner/self/role hierarchy guard before recording warning | Static/source check; live role matrix outstanding |
| `/clear` rejected thread channels by assertion | Accept Discord threads, which support purge | Installed-library API inspection and import checks; live purge outstanding |
| Several moderation actions lacked bot permission checks | Add action-specific bot permission predicates before callbacks | Command-tree serialization and static checks |
| `/server_json` called an administrator-heavy populated example “blank” | Canonical minimal example, downloadable file, and interactive reference browser | Actual export callback produces a validated plan |
| Custom JSON paths selected text between braces instead of requiring complete JSON | Shared strict parser with code-fence/BOM support and line/column errors | Strict-parser and actionable-error tests |
| AI drafts were rejected without a repair attempt | One bounded repair using the validation error; reject if still invalid | Mocked malformed→valid and malformed→malformed sequences |
| Builder AI paths bypassed the chat limiter | Apply the per-user limiter to generation entry points | Source/static check; provider not contacted |
| AI chat could reuse one user's memory across servers and publicly post it | Server-scoped keys and private responses; generation remains stateless | Key isolation and mocked provider-history tests; no legacy history is reused in new scopes |
| AI could sound as though it performed server actions | Explicit no-tools/no-live-state instruction; actions remain deterministic permission-checked commands | Source check; model behavior is not guaranteed by prompting |
| Authentication failures tried additional models unnecessarily | Stop on 401/403; make model configurable using `AI_MODEL`; bound tokens and request deadline | Source/import validation; provider access outstanding |
| Private generated categories could lock the bot out of its own new channels | Explicit bot member access in generated category/channel overwrites | Creation-call regression preserves both privacy and bot access |
| Duration parser accepted negative/junk/zero strings as usable times | Require a complete positive bounded duration | Negative, zero, junk, overflow, and compound-time tests |
| `/remind` slept inside the command and lost deadlines on restart | Atomic reminder queue, list/cancel commands, ownership checks, worker and retries | Actual command returns after saving; reload, cancellation, and delivery tests |
| Reaction polls depended on reaction permissions and had no bot-enforced single-vote behavior | Dropdown voting, one changeable vote per user, guarded close | Actual vote/close callback tests |
| `/rank` calculated progress from lifetime XP instead of the current level's floor | Calculate progress within the current level interval | Source/import check; live display outstanding |
| Very large `/setxp` requests used a linear search | Integer binary search for the level | Large-XP boundary test |
| Failed XP saves could leave changed in-memory stats | Restore prior state on failure; refuse to overwrite unreadable XP files | Failed-save rollback test |
| Shop's XP Boost did not affect XP gains | Save an expiry on purchase; message XP uses the active multiplier | Purchase, balance, multiplier, and expiry tests |
| Analytics writer stopped permanently after a transient save failure | Atomic snapshots, error reference, and retry on the next interval | Source/import validation; actual disk outage outstanding |
| Diagnostics crashed when latency was NaN | Render “N/A” until latency is available | Actual diagnostics callback test |
| A command-sync outage was only logged, with no guarded recovery command | Retain sync error reference; health reflects sync failure; add owner/developer `/sync_commands` | Unauthorized sync regression; live sync outstanding |
| Console coloring mutated the shared log record | Format a copy so file handlers receive the original record | Source/static check |

## JSON workflow

1. `/json_example`: select Minimal, Creator, Gaming, Community, Study, or Business.
2. Download JSON and the format guide. Edit only supported fields. See `examples/` for the same source-controlled references.
3. `/json_validate json_file:<file>` (or `json_text`) reports syntax/schema errors without creating anything.
4. Use Preview build, or upload through `/setup_custom`. Review roles, permissions, and cleanup setting, then confirm as an administrator.
5. Use `/diagnostics` when a module or command fails. Unexpected failures include a private reference ID matching operator logs.

Validation checks structure and known limits. It cannot guarantee a correct design or permissions on a real server. Adult-only text channels require `nsfw:true` and separately controlled access roles; do not use `auto_assign` for adult access.

## Validation performed

- 56 offline tests passed on Python 3.12 with discord.py 2.7.1, including the 26 earlier regressions.
- All nine cogs load without MongoDB credentials; 53 top-level entries contain 57 executable commands. All commands serialize their parameters into Discord payloads.
- `compileall`, Python 3.10 grammar parsing, `pip check`, active-source Pyflakes, and `git diff --check` passed.
- No live Discord, MongoDB, OpenRouter, deployment, or visual screenshot test was performed. The simulated failures logged during tests are intentional.

## Known limitations / not claimed fixed

- Run one bot process. Local locks and JSON state are not distributed. Keep `data/` on persistent storage and back it up. MongoDB only backs ticket state, not other features.
- Discord permissions, role position, intents, command propagation, token validity, model availability, and external service connectivity need a live test server. Diagnostics distinguishes configured settings from verified connectivity.
- Cleanup permanently deletes content; it cannot restore old messages/roles. Creation rollback is best-effort. Never trial cleanup on an established server.
- Poll votes are in memory and close after one hour or restart. Old message controls may remain visible after a restart but cannot act until a fresh poll is created.
- Reminder delivery is at-least-once across a crash between DM delivery and saving completion. DMs may be blocked; three failed attempts mark a reminder failed. Persistent storage is required for restart recovery.
- Ticket transcripts retain the existing 200-message text-only limit. Failed automatic deletions/recovery still need operator inspection/restart; there is no distributed retry queue.
- VIP badge/custom colour purchases remain inventory/voucher items requiring staff fulfillment, not automatic role assignment. XP Boost now has an implemented effect. Existing historical shop descriptions may require staff editing.
- AI has no live web access or action tools. It can be wrong; schema validation and prompting do not guarantee truth. Old unscoped memory remains on disk but is not reused in new server-scoped chats.
- `/help` is the command guide. Most functionality is slash-command-only; `!` is not an equivalent prefix command API.
- Not every intentional exception catch is recorded in the recent-error ring. Historical logs and the previous audit remain relevant.

## Command inventory

18 cogs load offline and register 110 executable slash commands.

| Command | Inputs |
| --- | --- |
| `/ai` | `prompt` |
| `/ai_persona` | `set` |
| `/ai_reset` | None |
| `/analytics commands` | None |
| `/analytics members` | None |
| `/analytics messages` | None |
| `/automod exception` | `rule`, `role`, `channel`, `remove` |
| `/automod recent-timeouts` | `member` |
| `/automod_exception` | `action`, `target`, `id_value`, `role`, `channel` |
| `/automod_reset` | `user` |
| `/automod_status` | `enabled` |
| `/autorole_setup` | `human_role`, `bot_role` |
| `/avatar` | `user` |
| `/balance` | None |
| `/ban` | `user`, `reason`, `delete_days` |
| `/banner` | `user` |
| `/bot_profile` | None |
| `/botinfo` | None |
| `/builder_bypass_add` | `member` |
| `/builder_bypass_list` | None |
| `/builder_bypass_remove` | `member` |
| `/builder_panel` | None |
| `/butler antiping cooldown` | `minutes` |
| `/butler antiping mode` | `mode` |
| `/butler exception add` | `channel` |
| `/butler exception clear` | None |
| `/butler exception list` | None |
| `/butler exception remove` | `channel`, `channel_id` |
| `/butler report` | `date` |
| `/butler status` | None |
| `/buy` | `item` |
| `/cancel_delete` | None |
| `/channel_summaries` | None |
| `/clear` | `amount`, `user` |
| `/clearwarnings` | `user` |
| `/commands_channel_setup` | `channel` |
| `/create_channel` | `name`, `kind`, `category`, `private` |
| `/daily` | None |
| `/damu` | None |
| `/damu_doctor` | None |
| `/damu_status` | None |
| `/dashboard_status` | None |
| `/diagnostics` | None |
| `/example_template` | None |
| `/generate_server` | `theme`, `preview` |
| `/help` | None |
| `/inventory` | None |
| `/invite` | None |
| `/json_example` | None |
| `/json_validate` | `json_file`, `json_text` |
| `/kick` | `user`, `reason` |
| `/leaderboard` | `type` |
| `/lock` | `channel` |
| `/mass_role` | `action`, `target`, `role` |
| `/mute` | `user`, `duration`, `reason` |
| `/pay` | `user`, `amount` |
| `/perm_sync_check` | `template`, `json_text`, `json_file` |
| `/ping` | None |
| `/poll` | `question`, `options` |
| `/quarantine apply` | `member`, `reason` |
| `/quarantine audit` | None |
| `/quarantine cleanup` | None |
| `/quarantine dashboard` | None |
| `/quarantine migrate` | None |
| `/quarantine permissions` | None |
| `/quarantine repair` | None |
| `/quarantine resources` | None |
| `/quarantine status` | None |
| `/quarantine sync` | None |
| `/quarantine_release` | `member` |
| `/quarantine_setup` | `staff_role` |
| `/rank` | `user` |
| `/remind` | `time`, `message` |
| `/reminder_cancel` | `reference` |
| `/reminders` | None |
| `/roleinfo` | `role` |
| `/security` | None |
| `/send_rules` | `channel`, `title`, `rules`, `image`, `footer` |
| `/server_analyze` | None |
| `/server_json` | `template` |
| `/server_manager` | None |
| `/server_templates` | None |
| `/serverinfo` | None |
| `/setup_custom` | `json_text`, `json_file`, `clean_existing` |
| `/setup_paste_json` | `json_text` |
| `/setup_server` | `template`, `clean_existing` |
| `/setup_tickets` | `title`, `description`, `support_role`, `mod_role`, `log_channel`, `ticket_category`, `ticket_image` |
| `/setupverification` | `verification_channel`, `verified_role`, `unverified_role`, `auto_create`, `embed_title`, `embed_description`, `button_text`, `verification_log_channel`, `account_age_check`, `min_account_age_days`, `lock_public_channels` |
| `/setxp` | `user`, `xp` |
| `/shop` | None |
| `/slowmode` | `channel`, `seconds` |
| `/soft_banned_diddy` | `log_channel`, `mod_role`, `channel` |
| `/soft_banned_diddy_disable` | None |
| `/soft_banned_diddy_status` | None |
| `/sudo_ban` | `member`, `reason` |
| `/sync_commands` | None |
| `/template_details` | `template` |
| `/ticket_blacklist add` | `user` |
| `/ticket_blacklist list` | None |
| `/ticket_blacklist remove` | `user` |
| `/unban` | `user_id` |
| `/unlock` | `channel` |
| `/unmute` | `user` |
| `/uptime` | None |
| `/userinfo` | `user` |
| `/verification_status` | None |
| `/warn` | `user`, `reason` |
| `/warnings` | `user` |
| `/welcome_setup` | `channel`, `title`, `description`, `footer`, `thumbnail_url`, `image`, `use_giphy`, `giphy_query` |
| `/work` | None |

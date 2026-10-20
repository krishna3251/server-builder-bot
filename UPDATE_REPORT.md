# Discord UI and reliability update — v2.1.0

Historical report: the newer [v2.2 command audit](COMMAND_AUDIT.md) supersedes ticket-storage, reminder, shop-boost, and command-flow limitations described here.

Reviewed base: `8e4fdc321958e1aba8b42820407def12aa3f6b5d` on `main`.

This report covers the problems identified during this review and the implemented fixes. It is not a claim that every possible defect has been found or that the changes have been deployed.

## Discord interface changes

- Builder dashboard shows the current server, resource counts, basic readiness, and three starting points: presets, custom JSON, and AI generation.
- The flow is organized as Choose → Review → Build. Preset selection has back navigation.
- Preview cards use one consistent layout. A section menu opens channel/category and role/permission pages, with Previous/Next controls and a complete JSON export.
- Preview cards highlight Administrator roles, member role assignment, and destructive cleanup.
- Confirmations recheck Administrator permission, validate the plan, check bot permissions/capacity, and reject repeated submissions.
- Controls stop or disable on completion/expiration. Existing help categories, shop, moderation confirmations, and pagination inherit the shared UI error handler.
- `/diagnostics` privately shows configuration, loaded/failed modules, core server permissions, and recent error references to administrators.
- Unexpected slash-command, view, and modal errors receive private reference IDs; full details are written to operator logs. Known permission and cooldown failures have direct messages.

## Error and fix register

| Problem | Change | Validation |
| --- | --- | --- |
| A plan could mass-assign an Administrator/moderation role | Reject privileged automatic member roles, including privileged channel grants; reject privileged grants to everyone | Schema regression tests; all presets remain valid |
| Cleanup ran before checking whether another build was running | One per-guild reservation encloses preflight, cleanup, and creation | Busy and malformed builds cannot call deletion mocks |
| A global lock blocked unrelated servers | Independent per-guild reservations | Concurrent reservations for different guilds pass |
| Nested schema fields were incompletely validated | Validate overwrite targets, allowed permissions, colors, booleans, voice values, topics, threads, duplicates, and bounds before mutations | Malformed-schema cases rejected |
| Channel overrides could discard category privacy | Merge category privacy rules into explicit channel overrides, including overrides for the same role | Private category visibility remains denied in the creation call |
| Permission removal or duplicate clicks could bypass confirmation intent | Check ownership/current Administrator permission; one-shot confirmation state | Unauthorized, removed-permission, and duplicate-confirm checks |
| Large role previews could exceed embed field limits | Bound summary fields and paginate details | Maximum-size previews stay within embed limits |
| Uploaded JSON could expire its interaction or fail UTF-8 decoding | Defer before downloading; bound upload size; handle UTF-8/BOM and encoding errors | Import/syntax checks; live attachment testing remains outstanding |
| Thread creation did not specify the intended public type | Request public threads explicitly; check the corresponding bot permission | Source/import validation; live API verification outstanding |
| Welcome messages could be posted to older channels with matching names | Restrict summaries to IDs created by the current build | Source review; live normalization behavior outstanding |
| Daily/work rewards could be claimed more than once concurrently | Commit eligibility, balance, and claim timestamp in one serialized transaction | 20 concurrent daily requests award exactly once; work/daily cooldowns are independent |
| Concurrent JSON writes could truncate files; failed wallet saves could leave changed memory | Atomic replacement for wallets/shop, AI memory, and XP; restore wallet state on save failure; refuse to overwrite unreadable wallet files | Wallet rollback, corrupt file, persisted balance, and transfer tests |
| Ticket clicks could create duplicate channels or overwrite guild state | Defer immediately and serialize ticket transitions per guild | Actual creation handler receives duplicate requests and creates one channel |
| Failure after creating a ticket could leave an untracked channel | Delete incomplete channels if control posting/state commit fails | Actual creation handler with simulated DB failure removes its channel |
| Ticket deletion timers disappeared after restart | Persist deletion deadlines; restore jobs after readiness; preserve deadlines on shutdown; remove them on explicit cancellation | Restoration and task-cancellation tests |
| A ticket's claim button could mutate the shared persistent view | Edit each ticket using a fresh control view | Independent views remain unclaimed; persistent custom IDs preserved |
| Close-modal submission could use stale authorization/state | Recheck opener/staff permissions and open-ticket state under the guild lock | Source review; live close flow outstanding |
| Tickets could close/delete despite failing to save their transcript | Keep the ticket open if its log channel or transcript upload is unavailable | Source/import validation; live failure testing outstanding |
| Shared moderation checks allowed Manage Messages users into ban commands | Require action-specific permissions; enforce owner/self/hierarchy protections; validate ban deletion days and timeout duration | Source/import checks; live role matrix testing outstanding |
| AI server generation reused personal chat/persona history | Use stateless generation requests; serialize per-user chat changes and memory saves | Mocked provider request excludes existing personal messages and does not save generation into history |
| Missing MongoDB configuration produced an obscure ticket load error | Explicit configuration failure; capture failed module references; document MONGO_URI | All eight cogs register offline with a placeholder URI; no database request made |
| Health endpoint could report success while the bot was unavailable | Return 503 until Discord is ready or when a cog failed; missing token exits with error | Source/import validation; hosting verification outstanding |
| CI checked syntax only | Run the offline regression suite on the existing Python 3.11/3.12 matrix | Workflow updated; remote CI result is separate from local validation |

## Verified locally

- Python 3.12, discord.py 2.7.1, dependencies installed from the existing requirements.
- `python -m unittest discover -s tests -v`: 26 tests passed.
- `python -m compileall -q .`: passed.
- Python 3.10 grammar parsing: passed for all Python files.
- `python -m pip check`: no broken requirements.
- `git diff --check`: passed.
- All eight cogs registered offline, with 48 top-level application commands. MongoDB was represented by a placeholder local URI and was not contacted.

The rollback and corrupt-file tests deliberately produce error logs. Those logs are expected test fixtures.

## Remaining limits and live checks

1. **No live deployment or Discord/MongoDB/OpenRouter integration run was performed.** Discord appearance, actual role hierarchy/overwrites, attachment handling, provider responses, MongoDB outages, and restart recovery still need a test-server run.
2. Run **one bot process**. Economy/ticket transition locks are process-local, not distributed. Do not run two deployments against the same state.
3. Economy, XP, AI memory, warnings, and analytics still use local files. MongoDB persists tickets only. Configure persistent storage/backups before relying on state across redeployments. Warnings/analytics retain their existing write paths.
4. Destructive cleanup cannot restore deleted messages, channels, or role assignments. A later API failure can leave partial cleanup; the operation stops and reports the error. Creation rollback is best-effort and only covers newly created resources.
5. Deletion deadlines are recovered at startup. There is no recurring retry queue for a failed deletion or a database outage during recovery; check logs/permissions and restart after resolving the cause.
6. Existing `/remind` timers and ticket cooldowns remain in memory. They do not survive restart. Ticket transcripts retain the existing 200-message text-only limit.
7. Existing shop purchases record inventory entries; the advertised XP boost/cosmetic items do not automatically implement their described effects.
8. Recent error references in `/diagnostics` are bounded to this process. Historical tracebacks remain in rotating/hosting logs. Intentional catches elsewhere may still produce only their existing log messages; this is not a complete telemetry system.
9. AI model availability and account limits were not checked. Generation validates its result, but human review of permissions and structure remains necessary.

## Suggested test-server rollout

1. Set DISCORD_TOKEN, OPENROUTER_API_KEY, and MONGO_URI; enable Member and Message Content intents; verify persistent data storage.
2. Start one instance and inspect `/diagnostics` and hosting logs.
3. Open `/builder_panel`, browse a preset's sections/pages, export it, cancel, and reopen it. Confirm an additive build in a disposable server.
4. Test a restricted category, role assignment, and rejected privileged auto-assignment; test permission removal after opening a preview.
5. Publish a ticket panel, double-click ticket creation, claim a ticket, and close it. Verify the transcript. Restart during a deletion countdown and verify recovery.
6. Submit concurrent daily/work claims and verify one reward; test moderation with separate Manage Messages, Ban Members, and Moderate Members roles.
7. Exercise cleanup only in a disposable server after reviewing the deletion warning.

# DAMU features in Server Builder

The deployment repository is [krishna3251/server-builder-bot](https://github.com/krishna3251/server-builder-bot). The DAMU comparison used `krishnverma32/damu-server-builder` at `93ecff2`.

| Feature in DAMU | Where to use it in Server Builder |
| --- | --- |
| Server control panel, channel/category/role tools, read-only analysis and approved repairs | `/server_manager`, `/damu`, `/server_analyze` |
| Runtime and permissions diagnostics | `/damu_status`, `/damu_doctor`, HTTP `/health`, `/ready`, and `/status` |
| Welcome cards, GIPHY support, separate human/bot auto roles and rules | `/dashboard_status`, `/welcome_setup`, `/autorole_setup`, `/commands_channel_setup`, `/mass_role`, `/send_rules` |
| Verification buttons and minimum account age | `/setupverification`, `/verification_status` |
| Bot-run link and image spam moderation, including role and channel exceptions | `/automod_status`, `/automod_exception`, `/automod_reset` |
| Template browsing, pasted JSON and permission comparisons | `/server_templates`, `/template_details`, `/example_template`, `/setup_paste_json`, `/perm_sync_check` |
| Controlled builder delegation | Server owners use `/builder_bypass_add`, `/builder_bypass_remove`, and `/builder_bypass_list` for simple, additive category and channel plans. The member needs Manage Channels. |
| Prompt-based plans with a template fallback and the Rukiya chat persona | **Plan from prompt** in the Server Manager builder, and `/ai_persona` |
| Startup network recovery and safer acknowledgements | Connection retry and error classification in the bot entry point |

Server Builder retains its economy, ticket handling, reminders, JSON tools, quarantine, Butler reports, and native Discord AutoMod commands. For GIF exceptions, `/automod_exception` controls the bot's own filter. `/automod exception` controls a selected native Discord rule. Use `/automod recent-timeouts` to see which actor or rule applied a recent timeout.

The bot-run filter starts disabled by default. Review imported exceptions before enabling it with `/automod_status enabled:True`. Verification can issue roles as soon as its panel is configured. Public-channel lockdown requires the explicit `lock_public_channels` option. Private channels are excluded from the lockdown.

## Bring over saved settings

1. Back up the old DAMU checkout's `data/` directory somewhere separate. Keep the existing Wispbyte startup command so you can restore it if needed.
2. Put the current Server Builder checkout in its own directory. The two repositories have different histories. Use the same bot token only for the single running bot instance.
3. With the destination bot stopped, run the dry run below from the Server Builder directory. Use a backup directory as the source and the new checkout's `data` as the target.

```bash
python scripts/import_damu_settings.py --source /path/to/damu-backup/data --target data
```

4. Review the listed files and add `--apply` to the same command when the dry run is clear. The importer recognizes AutoMod offenses and exceptions, dashboard and verification settings, welcome assets referenced by the dashboard, and the last build plans. It leaves existing destination files in place and rejects conflicting input before writing.
5. Use `/dashboard_status`, `/verification_status`, and `/automod_exception` to review imported IDs before enabling automation. Make any guild-scoped builder delegations separately. Runtime files such as Butler records, XP, ticket state, warnings, and economy data stay in their existing backups. Ticket state held in MongoDB can continue using the configured database.

`DAMU-AutoMod-and-Butler.patch` is a code patch prepared for DAMU's original checkout. Keep the patch outside `data/`. If it was copied over a JSON file, stop the bot and restore that specific JSON file from a known backup before starting it again. A patch contains no server settings or user history.

## Verify the hosting source

Use a fresh checkout of `krishna3251/server-builder-bot` in Wispbyte before changing the startup command. Keep credentials in the panel's environment variables or the checkout's private `.env`.

The production log should show `source=krishna3251/server-builder-bot`, the expected Git short SHA, and successful slash-command sync. Test a GIF with only an exempt role, then with only an exempt channel or its thread. If a timeout still happens, inspect `/automod recent-timeouts` to locate the actor.

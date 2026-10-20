# Server JSON reference

Start with /json_example. Select an example, download it, edit it, then run
/json_validate before /setup_custom. The preview never changes a server.

Required structure: a JSON object with roles[] and/or categories[]. Each entry
needs a name. Categories contain channels[]. Channel type is text or voice.

Role: {"name":"Member","color":"blue","permissions":["read_messages","send_messages"]}
Text: {"name":"general","type":"text","topic":"Chat","slowmode":3,"nsfw":false}
Voice: {"name":"Lounge","type":"voice","bitrate":64000,"user_limit":5}
Overwrite: {"role":"Member","allow":["read_messages"],"deny":[]}

Use permission_overwrites on categories/channels. An overwrite role must be
listed in roles, or be @everyone. Unspecified channel permissions inherit the
category plan. Use read_messages (alias view_channel) for visibility.

auto_assign names a non-privileged role to give existing human members; it is
not a join-role configuration. Never auto-assign moderation/Administrator or
18+ access roles. Set nsfw:true on every adult-only text channel and gate it
with a separate manually assigned adult role. A role label does not verify age.

Limits per build: 40 roles, 30 categories, 100 channels, 100 threads; at most
50 channels in one category. Names: 1–100 characters; server_name: 2–100.
slowmode: 0–21600; voice bitrate: 8000–384000 (clamped to server limit);
user_limit: 0–99; topic: at most 1024 characters. Thread auto_archive:
60/1440/4320/10080 minutes, supported by the target server.

JSON uses double quotes, lowercase true/false, no comments, and no trailing
commas. Upload UTF-8 JSON up to 256 KB. Only administrators can build, and bot
permissions/capacity are checked before changes. Keep clean_existing:false
unless you intend to permanently delete existing content. AI produces a draft;
validation does not guarantee a good design or replace permission review.

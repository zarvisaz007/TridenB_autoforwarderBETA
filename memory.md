# TridenB Autoforwarder — Memory

## What This Project Does
Local CLI tool that forwards Telegram messages from source channels to destination channels using a personal user account (MTProto via Telethon). No bot token.

## Credentials
- Stored in `.env` (never committed)
- API_ID=29363636, API_HASH=dd4f18f6956a38dc18087c7495181258
- Phone: +918544130087

## Session
- Telethon saves session to `tridenb_autoforwarder.session` after first auth
- Subsequent runs skip OTP

## Task Storage
- `tasks.json` — runtime file, excluded from git
- Schema: list of task objects with source/dest channel IDs, enabled flag, filters

## Filter Behavior
- `blacklist_words`: drop entire message if any word matches (case-insensitive)
- `clean_words`: remove specific strings from text
- `clean_urls`: strip `https?://\S+` patterns
- `clean_usernames`: strip `@word` patterns
- `skip_images/audio/videos`: drop media messages entirely
- No text mod → `forward_messages()` (preserves media + formatting)
- Text modified → `send_message()` (text only)

## Current Status (2026-03-16)
Core forwarder fully implemented. Background forwarder, logs, pause, loop protection all done.
Remaining: live test options 5, 6, and end-to-end forwarding with "Test" task.
See `tasks/progress.md` for full checklist.

## Menu Options
1. Get Channel ID — lists all channels/groups
2. Create Task — source + multiple destinations + filters
3. List Tasks — shows enabled/paused status
4. Toggle Task — enable/disable (persisted to tasks.json)
5. Edit Task Filters
6. Delete Task
7. Start Forwarder — background, non-blocking, returns to menu
8. Stop Forwarder — removes event handlers cleanly
9. Pause/Resume Task — session-only pause, resets loop counter on resume
10. View Logs — last 50 timestamped entries
0. Exit — prompts to stop forwarder if running

## Key Files
- `main.py` — all logic, single file
- `tasks.json` — auto-created, runtime persistence
- `.env` — credentials
- `tasks/progress.md` — feature verification checklist

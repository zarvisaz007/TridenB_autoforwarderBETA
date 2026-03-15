# TridenB Autoforwarder — Memory

## What This Project Does
A local CLI tool that forwards Telegram messages from source channels to destination channels using a personal user account (MTProto via Telethon). No bot token — authenticates with phone + OTP.

## Credentials
- Stored in `.env` (never committed)
- API_ID and API_HASH from my.telegram.org
- Phone: +918544130087

## Session
- Telethon saves session to `tridenb_autoforwarder.session` after first auth
- Subsequent runs skip OTP — session persists until revoked

## Task Storage
- `tasks.json` — runtime file, excluded from git
- Schema: list of task objects with source/dest channel IDs, enabled flag, filters

## Filter Behavior
- `blacklist_words`: drop entire message if any word matches (case-insensitive)
- `clean_words`: remove specific strings from text
- `clean_urls`: strip `https?://\S+` patterns
- `clean_usernames`: strip `@word` patterns
- `skip_images/audio/videos`: drop media messages entirely
- If text is cleaned → sends modified text only (no media forwarded)
- If no cleaning → uses `forward_messages` (preserves media + formatting)

## Key Files
- `main.py` — all logic, single file
- `tasks.json` — auto-created, runtime persistence
- `.env` — credentials

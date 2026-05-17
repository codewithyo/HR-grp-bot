# HR-grp-bot — Advanced Telegram Moderation Bot

An actively maintained Telegram moderation bot that combines robust moderation features, multi-group management, persistent storage, and an improved interactive help UI.

## Overview

This repository contains a moderation bot built with FastAPI and Pyrogram. It supports multi-group operation, MongoDB-backed persistence (with JSON fallback), an inline help system, games, and comprehensive moderator tools.

## Major Improvements & New Features

- MongoDB persistence for warns, notes, filters, blocklists, cases, and other state (with local JSON + fallback backups).
- Per-group isolated storage: each chat uses keys scoped by `str(chat_id)` to avoid cross-group contamination.
- Auto-bootstrap: group storage entries are created automatically on first connection.
- Blocklist system with per-group keywords and configurable actions (`warn`, `mute`, `ban`).
- Inline warn/unwarn buttons and enhanced `/warns` showing totals across chats when requested.
- Games (Tic-Tac-Toe) with leaderboard and player stats.
- A redesigned inline **Help** UI with complete category buttons and full-page command help.
- Many commands now accept either a reply to the target message or a direct argument (user id or `@username`).
- Startup verification that logs what stores were restored from persistent storage.

## Command Behavior: Reply vs Direct

Most moderation commands accept either:

- Reply usage (preferred when available): Reply to the target user's message and send the command (e.g. reply → `/hprotect`).
- Direct usage: pass a user ID or username: `/hprotect 123456789` or `/hban @username 2h spam`.

Examples:

- Reply: (reply to a message) `/hprotect` — protect the replied user
- Direct: `/hprotect 123456789` — protect the specified user id
- Reply: (reply to a message) `/pin` — pins the replied message
- Direct: `/hban @user 1d spam` — ban by username with duration and reason

When in doubt, reply to target messages to avoid parsing errors or mistaken IDs.

## Full Command Summary (representative)

Note: Some commands are Owner-only or Moderator-only. Use `/help` for interactive, role-aware help pages.

- `/start` — Bot welcome message
- `/help` — Interactive help menu (Ban, Mute, Warn, Kick, Protect, Notes, Filters, Blocklist, Connections, Authorization, Stats, Games)
- `/hr` / `/id` — Show user/chat id and profile info

- Moderation
	- `/hban [user|@user|reply] [duration] [reason]` — Ban user
	- `/hunban [user|@user|reply]` — Unban user
	- `/hkick [user|@user|reply] [reason]` — Kick user
	- `/hmute [user|@user|reply] [duration] [reason]` — Mute user
	- `/hunmute [user|@user|reply]` — Unmute user
	- `/hwarn [user|@user|reply] [reason]` — Warn user (creates a case)
	- `/warns [user|@user]` — Show warnings (per-group or cross-chat totals)
	- `/resetwarns [user|@user|reply]` — Reset warns for a user
	- `/pin` (reply) — Pin a message; `/unpin` — Unpin last pinned

- Protection & Authorization
	- `/hprotect [user|id|reply]` — Protect a user from moderation (Owner)
	- `/hunprotect [user|id|reply]` — Remove protection (Owner)
	- `/hauth <user_id>` — Authorize moderator (Owner)
	- `/hunauth <user_id>` — Remove moderator (Owner)
	- `/hgrant <user_id> <perm>` — Grant permission
	- `/hrevoke <user_id> <perm>` — Revoke permission
	- `/hfreeze <user_id>` / `/hunfreeze <user_id>` — Freeze/unfreeze moderator
	- `/hbadge <user_id> <badge_text>` — Set moderator badge

- Notes & Filters
	- `/notes` — List saved notes
	- `/save <name> <text>` or reply+`/save <name>` — Save note
	- `/get <name>` or `#name` — Retrieve note
	- `/clear <name>` — Delete note
	- `/filters` — List filters
	- `/filter <keyword> <response>` — Add filter (supports `-exact`, `-start`, `-regex`)
	- `/stop <keyword>` — Remove filter

- Blocklist
	- `/addblocklist <keyword>` — Add blocked keyword
	- `/deleteblocklist <keyword>` — Remove blocked keyword
	- `/blocklists` — List group blocklist keywords
	- `/blocklistmode [warn|mute|ban]` — Set action for blocked keywords

- Connections & Multi-Group
	- `/connect <chat_id>` — Connect PM to group for management
	- `/connections` — View and switch connected groups
	- `/disconnect [chat_id|all]` — Disconnect
	- `/allowconnections yes|no` — Toggle PM connection allowance

- Warn Configuration
	- `/hwarnconfig threshold <n>` — Set warn threshold
	- `/hwarnconfig action <ban|mute|kick>` — Set auto-action
	- `/hwarnconfig duration <30m|2h|1d>` — Action duration

- Games
	- `/ttt [user_id]` — Start Tic-Tac-Toe with a user
	- `/ttt [user_id] 5` — Start 5x5 board
	- `/tttleaderboard` — Show top players
	- `/tttmystats` — Show your stats
	- `/tttend` — Forfeit active game

If a command is missing here, open `/help` in the bot for the full interactive view.

## Help UI

- The help menu is interactive and updates the same message.
- Categories: Ban, Mute, Warn, Kick, Protect, Notes, Filters, Blocklist, Connections, Authorization, Stats, Games.
- Use the inline buttons to navigate to detailed pages which include examples and tips.

## Storage & Persistence (detailed)

The bot implements robust persistence:

1. Local JSON files (primary for single-instance setups)
2. Fallback `_fallback.json` copies for quick recovery if local is corrupted
3. MongoDB when `MONGO_URL` is configured — acts as authoritative backup

On save: cache → atomic write to local JSON → write fallback → save to Mongo (if available).
On load: cache → local JSON → Mongo → fallback. Cache TTL reduces frequent disk reads.

Startup process:

- `mongo_db.connect()` (if `MONGO_URL` provided)
- `sync_storage_with_mongo()` synchronizes Mongo→local files
- `verify_storage_restored()` logs counts for restored stores (warns, notes, filters, blocklists, cases, protections)

## Multi-Group Model

- All group-scoped data is stored under keys named by `str(chat_id)`.
- `create_group_defaults(chat_id)` bootstraps defaults automatically on connection.

## Deployment & Run

1. Install deps:

```bash
pip install -r requirements.txt
```

2. Required env vars (example):

```bash
export API_ID=YOUR_API_ID
export API_HASH=YOUR_API_HASH
export BOT_TOKEN=YOUR_BOT_TOKEN
export OWNER_ID=YOUR_OWNER_ID
export PORT=8000
# Optional: MongoDB
export MONGO_URL=mongodb://user:pass@host:port/db
```

3. Run locally:

```bash
python start.py
```

Optional: deploy via Docker/Koyeb using the provided `Dockerfile`.

## Troubleshooting & Testing

- Use `/help` for live command descriptions and examples.
- Check logs for `verify_storage_restored()` at startup to confirm persistence.
- If data is missing, check `STORAGE_PATH` and Mongo connectivity.

## Contributing

- Open issues or pull requests. Keep changes small and testable.

## License

Open source — adapt and extend as needed.

---

© HR-grp-bot contributors

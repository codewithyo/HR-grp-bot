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

## Commands & Usage

Most commands work either by replying to a target message (recommended) or by passing a user id / `@username` directly.

| Emoji | Command | Description | Permission |
|---:|---|---|---|
| 🟢 | `/start` | Bot welcome message | Anyone |
| ❓ | `/help` | Interactive help menu (category buttons) | Anyone |
| 🆔 | `/hr`, `/id` | Show user/chat id and profile info | Anyone |
| 🚫 | `/hban [user|@user|reply] [duration] [reason]` | Ban a user (temporary or permanent) | Moderator |
| ✅ | `/hunban [user|@user|reply]` | Unban a user | Moderator |
| 👢 | `/hkick [user|@user|reply] [reason]` | Kick a user from the group | Moderator |
| 🔇 | `/hmute [user|@user|reply] [duration] [reason]` | Mute a user | Moderator |
| 🔊 | `/hunmute [user|@user|reply]` | Unmute a user | Moderator |
| ⚠️ | `/hwarn [user|@user|reply] [reason]` | Issue a warning (creates a case) | Moderator |
| 📈 | `/warns [user|@user]` | Show warnings (per-group or cross-chat totals) | Anyone |
| ♻️ | `/resetwarns [user|@user|reply]` | Reset warnings for a user | Moderator |
| 📌 | `/pin` (reply) / `/unpin` | Pin/unpin a message | Moderator |
| 🛡️ | `/hprotect [user|id|reply]` | Protect a user from moderation | Owner |
| 🔓 | `/hunprotect [user|id|reply]` | Remove protection | Owner |
| 👥 | `/hauth <user_id>` | Authorize a moderator | Owner |
| 🚫👥 | `/hunauth <user_id>` | Remove moderator authorization | Owner |
| 🔧 | `/hgrant <user_id> <perm>` | Grant a permission to a moderator | Owner |
| 🛠️ | `/hrevoke <user_id> <perm>` | Revoke a permission | Owner |
| ❄️ | `/hfreeze <user_id>` / `/hunfreeze <user_id>` | Freeze/unfreeze moderator actions | Owner |
| 🏷️ | `/hbadge <user_id> <badge_text>` | Set moderator badge | Owner |
| 📝 | `/notes` | List saved notes for the group | Authorized |
| 💾 | `/save <name> <text>` or reply+`/save <name>` | Save a note | Authorized |
| 📖 | `/get <name>` or `#name` | Retrieve a saved note | Anyone (in group) |
| 🗑️ | `/clear <name>` | Delete a note | Authorized |
| 🔍 | `/filters` | List all filters | Anyone |
| ➕ | `/filter <keyword> <response>` | Add an auto-reply filter (`-exact`, `-start`, `-regex`) | Authorized |
| ⛔ | `/stop <keyword>` | Remove a filter | Authorized |
| 🔒 | `/addblocklist <keyword>` | Add a blocked keyword (group-scoped) | Authorized |
| ❌ | `/deleteblocklist <keyword>` | Remove a blocklist keyword | Authorized |
| 📋 | `/blocklists` | List blocklist keywords for this group | Authorized |
| ⚙️ | `/blocklistmode [warn|mute|ban]` | Set action for blocklist matches | Authorized |
| 🔗 | `/connect <chat_id>` | Connect PM to manage a group | Anyone (DM) |
| 🔁 | `/connections` | View/switch connected groups | Anyone (DM) |
| 🔌 | `/disconnect [chat_id|all]` | Disconnect a connected group | Anyone (DM) |
| 🔐 | `/allowconnections yes|no` | Allow/block PM connections | Moderator |
| ⚙️ | `/hwarnconfig threshold <n>` | Set warn threshold | Owner |
| ⚙️ | `/hwarnconfig action <ban|mute|kick>` | Set auto-action on threshold | Owner |
| ⏱️ | `/hwarnconfig duration <30m|2h|1d>` | Set duration for auto-action | Owner |
| 🎮 | `/ttt [user_id] [size]` | Start Tic-Tac-Toe (optional size, default 3) | Anyone |
| 🏆 | `/tttleaderboard` | Show top Tic-Tac-Toe players | Anyone |
| 📊 | `/tttmystats` | Show your game stats | Anyone |
| 🛑 | `/tttend` | Forfeit active game | Anyone |

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

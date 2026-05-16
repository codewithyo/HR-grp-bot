# Advanced Telegram Moderation Bot — Latest

A production-ready Telegram moderation bot with inline help navigation, moderation tools, notes, filters, connection management, and persistent storage.

## ✨ Features

✅ **Complete Moderation System**
- Ban users from group
- Kick users from group
- Mute users (restrict permissions)
- Warn system with history
- Delete messages
- User protection (prevent bans)
- Temporary bans and mutes

✅ **Advanced Administration**
- Authorize moderators
- Grant/revoke permissions (ban, unban, mute, unmute, kick, warn, delete, pin)
- Freeze/unfreeze moderators
- Custom moderator badges
- Anti-nuke protection (freeze moderator after 10 actions/min)
- Case management & history
- Comprehensive logging

✅ **User Utilities**
- Notes system with save/get/clear/list
- Filters with exact, regex, and prefix matching
- PM connection management for multi-group moderation
- Appeals from bot DM

✅ **Inline Help Menu**
- Category buttons for Ban, Mute, Kick, Protect, Notes, Filters, Connections, Stats, and Authorization
- Back button navigation
- Help pages update the same message instead of sending new ones

✅ **24/7 Uptime**
- Deployed on Koyeb (no cold starts)
- Always-on HTTP server for webhooks
- Automatic reconnection
- Persistent JSON storage

✅ **Developer Friendly**
- Health check endpoints
- Bot status API
- Detailed logging with timestamps
- Error handling & recovery
- Configuration validation

## 🚀 Quick Deploy

```bash
# 1. Create Koyeb account
https://www.koyeb.com

# 2. Set environment variables
API_ID=9605646
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
OWNER_ID=your_user_id
LOG_GROUP_ID=your_log_group_id
PORT=8000

# 3. Deploy from GitHub
Repository: codewithyo/HR-grp-bot
Branch: main
Run command: python start.py

# Optional: Docker deploy on Koyeb
Dockerfile: ./Dockerfile

# 4. Setup webhook
https://<your-app>.koyeb.app/api/setup_webhook

# 5. Test
/start in your Telegram group
```

## 📋 Bot Commands

| Command | Description | Permission |
|---------|-------------|-----------|
| `/start` | Start bot and view welcome message | Anyone |
| `/help` | Show inline help categories | Anyone |
| `/hr` or `/id` | Get user ID and profile info | Anyone |
| `/hauth` | Authorize moderator | Owner |
| `/hgrant <perm>` | Grant permission | Owner |
| `/hrevoke <perm>` | Revoke permission | Owner |
| `/hfreeze` | Freeze a moderator | Owner |
| `/hunfreeze` | Unfreeze a moderator | Owner |
| `/hbadge` | Set moderator badge | Owner |
| `/hwarnconfig` | Configure warn threshold/action/duration | Owner |
| `/hban` | Ban user | Moderator |
| `/hkick` | Kick user | Moderator |
| `/hmute` | Mute user | Moderator |
| `/hunban` | Unban user | Moderator |
| `/hunmute` | Unmute user | Moderator |
| `/hwarn` | Warn user | Moderator |
| `/hdel` | Delete replied message | Moderator |
| `/hstats` | Show moderation stats | Authorized |
| `/hmod list` | List authorized moderators | Authorized |
| `/hmodinfo` | Moderator info | Authorized |
| `/hcase <id>` | View case details | Authorized |
| `/hprotect` | Protect user | Owner |
| `/hunprotect` | Remove user protection | Owner |
| `/notes` | List saved notes | Authorized |
| `/save` | Save a note | Authorized |
| `/get` | Retrieve a note | Everyone in group |
| `/clear` | Delete a note | Authorized |
| `/filter` | Add a filter | Authorized |
| `/stop` | Remove a filter | Authorized |
| `/filters` | List filters | Everyone in group |
| `/connect` | Connect PM to a group | Anyone in DM |
| `/connections` | View or switch connected groups | Anyone in DM |
| `/disconnect` | Disconnect a group | Anyone in DM |
| `/allowconnections` | Allow or block PM connections | Moderator |
| `/adminlist` | Show all group admins | Authorized |
| `/zombies` | Scan and kick deleted/bot accounts | Moderator |
| `/happeal` | Appeal moderation case in DM | Anyone |

## 🔗 API Endpoints

```
GET  /health              → Health check
GET  /api/status          → Bot status
GET  /api/setup_webhook   → Register webhook
POST /api/webhook         → Telegram updates
```

## 📊 Architecture

```
Telegram API
     ↓
  Webhook (HTTPS)
     ↓
Koyeb HTTP Server (FastAPI)
     ↓
Pyrogram Client (in_memory)
     ↓
JSON Storage (/tmp/modbot/)
```

## 📖 Documentation

See [KOYEB_DEPLOYMENT.md](KOYEB_DEPLOYMENT.md) for:
- Step-by-step setup guide
- Getting API credentials
- Testing procedures
- Troubleshooting
- Security best practices
- Scaling information

## 🎯 Key Differences from Vercel

| Feature | Vercel | Koyeb |
|---------|--------|-------|
| Execution | Serverless (ephemeral) | Container (persistent) |
| Webhook | Required | Not needed |
| Uptime | As-needed | 24/7 |
| Cold Starts | Yes (3-5s delay) | No |
| Storage | Resets | Persists |

## 💡 Use Cases

✅ Group moderation (ban/mute/warn)
✅ Anti-spam protection
✅ Admin task automation
✅ User management
✅ Audit logging
✅ Permission-based access control
✅ Saved notes and auto-replies
✅ PM-to-group connections for multi-group moderation
✅ Appeal handling in bot DM

## 🔐 Security

- Configuration validation on startup
- Environment variables for secrets
- Permission-based access control
- Anti-nuke automatic moderator freeze
- Comprehensive logging & auditing
- Protected user whitelist for trusted accounts
- Role-based command checks with anonymous admin support

## 🛠 Tech Stack

- **Framework**: FastAPI + Uvicorn
- **Telegram Client**: Pyrogram 2.0
- **Deployment**: Koyeb
- **Storage**: JSON (migrate to DB for production)
- **Python**: 3.8+

## 📞 Support

- **Koyeb Docs**: https://docs.koyeb.com
- **Telegram Bot API**: https://core.telegram.org/bots/api
- **Pyrogram Docs**: https://docs.pyrogram.org

## 📄 License

Open source - feel free to fork and customize!

---

**🚀 Deploy now and enjoy 24/7 group moderation!**

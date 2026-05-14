# Advanced Telegram Moderation Bot — Koyeb Version

A production-ready Telegram moderation bot deployed on **Koyeb** with 24/7 uptime, no cold starts, and persistent storage.

## ✨ Features

✅ **Complete Moderation System**
- Ban users from group
- Mute users (restrict permissions)
- Warn system with history
- Delete messages
- User protection (prevent bans)

✅ **Advanced Administration**
- Authorize moderators
- Grant/revoke permissions (ban, mute, warn, delete, kick)
- Anti-nuke protection (freeze moderator after 10 actions/min)
- Case management & history
- Comprehensive logging

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
| `/start` | Start bot | Anyone |
| `/hauth` | Authorize moderator | Owner |
| `/hgrant <perm>` | Grant permission | Owner |
| `/hrevoke <perm>` | Revoke permission | Owner |
| `/hban` | Ban user (reply) | Moderator |
| `/hkick` | Kick user (reply) | Moderator |
| `/hmute` | Mute user (reply) | Moderator |
| `/pin` | Pin replied message | Moderator |
| `/unpin` | Unpin current message | Moderator |
| `/adminlist` | Show all group admins | Authorized |
| `/zombies` | Scan and kick deleted/bot accounts | Moderator |
| `/hwarn` | Warn user (reply) | Moderator |
| `/hdel` | Delete message (reply) | Moderator |
| `/hprotect` | Protect user (reply) | Owner |
| `/hcase <id>` | View case | Authorized |
| `/hmodinfo` | Moderator info | Authorized |

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

## 🔐 Security

- Configuration validation on startup
- Environment variables for secrets
- Permission-based access control
- Anti-nuke automatic moderator freeze
- Comprehensive logging & auditing

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


[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?name=hr-grp-bot&type=git&repository=yatinbst%2FHR-grp-bot&branch=main&builder=dockerfile&instance_type=free&regions=fra&instances_min=0&autoscaling_sleep_idle_delay=3900&env%5BAPI_HASH%5D=822d45aa548a53682a458efa1933e4c9&env%5BAPI_ID%5D=9605646&env%5BAPP_URL%5D=institutional-rebeka-yatinbst-344ddd90.koyeb.app%2F&env%5BBOT_TOKEN%5D=8707026358%3AAAF-DAP96HYUZe6d4aQ7g_d3lyE97q8KOBo&env%5BLOG_GROUP_ID%5D=-1003834934514&env%5BOWNER_ID%5D=8457503781)
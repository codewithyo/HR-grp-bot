# Advanced Telegram Moderation Bot - Koyeb Deployment Guide

## Overview
This bot has been converted from Vercel (serverless) to Koyeb (persistent containers). The main difference is that Koyeb runs your code continuously, while Vercel uses temporary functions.

## Key Changes

### Architecture
- **Vercel**: Webhook-based (Telegram pushes updates to your URL)
- **Koyeb**: HTTP server-based (bot creates an HTTP API for receiving updates)
- **Pyrogram**: Uses bot token authentication with no session file required

### Features
✅ Ban/Mute/Warn/Kick users
✅ Moderator authorization system
✅ Anti-nuke protection
✅ Case management
✅ User protection
✅ Comprehensive logging
✅ Health check endpoint

---

## Deployment on Koyeb

### Step 1: Create a Koyeb Account
1. Go to https://www.koyeb.com
2. Sign up with GitHub or email
3. Create a new app

### Step 2: Connect Repository
1. In Koyeb dashboard, click "Create App"
2. Select "GitHub" and authorize
3. Select this repository: `codewithyo/HR-grp-bot`
4. Choose main branch

### Step 3: Configure Environment Variables
In Koyeb dashboard, set these environment variables:

```
API_ID=9605646
API_HASH=<your_api_hash>
BOT_TOKEN=<your_bot_token>
OWNER_ID=<your_user_id>
LOG_GROUP_ID=<your_log_group_id>
PORT=8000
STORAGE_PATH=/tmp/modbot
```

#### Get Your Values:

**BOT_TOKEN**: From @BotFather on Telegram
```
/newbot (if you don't have one)
```

**API_ID & API_HASH**: From https://my.telegram.org
1. Login with your phone number
2. Go to "API development tools"
3. Create or select app
4. Copy API_ID and API_HASH

**OWNER_ID**: Your Telegram user ID
- Forward any message to @userinfobot
- It will show your ID

**LOG_GROUP_ID**: Create a private group and get its ID
- Use @userinfobot and forward a message from the group
- It will show the group ID (negative number)

### Step 4: Set Build & Runtime
- **Build Command**: Leave empty (Python auto-detected)
- **Run Command**: 
  ```
  python start.py
  ```
- **Port**: 8000

### Docker Deployment Option (Recommended)
If you deploy as a Docker service on Koyeb, this repository now includes a `Dockerfile`.

- **Dockerfile path**: `Dockerfile`
- **Exposed port**: `8000`
- **Container start command**: `python start.py`

Koyeb injects `$PORT` automatically and the app reads it from environment variables.

### Step 5: Deploy
1. Click "Deploy"
2. Wait for build to complete (~5 minutes)
3. Check logs for "Bot authenticated" message

---

## Testing

### Check Bot Status
Once deployed, visit:
```
https://your-app.koyeb.app/health
```

You should see:
```json
{"status": "healthy", "bot_ready": true}
```

### Check Bot Info
```
https://your-app.koyeb.app/api/status
```

### Test in Telegram
Send `/start` to your bot → It should respond immediately

---

## Commands

| Command | Usage | Permission |
|---------|-------|-----------|
| `/start` | Start bot | Public |
| `/hauth` | Authorize moderator | Owner only |
| `/hgrant <perm>` | Grant permission | Owner only |
| `/hrevoke <perm>` | Revoke permission | Owner only |
| `/hban` | Ban user | Moderator with ban permission |
| `/hmute` | Mute user | Moderator with mute permission |
| `/hwarn` | Warn user | Moderator with warn permission |
| `/hdel` | Delete message | Moderator with delete permission |
| `/hprotect` | Protect user | Owner only |
| `/hcase <id>` | View case | Authorized users |
| `/hmodinfo` | Moderator info | Authorized users |

---

## Workflow

### 1. Authorize a Moderator
```
1. Reply to the moderator's message
2. Send /hauth
3. Bot: "✅ Authorized [Name]"
```

### 2. Grant Permissions
```
1. Reply to authorized moderator
2. Send /hgrant ban
3. Bot: "✅ Granted `ban` to [Name]"
```

### 3. Ban a User
```
1. Reply to the user to ban
2. Send /hban <reason>
3. Bot will ban them and log the action
```

### 4. View Case
```
Send /hcase 1
```

---

## Storage

Currently uses JSON files in `/tmp/modbot`. For production, replace with:

- **MongoDB Atlas** (recommended for Koyeb)
- **PostgreSQL**
- **Supabase**

Update `load()` and `save()` functions to use your database.

---

## Troubleshooting

### Bot not responding?
1. Check Koyeb logs: 
   - Dashboard → Your App → Logs
2. Look for "Bot authenticated" message
3. Check `/health` endpoint returns `{"status": "ok"}`

### "BOT_TOKEN not set"?
- Verify environment variables in Koyeb dashboard
- Make sure value doesn't have quotes or spaces

### "Pyrogram Error"?
- Check API_ID and API_HASH are correct
- Verify BOT_TOKEN format (contains colon)

### Data loss?
- JSON storage is temporary
- Deploy database for persistence
- See Storage section above

---

## Logs

View real-time logs:
```
1. Koyeb Dashboard
2. Your App
3. Logs tab
```

Look for:
- `✅ Bot authenticated` → Success
- `❌ ERROR` → Problems to fix
- `📨 Received update` → Bot receiving updates

---

## Scaling

Koyeb allows:
- **Free tier**: 1 instance (perfect for bots)
- **Paid**: Multiple instances with load balancing

For a Telegram bot, 1 instance is usually enough.

---

## Support

Issues? Check:
1. Logs in Koyeb dashboard
2. Environment variables are set correctly
3. BOT_TOKEN is valid
4. OWNER_ID matches your Telegram ID

---

**Happy moderating! 🤖**

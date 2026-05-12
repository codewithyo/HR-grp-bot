# =========================================================
# ADVANCED TELEGRAM MODERATION BOT — KOYEB VERSION
# =========================================================
#
# Architecture:
#   • Runs as persistent HTTP server on Koyeb (24/7)
#   • Telegram sends webhook updates to /api/webhook
#   • FastAPI receives them and routes to handlers
#   • Pyrogram Client makes all outbound API calls
#   • No cold starts — bot stays connected continuously
#
# IMPORTANT — Storage:
#   JSON files stored in STORAGE_PATH (persists across restarts)
#   For production, migrate to: Vercel KV, Supabase, MongoDB Atlas
#
# ENV VARS required:
#   API_ID, API_HASH, BOT_TOKEN, OWNER_ID, LOG_GROUP_ID, PORT, STORAGE_PATH
# =========================================================

import os
import json
import time
import random
import string
import asyncio
import httpx
import traceback
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pyrogram import Client
from pyrogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton

# =========================================================
# LOGGING SETUP
# =========================================================

def log_msg(msg: str, level: str = "INFO"):
    """Log to stdout for Koyeb console"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}", flush=True)

# =========================================================
# CONFIG — read from environment variables
# =========================================================

API_ID       = int(os.environ.get("API_ID", "0"))
API_HASH     = os.environ.get("API_HASH", "")
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
OWNER_ID     = int(os.environ.get("OWNER_ID", "0"))
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "0"))
PORT         = int(os.environ.get("PORT", "8000"))
STORAGE_PATH = os.environ.get("STORAGE_PATH", "/tmp/modbot")

# =========================================================
# VALIDATE CONFIGURATION
# =========================================================

def validate_config():
    """Validate that all required environment variables are set"""
    errors = []
    
    if not BOT_TOKEN or BOT_TOKEN == "":
        errors.append("BOT_TOKEN not set")
    if not API_HASH or API_HASH == "":
        errors.append("API_HASH not set")
    if API_ID == 0:
        errors.append("API_ID not set or is 0")
    if OWNER_ID == 0:
        errors.append("OWNER_ID not set or is 0")
    if LOG_GROUP_ID == 0:
        errors.append("LOG_GROUP_ID not set or is 0")
    
    if errors:
        log_msg("CONFIGURATION ERRORS:", "ERROR")
        for error in errors:
            log_msg(f"  ❌ {error}", "ERROR")
        log_msg("Please set all required environment variables", "ERROR")
        sys.exit(1)

validate_config()

log_msg(f"CONFIG LOADED: API_ID={API_ID}, OWNER_ID={OWNER_ID}, PORT={PORT}", "INFO")
log_msg(f"BOT_TOKEN={'***' if BOT_TOKEN else 'MISSING'}", "INFO")

# =========================================================
# STORAGE SETUP
# =========================================================

Path(STORAGE_PATH).mkdir(parents=True, exist_ok=True)

AUTH_FILE    = f"{STORAGE_PATH}/auth.json"
WARN_FILE    = f"{STORAGE_PATH}/warns.json"
CASE_FILE    = f"{STORAGE_PATH}/cases.json"
PROTECT_FILE = f"{STORAGE_PATH}/protected.json"
ABUSE_FILE   = f"{STORAGE_PATH}/abuse.json"

for _f in [AUTH_FILE, WARN_FILE, CASE_FILE, PROTECT_FILE, ABUSE_FILE]:
    if not os.path.exists(_f):
        with open(_f, "w") as _fh:
            json.dump({}, _fh)

log_msg(f"Storage initialized at: {STORAGE_PATH}", "INFO")

# =========================================================
# JSON HELPERS
# =========================================================

def load(file: str) -> dict:
    """Load JSON file"""
    try:
        with open(file, "r") as f:
            return json.load(f)
    except Exception as e:
        log_msg(f"ERROR loading {file}: {e}", "ERROR")
        return {}

def save(file: str, data: dict):
    """Save JSON file"""
    try:
        with open(file, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        log_msg(f"ERROR saving {file}: {e}", "ERROR")

# =========================================================
# PERMISSION CHECKS
# =========================================================

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def is_authorized(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    return str(user_id) in load(AUTH_FILE)

def has_permission(user_id: int, permission: str) -> bool:
    if is_owner(user_id):
        return True
    data = load(AUTH_FILE)
    user = data.get(str(user_id))
    if not user:
        return False
    return user.get("permissions", {}).get(permission, False)

# =========================================================
# UTILITY HELPERS
# =========================================================

def generate_mod_id() -> str:
    chars = string.ascii_uppercase + string.digits
    return "MOD-" + "".join(random.choice(chars) for _ in range(5))

def get_mod_info(user_id: int) -> dict:
    return load(AUTH_FILE).get(str(user_id), {})

def is_protected(user_id: int) -> bool:
    return str(user_id) in load(PROTECT_FILE)

def make_mention(user: dict) -> str:
    """Build a Markdown mention from a Bot API user dict"""
    first = user.get("first_name", "")
    last  = user.get("last_name", "")
    name  = (first + " " + last).strip() or "User"
    return f"[{name}](tg://user?id={user['id']})"

def create_case(action: str, moderator: int, target: int, reason: str) -> str:
    cases   = load(CASE_FILE)
    case_id = str(len(cases) + 1)
    cases[case_id] = {
        "action": action,
        "moderator": moderator,
        "target": target,
        "reason": reason,
        "time": str(datetime.now()),
    }
    save(CASE_FILE, cases)
    return case_id

def track_action(user_id: int) -> int:
    data = load(ABUSE_FILE)
    uid  = str(user_id)
    now  = time.time()
    data.setdefault(uid, [])
    data[uid].append(now)
    data[uid] = [x for x in data[uid] if now - x <= 60]
    save(ABUSE_FILE, data)
    return len(data[uid])

# =========================================================
# PYROGRAM CLIENT — module-level singleton
# =========================================================

_bot: Client = None
bot_ready = False

async def get_bot() -> Client:
    """Get or initialize the Pyrogram bot client"""
    global _bot, bot_ready
    try:
        if _bot is None or not _bot.is_connected:
            log_msg("Initializing Pyrogram Client...", "INFO")
            _bot = Client(
                name         = "modbot",
                api_id       = API_ID,
                api_hash     = API_HASH,
                bot_token    = BOT_TOKEN,
                in_memory    = True,
                no_updates   = True,
            )
            await _bot.start()
            bot_ready = True
            me = await _bot.get_me()
            log_msg(f"✅ Bot authenticated as @{me.username}", "INFO")
        return _bot
    except Exception as e:
        log_msg(f"ERROR in get_bot: {e}", "ERROR")
        log_msg(traceback.format_exc(), "ERROR")
        bot_ready = False
        raise

async def shutdown_bot():
    """Gracefully shutdown the bot"""
    global _bot, bot_ready
    if _bot:
        try:
            await _bot.stop()
            _bot = None
            bot_ready = False
            log_msg("Bot disconnected", "INFO")
        except Exception as e:
            log_msg(f"ERROR shutting down bot: {e}", "ERROR")

# =========================================================
# ANTI-NUKE
# =========================================================

async def anti_nuke(bot: Client, chat_id: int, reply_to: int, user_id: int) -> bool:
    """Freeze moderator after 10 actions in 60 seconds"""
    total = track_action(user_id)
    if total < 10:
        return False

    auth = load(AUTH_FILE)
    if str(user_id) in auth:
        auth[str(user_id)]["frozen"] = True
        save(AUTH_FILE, auth)

    try:
        await bot.send_message(
            LOG_GROUP_ID,
            f"🚨 **ANTI-NUKE ACTIVATED**\n\n"
            f"Moderator: `{user_id}`\n"
            f"Actions in 60 sec: `{total}`\n\n"
            f"Moderator Frozen Automatically."
        )
        await bot.send_message(
            chat_id,
            "🚨 Anti-Nuke Triggered.\nModerator Frozen.",
            reply_to_message_id=reply_to
        )
    except Exception as e:
        log_msg(f"ERROR in anti_nuke: {e}", "ERROR")
    return True

# =========================================================
# ACTION LOG
# =========================================================

async def send_action_log(
    bot: Client,
    chat_id: int,
    reply_to: int,
    action: str,
    target: dict,
    reason: str,
    case_id: str,
    moderator_data: dict,
    extra: str = "",
):
    """Send moderation action log to chat and log group"""
    try:
        mention   = make_mention(target)
        target_id = target["id"]
        badge     = moderator_data.get("badge", "🛡 Moderator")
        mod_uid   = moderator_data.get("mod_id", "UNKNOWN")
        time_now  = datetime.now().strftime("%d %b %Y • %I:%M %p")

        text = (
            f"╭━━━〔 🚨 MODERATION ACTION 〕━━━╮\n\n"
            f"👤 User: {mention}\n"
            f"🆔 User ID: `{target_id}`\n\n"
            f"⚔ Action: {action}\n"
            f"📝 Reason: {reason}\n\n"
            f"👮 Moderator:\n"
            f"{badge} | {mod_uid}\n\n"
            f"⏰ Time: {time_now}\n"
            f"📜 Case ID: #{case_id}\n"
            f"{extra}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━╯"
        )

        buttons = []
        if action == "BAN":
            buttons.append([InlineKeyboardButton("🔓 Unban",       callback_data=f"unban_{target_id}")])
        elif action == "MUTE":
            buttons.append([InlineKeyboardButton("🔊 Unmute",      callback_data=f"unmute_{target_id}")])
        elif action == "WARN":
            buttons.append([InlineKeyboardButton("🗑 Remove Warn", callback_data=f"removewarn_{target_id}")])
        elif action == "DELETE":
            buttons.append([InlineKeyboardButton("👤 Profile",     url=f"tg://user?id={target_id}")])

        buttons.append([InlineKeyboardButton("📜 View Case", callback_data=f"case_{case_id}")])
        markup = InlineKeyboardMarkup(buttons)

        await bot.send_message(chat_id, text, reply_markup=markup, reply_to_message_id=reply_to)
        await bot.send_message(LOG_GROUP_ID, text, reply_markup=markup)
    except Exception as e:
        log_msg(f"ERROR in send_action_log: {e}", "ERROR")
        log_msg(traceback.format_exc(), "ERROR")

# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(title="Advanced Moderation Bot - Koyeb")

# =========================================================
# HEALTH CHECK ENDPOINTS
# =========================================================

@app.get("/health")
async def health_check():
    """Health check endpoint for Koyeb"""
    return {
        "status": "healthy",
        "bot_ready": bot_ready,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/status")
async def bot_status():
    """Get bot status and info"""
    try:
        bot = await get_bot()
        me = await bot.get_me()
        return {
            "status": "running",
            "bot_id": me.id,
            "bot_username": me.username,
            "bot_first_name": me.first_name,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

# =========================================================
# WEBHOOK SETUP (one-time call)
# =========================================================

@app.get("/api/setup_webhook")
async def setup_webhook(request: Request):
    """
    One-time setup to register webhook with Telegram
    Call this once: https://your-app.koyeb.app/api/setup_webhook
    """
    log_msg("=== WEBHOOK SETUP CALLED ===", "INFO")
    
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/api/webhook"
    
    log_msg(f"Setting webhook to: {webhook_url}", "INFO")

    result = {
        "webhook_url": webhook_url,
        "set_webhook": {},
        "webhook_info": {},
        "errors": []
    }

    try:
        async with httpx.AsyncClient() as client:
            # Set the webhook
            log_msg("Registering webhook with Telegram...", "INFO")
            set_resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": True,
                },
                timeout=10,
            )
            set_result = set_resp.json()
            result["set_webhook"] = set_result
            log_msg(f"Webhook response: {set_result}", "INFO")

            if not set_result.get("ok"):
                result["errors"].append(f"Webhook error: {set_result.get('description')}")
                log_msg(f"ERROR: {set_result.get('description')}", "ERROR")

            # Set bot commands
            log_msg("Setting bot commands...", "INFO")
            cmd_resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
                json={
                    "commands": [
                        {"command": "start",    "description": "Start Bot"},
                        {"command": "hauth",    "description": "Authorize Moderator"},
                        {"command": "hgrant",   "description": "Grant Permission"},
                        {"command": "hrevoke",  "description": "Revoke Permission"},
                        {"command": "hban",     "description": "Ban User"},
                        {"command": "hmute",    "description": "Mute User"},
                        {"command": "hwarn",    "description": "Warn User"},
                        {"command": "hdel",     "description": "Delete Message"},
                        {"command": "hprotect", "description": "Protect User"},
                        {"command": "hcase",    "description": "View Case"},
                        {"command": "hmodinfo", "description": "Moderator Info"},
                    ]
                },
                timeout=10,
            )
            cmd_result = cmd_resp.json()
            log_msg(f"Commands set: {cmd_result.get('ok')}", "INFO")

            # Verify webhook
            log_msg("Verifying webhook...", "INFO")
            info_resp = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo",
                timeout=10,
            )
            info_result = info_resp.json()
            result["webhook_info"] = info_result
            log_msg(f"Webhook verified: {info_result.get('ok')}", "INFO")

    except Exception as e:
        log_msg(f"ERROR in setup_webhook: {e}", "ERROR")
        log_msg(traceback.format_exc(), "ERROR")
        result["errors"].append(str(e))

    log_msg("=== WEBHOOK SETUP COMPLETE ===", "INFO")
    return result

# =========================================================
# WEBHOOK ENDPOINT (receives Telegram updates)
# =========================================================

@app.post("/api/webhook")
async def webhook(request: Request):
    """Receive and process Telegram webhook updates"""
    try:
        update = await request.json()
        log_msg(f"Update received: {update.get('update_id')}", "DEBUG")

        bot = await get_bot()

        if "message" in update:
            msg_text = update["message"].get("text", "")[:50]
            log_msg(f"Message: {msg_text}", "DEBUG")
            await handle_message(bot, update["message"])

        elif "callback_query" in update:
            cb_data = update["callback_query"].get("data", "")[:50]
            log_msg(f"Callback: {cb_data}", "DEBUG")
            await handle_callback(bot, update["callback_query"])

    except Exception as e:
        log_msg(f"ERROR processing webhook: {e}", "ERROR")
        log_msg(traceback.format_exc(), "ERROR")

    return {"ok": True}

# =========================================================
# MESSAGE HANDLER
# =========================================================

async def handle_message(bot: Client, msg: dict):
    """Handle incoming Telegram messages"""
    try:
        text = msg.get("text", "")
        if not text.startswith("/"):
            return

        chat_id   = msg["chat"]["id"]
        msg_id    = msg["message_id"]
        from_user = msg.get("from", {})
        user_id   = from_user.get("id", 0)
        reply     = msg.get("reply_to_message")

        raw_cmd = text.split()[0].split("@")[0].lstrip("/").lower()
        parts   = text.split(None, 1)
        args    = parts[1].split() if len(parts) > 1 else []
        reason  = " ".join(args) if args else "No Reason"

        log_msg(f"User {user_id} command: /{raw_cmd}", "INFO")

        async def reply_text(t: str):
            await bot.send_message(chat_id, t, reply_to_message_id=msg_id)

        async def security_fail():
            try:
                await bot.delete_messages(chat_id, msg_id)
            except Exception:
                pass

        async def check_mod(permission: str) -> bool:
            if not is_authorized(user_id):
                await security_fail()
                return False
            if not has_permission(user_id, permission):
                await reply_text("❌ No Permission")
                return False
            if get_mod_info(user_id).get("frozen"):
                await reply_text("🚫 Moderator Frozen")
                return False
            return True

        # ── /start
        if raw_cmd == "start":
            await reply_text("✅ Advanced Moderation Bot Running\n\nUse /hauth to authorize moderators.")
            return

        # ── /hauth
        if raw_cmd in ("hauth", "ha"):
            if not is_owner(user_id):
                return await reply_text("❌ Only owner can authorize")
            if not reply:
                return await reply_text("Reply to a user.")
            target    = reply.get("from", {})
            target_id = target.get("id")
            data      = load(AUTH_FILE)
            if str(target_id) not in data:
                data[str(target_id)] = {
                    "permissions": {},
                    "mod_id": generate_mod_id(),
                    "badge": "🛡 Moderator",
                    "frozen": False,
                }
                save(AUTH_FILE, data)
            await reply_text(f"✅ Authorized {make_mention(target)}")

        # ── /hgrant
        elif raw_cmd in ("hgrant", "hg"):
            if not is_owner(user_id):
                return
            if not reply or not args:
                return await reply_text("Usage: /hgrant <permission>\nReply to moderator")
            permission = args[0].lower()
            target     = reply.get("from", {})
            target_id  = str(target.get("id"))
            data       = load(AUTH_FILE)
            if target_id not in data:
                return await reply_text("❌ Authorize first with /hauth")
            data[target_id]["permissions"][permission] = True
            save(AUTH_FILE, data)
            await reply_text(f"✅ Granted `{permission}` to {make_mention(target)}")

        # ── /hrevoke
        elif raw_cmd in ("hrevoke", "hr"):
            if not is_owner(user_id):
                return
            if not reply or not args:
                return await reply_text("Usage: /hrevoke <permission>\nReply to moderator")
            permission = args[0].lower()
            target     = reply.get("from", {})
            target_id  = str(target.get("id"))
            data       = load(AUTH_FILE)
            if target_id in data:
                data[target_id]["permissions"][permission] = False
                save(AUTH_FILE, data)
            await reply_text(f"❌ Revoked `{permission}` from {make_mention(target)}")

        # ── /hban
        elif raw_cmd in ("hban", "hb"):
            if not await check_mod("ban"):
                return
            if not reply:
                return await reply_text("Reply to user to ban")
            target    = reply.get("from", {})
            target_id = target.get("id")
            if is_protected(target_id):
                return await reply_text("🛡 Protected User")
            if await anti_nuke(bot, chat_id, msg_id, user_id):
                return
            await bot.ban_chat_member(chat_id, target_id)
            case_id = create_case("BAN", user_id, target_id, reason)
            await send_action_log(bot, chat_id, msg_id, "BAN", target, reason, case_id, get_mod_info(user_id))

        # ── /hmute
        elif raw_cmd in ("hmute", "hm"):
            if not await check_mod("mute"):
                return
            if not reply:
                return await reply_text("Reply to user to mute")
            target    = reply.get("from", {})
            target_id = target.get("id")
            if is_protected(target_id):
                return await reply_text("🛡 Protected User")
            if await anti_nuke(bot, chat_id, msg_id, user_id):
                return
            await bot.restrict_chat_member(chat_id, target_id, ChatPermissions())
            case_id = create_case("MUTE", user_id, target_id, reason)
            await send_action_log(bot, chat_id, msg_id, "MUTE", target, reason, case_id, get_mod_info(user_id))

        # ── /hwarn
        elif raw_cmd in ("hwarn", "hw"):
            if not await check_mod("warn"):
                return
            if not reply:
                return await reply_text("Reply to user to warn")
            target    = reply.get("from", {})
            target_id = target.get("id")
            warns     = load(WARN_FILE)
            uid       = str(target_id)
            warns.setdefault(uid, 0)
            warns[uid] += 1
            save(WARN_FILE, warns)
            case_id = create_case("WARN", user_id, target_id, "Warning")
            await send_action_log(
                bot, chat_id, msg_id, "WARN", target,
                f"Warning #{warns[uid]}", case_id, get_mod_info(user_id),
                extra=f"📊 Total Warns: {warns[uid]}"
            )

        # ── /hdel
        elif raw_cmd in ("hdel", "hd"):
            if not await check_mod("delete"):
                return
            if not reply:
                return await reply_text("Reply to message to delete")
            target        = reply.get("from", {})
            target_id     = target.get("id")
            reply_msg_id  = reply.get("message_id")
            deleted_text  = reply.get("text") or "Media Message"
            await bot.delete_messages(chat_id, reply_msg_id)
            case_id = create_case("DELETE", user_id, target_id, "Message Deleted")
            await send_action_log(
                bot, chat_id, msg_id, "DELETE", target,
                "Message Deleted", case_id, get_mod_info(user_id),
                extra=f"💬 Deleted: {deleted_text[:200]}"
            )

        # ── /hprotect
        elif raw_cmd in ("hprotect", "hp"):
            if not is_owner(user_id):
                return
            if not reply:
                return await reply_text("Reply to user to protect")
            target    = reply.get("from", {})
            target_id = target.get("id")
            data      = load(PROTECT_FILE)
            data[str(target_id)] = True
            save(PROTECT_FILE, data)
            await reply_text(f"🛡 {make_mention(target)} is now protected.")

        # ── /hcase
        elif raw_cmd in ("hcase", "hc"):
            if not is_authorized(user_id):
                return
            if not args:
                return await reply_text("Usage: /hcase <case_id>")
            cases   = load(CASE_FILE)
            case    = cases.get(args[0])
            if not case:
                return await reply_text("❌ Case not found.")
            await reply_text(
                f"📜 **Case #{args[0]}**\n\n"
                f"⚔ Action: {case['action']}\n"
                f"👤 Target: `{case['target']}`\n"
                f"👮 Moderator: `{case['moderator']}`\n"
                f"📝 Reason: {case['reason']}\n"
                f"⏰ Time: {case['time']}"
            )

        # ── /hmodinfo
        elif raw_cmd in ("hmodinfo", "hmi"):
            if not is_authorized(user_id):
                return
            lookup_id = reply.get("from", {}).get("id", user_id) if reply else user_id
            mod_data  = get_mod_info(lookup_id)
            if not mod_data:
                return await reply_text("❌ Not a moderator.")
            perms     = mod_data.get("permissions", {})
            perm_list = "\n".join(
                f"  {'✅' if v else '❌'} {k}" for k, v in perms.items()
            ) or "  No permissions set"
            status = "🔴 Frozen" if mod_data.get("frozen") else "🟢 Active"
            await reply_text(
                f"👮 **Moderator Info**\n\n"
                f"🆔 Mod ID: `{mod_data.get('mod_id', 'N/A')}`\n"
                f"{mod_data.get('badge', '🛡 Moderator')}\n"
                f"Status: {status}\n\n"
                f"**Permissions:**\n{perm_list}"
            )

    except Exception as e:
        log_msg(f"ERROR in handle_message: {e}", "ERROR")
        log_msg(traceback.format_exc(), "ERROR")

# =========================================================
# CALLBACK HANDLER
# =========================================================

async def handle_callback(bot: Client, cb: dict):
    """Handle inline button callbacks"""
    try:
        cb_id     = cb["id"]
        data      = cb.get("data", "")
        user_id   = cb.get("from", {}).get("id", 0)
        message   = cb.get("message", {})
        chat_id   = message.get("chat", {}).get("id")

        if user_id != OWNER_ID:
            await bot.answer_callback_query(cb_id, "Only Owner Can Use This", show_alert=True)
            return

        if data.startswith("unban_"):
            target_id = int(data.split("_")[1])
            await bot.unban_chat_member(chat_id, target_id)
            await bot.answer_callback_query(cb_id, "✅ User Unbanned")

        elif data.startswith("unmute_"):
            target_id = int(data.split("_")[1])
            await bot.restrict_chat_member(
                chat_id, target_id,
                ChatPermissions(can_send_messages=True)
            )
            await bot.answer_callback_query(cb_id, "✅ User Unmuted")

        elif data.startswith("removewarn_"):
            target_id = int(data.split("_")[1])
            warns     = load(WARN_FILE)
            uid       = str(target_id)
            if uid in warns and warns[uid] > 0:
                warns[uid] -= 1
                save(WARN_FILE, warns)
            await bot.answer_callback_query(cb_id, f"✅ Warn removed. Total: {warns.get(uid, 0)}")

        elif data.startswith("case_"):
            case_id = data.split("_")[1]
            cases   = load(CASE_FILE)
            case    = cases.get(case_id)
            if case:
                await bot.answer_callback_query(
                    cb_id,
                    f"Case #{case_id}\n{case['action']} | {case['reason']}",
                    show_alert=True
                )
            else:
                await bot.answer_callback_query(cb_id, "Case not found", show_alert=True)

    except Exception as e:
        log_msg(f"ERROR in handle_callback: {e}", "ERROR")
        log_msg(traceback.format_exc(), "ERROR")

# =========================================================
# STARTUP / SHUTDOWN EVENTS
# =========================================================

@app.on_event("startup")
async def startup_event():
    """Initialize bot on server startup"""
    log_msg("🚀 Server starting up...", "INFO")
    try:
        bot = await get_bot()
        log_msg("✅ Bot initialized successfully", "INFO")
    except Exception as e:
        log_msg(f"❌ Failed to initialize bot: {e}", "ERROR")

@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully shutdown bot"""
    log_msg("🛑 Server shutting down...", "INFO")
    await shutdown_bot()

# =========================================================
# MAIN ENTRY POINT
# =========================================================

if __name__ == "__main__":
    import uvicorn
    
    log_msg(f"Starting Koyeb Bot Server on port {PORT}", "INFO")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

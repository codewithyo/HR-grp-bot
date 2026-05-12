# =========================================================
# ADVANCED TELEGRAM MODERATION BOT — VERCEL WEBHOOK VERSION
# =========================================================
#
# Architecture:
#   • Telegram sends POST updates (Bot API JSON) to /api/webhook
#   • FastAPI receives them and routes to handler functions
#   • Pyrogram Client (no_updates=True, in_memory=True) makes
#     all outbound API calls (ban, mute, send_message, etc.)
#   • /api/set_webhook auto-registers the webhook URL with Telegram
#
# IMPORTANT — Ephemeral storage warning:
#   /tmp is NOT shared between Vercel function instances and resets
#   on cold starts. For production, replace JSON files with:
#   Vercel KV (Redis), Supabase, PlanetScale, or MongoDB Atlas.
#
# ENV VARS required in Vercel dashboard:
#   API_ID, API_HASH, BOT_TOKEN, OWNER_ID, LOG_GROUP_ID
# =========================================================

import os
import json
import time
import random
import string
import asyncio
import httpx
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from mangum import Mangum

from pyrogram import Client
from pyrogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton

# =========================================================
# CONFIG — read from environment variables
# =========================================================

API_ID       = int(os.environ.get("API_ID", "9605646"))
API_HASH     = os.environ.get("API_HASH", "822d45aa548a53682a458efa1933e4c9")
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "822d45aa548a53682a458efa1933e4c9")
OWNER_ID     = int(os.environ.get("OWNER_ID", "8457503781"))
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "-1003834934514"))

# =========================================================
# STORAGE — /tmp (ephemeral; swap for a DB in production)
# =========================================================

TMP_DIR = "/tmp/modbot"
os.makedirs(TMP_DIR, exist_ok=True)

AUTH_FILE    = f"{TMP_DIR}/auth.json"
WARN_FILE    = f"{TMP_DIR}/warns.json"
CASE_FILE    = f"{TMP_DIR}/cases.json"
PROTECT_FILE = f"{TMP_DIR}/protected.json"
ABUSE_FILE   = f"{TMP_DIR}/abuse.json"

for _f in [AUTH_FILE, WARN_FILE, CASE_FILE, PROTECT_FILE, ABUSE_FILE]:
    if not os.path.exists(_f):
        with open(_f, "w") as _fh:
            json.dump({}, _fh)

# =========================================================
# JSON HELPERS
# =========================================================

def load(file: str) -> dict:
    try:
        with open(file, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save(file: str, data: dict):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

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
    """Build a Markdown mention from a Bot API user dict."""
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
# Reused across warm Lambda/Vercel invocations.
# Uses in_memory session (no session file needed) and
# no_updates=True (we feed updates via webhook, not polling).
# =========================================================

_bot: Client = None

async def get_bot() -> Client:
    global _bot
    if _bot is None or not _bot.is_connected:
        _bot = Client(
            name         = "modbot",
            api_id       = API_ID,
            api_hash     = API_HASH,
            bot_token    = BOT_TOKEN,
            in_memory    = True,
            no_updates   = True,   # webhook mode — no MTProto polling
        )
        await _bot.start()
    return _bot

# =========================================================
# ANTI-NUKE
# =========================================================

async def anti_nuke(bot: Client, chat_id: int, reply_to: int, user_id: int) -> bool:
    total = track_action(user_id)
    if total < 10:
        return False

    auth = load(AUTH_FILE)
    if str(user_id) in auth:
        auth[str(user_id)]["frozen"] = True
        save(AUTH_FILE, auth)

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

    await bot.send_message(chat_id,    text, reply_markup=markup, reply_to_message_id=reply_to)
    await bot.send_message(LOG_GROUP_ID, text, reply_markup=markup)

# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(title="Advanced Moderation Bot")

# =========================================================
# ROUTE — /api/set_webhook
# Call this once after deploying to register the webhook URL.
# Opens in browser: https://<project>.vercel.app/api/set_webhook
# =========================================================

@app.get("/api/set_webhook")
async def set_webhook(request: Request):
    base_url    = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/api/webhook"

    async with httpx.AsyncClient() as client:
        # Set the webhook
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

        # Set bot commands
        await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
            json={
                "commands": [
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

        # Verify webhook info
        info_resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo",
            timeout=10,
        )
        info_result = info_resp.json()

    return {
        "webhook_registered": webhook_url,
        "set_webhook":        set_result,
        "webhook_info":       info_result,
    }

# =========================================================
# ROUTE — /api/webhook  (receives every Telegram update)
# =========================================================

@app.post("/api/webhook")
async def webhook(request: Request):
    update = await request.json()

    try:
        bot = await get_bot()

        if "message" in update:
            await handle_message(bot, update["message"])

        elif "callback_query" in update:
            await handle_callback(bot, update["callback_query"])

    except Exception as e:
        # Log silently — always return 200 so Telegram doesn't retry
        print(f"[ERROR] {type(e).__name__}: {e}")

    return {"ok": True}

# =========================================================
# MESSAGE ROUTER
# =========================================================

async def handle_message(bot: Client, msg: dict):
    text = msg.get("text", "")
    if not text.startswith("/"):
        return

    chat_id   = msg["chat"]["id"]
    msg_id    = msg["message_id"]
    from_user = msg.get("from", {})
    user_id   = from_user.get("id", 0)
    reply     = msg.get("reply_to_message")

    # Parse /cmd@botusername → cmd, strip leading slash
    raw_cmd = text.split()[0].split("@")[0].lstrip("/").lower()
    parts   = text.split(None, 1)
    args    = parts[1].split() if len(parts) > 1 else []
    reason  = " ".join(args) if args else "No Reason"

    # ── Shortcuts ──────────────────────────────────────────

    async def reply_text(t: str):
        await bot.send_message(chat_id, t, reply_to_message_id=msg_id)

    async def security_fail():
        try:
            await bot.delete_messages(chat_id, msg_id)
        except Exception:
            pass

    async def check_mod(permission: str) -> bool:
        """Returns True if the command should proceed."""
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

    # ── /hauth ─────────────────────────────────────────────
    if raw_cmd in ("hauth", "ha"):
        if not is_owner(user_id):
            return
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

    # ── /hgrant ────────────────────────────────────────────
    elif raw_cmd in ("hgrant", "hg"):
        if not is_owner(user_id) or not reply or not args:
            return
        permission = args[0].lower()
        target     = reply.get("from", {})
        target_id  = str(target.get("id"))
        data       = load(AUTH_FILE)
        if target_id not in data:
            return await reply_text("❌ Authorize the user first (/hauth).")
        data[target_id]["permissions"][permission] = True
        save(AUTH_FILE, data)
        await reply_text(f"✅ Granted `{permission}` to {make_mention(target)}")

    # ── /hrevoke ───────────────────────────────────────────
    elif raw_cmd in ("hrevoke", "hr"):
        if not is_owner(user_id) or not reply or not args:
            return
        permission = args[0].lower()
        target     = reply.get("from", {})
        target_id  = str(target.get("id"))
        data       = load(AUTH_FILE)
        if target_id in data:
            data[target_id]["permissions"][permission] = False
            save(AUTH_FILE, data)
        await reply_text(f"❌ Revoked `{permission}` from {make_mention(target)}")

    # ── /hban ──────────────────────────────────────────────
    elif raw_cmd in ("hban", "hb"):
        if not await check_mod("ban"):
            return
        if not reply:
            return
        target    = reply.get("from", {})
        target_id = target.get("id")
        if is_protected(target_id):
            return await reply_text("🛡 Protected User")
        if await anti_nuke(bot, chat_id, msg_id, user_id):
            return
        await bot.ban_chat_member(chat_id, target_id)
        case_id = create_case("BAN", user_id, target_id, reason)
        await send_action_log(bot, chat_id, msg_id, "BAN", target, reason, case_id, get_mod_info(user_id))

    # ── /hmute ─────────────────────────────────────────────
    elif raw_cmd in ("hmute", "hm"):
        if not await check_mod("mute"):
            return
        if not reply:
            return
        target    = reply.get("from", {})
        target_id = target.get("id")
        if is_protected(target_id):
            return await reply_text("🛡 Protected User")
        if await anti_nuke(bot, chat_id, msg_id, user_id):
            return
        await bot.restrict_chat_member(chat_id, target_id, ChatPermissions())
        case_id = create_case("MUTE", user_id, target_id, reason)
        await send_action_log(bot, chat_id, msg_id, "MUTE", target, reason, case_id, get_mod_info(user_id))

    # ── /hwarn ─────────────────────────────────────────────
    elif raw_cmd in ("hwarn", "hw"):
        if not await check_mod("warn"):
            return
        if not reply:
            return
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

    # ── /hdel ──────────────────────────────────────────────
    elif raw_cmd in ("hdel", "hd"):
        if not await check_mod("delete"):
            return
        if not reply:
            return
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

    # ── /hprotect ──────────────────────────────────────────
    elif raw_cmd in ("hprotect", "hp"):
        if not is_owner(user_id) or not reply:
            return
        target    = reply.get("from", {})
        target_id = target.get("id")
        data      = load(PROTECT_FILE)
        data[str(target_id)] = True
        save(PROTECT_FILE, data)
        await reply_text(f"🛡 {make_mention(target)} is now protected.")

    # ── /hcase ─────────────────────────────────────────────
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

    # ── /hmodinfo ──────────────────────────────────────────
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

    # ── /start ─────────────────────────────────────────────
    elif raw_cmd == "start":
        if not is_authorized(user_id):
            return
        await reply_text("✅ Advanced Moderation Bot Running")

# =========================================================
# CALLBACK ROUTER
# =========================================================

async def handle_callback(bot: Client, cb: dict):
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

# =========================================================
# VERCEL HANDLER — Mangum wraps FastAPI as an ASGI handler
# =========================================================

handler = Mangum(app, lifespan="off")

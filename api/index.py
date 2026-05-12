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
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pyrogram import Client
from pyrogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton

BOT_COMMANDS = [
    {"command": "start", "description": "Start Bot"},
    {"command": "help", "description": "Show command help"},
    {"command": "happeal", "description": "Appeal moderation case"},
    {"command": "hauth", "description": "Authorize Moderator"},
    {"command": "hgrant", "description": "Grant Permission"},
    {"command": "hrevoke", "description": "Revoke Permission"},
    {"command": "hban", "description": "Ban User"},
    {"command": "hmute", "description": "Mute User"},
    {"command": "hwarn", "description": "Warn User"},
    {"command": "hdel", "description": "Delete Message"},
    {"command": "hprotect", "description": "Protect User"},
    {"command": "hcase", "description": "View Case"},
    {"command": "hmodinfo", "description": "Moderator Info"},
]

VALID_PERMISSIONS = {"ban", "mute", "warn", "delete"}

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
OWNER_DEBUG_NOTIFICATIONS = os.environ.get("OWNER_DEBUG_NOTIFICATIONS", "1") == "1"

def resolve_storage_path() -> str:
    """Prefer persistent disk and fallback to /tmp when unavailable."""
    preferred = os.environ.get("STORAGE_PATH", "/data/modbot")
    try:
        Path(preferred).mkdir(parents=True, exist_ok=True)
        test_file = Path(preferred) / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return preferred
    except Exception:
        fallback = "/tmp/modbot"
        Path(fallback).mkdir(parents=True, exist_ok=True)
        log_msg(f"Storage path '{preferred}' unavailable. Falling back to {fallback}", "WARNING")
        return fallback

STORAGE_PATH = resolve_storage_path()
FALLBACK_STORAGE_PATH = os.environ.get("FALLBACK_STORAGE_PATH", "/tmp/modbot_fallback")
Path(FALLBACK_STORAGE_PATH).mkdir(parents=True, exist_ok=True)

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
TEMP_ACTIONS_FILE = f"{STORAGE_PATH}/temp_actions.json"
APPEALS_FILE = f"{STORAGE_PATH}/appeals.json"

FALLBACK_AUTH_FILE    = f"{FALLBACK_STORAGE_PATH}/auth.json"
FALLBACK_WARN_FILE    = f"{FALLBACK_STORAGE_PATH}/warns.json"
FALLBACK_CASE_FILE    = f"{FALLBACK_STORAGE_PATH}/cases.json"
FALLBACK_PROTECT_FILE = f"{FALLBACK_STORAGE_PATH}/protected.json"
FALLBACK_ABUSE_FILE   = f"{FALLBACK_STORAGE_PATH}/abuse.json"
FALLBACK_TEMP_ACTIONS_FILE = f"{FALLBACK_STORAGE_PATH}/temp_actions.json"
FALLBACK_APPEALS_FILE = f"{FALLBACK_STORAGE_PATH}/appeals.json"

FALLBACK_FILE_MAP = {
    AUTH_FILE: FALLBACK_AUTH_FILE,
    WARN_FILE: FALLBACK_WARN_FILE,
    CASE_FILE: FALLBACK_CASE_FILE,
    PROTECT_FILE: FALLBACK_PROTECT_FILE,
    ABUSE_FILE: FALLBACK_ABUSE_FILE,
    TEMP_ACTIONS_FILE: FALLBACK_TEMP_ACTIONS_FILE,
    APPEALS_FILE: FALLBACK_APPEALS_FILE,
}

def ensure_json_file(file_path: str):
    """Create missing JSON files with an empty object payload."""
    if os.path.exists(file_path):
        return
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as fh:
        json.dump({}, fh)

def init_storage_files():
    """Initialize primary and fallback JSON files and bootstrap when needed."""
    for primary, fallback in FALLBACK_FILE_MAP.items():
        ensure_json_file(fallback)

        if not os.path.exists(primary):
            try:
                shutil.copy2(fallback, primary)
                log_msg(f"Bootstrapped storage file from fallback: {primary}", "INFO")
            except Exception:
                ensure_json_file(primary)
        else:
            # Keep fallback warm with latest primary content.
            try:
                shutil.copy2(primary, fallback)
            except Exception as e:
                log_msg(f"WARNING syncing fallback file {fallback}: {e}", "WARNING")

init_storage_files()

log_msg(f"Storage initialized at: {STORAGE_PATH}", "INFO")
log_msg(f"Fallback storage initialized at: {FALLBACK_STORAGE_PATH}", "INFO")

# =========================================================
# JSON HELPERS
# =========================================================

def load(file: str) -> dict:
    """Load JSON file"""
    try:
        if not os.path.exists(file):
            fallback_file = FALLBACK_FILE_MAP.get(file)
            if fallback_file and os.path.exists(fallback_file):
                with open(fallback_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                save(file, data)
                log_msg(f"Recovered missing file from fallback: {file}", "WARNING")
                return data
            return {}
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        backup_file = f"{file}.bak"
        if os.path.exists(backup_file):
            try:
                with open(backup_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                log_msg(f"Recovered storage from backup: {backup_file}", "WARNING")
                return data
            except Exception as backup_error:
                log_msg(f"ERROR loading backup {backup_file}: {backup_error}", "ERROR")
        fallback_file = FALLBACK_FILE_MAP.get(file)
        if fallback_file and os.path.exists(fallback_file):
            try:
                with open(fallback_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                save(file, data)
                log_msg(f"Recovered storage from fallback JSON: {fallback_file}", "WARNING")
                return data
            except Exception as fallback_error:
                log_msg(f"ERROR loading fallback {fallback_file}: {fallback_error}", "ERROR")
        log_msg(f"ERROR loading {file}: invalid JSON and no valid backup", "ERROR")
        return {}
    except Exception as e:
        log_msg(f"ERROR loading {file}: {e}", "ERROR")
        return {}

def save(file: str, data: dict):
    """Save JSON file atomically to reduce restart corruption risk."""
    try:
        temp_file = f"{file}.tmp"
        backup_file = f"{file}.bak"

        if os.path.exists(file):
            shutil.copy2(file, backup_file)

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        os.replace(temp_file, file)

        fallback_file = FALLBACK_FILE_MAP.get(file)
        if fallback_file:
            fallback_temp_file = f"{fallback_file}.tmp"
            Path(fallback_file).parent.mkdir(parents=True, exist_ok=True)
            with open(fallback_temp_file, "w", encoding="utf-8") as ff:
                json.dump(data, ff, indent=4)
            os.replace(fallback_temp_file, fallback_file)
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
    if not isinstance(user, dict):
        return "User"
    uid = user.get("id")
    first = user.get("first_name", "")
    last  = user.get("last_name", "")
    name  = (first + " " + last).strip() or "User"
    if not uid:
        return name
    return f"[{name}](tg://user?id={uid})"

def extract_actor_user_id(msg: dict) -> tuple[int | None, str | None]:
    """Resolve the real Telegram user id for command authorization checks."""
    from_user = msg.get("from") if isinstance(msg, dict) else None
    if isinstance(from_user, dict):
        uid = from_user.get("id")
        if isinstance(uid, int) and uid > 0:
            return uid, None

    if msg.get("sender_chat"):
        return None, "❌ Anonymous admin/channel messages are not supported for admin commands. Disable anonymous mode and retry."

    return None, "❌ Could not identify your Telegram account for permission checks."

def extract_reply_user(reply: dict) -> tuple[dict, int | None]:
    """Get replied user dict and id, if present."""
    if not isinstance(reply, dict):
        return {}, None
    target = reply.get("from")
    if not isinstance(target, dict):
        return {}, None
    target_id = target.get("id")
    if not isinstance(target_id, int) or target_id <= 0:
        return target, None
    return target, target_id

def parse_positive_user_id(value: str) -> int | None:
    """Parse a positive Telegram user id from command argument."""
    try:
        uid = int(value.strip())
        if uid > 0:
            return uid
    except Exception:
        return None
    return None

def resolve_target_from_reply_or_args(
    reply: dict,
    args: list[str],
    user_id_arg_index: int,
) -> tuple[dict, int | None, str | None]:
    """Resolve target user from replied message first, then from command args."""
    target, target_id = extract_reply_user(reply)
    if target_id:
        return target, target_id, None

    if len(args) > user_id_arg_index:
        parsed_uid = parse_positive_user_id(args[user_id_arg_index])
        if parsed_uid:
            return {"id": parsed_uid, "first_name": "User"}, parsed_uid, None
        return {}, None, "❌ Invalid user ID. Send a numeric Telegram user ID."

    return {}, None, "❌ Reply to a user or pass their user ID."

def extract_reason_from_args(args: list[str], start_index: int, default: str = "No Reason") -> str:
    """Build reason text from command args after a given index."""
    if len(args) <= start_index:
        return default
    reason = " ".join(args[start_index:]).strip()
    return reason or default

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

def load_temp_actions() -> list[dict]:
    data = load(TEMP_ACTIONS_FILE)
    if isinstance(data, list):
        return data
    return []

def save_temp_actions(actions: list[dict]):
    save(TEMP_ACTIONS_FILE, actions)

def parse_duration_token(token: str) -> int | None:
    """Parse duration token like 15m, 2h, 3d into seconds."""
    if not token:
        return None
    token = token.strip().lower()
    if len(token) < 2:
        return None
    unit = token[-1]
    num_part = token[:-1]
    if not num_part.isdigit():
        return None
    value = int(num_part)
    if value <= 0:
        return None
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in scale:
        return None
    return value * scale[unit]

def parse_duration_and_reason(args: list[str], start_index: int, default_reason: str = "No Reason") -> tuple[int | None, str]:
    """Parse optional duration token and reason from args."""
    if len(args) <= start_index:
        return None, default_reason
    maybe_duration = parse_duration_token(args[start_index])
    if maybe_duration is not None:
        reason = extract_reason_from_args(args, start_index + 1, default_reason)
        return maybe_duration, reason
    return None, extract_reason_from_args(args, start_index, default_reason)

def format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"

def role_help_text(user_id: int) -> str:
    if is_owner(user_id):
        return (
            "📘 **Owner Help**\n\n"
            "General: /start, /help, /happeal <case_id> <message>\n"
            "Owner: /hauth, /hgrant, /hrevoke, /hprotect\n"
            "Moderation: /hban, /hmute, /hwarn, /hdel, /hcase, /hmodinfo\n\n"
            "Timed actions: use duration token like `30m`, `2h`, `1d`\n"
            "Examples:\n"
            "`/hban 123456789 2h spam`\n"
            "`/hmute 123456789 30m abuse`"
        )
    if is_authorized(user_id):
        return (
            "📘 **Moderator Help**\n\n"
            "Commands: /hban, /hmute, /hwarn, /hdel, /hcase, /hmodinfo\n"
            "Timed actions supported for ban/mute with `30m`, `2h`, `1d`."
        )
    return (
        "📘 **User Help**\n\n"
        "Commands: /start, /help\n"
        "Appeal: `/happeal <case_id> <message>` in bot DM."
    )

def create_appeal(user_id: int, case_id: str, message: str) -> str:
    appeals = load(APPEALS_FILE)
    if not isinstance(appeals, dict):
        appeals = {}
    appeal_id = str(len(appeals) + 1)
    appeals[appeal_id] = {
        "case_id": case_id,
        "user_id": user_id,
        "message": message,
        "status": "open",
        "time": str(datetime.now()),
    }
    save(APPEALS_FILE, appeals)
    return appeal_id

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
_temp_action_worker_task: asyncio.Task | None = None

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

async def notify_owner(bot: Client, text: str):
    """Send operational notifications to owner without breaking runtime."""
    if OWNER_ID == 0:
        return
    try:
        await bot.send_message(OWNER_ID, text)
    except Exception as e:
        log_msg(f"Owner notify failed: {e}", "WARNING")

async def ensure_webhook_registered() -> str:
    """Ensure webhook is configured when APP_URL or WEBHOOK_URL is available."""
    base = os.environ.get("WEBHOOK_URL") or os.environ.get("APP_URL")
    if not base:
        return "skipped:no-url"

    base = base.rstrip("/")
    webhook_url = f"{base}/api/webhook"
    try:
        async with httpx.AsyncClient() as client:
            info_resp = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo",
                timeout=10,
            )
            info = info_resp.json().get("result", {})
            current = info.get("url", "")

            if current == webhook_url:
                return "ok:already-set"

            set_resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": False,
                },
                timeout=10,
            )
            set_result = set_resp.json()
            if set_result.get("ok"):
                return f"ok:set:{webhook_url}"
            return f"error:{set_result.get('description', 'unknown')}"
    except Exception as e:
        return f"error:{e}"

async def sync_bot_commands() -> str:
    """Set Telegram bot menu commands and return status string."""
    try:
        async with httpx.AsyncClient() as client:
            cmd_resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
                json={"commands": BOT_COMMANDS},
                timeout=10,
            )
            result = cmd_resp.json()
            if result.get("ok"):
                return "ok"
            return f"error:{result.get('description', 'unknown')}"
    except Exception as e:
        return f"error:{e}"

async def process_due_temp_actions(bot: Client):
    """Process due unmute/unban actions from persistent storage."""
    actions = load_temp_actions()
    if not actions:
        return

    now_ts = int(time.time())
    pending: list[dict] = []

    for action in actions:
        until_ts = int(action.get("until_ts", 0))
        if until_ts > now_ts:
            pending.append(action)
            continue

        chat_id = action.get("chat_id")
        target_id = action.get("target_id")
        action_type = action.get("type")

        try:
            if action_type == "mute":
                await bot.restrict_chat_member(
                    chat_id,
                    target_id,
                    ChatPermissions(can_send_messages=True),
                )
                await bot.send_message(chat_id, f"🔊 Temporary mute ended for `{target_id}`")
            elif action_type == "ban":
                await bot.unban_chat_member(chat_id, target_id)
                await bot.send_message(chat_id, f"🔓 Temporary ban ended for `{target_id}`")
        except Exception as e:
            log_msg(f"ERROR processing temp action {action}: {e}", "ERROR")
            pending.append(action)

    if len(pending) != len(actions):
        save_temp_actions(pending)

async def temp_action_worker():
    """Background worker to auto-revert timed moderation actions."""
    while True:
        try:
            bot = await get_bot()
            await process_due_temp_actions(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_msg(f"ERROR in temp action worker: {e}", "ERROR")
        await asyncio.sleep(10)

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

async def send_grant_confirmation(
    bot: Client,
    chat_id: int,
    reply_to: int,
    granted_by: int,
    target: dict,
    permission: str,
    case_id: str | None = None,
):
    """Post permission grant confirmation in the current group/chat."""
    try:
        case_line = f"\n📜 Case ID: #{case_id}" if case_id else ""
        text = (
            "✅ Permission Granted\n\n"
            f"👤 Moderator: {make_mention(target)}\n"
            f"🔐 Permission: `{permission}`\n"
            f"🛡 Granted by: `{granted_by}`"
            f"{case_line}"
        )
        await bot.send_message(chat_id, text, reply_to_message_id=reply_to)
        await bot.send_message(LOG_GROUP_ID, f"📝 Grant logged\n\n{text}")
    except Exception as e:
        log_msg(f"ERROR sending grant confirmation: {e}", "ERROR")

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
        "set_commands": {},
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

            # Set bot menu commands
            log_msg("Setting bot commands...", "INFO")
            commands_status = await sync_bot_commands()
            result["set_commands"] = {"status": commands_status}
            if commands_status.startswith("error:"):
                result["errors"].append(f"Commands error: {commands_status}")
            log_msg(f"Commands status: {commands_status}", "INFO")

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
        if not isinstance(update, dict):
            log_msg("Invalid webhook payload: expected JSON object", "WARNING")
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
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
        user_id, user_identity_error = extract_actor_user_id(msg)
        reply     = msg.get("reply_to_message")

        raw_cmd = text.split()[0].split("@")[0].lstrip("/").lower()
        parts   = text.split(None, 1)
        args    = parts[1].split() if len(parts) > 1 else []
        reason  = " ".join(args) if args else "No Reason"

        log_msg(f"User {user_id} command: /{raw_cmd}", "INFO")

        async def reply_text(t: str):
            await bot.send_message(chat_id, t, reply_to_message_id=msg_id)

        if user_identity_error:
            await reply_text(user_identity_error)
            return

        if OWNER_DEBUG_NOTIFICATIONS:
            await notify_owner(
                bot,
                (
                    f"📨 Command received\n"
                    f"User ID: `{user_id}`\n"
                    f"Chat ID: `{chat_id}`\n"
                    f"Command: `/{raw_cmd}`"
                ),
            )

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
            await reply_text(
                "✅ Advanced Moderation Bot Running\n\n"
                "Use /help to see commands based on your role.\n"
                "📋 Command menu is auto-synced on startup.\n"
                "Use menu button or these commands:\n"
                "/help, /happeal, /hauth, /hgrant, /hrevoke, /hban, /hmute, /hwarn, /hdel, /hprotect, /hcase, /hmodinfo"
            )
            return

        # ── /help
        if raw_cmd == "help":
            await reply_text(role_help_text(user_id))
            return

        # ── /happeal
        if raw_cmd == "happeal":
            chat_type = msg.get("chat", {}).get("type")
            if chat_type != "private":
                return await reply_text("❌ Use /happeal in bot private chat (DM) only.")
            if len(args) < 2:
                return await reply_text("Usage: /happeal <case_id> <message>")
            case_id = args[0]
            appeal_message = " ".join(args[1:]).strip()
            if not appeal_message:
                return await reply_text("❌ Appeal message is required.")

            cases = load(CASE_FILE)
            if case_id not in cases:
                return await reply_text("❌ Case not found.")

            case = cases[case_id]
            if int(case.get("target", 0)) != user_id and not is_owner(user_id):
                return await reply_text("❌ You can only appeal your own case.")

            appeal_id = create_appeal(user_id, case_id, appeal_message)
            await reply_text(f"✅ Appeal submitted. Appeal ID: #{appeal_id}")
            await notify_owner(
                bot,
                (
                    f"📨 New appeal #{appeal_id}\n"
                    f"Case: #{case_id}\n"
                    f"User: `{user_id}`\n"
                    f"Message: {appeal_message}"
                ),
            )
            return

        # ── /hauth
        if raw_cmd in ("hauth", "ha"):
            if not is_owner(user_id):
                return await reply_text("❌ Only owner can authorize")
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 0)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hauth <user_id>\nOr reply to a user with /hauth"
                )
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
            if not args:
                return await reply_text("Usage: /hgrant <permission> <user_id>\nOr reply to moderator with /hgrant <permission>")
            permission = args[0].lower()
            if permission not in VALID_PERMISSIONS:
                return await reply_text("❌ Invalid permission. Use: ban, mute, warn, delete")
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 1)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hgrant <permission> <user_id>\nOr reply to moderator with /hgrant <permission>"
                )
            target_id  = str(target_id)
            data       = load(AUTH_FILE)
            if target_id not in data:
                return await reply_text("❌ Authorize first with /hauth")
            data[target_id]["permissions"][permission] = True
            save(AUTH_FILE, data)
            case_id = create_case("GRANT", user_id, int(target_id), f"Granted permission: {permission}")
            await reply_text(f"✅ Granted `{permission}` to {make_mention(target)}")
            await send_grant_confirmation(bot, chat_id, msg_id, user_id, target, permission, case_id)

        # ── /hrevoke
        elif raw_cmd in ("hrevoke", "hr"):
            if not is_owner(user_id):
                return
            if not args:
                return await reply_text("Usage: /hrevoke <permission> <user_id>\nOr reply to moderator with /hrevoke <permission>")
            permission = args[0].lower()
            if permission not in VALID_PERMISSIONS:
                return await reply_text("❌ Invalid permission. Use: ban, mute, warn, delete")
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 1)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hrevoke <permission> <user_id>\nOr reply to moderator with /hrevoke <permission>"
                )
            target_id  = str(target_id)
            data       = load(AUTH_FILE)
            if target_id in data:
                data[target_id]["permissions"][permission] = False
                save(AUTH_FILE, data)
                case_id = create_case("REVOKE", user_id, int(target_id), f"Revoked permission: {permission}")
                await bot.send_message(LOG_GROUP_ID, (
                    "📝 Permission Revoked\n\n"
                    f"👤 Moderator: {make_mention(target)}\n"
                    f"🔐 Permission: `{permission}`\n"
                    f"🛡 Revoked by: `{user_id}`\n"
                    f"📜 Case ID: #{case_id}"
                ))
            await reply_text(f"❌ Revoked `{permission}` from {make_mention(target)}")

        # ── /hban
        elif raw_cmd in ("hban", "hb"):
            if not await check_mod("ban"):
                return
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 0)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hban <user_id> [duration] [reason]\nOr reply to user with /hban [duration] [reason]"
                )
            duration_secs, action_reason = parse_duration_and_reason(args, 1, "No Reason") if not reply else parse_duration_and_reason(args, 0, "No Reason")
            if is_protected(target_id):
                return await reply_text("🛡 Protected User")
            if await anti_nuke(bot, chat_id, msg_id, user_id):
                return
            until_ts = None
            if duration_secs:
                until_ts = int(time.time()) + duration_secs
            await bot.ban_chat_member(chat_id, target_id, until_date=until_ts)

            if duration_secs:
                actions = load_temp_actions()
                actions.append(
                    {
                        "type": "ban",
                        "chat_id": chat_id,
                        "target_id": target_id,
                        "until_ts": until_ts,
                        "set_by": user_id,
                        "reason": action_reason,
                    }
                )
                save_temp_actions(actions)
                action_reason = f"{action_reason} | Duration: {format_duration(duration_secs)}"

            case_id = create_case("BAN", user_id, target_id, action_reason)
            await send_action_log(bot, chat_id, msg_id, "BAN", target, action_reason, case_id, get_mod_info(user_id))

        # ── /hmute
        elif raw_cmd in ("hmute", "hm"):
            if not await check_mod("mute"):
                return
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 0)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hmute <user_id> [duration] [reason]\nOr reply to user with /hmute [duration] [reason]"
                )
            duration_secs, action_reason = parse_duration_and_reason(args, 1, "No Reason") if not reply else parse_duration_and_reason(args, 0, "No Reason")
            if is_protected(target_id):
                return await reply_text("🛡 Protected User")
            if await anti_nuke(bot, chat_id, msg_id, user_id):
                return
            until_ts = None
            if duration_secs:
                until_ts = int(time.time()) + duration_secs
            await bot.restrict_chat_member(chat_id, target_id, ChatPermissions(), until_date=until_ts)

            if duration_secs:
                actions = load_temp_actions()
                actions.append(
                    {
                        "type": "mute",
                        "chat_id": chat_id,
                        "target_id": target_id,
                        "until_ts": until_ts,
                        "set_by": user_id,
                        "reason": action_reason,
                    }
                )
                save_temp_actions(actions)
                action_reason = f"{action_reason} | Duration: {format_duration(duration_secs)}"

            case_id = create_case("MUTE", user_id, target_id, action_reason)
            await send_action_log(bot, chat_id, msg_id, "MUTE", target, action_reason, case_id, get_mod_info(user_id))

        # ── /hwarn
        elif raw_cmd in ("hwarn", "hw"):
            if not await check_mod("warn"):
                return
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 0)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hwarn <user_id> [reason]\nOr reply to user with /hwarn [reason]"
                )
            warns     = load(WARN_FILE)
            uid       = str(target_id)
            warns.setdefault(uid, 0)
            warns[uid] += 1
            save(WARN_FILE, warns)
            warn_reason = extract_reason_from_args(args, 1, "Warning") if not reply else extract_reason_from_args(args, 0, "Warning")
            case_id = create_case("WARN", user_id, target_id, warn_reason)
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
            target, target_id = extract_reply_user(reply)
            if not target_id:
                return await reply_text("❌ Reply to a normal user message (not anonymous/channel).")
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
            target, target_id, target_error = resolve_target_from_reply_or_args(reply, args, 0)
            if not target_id:
                return await reply_text(
                    f"{target_error}\nUsage: /hprotect <user_id>\nOr reply to user with /hprotect"
                )
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
            lookup_id = user_id
            if reply:
                _, reply_uid = extract_reply_user(reply)
                if reply_uid:
                    lookup_id = reply_uid
            elif args:
                parsed_uid = parse_positive_user_id(args[0])
                if not parsed_uid:
                    return await reply_text("❌ Invalid user ID. Usage: /hmodinfo <user_id>")
                lookup_id = parsed_uid
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
        try:
            if OWNER_DEBUG_NOTIFICATIONS:
                await notify_owner(bot, f"❌ handle_message error: {e}")
        except Exception:
            pass

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
    global _temp_action_worker_task
    log_msg("🚀 Server starting up...", "INFO")
    try:
        bot = await get_bot()
        webhook_status = await ensure_webhook_registered()
        commands_status = await sync_bot_commands()
        if _temp_action_worker_task is None or _temp_action_worker_task.done():
            _temp_action_worker_task = asyncio.create_task(temp_action_worker())
        log_msg("✅ Bot initialized successfully", "INFO")
        await notify_owner(
            bot,
            (
                "✅ Bot is running on Koyeb\n"
                f"Port: `{PORT}`\n"
                f"Storage: `{STORAGE_PATH}`\n"
                f"Webhook: `{webhook_status}`\n"
                f"Commands: `{commands_status}`"
            ),
        )
    except Exception as e:
        log_msg(f"❌ Failed to initialize bot: {e}", "ERROR")

@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully shutdown bot"""
    global _temp_action_worker_task
    log_msg("🛑 Server shutting down...", "INFO")
    if _temp_action_worker_task and not _temp_action_worker_task.done():
        _temp_action_worker_task.cancel()
        try:
            await _temp_action_worker_task
        except asyncio.CancelledError:
            pass
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

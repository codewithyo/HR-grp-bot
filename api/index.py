# =========================================================
# ADVANCED TELEGRAM MODERATION BOT — KOYEB VERSION (FIXED)
# =========================================================
#
# ENV VARS required:
#   API_ID  API_HASH  BOT_TOKEN  OWNER_ID  PORT
#   LOG_GROUP_ID      (0 = auto-detect)
#   BACKUP_CHAT_ID    (defaults to LOG_GROUP_ID; must be a
#                      chat the bot can send documents to)
#   STORAGE_PATH      (optional, default /data/modbot)
#   OWNER_DEBUG_NOTIFICATIONS  (1 = enable noisy debug DMs)
# =========================================================

import os, json, time, random, string, asyncio, httpx
import traceback, sys, shutil, io, threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pyrogram import Client, enums
from db import mongo_db
from games import (
    init_games,
    handle_ttt_command,
    handle_ttt_leaderboard,
    handle_ttt_mystats,
    handle_ttt_end,
    handle_ttt_callback,
    games_cleanup_worker,
    shutdown_games,
    TTT_CALLBACK_PREFIXES,
)

# =========================================================
# CACHING LAYER
# =========================================================

class DataCache:
    """Thread-safe in-memory cache with TTL."""
    def __init__(self, ttl_seconds: int = 60):
        self.cache: Dict[str, tuple[Any, float]] = {}
        self.lock = threading.RLock()
        self.ttl = ttl_seconds

    def get(self, key: str) -> Any:
        with self.lock:
            if key not in self.cache:
                return None
            data, ts = self.cache[key]
            if time.time() - ts > self.ttl:
                del self.cache[key]
                return None
            return data

    def set(self, key: str, data: Any):
        with self.lock:
            self.cache[key] = (data, time.time())

    def invalidate(self, key: str = None):
        with self.lock:
            if key:
                self.cache.pop(key, None)
            else:
                self.cache.clear()

    def keys(self):
        with self.lock:
            return list(self.cache.keys())

_cache = DataCache(ttl_seconds=60)

# =========================================================
# BOT COMMANDS MANIFEST
# =========================================================

BOT_COMMANDS = [
    {"command": "start",         "description": "🚀 Start the bot & view welcome message"},
    {"command": "help",          "description": "📖 Display available commands for your role"},
    {"command": "hr",            "description": "🆔 Get user ID & profile information"},
    {"command": "ttt",           "description": "🎮 Play Tic-Tac-Toe"},
    {"command": "tttleaderboard","description": "📊 Tic-Tac-Toe leaderboard"},
    {"command": "tttmystats",    "description": "📈 Your Tic-Tac-Toe stats"},
    {"command": "tttend",        "description": "🏳️ End your Tic-Tac-Toe game"},
    {"command": "happeal",       "description": "📢 Appeal a moderation case (DM only)"},
    {"command": "hauth",         "description": "🔐 Authorize a moderator (Owner)"},
    {"command": "hgrant",        "description": "✅ Grant permission to moderator (Owner)"},
    {"command": "hrevoke",       "description": "❌ Revoke permission from moderator (Owner)"},
    {"command": "hban",          "description": "🚫 Ban a user from group"},
    {"command": "hkick",         "description": "👢 Kick a user from group"},
    {"command": "hmute",         "description": "🔇 Mute a user in group"},
    {"command": "hunban",        "description": "🔓 Unban a user from group"},
    {"command": "hunmute",       "description": "🔊 Unmute a user in group"},
    {"command": "hstats",        "description": "📊 Show group moderation stats"},
    {"command": "hmod",          "description": "👮 List authorized moderators"},
    {"command": "pin",           "description": "📌 Pin replied message"},
    {"command": "unpin",         "description": "📍 Unpin current message"},
    {"command": "adminlist",     "description": "👮 Show all group admins"},
    {"command": "zombies",       "description": "🧟 Scan and kick deleted/bot accounts"},
    {"command": "hwarn",         "description": "⚠️ Issue warning to user"},
    {"command": "hdel",          "description": "🗑️ Delete a message"},
    {"command": "hprotect",      "description": "🛡️ Protect user from moderation"},
    {"command": "hcase",         "description": "📋 View moderation case details"},
    {"command": "hmodinfo",      "description": "👮 View moderator information"},
]

VALID_PERMISSIONS = {"ban", "unban", "mute", "unmute", "kick", "warn", "delete", "pin"}
ACTION_LOG_AUTO_DELETE = 60  # seconds

# =========================================================
# LOGGING
# =========================================================

def log_msg(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# =========================================================
# CONFIG
# =========================================================

API_ID       = int(os.environ.get("API_ID", "0"))
API_HASH     = os.environ.get("API_HASH", "")
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
OWNER_ID     = int(os.environ.get("OWNER_ID", "0"))
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "0"))
PORT         = int(os.environ.get("PORT", "8000"))
OWNER_DEBUG_NOTIFICATIONS = os.environ.get("OWNER_DEBUG_NOTIFICATIONS", "0") == "1"

_log_group_id: int  = LOG_GROUP_ID
_backup_chat_id: int = int(os.environ.get("BACKUP_CHAT_ID", str(LOG_GROUP_ID)))

def get_log_group() -> int:
    return _log_group_id

def get_backup_chat() -> int:
    return _backup_chat_id if _backup_chat_id != 0 else _log_group_id

def resolve_storage_path() -> str:
    preferred = os.environ.get("STORAGE_PATH", "/data/modbot")
    try:
        Path(preferred).mkdir(parents=True, exist_ok=True)
        tf = Path(preferred) / ".write_test"
        tf.write_text("ok")
        tf.unlink(missing_ok=True)
        return preferred
    except Exception:
        fallback = "/tmp/modbot"
        Path(fallback).mkdir(parents=True, exist_ok=True)
        log_msg(f"Storage path '{preferred}' unavailable → falling back to {fallback}", "WARNING")
        return fallback

STORAGE_PATH          = resolve_storage_path()
FALLBACK_STORAGE_PATH = os.environ.get("FALLBACK_STORAGE_PATH", "/tmp/modbot_fallback")
Path(FALLBACK_STORAGE_PATH).mkdir(parents=True, exist_ok=True)

# =========================================================
# VALIDATE CONFIG
# =========================================================

def validate_config():
    errors = []
    if not BOT_TOKEN: errors.append("BOT_TOKEN not set")
    if not API_HASH:  errors.append("API_HASH not set")
    if API_ID == 0:   errors.append("API_ID not set")
    if OWNER_ID == 0: errors.append("OWNER_ID not set")
    if errors:
        for e in errors:
            log_msg(f"  ❌ {e}", "ERROR")
        sys.exit(1)

validate_config()
log_msg(f"CONFIG: API_ID={API_ID} OWNER_ID={OWNER_ID} PORT={PORT} LOG_GROUP={LOG_GROUP_ID}", "INFO")

# =========================================================
# STORAGE FILENAMES
# =========================================================

Path(STORAGE_PATH).mkdir(parents=True, exist_ok=True)

AUTH_FILE         = f"{STORAGE_PATH}/auth.json"
WARN_FILE         = f"{STORAGE_PATH}/warns.json"
CASE_FILE         = f"{STORAGE_PATH}/cases.json"
WARN_CONFIG_FILE  = f"{STORAGE_PATH}/warn_config.json"
PROTECT_FILE      = f"{STORAGE_PATH}/protected.json"
ABUSE_FILE        = f"{STORAGE_PATH}/abuse.json"
TEMP_ACTIONS_FILE = f"{STORAGE_PATH}/temp_actions.json"
APPEALS_FILE      = f"{STORAGE_PATH}/appeals.json"
TTT_SCORES_FILE   = f"{STORAGE_PATH}/ttt_scores.json"
TTT_STATE_FILE    = f"{STORAGE_PATH}/ttt_state.json"

FALLBACK_FILE_MAP = {
    AUTH_FILE:         f"{FALLBACK_STORAGE_PATH}/auth.json",
    WARN_FILE:         f"{FALLBACK_STORAGE_PATH}/warns.json",
    CASE_FILE:         f"{FALLBACK_STORAGE_PATH}/cases.json",
    WARN_CONFIG_FILE:  f"{FALLBACK_STORAGE_PATH}/warn_config.json",
    PROTECT_FILE:      f"{FALLBACK_STORAGE_PATH}/protected.json",
    ABUSE_FILE:        f"{FALLBACK_STORAGE_PATH}/abuse.json",
    TEMP_ACTIONS_FILE: f"{FALLBACK_STORAGE_PATH}/temp_actions.json",
    APPEALS_FILE:      f"{FALLBACK_STORAGE_PATH}/appeals.json",
    TTT_SCORES_FILE:   f"{FALLBACK_STORAGE_PATH}/ttt_scores.json",
    TTT_STATE_FILE:    f"{FALLBACK_STORAGE_PATH}/ttt_state.json",
}

ALL_FILES = list(FALLBACK_FILE_MAP.keys())

# FIX #12 — ABUSE_FILE added to FILE_LABEL
FILE_LABEL = {
    AUTH_FILE:         "auth",
    WARN_FILE:         "warns",
    CASE_FILE:         "cases",
    WARN_CONFIG_FILE:  "warn_config",
    PROTECT_FILE:      "protected",
    ABUSE_FILE:        "abuse",
    TEMP_ACTIONS_FILE: "temp_actions",
    APPEALS_FILE:      "appeals",
    TTT_SCORES_FILE:   "ttt_scores",
    TTT_STATE_FILE:    "ttt_state",
}

MONGO_LOADERS = {
    AUTH_FILE:         mongo_db.load_auth,
    WARN_FILE:         mongo_db.load_warns,
    CASE_FILE:         mongo_db.load_cases,
    WARN_CONFIG_FILE:  mongo_db.load_warn_config,
    PROTECT_FILE:      mongo_db.load_protected,
    ABUSE_FILE:        mongo_db.load_abuse,
    TEMP_ACTIONS_FILE: mongo_db.load_temp_actions,
    APPEALS_FILE:      mongo_db.load_appeals,
    TTT_SCORES_FILE:   mongo_db.load_ttt_scores,
    TTT_STATE_FILE:    mongo_db.load_ttt_state,
}

MONGO_SAVERS = {
    AUTH_FILE:         mongo_db.save_auth,
    WARN_FILE:         mongo_db.save_warns,
    CASE_FILE:         mongo_db.save_cases,
    WARN_CONFIG_FILE:  mongo_db.save_warn_config,
    PROTECT_FILE:      mongo_db.save_protected,
    ABUSE_FILE:        mongo_db.save_abuse,
    TEMP_ACTIONS_FILE: mongo_db.save_temp_actions,
    APPEALS_FILE:      mongo_db.save_appeals,
    TTT_SCORES_FILE:   mongo_db.save_ttt_scores,
    TTT_STATE_FILE:    mongo_db.save_ttt_state,
}

# =========================================================
# STORAGE HELPERS
# =========================================================

def _read_local_json(file: str):
    if not os.path.exists(file):
        return None
    try:
        with open(file, "r") as f:
            return json.load(f)
    except Exception:
        return None

def _write_local_json(file: str, data):
    Path(file).parent.mkdir(parents=True, exist_ok=True)
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

def _is_empty_payload(data) -> bool:
    return data is None or data == {} or data == []

def ensure_json_file(path: str):
    if os.path.exists(path):
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({}, f)

def init_storage_files():
    for primary, fallback in FALLBACK_FILE_MAP.items():
        ensure_json_file(fallback)
        if not os.path.exists(primary):
            try:
                shutil.copy2(fallback, primary)
                log_msg(f"Bootstrapped {primary} from fallback", "INFO")
            except Exception:
                ensure_json_file(primary)
        else:
            try:
                shutil.copy2(primary, fallback)
            except Exception as e:
                log_msg(f"WARNING syncing fallback: {e}", "WARNING")

init_storage_files()
log_msg(f"Storage: {STORAGE_PATH}  Fallback: {FALLBACK_STORAGE_PATH}", "INFO")

# =========================================================
# JSON HELPERS
# =========================================================

def load(file: str):
    cached = _cache.get(file)
    if cached is not None:
        return cached
    try:
        data = None
        if os.path.exists(file):
            try:
                with open(file, "r") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                data = None
        if _is_empty_payload(data):
            loader = MONGO_LOADERS.get(file)
            if loader and mongo_db.is_connected():
                try:
                    remote = loader()
                    if not _is_empty_payload(remote):
                        data = remote
                        _write_local_json(file, remote)
                except Exception:
                    pass
        if _is_empty_payload(data):
            fallback = FALLBACK_FILE_MAP.get(file)
            if fallback and os.path.exists(fallback):
                try:
                    with open(fallback, "r") as f:
                        data = json.load(f)
                    if not _is_empty_payload(data):
                        _write_local_json(file, data)
                        log_msg(f"Recovered {file} from fallback", "WARNING")
                except Exception:
                    pass
        result = data if not _is_empty_payload(data) else {}
        _cache.set(file, result)
        return result
    except Exception as e:
        log_msg(f"ERROR loading {file}: {e}", "ERROR")
        return {}

def save(file: str, data):
    try:
        _cache.set(file, data)
        if file == AUTH_FILE:
            for key in _cache.keys():
                if key.startswith(("auth:", "perm:", "frozen:")):
                    _cache.invalidate(key)
        elif file == PROTECT_FILE:
            for key in _cache.keys():
                if key.startswith("protected:"):
                    _cache.invalidate(key)
        tmp = f"{file}.tmp"
        try:
            Path(file).parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, file)
        except Exception as e:
            log_msg(f"WARNING writing {file}: {e}", "WARNING")
        fallback = FALLBACK_FILE_MAP.get(file)
        if fallback:
            try:
                Path(fallback).parent.mkdir(parents=True, exist_ok=True)
                with open(f"{fallback}.tmp", "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(f"{fallback}.tmp", fallback)
            except Exception:
                pass
        saver = MONGO_SAVERS.get(file)
        if saver and mongo_db.is_connected():
            try:
                saver(data)
            except Exception:
                pass
    except Exception as e:
        log_msg(f"ERROR saving {file}: {e}", "ERROR")

def sync_storage_with_mongo():
    if not mongo_db.is_connected():
        return
    for file, loader in MONGO_LOADERS.items():
        local_data  = _read_local_json(file)
        remote_data = loader()
        if not _is_empty_payload(local_data):
            saver = MONGO_SAVERS.get(file)
            if saver:
                saver(local_data)
        elif not _is_empty_payload(remote_data):
            _write_local_json(file, remote_data)

# =========================================================
# PERSISTENT HTTP CLIENT
# =========================================================

_http_client: httpx.AsyncClient = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=15,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client

# =========================================================
# BOT API HELPER
# =========================================================

async def tg_api(method: str, **kwargs) -> dict:
    try:
        client = await get_http_client()
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            **kwargs,
        )
        return resp.json()
    except Exception as e:
        log_msg(f"tg_api/{method} error: {e}", "ERROR")
        return {"ok": False, "description": str(e)}

async def tg_send(
    chat_id: int,
    text: str,
    reply_to: int = None,
    markup: dict = None,
    parse_mode: str = "Markdown",
) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if markup:
        payload["reply_markup"] = markup
    return await tg_api("sendMessage", json=payload)

async def tg_answer_cb(cb_id: str, text: str, alert: bool = False):
    await tg_api("answerCallbackQuery", json={
        "callback_query_id": cb_id,
        "text": text,
        "show_alert": alert,
    })

async def tg_delete(chat_id: int, message_id: int):
    await tg_api("deleteMessage", json={"chat_id": chat_id, "message_id": message_id})

# =========================================================
# MODERATION API WRAPPERS
# =========================================================

_MUTE_PERMISSIONS = {
    "can_send_messages":         False,
    "can_send_audios":           False,
    "can_send_documents":        False,
    "can_send_photos":           False,
    "can_send_videos":           False,
    "can_send_video_notes":      False,
    "can_send_voice_notes":      False,
    "can_send_polls":            False,
    "can_send_other_messages":   False,
    "can_add_web_page_previews": False,
}

_FULL_PERMISSIONS = {
    "can_send_messages":         True,
    "can_send_audios":           True,
    "can_send_documents":        True,
    "can_send_photos":           True,
    "can_send_videos":           True,
    "can_send_video_notes":      True,
    "can_send_voice_notes":      True,
    "can_send_polls":            True,
    "can_send_other_messages":   True,
    "can_add_web_page_previews": True,
    "can_invite_users":          True,
}

async def api_ban(chat_id: int, user_id: int, until_date: int = None) -> tuple[bool, str]:
    payload = {"chat_id": chat_id, "user_id": user_id}
    if until_date:
        payload["until_date"] = until_date
    r = await tg_api("banChatMember", json=payload)
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_unban(chat_id: int, user_id: int) -> tuple[bool, str]:
    r = await tg_api("unbanChatMember", json={
        "chat_id": chat_id, "user_id": user_id, "only_if_banned": True,
    })
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_mute(chat_id: int, user_id: int, until_date: int = None) -> tuple[bool, str]:
    payload = {"chat_id": chat_id, "user_id": user_id, "permissions": _MUTE_PERMISSIONS}
    if until_date:
        payload["until_date"] = until_date
    r = await tg_api("restrictChatMember", json=payload)
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_unmute(chat_id: int, user_id: int) -> tuple[bool, str]:
    r = await tg_api("restrictChatMember", json={
        "chat_id": chat_id, "user_id": user_id, "permissions": _FULL_PERMISSIONS,
    })
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_pin(chat_id: int, message_id: int) -> tuple[bool, str]:
    r = await tg_api("pinChatMessage", json={"chat_id": chat_id, "message_id": message_id})
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_unpin(chat_id: int) -> tuple[bool, str]:
    r = await tg_api("unpinChatMessage", json={"chat_id": chat_id})
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_kick(chat_id: int, user_id: int) -> tuple[bool, str]:
    banned, err = await api_ban(chat_id, user_id)
    if not banned:
        return False, err
    unbanned, err = await api_unban(chat_id, user_id)
    if not unbanned:
        return False, err
    return True, ""

async def api_delete_msg(chat_id: int, message_id: int) -> tuple[bool, str]:
    r = await tg_api("deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

# =========================================================
# TELEGRAM BACKUP
# =========================================================

_tg_backup_enabled = False

async def upload_backup(label: str, data) -> bool:
    backup_chat = get_backup_chat()
    if not _tg_backup_enabled or backup_chat == 0:
        return False
    try:
        content = json.dumps(data, indent=2).encode()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": backup_chat, "caption": f"MODBOT_BACKUP:{label}"},
                files={"document": (f"{label}.json", io.BytesIO(content), "application/json")},
            )
            return resp.json().get("ok", False)
    except Exception as e:
        log_msg(f"Backup upload failed ({label}): {e}", "WARNING")
        return False

# FIX #3/#4 — all critical files go through save_and_backup
_BACKUP_LABELS = {"auth", "warns", "cases", "protected", "abuse", "temp_actions", "appeals"}

async def save_and_backup(file: str, data):
    save(file, data)
    label = FILE_LABEL.get(file)
    if label and label in _BACKUP_LABELS:
        asyncio.create_task(upload_backup(label, data))

async def restore_from_telegram_pyrogram(bot: Client) -> int:
    if not _tg_backup_enabled:
        return 0
    backup_chat = get_backup_chat()
    if backup_chat == 0:
        return 0
    label_to_file = {v: k for k, v in FILE_LABEL.items()}
    restored_count = 0
    seen_labels: set[str] = set()
    try:
        try:
            await bot.get_chat(backup_chat)
        except Exception as e:
            log_msg(f"Backup restore skipped: chat {backup_chat} not accessible ({e})", "WARNING")
            return 0
        async for msg in bot.get_chat_history(backup_chat, limit=500):
            caption = (msg.caption or "") + (msg.text or "")
            if not caption.startswith("MODBOT_BACKUP:"):
                continue
            label = caption.replace("MODBOT_BACKUP:", "").strip()
            if label in seen_labels or label not in label_to_file:
                continue
            seen_labels.add(label)
            target_file = label_to_file[label]
            if os.path.exists(target_file):
                try:
                    with open(target_file, "r") as f:
                        existing = json.load(f)
                    if existing:
                        continue
                except Exception:
                    pass
            if msg.document:
                try:
                    file_bytes = await bot.download_media(msg, in_memory=True)
                    if file_bytes:
                        data = json.loads(bytes(file_bytes.getvalue()))
                        save(target_file, data)
                        log_msg(f"✅ Restored {label} from Telegram backup", "INFO")
                        restored_count += 1
                except Exception as e:
                    log_msg(f"Failed to restore {label}: {e}", "WARNING")
            if len(seen_labels) == len(label_to_file):
                break
    except Exception as e:
        log_msg(f"restore_from_telegram_pyrogram skipped: {e}", "WARNING")
    return restored_count

# =========================================================
# PERMISSION CHECKS
# =========================================================

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_authorized(uid: int) -> bool:
    if is_owner(uid):
        return True
    cache_key = f"auth:{uid}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    is_auth = str(uid) in load(AUTH_FILE)
    _cache.set(cache_key, is_auth)
    return is_auth

def has_permission(uid: int, perm: str) -> bool:
    if is_owner(uid):
        return True
    cache_key = f"perm:{uid}:{perm}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    result = load(AUTH_FILE).get(str(uid), {}).get("permissions", {}).get(perm, False)
    _cache.set(cache_key, result)
    return result

def is_frozen(uid: int) -> bool:
    if is_owner(uid):
        return False
    cache_key = f"frozen:{uid}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    result = bool(load(AUTH_FILE).get(str(uid), {}).get("frozen", False))
    _cache.set(cache_key, result)
    return result

# =========================================================
# UTILITY HELPERS
# =========================================================

def generate_mod_id() -> str:
    return "MOD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))

def get_mod_info(uid: int) -> dict:
    return load(AUTH_FILE).get(str(uid), {})

def is_protected(uid: int) -> bool:
    cache_key = f"protected:{uid}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    result = str(uid) in load(PROTECT_FILE)
    _cache.set(cache_key, result)
    return result

def make_mention(user: dict) -> str:
    if not isinstance(user, dict):
        return "User"
    uid  = user.get("id")
    name = ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip() or "User"
    return f"[{name}](tg://user?id={uid})" if uid else name

def extract_actor_user_id(msg: dict) -> tuple[int | None, str | None, bool]:
    if not isinstance(msg, dict):
        return None, "❌ Could not identify your Telegram account.", False
    sender_chat = msg.get("sender_chat")
    chat        = msg.get("chat") or {}
    chat_id     = chat.get("id")
    chat_type   = chat.get("type")
    if isinstance(sender_chat, dict):
        sender_chat_id = sender_chat.get("id")
        if sender_chat_id == chat_id and chat_type in ("group", "supergroup"):
            return -abs(int(chat_id)), None, True
        return None, "❌ Channel messages are not supported for moderation commands.", False
    from_user = msg.get("from")
    if isinstance(from_user, dict):
        uid = from_user.get("id")
        if isinstance(uid, int) and uid > 0:
            return uid, None, False
    return None, "❌ Could not identify your Telegram account.", False

def extract_reply_user(reply: dict) -> tuple[dict, int | None]:
    if not isinstance(reply, dict):
        return {}, None
    target = reply.get("from")
    if not isinstance(target, dict):
        return {}, None
    tid = target.get("id")
    if not isinstance(tid, int) or tid <= 0:
        return target, None
    return target, tid

def parse_positive_user_id(value: str) -> int | None:
    try:
        uid = int(value.strip())
        return uid if uid > 0 else None
    except Exception:
        return None

def resolve_target(reply, args, idx) -> tuple[dict, int | None, str | None]:
    target, tid = extract_reply_user(reply)
    if tid:
        return target, tid, None
    if len(args) > idx:
        uid = parse_positive_user_id(args[idx])
        if uid:
            return {"id": uid, "first_name": "User"}, uid, None
        return {}, None, "❌ Invalid user ID."
    return {}, None, "❌ Reply to a user or pass their user ID."

def extract_reason(args, start, default="No Reason") -> str:
    return " ".join(args[start:]).strip() or default if len(args) > start else default

def create_case(action, moderator, target, reason, extra: dict | None = None) -> str:
    cases = load(CASE_FILE)
    cid   = str(len(cases) + 1)
    cases[cid] = {
        "action": action, "moderator": moderator,
        "target": target, "reason": reason,
        "time": str(datetime.now()),
    }
    if extra:
        cases[cid].update(extra)
    save(CASE_FILE, cases)
    return cid

def load_temp_actions() -> list:
    d = load(TEMP_ACTIONS_FILE)
    return d if isinstance(d, list) else []

def save_temp_actions(actions: list):
    save(TEMP_ACTIONS_FILE, actions)

# FIX #6 — shared helper to cancel pending temp actions
def cancel_temp_action(action_type: str, chat_id: int, target_id: int):
    actions  = load_temp_actions()
    filtered = [
        a for a in actions
        if not (
            a.get("type") == action_type
            and a.get("chat_id") == chat_id
            and a.get("target_id") == target_id
        )
    ]
    if len(filtered) != len(actions):
        save_temp_actions(filtered)

def get_warn_config() -> dict:
    config = load(WARN_CONFIG_FILE)
    if not isinstance(config, dict):
        config = {}
    config.setdefault("threshold", 3)
    config.setdefault("action", "mute")
    config.setdefault("duration", 3600)
    return config

def save_warn_config(config: dict):
    save(WARN_CONFIG_FILE, config)

def schedule_message_delete(chat_id: int, message_id: int, delay: int = 60):
    actions = load_temp_actions()
    actions.append({
        "type":     "delete",
        "chat_id":  chat_id,
        "target_id": message_id,
        "until_ts": int(time.time()) + delay,
    })
    save_temp_actions(actions)

def parse_duration_token(token: str) -> int | None:
    if not token or len(token) < 2:
        return None
    token = token.strip().lower()
    num, unit = token[:-1], token[-1]
    if not num.isdigit() or unit not in "smhd":
        return None
    v = int(num)
    return v * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] if v > 0 else None

def parse_duration_and_reason(args, start, default="No Reason"):
    if len(args) <= start:
        return None, default
    dur = parse_duration_token(args[start])
    if dur is not None:
        return dur, extract_reason(args, start + 1, default)
    return None, extract_reason(args, start, default)

def format_duration(secs: int) -> str:
    for divisor, suffix in [(86400, "d"), (3600, "h"), (60, "m")]:
        if secs % divisor == 0:
            return f"{secs // divisor}{suffix}"
    return f"{secs}s"

def format_timestamp(ts: int | None) -> str:
    if not ts:
        return "N/A"
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")

def format_permission_set(perms: dict) -> str:
    granted = [p for p, enabled in sorted(perms.items()) if enabled]
    return ", ".join(granted) if granted else "none"

def format_admin_privileges(privileges) -> str:
    fields = [
        "can_change_info", "can_delete_messages", "can_restrict_members",
        "can_promote_members", "can_manage_video_chats", "can_post_messages",
        "can_edit_messages", "is_anonymous", "can_invite_users",
        "can_pin_messages", "can_manage_chat", "can_manage_topics",
    ]
    if not privileges:
        return "none"
    granted = [
        f.replace("can_", "").replace("_", " ")
        for f in fields if getattr(privileges, f, False)
    ]
    return ", ".join(granted) if granted else "none"

# FIX #5 — UNBAN/UNMUTE/GRANT/REVOKE counted in stats
def get_moderation_stats() -> dict:
    cases = load(CASE_FILE)
    if not isinstance(cases, dict):
        cases = {}
    counts: dict[str, int] = {
        "BAN": 0, "UNBAN": 0, "MUTE": 0, "UNMUTE": 0,
        "WARN": 0, "KICK": 0, "DELETE": 0,
    }
    moderator_counts: dict[str, int] = {}
    for case in cases.values():
        if not isinstance(case, dict):
            continue
        action    = str(case.get("action", "")).upper()
        moderator = str(case.get("moderator", "UNKNOWN"))
        if action in counts:
            counts[action] += 1
            moderator_counts[moderator] = moderator_counts.get(moderator, 0) + 1
    top_mods = sorted(moderator_counts.items(), key=lambda x: (-x[1], x[0]))[:5]
    return {
        "counts":        counts,
        "top_mods":      top_mods,
        "total_actions": sum(counts.values()),
    }

def build_moderator_list_text() -> str:
    auth = load(AUTH_FILE)
    if not isinstance(auth, dict):
        auth = {}
    lines = ["👮 **Authorized Moderators**\n", "👑 Owner: all permissions\n"]
    if not auth:
        lines.append("No authorized moderators found.")
        return "\n".join(lines)
    sorted_mods = sorted(
        auth.items(),
        key=lambda item: int(item[0]) if str(item[0]).isdigit() else 10**18,
    )
    for uid_str, mod in sorted_mods:
        if not isinstance(mod, dict):
            continue
        perms = mod.get("permissions", {}) if isinstance(mod.get("permissions"), dict) else {}
        lines.append(
            f"• `{uid_str}` | {mod.get('mod_id', 'N/A')} | {mod.get('badge', '🛡 Moderator')} | "
            f"{'🔴 Frozen' if mod.get('frozen') else '🟢 Active'} | perms: {format_permission_set(perms)}"
        )
    return "\n".join(lines)

# FIX #5 — display UNBAN/UNMUTE in stats
def build_stats_text() -> str:
    stats  = get_moderation_stats()
    counts = stats["counts"]
    top_mods = stats["top_mods"]
    lines = [
        "📊 **Group Moderation Stats**\n",
        f"🚫 Total bans:    `{counts['BAN']}`",
        f"🔓 Total unbans:  `{counts['UNBAN']}`",
        f"🔇 Total mutes:   `{counts['MUTE']}`",
        f"🔊 Total unmutes: `{counts['UNMUTE']}`",
        f"⚠️ Total warns:   `{counts['WARN']}`",
        f"👢 Total kicks:   `{counts['KICK']}`",
        f"🗑️ Total deletes: `{counts['DELETE']}`",
        f"🧮 Total actions: `{stats['total_actions']}`",
        "",
        "👮 **Most Active Moderators**",
    ]
    if top_mods:
        for idx, (moderator, count) in enumerate(top_mods, start=1):
            mod_info = get_mod_info(int(moderator)) if moderator.isdigit() else {}
            label    = mod_info.get("mod_id", moderator) if mod_info else moderator
            lines.append(f"{idx}. `{label}` — `{count}` actions")
    else:
        lines.append("No moderation activity yet.")
    return "\n".join(lines)

async def build_admin_list_text(bot: Client, chat_id: int) -> str:
    lines = ["👮 **Group Administrators**\n"]
    try:
        async for member in bot.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            user = getattr(member, "user", None)
            if not user:
                continue
            title     = getattr(member, "rank", None) or getattr(member, "custom_title", None) or "Administrator"
            status    = "owner" if member.status == enums.ChatMemberStatus.OWNER else "admin"
            username  = f"@{user.username}" if getattr(user, "username", None) else "—"
            full_name = (user.first_name or "User") + (f" {user.last_name}" if getattr(user, "last_name", None) else "")
            lines.append(
                f"• `{user.id}` | {full_name} | {username} | {title} | {status}"
                f" | perms: {format_admin_privileges(getattr(member, 'privileges', None))}"
            )
    except Exception as e:
        return f"❌ Could not load admins: {e}"
    if len(lines) == 1:
        lines.append("No administrators found.")
    return "\n".join(lines)

async def scan_zombies(bot: Client, chat_id: int) -> tuple[int, int, list[str]]:
    kicked_deleted = 0
    kicked_bots    = 0
    failures: list[str] = []
    async for member in bot.get_chat_members(chat_id):
        user = getattr(member, "user", None)
        if not user:
            continue
        if member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR):
            continue
        is_deleted = bool(getattr(user, "is_deleted", False))
        is_bot     = bool(getattr(user, "is_bot", False))
        if not is_deleted and not is_bot:
            continue
        ok, err = await api_kick(chat_id, user.id)
        if ok:
            if is_deleted: kicked_deleted += 1
            if is_bot:     kicked_bots    += 1
        else:
            failures.append(f"{user.id}: {err}")
    return kicked_deleted, kicked_bots, failures

def schedule_temp_action(
    action_type: str, chat_id: int, target_id: int,
    until_ts: int, set_by: int, reason: str, case_id: str | None = None,
):
    actions = load_temp_actions()
    action  = {
        "type": action_type, "chat_id": chat_id,
        "target_id": target_id, "until_ts": until_ts,
        "set_by": set_by, "reason": reason,
    }
    if case_id:
        action["case_id"] = case_id
    actions.append(action)
    save_temp_actions(actions)

def track_action(uid: int) -> int:
    data = load(ABUSE_FILE)
    key  = str(uid)
    now  = time.time()
    data.setdefault(key, [])
    data[key] = [x for x in data[key] if now - x <= 60]
    data[key].append(now)
    save(ABUSE_FILE, data)
    return len(data[key])

def create_appeal(uid: int, case_id: str, message: str) -> str:
    appeals = load(APPEALS_FILE)
    if not isinstance(appeals, dict):
        appeals = {}
    aid = str(len(appeals) + 1)
    appeals[aid] = {
        "case_id": case_id, "user_id": uid,
        "message": message, "status": "open",
        "time": str(datetime.now()),
    }
    # FIX #3 — save done via caller with save_and_backup
    save(APPEALS_FILE, appeals)
    return aid

def role_help_text(uid: int) -> str:
    if is_owner(uid):
        return (
            "╔════════════════════════════════════════╗\n"
            "║  👑 OWNER COMMAND REFERENCE            ║\n"
            "╚════════════════════════════════════════╝\n\n"
            "🔐 **Authorization Commands:**\n"
            "`/hauth <user_id>` - Authorize a moderator\n"
            "`/hrevoke <perm> <user_id>` - Remove permission\n"
            "`/hgrant <perm> <user_id>` - Grant permission\n"
            "Permissions: ban, unban, mute, unmute, kick, warn, delete, pin\n\n"
            "🛡️ **Protection Commands:**\n"
            "`/hprotect <user_id>` - Protect user from moderation\n\n"
            "📋 **Moderation Commands:**\n"
            "`/hban [user_id] [duration] [reason]` - Ban user\n"
            "`/hkick <user_id> [reason]` - Kick user from group\n"
            "`/hmute [user_id] [duration] [reason]` - Mute user\n"
            "`/hunban [user_id] [reason]` - Unban a user\n"
            "`/hunmute [user_id] [reason]` - Unmute a user\n"
            "`/hstats` - Show moderation stats\n"
            "`/hmod list` - List authorized moderators\n"
            "`/hwarn [user_id] [reason]` - Warn user\n"
            "`/hdel` - Delete replied message\n"
            "`/hcase <case_id>` - View case details\n"
            "`/hmodinfo [user_id]` - View moderator info\n\n"
            "🎮 **Games:**\n"
            "`/ttt [user_id]` - Start Tic-Tac-Toe\n"
            "`/tttleaderboard` - Show top players\n"
            "`/tttmystats` - Show your game stats\n"
            "`/tttend` - Forfeit active game\n\n"
            "⏱️ **Duration Format:** 30m, 2h, 1d\n\n"
            "💡 **Tip:** Reply to a message to target without ID\n\n"
            "📖 **Examples:**\n"
            "`/hban @user 2h spam` - Ban for 2 hours\n"
            "`/hmute 123456789 30m abuse` - Mute for 30 mins\n"
            "`/hwarn @user off-topic` - Issue warning\n"
        )
    if is_authorized(uid):
        return (
            "╔════════════════════════════════════════╗\n"
            "║  👮 MODERATOR COMMAND REFERENCE       ║\n"
            "╚════════════════════════════════════════╝\n\n"
            "🚫 **Moderation Commands:**\n"
            "`/hban [user_id] [duration] [reason]` - Ban user\n"
            "`/hkick <user_id> [reason]` - Kick user from group\n"
            "`/hmute [user_id] [duration] [reason]` - Mute user\n"
            "`/hunban [user_id] [reason]` - Unban a user\n"
            "`/hunmute [user_id] [reason]` - Unmute a user\n"
            "`/hstats` - Show moderation stats\n"
            "`/hwarn [user_id] [reason]` - Warn user\n"
            "`/hdel` - Delete replied message\n\n"
            "📋 **Information Commands:**\n"
            "`/hcase <case_id>` - View case details\n"
            "`/hmod list` - List authorized moderators\n"
            "`/hmodinfo` - View your moderator info\n"
            "`/hr` - Get user information\n\n"
            "🎮 **Games:**\n"
            "`/ttt [user_id]` - Start Tic-Tac-Toe\n"
            "`/tttleaderboard` - Show top players\n"
            "`/tttmystats` - Show your game stats\n"
            "`/tttend` - Forfeit active game\n\n"
            "⏱️ **Duration Format:** 30m, 2h, 1d\n\n"
            "💡 **Tip:** Reply to a message to target without ID\n\n"
            "📖 **Examples:**\n"
            "`/hban @user 30m spam` - Ban for 30 minutes\n"
            "`/hwarn 123456789 off-topic` - Issue warning\n"
        )
    return (
        "╔════════════════════════════════════════╗\n"
        "║  👤 USER COMMAND REFERENCE            ║\n"
        "╚════════════════════════════════════════╝\n\n"
        "🆔 **Available Commands:**\n"
        "`/start` - View welcome message\n"
        "`/help` - Show this help message\n"
        "`/hr` - Get user ID & profile info\n"
        "`/ttt [user_id]` - Play Tic-Tac-Toe\n"
        "`/tttleaderboard` - Show top players\n"
        "`/tttmystats` - Show your game stats\n"
        "`/tttend` - Forfeit active game\n\n"
        "📢 **Appeals:**\n"
        "Disputed a moderation action?\n"
        "Use `/happeal <case_id> <message>` in bot DM\n\n"
        "📞 **Support:**\n"
        "Contact your group administrator for assistance\n\n"
        "✨ *For more features, ask a group moderator*"
    )

# =========================================================
# INLINE KEYBOARD BUILDER
# =========================================================

def build_markup(*rows) -> dict:
    keyboard = []
    for row in rows:
        btn_row = []
        for text, data in row:
            if data.startswith("url:"):
                btn_row.append({"text": text, "url": data[4:]})
            else:
                btn_row.append({"text": text, "callback_data": data.replace("cb:", "")})
        keyboard.append(btn_row)
    return {"inline_keyboard": keyboard}

# =========================================================
# PYROGRAM CLIENT
# =========================================================

_bot: Client       = None
bot_ready: bool    = False
_temp_worker_task  = None
_games_worker_task = None

async def get_bot() -> Client:
    global _bot, bot_ready
    if _bot is None or not _bot.is_connected:
        log_msg("Initializing Pyrogram client...", "INFO")
        _bot = Client(
            name="modbot", api_id=API_ID, api_hash=API_HASH,
            bot_token=BOT_TOKEN, in_memory=True, no_updates=True,
        )
        await _bot.start()
        bot_ready = True
        me = await _bot.get_me()
        log_msg(f"✅ Authenticated as @{me.username}", "INFO")
    return _bot

async def shutdown_bot():
    global _bot, bot_ready
    if _bot:
        try:
            await _bot.stop()
        except Exception:
            pass
        _bot      = None
        bot_ready = False

# =========================================================
# LOG GROUP
# =========================================================

async def detect_admin_groups(bot: Client) -> list[int]:
    _ = bot
    return []

async def resolve_log_group(bot: Client):
    global _log_group_id, _backup_chat_id, _tg_backup_enabled
    if _log_group_id != 0:
        try:
            chat = await bot.get_chat(_log_group_id)
            log_msg(f"✅ Log group confirmed: {chat.title} ({_log_group_id})", "INFO")
            _tg_backup_enabled = True
            if _backup_chat_id == 0:
                _backup_chat_id = _log_group_id
            return
        except Exception as e:
            log_msg(f"⚠️ LOG_GROUP_ID={_log_group_id} inaccessible: {e}", "WARNING")
            _tg_backup_enabled = True
            if _backup_chat_id == 0:
                _backup_chat_id = _log_group_id
            await tg_send(
                OWNER_ID,
                f"⚠️ LOG_GROUP_ID `{_log_group_id}` set but bot can't access that chat.\n"
                "Add the bot as admin to the log group, then restart.",
            )
            return
    groups = await detect_admin_groups(bot)
    if not groups:
        log_msg("⚠️ LOG_GROUP_ID not set. Log messages will be skipped.", "WARNING")
        await tg_send(OWNER_ID, "⚠️ Set LOG_GROUP_ID in Koyeb env vars. Auto-detection unavailable for bots.")
        return
    _log_group_id   = groups[0]
    if _backup_chat_id == 0:
        _backup_chat_id = _log_group_id
    _tg_backup_enabled = True
    try:
        chat = await bot.get_chat(_log_group_id)
        log_msg(f"✅ Auto-detected log group: {chat.title} ({_log_group_id})", "INFO")
        await tg_send(
            OWNER_ID,
            f"ℹ️ Auto-detected log group: **{chat.title}**\n`{_log_group_id}`\n\n"
            f"Set `LOG_GROUP_ID={_log_group_id}` in env vars to make permanent.",
        )
    except Exception:
        log_msg(f"Auto-detected log group: {_log_group_id}", "INFO")

# =========================================================
# WEBHOOK
# =========================================================

async def ensure_webhook(base_url: str = None) -> str:
    url = os.environ.get("WEBHOOK_URL") or os.environ.get("APP_URL") or base_url
    if not url:
        return "skipped:no-url"
    webhook_url = url.rstrip("/") + "/api/webhook"
    info = await tg_api("getWebhookInfo")
    if info.get("result", {}).get("url") == webhook_url:
        return "ok:already-set"
    result = await tg_api("setWebhook", json={
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    return f"ok:set:{webhook_url}" if result.get("ok") else f"error:{result.get('description')}"

async def sync_commands() -> str:
    result = await tg_api("setMyCommands", json={"commands": BOT_COMMANDS})
    return "ok" if result.get("ok") else f"error:{result.get('description')}"

# =========================================================
# ANTI-NUKE
# =========================================================

async def anti_nuke(chat_id: int, reply_to: int, uid: int) -> bool:
    total = track_action(uid)
    if total < 10:
        return False
    auth = load(AUTH_FILE)
    if str(uid) in auth:
        auth[str(uid)]["frozen"] = True
        save(AUTH_FILE, auth)
    lg = get_log_group()
    if lg:
        await tg_send(
            lg,
            f"🚨 **ANTI-NUKE ACTIVATED**\n\n"
            f"Moderator: `{uid}`\n"
            f"Actions in 60 sec: `{total}`\n"
            f"Moderator frozen automatically.",
        )
    await tg_send(chat_id, "🚨 Anti-Nuke triggered — moderator frozen.", reply_to=reply_to)
    return True

# =========================================================
# ACTION LOG
# =========================================================

async def send_action_log(
    source_chat, reply_to, action, target, reason, case_id, mod_data, extra=""
):
    mention   = make_mention(target)
    target_id = target.get("id")
    badge     = mod_data.get("badge", "🛡 Moderator")
    mod_uid   = mod_data.get("mod_id", "UNKNOWN")
    time_now  = datetime.now().strftime("%d %b %Y • %I:%M %p")

    text = (
        f"╭━━〔 🚨 MOD ACTION 〕━━╮\n"
        f"👤 {mention}\n"
        f"🆔 `{target_id}`\n"
        f"⚔ {action}\n"
        f"📝 {reason}\n"
        f"👮 {badge} | {mod_uid}\n"
        f"⏰ {time_now}\n"
        f"📜 #{case_id}\n"
        f"{extra}\n"
        f"╰━━━━━━━━━━━━━━╯"
    )

    rows = []
    if action == "BAN":
        rows.append([("🔓 Unban",       f"cb:unban_{target_id}")])
    elif action == "KICK":
        rows.append([("👤 Profile",     f"url:tg://user?id={target_id}")])
    elif action == "MUTE":
        rows.append([("🔊 Unmute",      f"cb:unmute_{target_id}")])
    elif action == "WARN":
        rows.append([("🗑 Remove Warn", f"cb:removewarn_{target_id}")])
    elif action in ("DELETE", "UNBAN", "UNMUTE"):
        rows.append([("👤 Profile",     f"url:tg://user?id={target_id}")])
    rows.append([("📜 View Case",       f"cb:case_{case_id}")])
    markup = build_markup(*rows)

    resp = await tg_send(source_chat, text, reply_to=reply_to, markup=markup)
    if resp.get("ok") and resp.get("result"):
        msg_id = resp["result"].get("message_id")
        if msg_id:
            schedule_message_delete(source_chat, msg_id, ACTION_LOG_AUTO_DELETE)

    lg = get_log_group()
    if lg and lg != source_chat:
        await tg_send(lg, text, markup=markup)

async def send_grant_log(chat_id, reply_to, granted_by, target, permission, case_id=None):
    case_line = f"\n📜 Case ID: #{case_id}" if case_id else ""
    text = (
        f"✅ **Permission Granted**\n\n"
        f"👤 Moderator: {make_mention(target)}\n"
        f"🔐 Permission: `{permission}`\n"
        f"🛡 Granted by: `{granted_by}`{case_line}"
    )
    resp = await tg_send(chat_id, text, reply_to=reply_to)
    if resp.get("ok") and resp.get("result"):
        msg_id = resp["result"].get("message_id")
        if msg_id:
            schedule_message_delete(chat_id, msg_id, ACTION_LOG_AUTO_DELETE)
    lg = get_log_group()
    if lg and lg != chat_id:
        await tg_send(lg, f"📝 Grant logged\n\n{text}")

async def notify_owner(text: str):
    if OWNER_ID:
        await tg_send(OWNER_ID, text)

# =========================================================
# TEMP ACTION WORKER
# =========================================================

async def process_due_temp_actions(bot: Client):
    actions = load_temp_actions()
    if not actions:
        return
    now_ts  = int(time.time())
    pending = []
    for action in actions:
        until_ts  = int(action.get("until_ts", 0))
        if until_ts > now_ts:
            pending.append(action)
            continue
        chat_id   = action["chat_id"]
        target_id = action["target_id"]
        atype     = action["type"]
        try:
            if atype == "mute":
                ok, err = await api_unmute(chat_id, target_id)
                if ok:
                    msg_text = f"🔊 Temporary mute ended for `{target_id}`\n⏰ Expired: {format_timestamp(until_ts)}"
                    if action.get("case_id"):
                        msg_text += f"\n📜 Case #{action['case_id']}"
                    if action.get("reason"):
                        msg_text += f"\n📝 Reason: {action['reason']}"
                    try:
                        await tg_send(chat_id, msg_text)
                    except Exception as send_err:
                        log_msg(f"temp mute update failed for {target_id}: {send_err}", "WARNING")
                else:
                    pending.append(action)
                    log_msg(f"auto-unmute failed for {target_id}: {err}", "WARNING")
                    continue
            elif atype == "ban":
                ok, err = await api_unban(chat_id, target_id)
                if ok:
                    msg_text = f"🔓 Temporary ban ended for `{target_id}`\n⏰ Expired: {format_timestamp(until_ts)}"
                    if action.get("case_id"):
                        msg_text += f"\n📜 Case #{action['case_id']}"
                    if action.get("reason"):
                        msg_text += f"\n📝 Reason: {action['reason']}"
                    try:
                        await tg_send(chat_id, msg_text)
                    except Exception as send_err:
                        log_msg(f"temp ban update failed for {target_id}: {send_err}", "WARNING")
                else:
                    pending.append(action)
                    log_msg(f"auto-unban failed for {target_id}: {err}", "WARNING")
                    continue
            elif atype == "delete":
                retry_count = action.get("retry_count", 0)
                try:
                    await tg_delete(chat_id, target_id)
                except Exception as del_err:
                    if retry_count < 3:
                        action["retry_count"] = retry_count + 1
                        pending.append(action)
                        log_msg(f"auto-delete failed for msg {target_id} (retry {retry_count + 1}): {del_err}", "WARNING")
                    else:
                        log_msg(f"auto-delete gave up for msg {target_id}: {del_err}", "WARNING")
        except Exception as e:
            log_msg(f"temp action error {action}: {e}", "ERROR")
            pending.append(action)
    if len(pending) != len(actions):
        save_temp_actions(pending)

async def temp_action_worker():
    while True:
        try:
            bot = await get_bot()
            await process_due_temp_actions(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_msg(f"temp_action_worker error: {e}", "ERROR")
        await asyncio.sleep(10)

# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(title="Moderation Bot")

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {
        "service":   "Telegram Moderation Bot",
        "status":    "running" if bot_ready else "starting",
        "endpoints": ["/health", "/api/status", "/api/webhook"],
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "bot_ready": bot_ready, "timestamp": datetime.now().isoformat()}

@app.get("/api/status")
async def bot_status():
    try:
        bot = await get_bot()
        me  = await bot.get_me()
        return {
            "status": "running", "bot_id": me.id,
            "bot_username": me.username,
            "log_group_id": get_log_group(),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/api/setup_webhook")
async def setup_webhook_endpoint():
    return {
        "set_webhook": await ensure_webhook(),
        "commands":    await sync_commands(),
        "info":        await tg_api("getWebhookInfo"),
    }

@app.post("/api/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
        if not isinstance(update, dict):
            return JSONResponse({"ok": False}, status_code=400)
        bot = await get_bot()
        if "message" in update:
            await handle_message(bot, update["message"])
        elif "callback_query" in update:
            await handle_callback(bot, update["callback_query"])
    except Exception as e:
        log_msg(f"webhook error: {e}\n{traceback.format_exc()}", "ERROR")
    return {"ok": True}

# =========================================================
# MESSAGE HANDLER
# =========================================================

async def handle_message(bot: Client, msg: dict):
    try:
        text = msg.get("text", "")
        if not text.startswith("/"):
            return

        chat_id              = msg["chat"]["id"]
        msg_id               = msg["message_id"]
        uid, err, is_anon_admin = extract_actor_user_id(msg)
        reply                = msg.get("reply_to_message") or {}

        parts   = text.split(None, 1)
        raw_cmd = parts[0].split("@")[0].lstrip("/").lower()
        args    = parts[1].split() if len(parts) > 1 else []

        actor_label = f"anon_admin:{chat_id}" if is_anon_admin else str(uid)
        log_msg(f"/{raw_cmd} from actor={actor_label} chat={chat_id}", "INFO")

        is_private = msg.get("chat", {}).get("type") == "private"

        async def reply_text(t: str):
            sent = await tg_send(chat_id, t, reply_to=msg_id)
            if not is_private and sent.get("ok"):
                rmid = sent.get("result", {}).get("message_id")
                if rmid:
                    schedule_message_delete(chat_id, rmid)

        # Auto-delete commands in groups
        if not is_private:
            schedule_message_delete(chat_id, msg_id)

        if err:
            await reply_text(err)
            return

        if OWNER_DEBUG_NOTIFICATIONS and raw_cmd not in ("start", "help"):
            await notify_owner(f"📨 /{raw_cmd}\nActor: `{actor_label}`\nChat: `{chat_id}`")

        # ── Actor helpers ─────────────────────────────────
        def is_owner_actor() -> bool:
            return (not is_anon_admin) and is_owner(uid)

        def is_authorized_actor() -> bool:
            return is_anon_admin or is_authorized(uid)

        def is_frozen_actor() -> bool:
            return False if is_anon_admin else is_frozen(uid)

        def has_permission_actor(perm: str) -> bool:
            return True if is_anon_admin else has_permission(uid, perm)

        def actor_mod_info() -> dict:
            if is_anon_admin:
                return {"badge": "🎭 Anonymous Admin", "mod_id": f"ANON-{abs(chat_id)}"}
            return get_mod_info(uid)

        async def security_fail():
            await tg_delete(chat_id, msg_id)

        async def check_mod(perm: str) -> bool:
            if not is_authorized_actor():
                await security_fail()
                return False
            if is_frozen_actor():
                await reply_text("🚫 Your moderator account is frozen.")
                return False
            if not has_permission_actor(perm):
                await reply_text(f"❌ You don't have `{perm}` permission.")
                return False
            return True

        # ── /start ────────────────────────────────────────
        if raw_cmd == "start":
            await reply_text(
                "🤖 **HR Group Bot**\n\n"
                "Fast moderation + utility tools for your group.\n\n"
                "**Quick Commands**\n"
                "• `/help` - Commands by your role\n"
                "• `/hr` - User info\n"
                "• `/ttt` - Play Tic-Tac-Toe\n"
                "• `/happeal` - Appeal in DM\n\n"
                "✅ Online and ready."
            )
            return

        # ── /help ─────────────────────────────────────────
        if raw_cmd == "help":
            if is_anon_admin:
                # FIX #11 — hunban/hunmute listed for anon admins
                await reply_text(
                    "🎭 **Anonymous Admin Mode**\n\n"
                    "You can use moderation commands while posting anonymously:\n"
                    "`/hban`, `/hkick`, `/hmute`, `/hunban`, `/hunmute`, `/hstats`, "
                    "`/hwarn`, `/hdel`, `/hcase`, `/hmod list`, `/hmodinfo`, `/hr`, "
                    "`/pin`, `/unpin`, `/adminlist`, `/zombies`\n\n"
                    "Owner-only commands (`/hauth`, `/hgrant`, `/hrevoke`, `/hprotect`) "
                    "require a normal account identity."
                )
            else:
                await reply_text(role_help_text(uid))
            return

        # ── /hr ───────────────────────────────────────────
        # FIX #2 — removed all inner `bot = await get_bot()` assignments;
        #           the outer `bot` parameter is used directly throughout.
        if raw_cmd in ("hr", "id"):
            try:
                target_id   = None
                target_user = None

                if args and args[0].lower() == "me":
                    if is_anon_admin:
                        return await reply_text("❌ `me` unavailable in anonymous mode.")
                    target_id = uid

                elif args and args[0].startswith("@"):
                    username = args[0][1:]
                    try:
                        target_user = await bot.get_user(username)
                        target_id   = target_user.id
                    except Exception as e:
                        return await reply_text(f"❌ User @{username} not found: {e}")

                elif args and args[0].isdigit():
                    parsed_id = parse_positive_user_id(args[0])
                    if parsed_id:
                        target_id = parsed_id
                        try:
                            target_user = await bot.get_user(target_id)
                        except Exception as e:
                            return await reply_text(f"❌ User ID {target_id} not found: {e}")
                    else:
                        return await reply_text("❌ Invalid user ID.")

                elif reply:
                    target_dict, tid, terr = resolve_target(reply, [], 0)
                    if tid:
                        target_id = tid
                        try:
                            target_user = await bot.get_user(target_id)
                        except Exception:
                            pass
                    else:
                        return await reply_text("❌ Reply to a user or use /hr @username, /hr me, or /hr <user_id>")
                else:
                    return await reply_text("❌ Reply to a user or use /hr @username, /hr me, or /hr <user_id>")

                if target_id is None:
                    return await reply_text("❌ Could not determine target user.")

                first_name = target_user.first_name if target_user and target_user.first_name else "User"
                last_name  = target_user.last_name  if target_user and target_user.last_name  else ""
                uname      = target_user.username   if target_user and target_user.username   else None

                response  = f"👤 **User Profile**\n\n🆔 ID: `{target_id}`\n📝 Name: {first_name}"
                if last_name:
                    response += f" {last_name}"
                response += "\n"
                if uname:
                    response += f"🔗 Username: @{uname}\n"
                response += f"📌 Link: [Profile](tg://user?id={target_id})"

                if is_owner_actor() and not is_private:
                    await notify_owner(response)
                else:
                    await reply_text(response)
                return
            except Exception as e:
                log_msg(f"Error in /hr: {e}\n{traceback.format_exc()}", "ERROR")
                return await reply_text(f"❌ Error: {e}")

        # ── /ttt commands ─────────────────────────────────
        if raw_cmd in ("ttt", "ttt_game"):
            await handle_ttt_command(bot, msg, args, reply, uid, chat_id, msg_id)
            return
        if raw_cmd == "tttleaderboard":
            await handle_ttt_leaderboard(chat_id, msg_id)
            return
        if raw_cmd == "tttmystats":
            await handle_ttt_mystats(uid, chat_id, msg_id)
            return
        if raw_cmd == "tttend":
            await handle_ttt_end(uid, chat_id, msg_id, is_owner=is_owner_actor())
            return

        # ── /happeal ──────────────────────────────────────
        if raw_cmd == "happeal":
            if not is_private:
                return await reply_text("❌ Use /happeal in bot DM only.")
            if len(args) < 2:
                return await reply_text("Usage: /happeal <case_id> <message>")
            case_id    = args[0]
            appeal_msg = " ".join(args[1:]).strip()
            if not appeal_msg:
                return await reply_text("❌ Appeal message is required.")
            cases = load(CASE_FILE)
            if case_id not in cases:
                return await reply_text("❌ Case not found.")
            if int(cases[case_id].get("target", 0)) != uid and not is_owner_actor():
                return await reply_text("❌ You can only appeal your own case.")
            aid = create_appeal(uid, case_id, appeal_msg)
            # FIX #3 — backup appeals after creation
            asyncio.create_task(upload_backup("appeals", load(APPEALS_FILE)))
            await reply_text(f"✅ Appeal submitted. Appeal ID: #{aid}")
            await notify_owner(f"📨 New appeal #{aid}\nCase: #{case_id}\nUser: `{uid}`\n{appeal_msg}")
            return

        # ── /hauth ────────────────────────────────────────
        if raw_cmd in ("hauth", "ha"):
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hauth <user_id>")
            data = load(AUTH_FILE)
            key  = str(tid)
            if key in data:
                return await reply_text(
                    f"ℹ️ {make_mention(target)} already authorized.\n"
                    f"Mod ID: `{data[key].get('mod_id', 'N/A')}`"
                )
            data[key] = {
                "permissions": {}, "mod_id": generate_mod_id(),
                "badge": "🛡 Moderator", "frozen": False,
            }
            await save_and_backup(AUTH_FILE, data)
            await reply_text(f"✅ {make_mention(target)} authorized.\nMod ID: `{data[key]['mod_id']}`")
            lg = get_log_group()
            if lg:
                await tg_send(lg, f"✅ Moderator authorized\n👤 {make_mention(target)} (`{tid}`)\n🛡 By: `{uid}`")
            return

        # ── /hgrant ───────────────────────────────────────
        if raw_cmd in ("hgrant", "hg"):
            if not is_owner_actor():
                return
            if not args:
                return await reply_text(
                    f"Usage: /hgrant <permission> <user_id>\n"
                    f"Valid: {', '.join(sorted(VALID_PERMISSIONS))}"
                )
            perm = args[0].lower()
            if perm not in VALID_PERMISSIONS:
                return await reply_text(f"❌ Invalid permission. Valid: {', '.join(sorted(VALID_PERMISSIONS))}")
            target, tid, terr = resolve_target(reply, args, 1)
            if not tid:
                return await reply_text(terr)
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User not authorized. Run /hauth first.")
            data[str(tid)]["permissions"][perm] = True
            await save_and_backup(AUTH_FILE, data)
            case_id = create_case("GRANT", uid, tid, f"Granted: {perm}")
            await send_grant_log(chat_id, msg_id, uid, target, perm, case_id)
            return

        # ── /hrevoke ──────────────────────────────────────
        if raw_cmd in ("hrevoke", "hrev"):
            if not is_owner_actor():
                return
            if not args:
                return await reply_text("Usage: /hrevoke <permission> <user_id>")
            perm = args[0].lower()
            if perm not in VALID_PERMISSIONS:
                return await reply_text(f"❌ Invalid. Valid: {', '.join(sorted(VALID_PERMISSIONS))}")
            target, tid, terr = resolve_target(reply, args, 1)
            if not tid:
                return await reply_text(terr)
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User is not a moderator.")
            data[str(tid)]["permissions"][perm] = False
            await save_and_backup(AUTH_FILE, data)
            case_id = create_case("REVOKE", uid, tid, f"Revoked: {perm}")
            await reply_text(f"✅ Revoked `{perm}` from {make_mention(target)}")
            lg = get_log_group()
            if lg:
                await tg_send(
                    lg,
                    f"📝 Permission Revoked\n\n"
                    f"👤 {make_mention(target)}\n🔐 `{perm}`\n"
                    f"🛡 By: `{uid}`\n📜 Case #{case_id}",
                )
            return

        # ── /hban ─────────────────────────────────────────
        if raw_cmd in ("hban", "hb"):
            if not await check_mod("ban"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hban <user_id> [duration] [reason]")
            rs = 0 if reply else 1
            dur, reason = parse_duration_and_reason(args, rs)
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            until_ts = int(time.time()) + dur if dur else None
            ok, err  = await api_ban(chat_id, tid, until_date=until_ts)
            if not ok:
                return await reply_text(f"❌ Ban failed: {err}")
            if dur:
                reason = f"{reason} | Duration: {format_duration(dur)}"
            case_id = create_case("BAN", uid, tid, reason, extra={
                "temporary": bool(dur), "duration": dur, "expires_at": until_ts,
            })
            if dur:
                schedule_temp_action("ban", chat_id, tid, until_ts, uid, reason, case_id=case_id)
            await send_action_log(chat_id, msg_id, "BAN", target, reason, case_id, actor_mod_info())
            return

        # ── /hkick ────────────────────────────────────────
        if raw_cmd in ("hkick", "hk"):
            if not await check_mod("kick"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hkick <user_id> [reason]")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No Reason")
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            ok, err = await api_kick(chat_id, tid)
            if not ok:
                return await reply_text(f"❌ Kick failed: {err}")
            case_id = create_case("KICK", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "KICK", target, reason, case_id, actor_mod_info())
            return

        # ── /hmute ────────────────────────────────────────
        if raw_cmd in ("hmute", "hm"):
            if not await check_mod("mute"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hmute <user_id> [duration] [reason]")
            rs = 0 if reply else 1
            dur, reason = parse_duration_and_reason(args, rs)
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            until_ts = int(time.time()) + dur if dur else None
            ok, err  = await api_mute(chat_id, tid, until_date=until_ts)
            if not ok:
                return await reply_text(f"❌ Mute failed: {err}")
            if dur:
                reason = f"{reason} | Duration: {format_duration(dur)}"
            case_id = create_case("MUTE", uid, tid, reason, extra={
                "temporary": bool(dur), "duration": dur, "expires_at": until_ts,
            })
            if dur:
                schedule_temp_action("mute", chat_id, tid, until_ts, uid, reason, case_id=case_id)
            await send_action_log(chat_id, msg_id, "MUTE", target, reason, case_id, actor_mod_info())
            return

        # ── /hunban ───────────────────────────────────────
        if raw_cmd in ("hunban", "hub"):
            if not await check_mod("unban"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunban <user_id> [reason]")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No reason given")
            ok, err = await api_unban(chat_id, tid)
            if not ok:
                return await reply_text(f"❌ Unban failed: {err}")
            # FIX #6 — cancel any pending temp ban
            cancel_temp_action("ban", chat_id, tid)
            case_id = create_case("UNBAN", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "UNBAN", target, reason, case_id, actor_mod_info())
            return

        # ── /hunmute ──────────────────────────────────────
        if raw_cmd in ("hunmute", "hum"):
            if not await check_mod("unmute"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunmute <user_id> [reason]")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No reason given")
            ok, err = await api_unmute(chat_id, tid)
            if not ok:
                return await reply_text(f"❌ Unmute failed: {err}")
            # FIX #6 — cancel any pending temp mute
            cancel_temp_action("mute", chat_id, tid)
            case_id = create_case("UNMUTE", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "UNMUTE", target, reason, case_id, actor_mod_info())
            return

        # ── /pin ──────────────────────────────────────────
        if raw_cmd == "pin":
            if not await check_mod("pin"):
                return
            if not reply:
                return await reply_text("❌ Reply to the message you want to pin.")
            reply_msg_id = reply.get("message_id")
            if not reply_msg_id:
                return await reply_text("❌ Could not determine the replied message.")
            ok, err = await api_pin(chat_id, reply_msg_id)
            if not ok:
                return await reply_text(f"❌ Pin failed: {err}")
            await reply_text("📌 Message pinned.")
            return

        # ── /unpin ────────────────────────────────────────
        if raw_cmd == "unpin":
            if not await check_mod("pin"):
                return
            ok, err = await api_unpin(chat_id)
            if not ok:
                return await reply_text(f"❌ Unpin failed: {err}")
            await reply_text("📍 Pinned message removed.")
            return

        # ── /adminlist ────────────────────────────────────
        if raw_cmd == "adminlist":
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            await reply_text(await build_admin_list_text(bot, chat_id))
            return

        # ── /zombies ──────────────────────────────────────
        if raw_cmd == "zombies":
            if not await check_mod("kick"):
                return
            if await anti_nuke(chat_id, msg_id, uid):
                return
            await reply_text("🔎 Scanning for deleted/bot accounts...")
            kicked_deleted, kicked_bots, failures = await scan_zombies(bot, chat_id)
            summary = (
                f"🧟 Zombie scan complete\n"
                f"• Deleted accounts kicked: `{kicked_deleted}`\n"
                f"• Bot accounts kicked: `{kicked_bots}`"
            )
            if failures:
                summary += f"\n• Failures: `{len(failures)}`"
            await reply_text(summary)
            return

        # ── /hstats ───────────────────────────────────────
        if raw_cmd == "hstats":
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            await reply_text(build_stats_text())
            return

        # ── /hmod ─────────────────────────────────────────
        if raw_cmd == "hmod":
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            if not args or args[0].lower() != "list":
                return await reply_text("Usage: /hmod list")
            await reply_text(build_moderator_list_text())
            return

        # ── /hwarn ────────────────────────────────────────
        if raw_cmd in ("hwarn", "hw"):
            if not await check_mod("warn"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hwarn <user_id> [reason]")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No reason given")
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            warns = load(WARN_FILE)
            key   = str(tid)
            warns.setdefault(key, 0)
            warns[key] += 1
            await save_and_backup(WARN_FILE, warns)
            warn_config = get_warn_config()
            threshold   = warn_config.get("threshold", 3)
            auto_action = warn_config.get("action", "mute")
            duration    = warn_config.get("duration", 3600)
            case_id = create_case("WARN", uid, tid, reason)
            await send_action_log(
                chat_id, msg_id, "WARN", target, reason, case_id, actor_mod_info(),
                extra=f"📊 Total Warns: {warns[key]}",
            )
            # FIX #7 — safe defaults before branching
            if warns[key] >= threshold:
                ok           = False
                action_label = "MUTE"
                until_ts     = int(time.time()) + duration
                if auto_action == "ban":
                    ok, err  = await api_ban(chat_id, tid, until_date=until_ts)
                    action_label = "BAN"
                elif auto_action == "kick":
                    ok, err  = await api_kick(chat_id, tid)
                    action_label = "KICK"
                else:
                    ok, err  = await api_mute(chat_id, tid, until_date=until_ts)
                    action_label = "MUTE"
                if ok:
                    auto_reason = f"Warn threshold ({threshold}) reached"
                    auto_case   = create_case(action_label, uid, tid, auto_reason)
                    await send_action_log(
                        chat_id, msg_id, action_label, target,
                        auto_reason, auto_case, actor_mod_info(),
                        extra=f"⚡ Auto-action on {warns[key]} warns",
                    )
                    if auto_action in ("ban", "mute"):
                        schedule_temp_action(
                            auto_action, chat_id, tid, until_ts,
                            uid, auto_reason, case_id=auto_case,
                        )
            return

        # ── /hdel ─────────────────────────────────────────
        # FIX #1 — correct indentation aligned with all other blocks
        if raw_cmd in ("hdel", "hd"):
            if not await check_mod("delete"):
                return
            if not reply:
                return  # command msg already scheduled for delete at top
            target, tid = extract_reply_user(reply)
            if not tid:
                return
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            reply_msg_id = reply.get("message_id")
            ok, err = await api_delete_msg(chat_id, reply_msg_id)
            if not ok:
                return await reply_text(f"❌ Delete failed: {err}")
            case_id = create_case("DELETE", uid, tid, "Message Deleted")
            await send_action_log(
                chat_id, msg_id, "DELETE", target,
                "Message Deleted", case_id, actor_mod_info(),
            )
            return

        # ── /hprotect ─────────────────────────────────────
        if raw_cmd in ("hprotect", "hp"):
            if not is_owner_actor():
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hprotect <user_id>")
            data = load(PROTECT_FILE)
            key  = str(tid)
            if key in data:
                return await reply_text(f"ℹ️ {make_mention(target)} is already protected.")
            data[key] = True
            # FIX #4 — protect file backed up
            await save_and_backup(PROTECT_FILE, data)
            await reply_text(f"🛡 {make_mention(target)} is now protected.")
            return

        # ── /hcase ────────────────────────────────────────
        # FIX #9 — proper error reply for unauthorised
        if raw_cmd in ("hcase", "hc"):
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            if not args:
                return await reply_text("Usage: /hcase <case_id>")
            cases = load(CASE_FILE)
            case  = cases.get(args[0])
            if not case:
                return await reply_text(f"❌ Case #{args[0]} not found.")
            await reply_text(
                f"📜 **Case #{args[0]}**\n\n"
                f"⚔ Action: {case['action']}\n"
                f"👤 Target: `{case['target']}`\n"
                f"👮 Moderator: `{case['moderator']}`\n"
                f"📝 Reason: {case['reason']}\n"
                f"⏰ Time: {case['time']}"
                + (f"\n⏳ Expires: {format_timestamp(case.get('expires_at'))}" if case.get("temporary") else "")
            )
            return

        # ── /hmodinfo ─────────────────────────────────────
        # FIX #9 — proper error reply for unauthorised
        if raw_cmd in ("hmodinfo", "hmi"):
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            if is_anon_admin and not reply and not args:
                await reply_text(
                    "👮 **Moderator Info**\n\n"
                    f"🆔 Mod ID: `ANON-{abs(chat_id)}`\n"
                    "🎭 Anonymous Admin\nStatus: 🟢 Active\n\n"
                    "**Permissions:**\n  ✅ ban\n  ✅ mute\n  ✅ warn\n  ✅ delete"
                )
                return
            lookup = uid
            if reply:
                _, rid = extract_reply_user(reply)
                if rid:
                    lookup = rid
            elif args:
                p = parse_positive_user_id(args[0])
                if not p:
                    return await reply_text("❌ Invalid user ID.")
                lookup = p
            mod = get_mod_info(lookup)
            if not mod:
                return await reply_text("❌ That user is not a moderator.")
            perms     = mod.get("permissions", {})
            perm_list = "\n".join(
                f"  {'✅' if v else '❌'} {k}" for k, v in perms.items()
            ) or "  No permissions set"
            status = "🔴 Frozen" if mod.get("frozen") else "🟢 Active"
            await reply_text(
                f"👮 **Moderator Info**\n\n"
                f"🆔 Mod ID: `{mod.get('mod_id', 'N/A')}`\n"
                f"{mod.get('badge', '🛡 Moderator')}\n"
                f"Status: {status}\n\n"
                f"**Permissions:**\n{perm_list}"
            )
            return

    except Exception as e:
        log_msg(f"handle_message error: {e}\n{traceback.format_exc()}", "ERROR")

# =========================================================
# CALLBACK HANDLER
# =========================================================

async def handle_callback(bot: Client, cb: dict):
    try:
        cb_id     = cb["id"]
        data      = cb.get("data", "")
        from_user = cb.get("from", {})
        uid       = from_user.get("id", 0)
        message   = cb.get("message", {})
        chat_id   = message.get("chat", {}).get("id")

        for prefix in TTT_CALLBACK_PREFIXES:
            if data.startswith(prefix):
                await handle_ttt_callback(cb_id, data, uid, from_user, chat_id, message)
                return

        if not is_authorized(uid):
            await tg_answer_cb(cb_id, "⛔ Only moderators can use this.", alert=True)
            return

        if data.startswith("unban_"):
            if not has_permission(uid, "unban"):
                return await tg_answer_cb(cb_id, "❌ No unban permission.", alert=True)
            tid     = int(data.split("_", 1)[1])
            ok, err = await api_unban(chat_id, tid)
            if ok:
                # FIX #6 — cancel pending temp ban from inline button
                cancel_temp_action("ban", chat_id, tid)
                await tg_answer_cb(cb_id, "✅ User unbanned.")
            else:
                await tg_answer_cb(cb_id, f"❌ {err}", alert=True)

        elif data.startswith("unmute_"):
            if not has_permission(uid, "unmute"):
                return await tg_answer_cb(cb_id, "❌ No unmute permission.", alert=True)
            tid     = int(data.split("_", 1)[1])
            ok, err = await api_unmute(chat_id, tid)
            if ok:
                # FIX #6 — cancel pending temp mute from inline button
                cancel_temp_action("mute", chat_id, tid)
                await tg_answer_cb(cb_id, "✅ User unmuted.")
            else:
                await tg_answer_cb(cb_id, f"❌ {err}", alert=True)

        elif data.startswith("removewarn_"):
            if not has_permission(uid, "warn"):
                return await tg_answer_cb(cb_id, "❌ No warn permission.", alert=True)
            tid   = int(data.split("_", 1)[1])
            warns = load(WARN_FILE)
            key   = str(tid)
            if key in warns and warns[key] > 0:
                warns[key] -= 1
                save(WARN_FILE, warns)
            await tg_answer_cb(cb_id, f"✅ Warning removed. Total: {warns.get(key, 0)}")

        elif data.startswith("case_"):
            case_id = data.split("_", 1)[1]
            cases   = load(CASE_FILE)
            case    = cases.get(case_id)
            if case:
                expiry = f"\n⏳ Expires: {format_timestamp(case.get('expires_at'))}" if case.get("temporary") else ""
                await tg_answer_cb(
                    cb_id,
                    f"Case #{case_id}\n{case['action']} — {case['reason']}\n{case['time']}{expiry}",
                    alert=True,
                )
            else:
                await tg_answer_cb(cb_id, "❌ Case not found.", alert=True)

    except Exception as e:
        log_msg(f"handle_callback error: {e}\n{traceback.format_exc()}", "ERROR")

# =========================================================
# STARTUP / SHUTDOWN
# =========================================================

@app.on_event("startup")
async def startup_event():
    global _temp_worker_task, _games_worker_task
    log_msg("🚀 Starting up...", "INFO")
    try:
        mongo_db.connect()
        sync_storage_with_mongo()
        bot = await get_bot()

        init_games(
            save_fn=save, load_fn=load,
            scores_file=TTT_SCORES_FILE, bot_token=BOT_TOKEN,
        )

        await resolve_log_group(bot)

        restored = 0
        if _tg_backup_enabled and get_backup_chat() != 0:
            restored = await restore_from_telegram_pyrogram(bot)
            if restored:
                log_msg(f"✅ Restored {restored} file(s) from Telegram backup", "INFO")
        else:
            log_msg("Telegram backup restore skipped: log group not configured.", "INFO")

        wh_status  = await ensure_webhook()
        cmd_status = await sync_commands()

        if _temp_worker_task is None or _temp_worker_task.done():
            _temp_worker_task = asyncio.create_task(temp_action_worker())
        if _games_worker_task is None or _games_worker_task.done():
            _games_worker_task = asyncio.create_task(games_cleanup_worker())

        lg = get_log_group()
        startup_text = (
            f"✅ **Bot started**\n\n"
            f"🖥 Port: `{PORT}`\n"
            f"💾 Storage: `{STORAGE_PATH}`\n"
            f"📋 Log group: `{lg}`\n"
            f"🔗 Webhook: `{wh_status}`\n"
            f"📌 Commands: `{cmd_status}`\n"
            f"📦 Restored files: `{restored}`"
        )
        await notify_owner(startup_text)
        if lg:
            await tg_send(lg, startup_text)

        log_msg("✅ Startup complete", "INFO")
    except Exception as e:
        log_msg(f"❌ Startup failed: {e}\n{traceback.format_exc()}", "ERROR")

@app.on_event("shutdown")
async def shutdown_event():
    global _temp_worker_task, _games_worker_task, _http_client
    log_msg("🛑 Shutting down...", "INFO")
    for task in (_temp_worker_task, _games_worker_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await shutdown_bot()
    await shutdown_games()
    if _http_client:
        await _http_client.aclose()
        _http_client = None
    mongo_db.disconnect()

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
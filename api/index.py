# =========================================================
# ADVANCED TELEGRAM MODERATION BOT — KOYEB VERSION (FIXED)
# =========================================================
# FIXES APPLIED:
#   FIX-A: Replaced ALL bot.get_chat_member() calls with direct Bot API
#           (getChatMember) to avoid Pyrogram "Peer id invalid" errors
#           caused by the empty in-memory peer cache after restarts.
#   FIX-B: Added _BotApiMember wrapper + api_get_chat_member() helper.
#   FIX-C: Removed dead code block in moderation_help_markup().
#   FIX-D: Removed duplicate `if raw_cmd == "rules":` handler block.
#   FIX-E: Fixed hlock/hunlock to allow PM-connected users.
#   FIX-F: Added missing /hunauth command handler.
#   FIX-G: Added stub handler for locktype/locktypes commands.
# =========================================================

import os, json, time, random, string, asyncio, httpx, re
import traceback, sys, shutil, io, threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pyrogram import Client, enums
from pyrogram.errors import BadRequest, UserNotParticipant
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
    {"command": "start",          "description": "🚀 Start the bot & view welcome message"},
    {"command": "help",           "description": "📖 Display available commands for your role"},
    {"command": "hr",             "description": "🆔 Get user ID & profile information"},
    {"command": "hstats",         "description": "📊 Show group moderation stats"},
]
VALID_PERMISSIONS = {"ban", "unban", "mute", "unmute", "kick", "warn", "delete", "pin"}

MODERATION_COMMANDS = {
    "hban", "hb", "hkick", "hk", "hmute", "hm",
    "hunban", "hub", "hunmute", "hum", "hwarn", "hw",
    "resetwarns", "hdel", "hd", "pin", "unpin",
    "save", "get", "clear", "notes",
    "filter", "stop", "filters",
    "addblocklist", "deleteblocklist", "removeblocklist", "blocklists", "blocklistmode",
    "hprotect", "hp", "hunprotect", "hup", "hprotected", "protect", "unprotect", "protected",
    "setwelcome", "setgoodbye", "setrules", "hlock", "lock", "hunlock", "unlock",
    "locktype", "locktypes",
    # toggle commands
    "welcome", "goodbye", "rules", "bot",
}
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

_log_group_id: int   = LOG_GROUP_ID
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

AUTH_FILE             = f"{STORAGE_PATH}/auth.json"
WARN_FILE             = f"{STORAGE_PATH}/warns.json"
CASE_FILE             = f"{STORAGE_PATH}/cases.json"
WARN_CONFIG_FILE      = f"{STORAGE_PATH}/warn_config.json"
PROTECT_FILE          = f"{STORAGE_PATH}/protected.json"
ABUSE_FILE            = f"{STORAGE_PATH}/abuse.json"
TEMP_ACTIONS_FILE     = f"{STORAGE_PATH}/temp_actions.json"
APPEALS_FILE          = f"{STORAGE_PATH}/appeals.json"
CONNECTIONS_FILE      = f"{STORAGE_PATH}/connections.json"
USER_CONNECTIONS_FILE = f"{STORAGE_PATH}/user_connections.json"
ACTIVE_CONN_FILE      = f"{STORAGE_PATH}/active_conn.json"
NOTES_FILE            = f"{STORAGE_PATH}/notes.json"
FILTERS_FILE          = f"{STORAGE_PATH}/filters.json"
BLOCKLIST_FILE        = f"{STORAGE_PATH}/blocklists.json"
BLOCKLIST_MODE_FILE   = f"{STORAGE_PATH}/blocklist_mode.json"
WELCOME_FILE          = f"{STORAGE_PATH}/welcome.json"
GOODBYE_FILE          = f"{STORAGE_PATH}/goodbye.json"
RULES_FILE            = f"{STORAGE_PATH}/rules.json"
LOCKS_FILE            = f"{STORAGE_PATH}/chat_locks.json"
LOCK_TYPES_FILE       = f"{STORAGE_PATH}/lock_types.json"
URL_DELETE_FILE       = f"{STORAGE_PATH}/url_delete.json"
TTT_SCORES_FILE       = f"{STORAGE_PATH}/ttt_scores.json"
TTT_STATE_FILE        = f"{STORAGE_PATH}/ttt_state.json"
CHAT_TITLES_FILE      = f"{STORAGE_PATH}/chat_titles.json"
BOT_STATUS_FILE = f"{STORAGE_PATH}/bot_status.json"

FALLBACK_FILE_MAP = {
    AUTH_FILE:             f"{FALLBACK_STORAGE_PATH}/auth.json",
    WARN_FILE:             f"{FALLBACK_STORAGE_PATH}/warns.json",
    CASE_FILE:             f"{FALLBACK_STORAGE_PATH}/cases.json",
    WARN_CONFIG_FILE:      f"{FALLBACK_STORAGE_PATH}/warn_config.json",
    PROTECT_FILE:          f"{FALLBACK_STORAGE_PATH}/protected.json",
    ABUSE_FILE:            f"{FALLBACK_STORAGE_PATH}/abuse.json",
    TEMP_ACTIONS_FILE:     f"{FALLBACK_STORAGE_PATH}/temp_actions.json",
    APPEALS_FILE:          f"{FALLBACK_STORAGE_PATH}/appeals.json",
    CONNECTIONS_FILE:      f"{FALLBACK_STORAGE_PATH}/connections.json",
    USER_CONNECTIONS_FILE: f"{FALLBACK_STORAGE_PATH}/user_connections.json",
    ACTIVE_CONN_FILE:      f"{FALLBACK_STORAGE_PATH}/active_conn.json",
    NOTES_FILE:            f"{FALLBACK_STORAGE_PATH}/notes.json",
    FILTERS_FILE:          f"{FALLBACK_STORAGE_PATH}/filters.json",
    BLOCKLIST_FILE:        f"{FALLBACK_STORAGE_PATH}/blocklists.json",
    TTT_SCORES_FILE:       f"{FALLBACK_STORAGE_PATH}/ttt_scores.json",
    TTT_STATE_FILE:        f"{FALLBACK_STORAGE_PATH}/ttt_state.json",
    BLOCKLIST_MODE_FILE:   f"{FALLBACK_STORAGE_PATH}/blocklist_mode.json",
    WELCOME_FILE:          f"{FALLBACK_STORAGE_PATH}/welcome.json",
    GOODBYE_FILE:          f"{FALLBACK_STORAGE_PATH}/goodbye.json",
    RULES_FILE:            f"{FALLBACK_STORAGE_PATH}/rules.json",
    LOCKS_FILE:            f"{FALLBACK_STORAGE_PATH}/chat_locks.json",
    CHAT_TITLES_FILE:      f"{FALLBACK_STORAGE_PATH}/chat_titles.json",
    BOT_STATUS_FILE:       f"{FALLBACK_STORAGE_PATH}/bot_status.json",
}

ALL_FILES = list(FALLBACK_FILE_MAP.keys())

FILE_LABEL = {
    AUTH_FILE:             "auth",
    WARN_FILE:             "warns",
    CASE_FILE:             "cases",
    WARN_CONFIG_FILE:      "warn_config",
    PROTECT_FILE:          "protected",
    ABUSE_FILE:            "abuse",
    TEMP_ACTIONS_FILE:     "temp_actions",
    APPEALS_FILE:          "appeals",
    CONNECTIONS_FILE:      "connections",
    USER_CONNECTIONS_FILE: "user_connections",
    ACTIVE_CONN_FILE:      "active_conn",
    NOTES_FILE:            "notes",
    FILTERS_FILE:          "filters",
    BLOCKLIST_FILE:        "blocklists",
    TTT_SCORES_FILE:       "ttt_scores",
    TTT_STATE_FILE:        "ttt_state",
    BLOCKLIST_MODE_FILE:   "blocklist_mode",
    WELCOME_FILE:          "welcome",
    GOODBYE_FILE:          "goodbye",
    RULES_FILE:            "rules",
    LOCKS_FILE:            "chat_locks",
    CHAT_TITLES_FILE:      "chat_titles",
    BOT_STATUS_FILE:       "bot_status",
}

MONGO_LOADERS = {
    AUTH_FILE:             mongo_db.load_auth,
    WARN_FILE:             mongo_db.load_warns,
    CASE_FILE:             mongo_db.load_cases,
    WARN_CONFIG_FILE:      mongo_db.load_warn_config,
    PROTECT_FILE:          mongo_db.load_protected,
    ABUSE_FILE:            mongo_db.load_abuse,
    TEMP_ACTIONS_FILE:     mongo_db.load_temp_actions,
    APPEALS_FILE:          mongo_db.load_appeals,
    CONNECTIONS_FILE:      mongo_db.load_connections,
    USER_CONNECTIONS_FILE: mongo_db.load_user_connections,
    TTT_SCORES_FILE:       mongo_db.load_ttt_scores,
    TTT_STATE_FILE:        mongo_db.load_ttt_state,
    ACTIVE_CONN_FILE:      mongo_db.load_active_conn,
    NOTES_FILE:            mongo_db.load_notes,
    FILTERS_FILE:          mongo_db.load_filters,
    BLOCKLIST_FILE:        mongo_db.load_blocklists,
    BLOCKLIST_MODE_FILE:   mongo_db.load_blocklist_mode,
    WELCOME_FILE:          mongo_db.load_welcome,
    GOODBYE_FILE:          mongo_db.load_goodbye,
    RULES_FILE:            mongo_db.load_rules,
    LOCKS_FILE:            mongo_db.load_chat_locks,
    CHAT_TITLES_FILE:      mongo_db.load_chat_titles,
    BOT_STATUS_FILE:       mongo_db.load_bot_status,
}

MONGO_SAVERS = {
    AUTH_FILE:             mongo_db.save_auth,
    WARN_FILE:             mongo_db.save_warns,
    CASE_FILE:             mongo_db.save_cases,
    WARN_CONFIG_FILE:      mongo_db.save_warn_config,
    PROTECT_FILE:          mongo_db.save_protected,
    ABUSE_FILE:            mongo_db.save_abuse,
    TEMP_ACTIONS_FILE:     mongo_db.save_temp_actions,
    APPEALS_FILE:          mongo_db.save_appeals,
    CONNECTIONS_FILE:      mongo_db.save_connections,
    USER_CONNECTIONS_FILE: mongo_db.save_user_connections,
    TTT_SCORES_FILE:       mongo_db.save_ttt_scores,
    TTT_STATE_FILE:        mongo_db.save_ttt_state,
    ACTIVE_CONN_FILE:      mongo_db.save_active_conn,
    NOTES_FILE:            mongo_db.save_notes,
    FILTERS_FILE:          mongo_db.save_filters,
    BLOCKLIST_FILE:        mongo_db.save_blocklists,
    BLOCKLIST_MODE_FILE:   mongo_db.save_blocklist_mode,
    WELCOME_FILE:          mongo_db.save_welcome,
    GOODBYE_FILE:          mongo_db.save_goodbye,
    RULES_FILE:            mongo_db.save_rules,
    LOCKS_FILE:            mongo_db.save_chat_locks,
    CHAT_TITLES_FILE:      mongo_db.save_chat_titles,
    BOT_STATUS_FILE:       mongo_db.save_bot_status,
}

MONGO_PERSISTENT_FILES = {
    CONNECTIONS_FILE,
    USER_CONNECTIONS_FILE,
    ACTIVE_CONN_FILE,
    BOT_STATUS_FILE,
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

    try:
        if os.path.exists(TEMP_ACTIONS_FILE):
            with open(TEMP_ACTIONS_FILE, "r") as f:
                data = None
                try:
                    data = json.load(f)
                except Exception:
                    data = None
            if isinstance(data, dict) or data is None:
                try:
                    with open(TEMP_ACTIONS_FILE, "w") as f:
                        json.dump([], f, indent=2)
                    log_msg(f"Normalized {TEMP_ACTIONS_FILE} to an empty list", "INFO")
                except Exception:
                    pass
    except Exception:
        pass

init_storage_files()
log_msg(f"Storage: {STORAGE_PATH}  Fallback: {FALLBACK_STORAGE_PATH}", "INFO")

# =========================================================
# JSON HELPERS
# =========================================================

def load(file: str):
    cached = _cache.get(file)
    if cached is not None:
        if file in MONGO_PERSISTENT_FILES and _is_empty_payload(cached) and mongo_db.is_connected():
            _cache.invalidate(file)
        else:
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
        elif file == NOTES_FILE:
            for key in _cache.keys():
                if key.startswith("notes:"):
                    _cache.invalidate(key)
        elif file == FILTERS_FILE:
            for key in _cache.keys():
                if key.startswith("filters:"):
                    _cache.invalidate(key)
        elif file == BLOCKLIST_FILE:
            for key in _cache.keys():
                if key.startswith("blocklists:"):
                    _cache.invalidate(key)
        elif file == WELCOME_FILE:
            for key in _cache.keys():
                if key.startswith("welcome:"):
                    _cache.invalidate(key)
        elif file == GOODBYE_FILE:
            for key in _cache.keys():
                if key.startswith("goodbye:"):
                    _cache.invalidate(key)
        elif file == RULES_FILE:
            for key in _cache.keys():
                if key.startswith("rules:"):
                    _cache.invalidate(key)
        elif file == LOCKS_FILE:
            for key in _cache.keys():
                if key.startswith("lock:"):
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
        if file in MONGO_PERSISTENT_FILES:
            _cache.invalidate(file)

def verify_storage_restored():
    protected_data = _normalize_protected_store(load(PROTECT_FILE))
    protected_count = sum(
        len(v) for v in protected_data.values() if isinstance(v, dict)
    )
    stats = {
        "warns":       len(load(WARN_FILE)),
        "notes":       sum(len(v) for v in load(NOTES_FILE).values() if isinstance(v, dict)),
        "filters":     sum(len(v) for v in load(FILTERS_FILE).values() if isinstance(v, dict)),
        "blocklists":  sum(len(v) for v in load(BLOCKLIST_FILE).values() if isinstance(v, dict)),
        "cases":       len(load(CASE_FILE)),
        "protections": protected_count,
        "welcome":     len(load(WELCOME_FILE)) if isinstance(load(WELCOME_FILE), dict) else 0,
        "goodbye":     len(load(GOODBYE_FILE)) if isinstance(load(GOODBYE_FILE), dict) else 0,
        "rules":       len(load(RULES_FILE)) if isinstance(load(RULES_FILE), dict) else 0,
        "locks":       len(load(LOCKS_FILE)) if isinstance(load(LOCKS_FILE), dict) else 0,
    }
    status_lines = []
    for key, count in stats.items():
        if count > 0:
            status_lines.append(f"  • {key.capitalize()}: {count} entries")
    if status_lines:
        log_msg("📦 Storage Restored:\n" + "\n".join(status_lines), "INFO")
    else:
        log_msg("📦 Storage: No persisted data found (fresh start)", "INFO")

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
    entities: list = None,
) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if markup:
        payload["reply_markup"] = markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if entities is not None:
        payload["entities"] = entities
    return await tg_api("sendMessage", json=payload)

async def tg_edit_text(
    chat_id: int,
    message_id: int,
    text: str,
    markup: dict = None,
    parse_mode: str = "Markdown",
    entities: list = None,
) -> dict:
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if entities is not None:
        payload["entities"] = entities
    return await tg_api("editMessageText", json=payload)

async def tg_send_media(chat_id: int, note: dict, reply_to: int = None):
    ntype   = note.get("type", "text")
    fid     = note.get("file_id")
    caption = note.get("content") or ""
    entities = note.get("entities")
    payload = {"chat_id": chat_id}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        if ntype == "photo" and fid:
            payload["photo"] = fid
            if caption:
                payload["caption"] = caption
                if entities is not None:
                    payload["caption_entities"] = entities
            return await tg_api("sendPhoto", json=payload)
        if ntype == "document" and fid:
            payload["document"] = fid
            if caption:
                payload["caption"] = caption
                if entities is not None:
                    payload["caption_entities"] = entities
            return await tg_api("sendDocument", json=payload)
        if ntype == "video" and fid:
            payload["video"] = fid
            if caption:
                payload["caption"] = caption
                if entities is not None:
                    payload["caption_entities"] = entities
            return await tg_api("sendVideo", json=payload)
        if ntype == "audio" and fid:
            payload["audio"] = fid
            if caption:
                payload["caption"] = caption
                if entities is not None:
                    payload["caption_entities"] = entities
            return await tg_api("sendAudio", json=payload)
        if ntype == "voice" and fid:
            payload["voice"] = fid
            return await tg_api("sendVoice", json=payload)
        if ntype == "sticker" and fid:
            payload["sticker"] = fid
            return await tg_api("sendSticker", json=payload)
        if ntype == "animation" and fid:
            payload["animation"] = fid
            if caption:
                payload["caption"] = caption
                if entities is not None:
                    payload["caption_entities"] = entities
            return await tg_api("sendAnimation", json=payload)
    except Exception as e:
        log_msg(f"tg_send_media error: {e}", "ERROR")
    if caption:
        if entities is not None:
            return await tg_send(chat_id, caption, reply_to=reply_to, parse_mode=None, entities=entities)
        return await tg_send(chat_id, caption, reply_to=reply_to, parse_mode=None)
    return {"ok": False, "description": "No media to send."}

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

async def api_unmute(chat_id: int, user_id: int, permissions: dict | None = None) -> tuple[bool, str]:
    perms = permissions if isinstance(permissions, dict) else _FULL_PERMISSIONS
    r = await tg_api("restrictChatMember", json={
        "chat_id": chat_id, "user_id": user_id, "permissions": perms,
    })
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_set_chat_permissions(chat_id: int, permissions: dict) -> tuple[bool, str]:
    r = await tg_api("setChatPermissions", json={
        "chat_id": chat_id, "permissions": permissions,
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
# FIX-A + FIX-B: Bot API getChatMember — avoids Pyrogram peer-cache errors
# =========================================================
# Pyrogram uses in_memory=True, so its peer cache is empty after every restart.
# Calling bot.get_chat_member() on an uncached peer raises:
#   ValueError: Peer id invalid: -100XXXXXXXXXX
# We bypass this entirely by calling the Bot API directly.
# =========================================================

# Status strings returned by Bot API getChatMember
_BOT_API_ADMIN_STATUSES = {"creator", "administrator"}
_BOT_API_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}

class _BotApiMember:
    """Minimal wrapper around a Bot API getChatMember result dict.

    Exposes a `.status` attribute mapped to pyrogram.enums.ChatMemberStatus
    so existing status comparisons continue to work unchanged.
    """
    _STATUS_MAP = {
        "creator":       enums.ChatMemberStatus.OWNER,
        "administrator": enums.ChatMemberStatus.ADMINISTRATOR,
        "member":        enums.ChatMemberStatus.MEMBER,
        "restricted":    enums.ChatMemberStatus.RESTRICTED,
        "left":          enums.ChatMemberStatus.LEFT,
        "kicked":        enums.ChatMemberStatus.BANNED,
    }

    def __init__(self, data: dict):
        self._data  = data or {}
        raw_status  = self._data.get("status", "")
        self.status = self._STATUS_MAP.get(raw_status, enums.ChatMemberStatus.MEMBER)
        self._raw_status = raw_status  # keep original string for quick checks

    def is_admin(self) -> bool:
        return self._raw_status in _BOT_API_ADMIN_STATUSES


async def api_get_chat_member(chat_id: int, user_id: int) -> tuple[dict | None, str | None]:
    """Call Bot API getChatMember and return (result_dict, error_string).

    Returns (None, "UserNotParticipant") when the user is not in the chat.
    Returns (None, "❌ ...") for other failures.
    """
    r = await tg_api("getChatMember", json={"chat_id": int(chat_id), "user_id": int(user_id)})
    if r.get("ok"):
        return r.get("result", {}), None
    desc = r.get("description", "")
    desc_lower = desc.lower()
    if any(x in desc_lower for x in (
        "user not found", "not a member", "not found",
        "participant", "user_not_participant", "member_status",
    )):
        return None, "UserNotParticipant"
    return None, f"❌ {desc}"


async def get_chat_member_safe(bot: Client, chat_id, user_identifier):
    """FIX-A: Uses Bot API instead of Pyrogram to avoid peer-cache errors.

    Returns: (_BotApiMember, None) on success,
             (None, "UserNotParticipant") if user not in chat,
             (None, "❌ ...") for other failures.
    """
    try:
        uid = int(user_identifier)
    except Exception:
        return None, "❌ Invalid user ID."

    data, err = await api_get_chat_member(int(chat_id), uid)
    if err:
        return None, err
    return _BotApiMember(data), None


async def is_chat_admin(bot: Client, chat_id: int, user_id: int) -> bool:
    """FIX-A: Uses Bot API instead of Pyrogram to avoid peer-cache errors."""
    data, err = await api_get_chat_member(int(chat_id), int(user_id))
    if err or not data:
        return False
    return data.get("status") in _BOT_API_ADMIN_STATUSES

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

_BACKUP_LABELS = {
    "auth", "warns", "cases", "protected", "abuse",
    "temp_actions", "appeals", "connections", "user_connections",
    "notes", "filters", "welcome", "goodbye", "rules", "chat_locks",
}

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

def allow_connect_to_chat(chat_id: int) -> bool:
    return bool(load(CONNECTIONS_FILE).get(str(chat_id), False))

def set_allow_connect_to_chat(chat_id: int, enabled: bool):
    data = load(CONNECTIONS_FILE)
    data[str(chat_id)] = bool(enabled)
    save(CONNECTIONS_FILE, data)

# =========================================================
# MULTI-GROUP CONNECTION SYSTEM
# =========================================================

def get_connected_chats(user_id: int) -> list[int]:
    data = load(USER_CONNECTIONS_FILE)
    value = data.get(str(user_id))
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, list):
        return [int(x) for x in value if str(x).lstrip("-").isdigit()]
    return []

def get_active_connection(user_id: int) -> int | None:
    data = load(ACTIVE_CONN_FILE)
    value = data.get(str(user_id))
    try:
        return int(value) if value is not None else None
    except Exception:
        return None

def set_active_connection(user_id: int, chat_id: int):
    data = load(ACTIVE_CONN_FILE)
    data[str(user_id)] = int(chat_id)
    save(ACTIVE_CONN_FILE, data)

def add_user_connection(user_id: int, chat_id: int):
    data  = load(USER_CONNECTIONS_FILE)
    key   = str(user_id)
    chats = get_connected_chats(user_id)
    if int(chat_id) not in chats:
        chats.append(int(chat_id))
    data[key] = chats
    save(USER_CONNECTIONS_FILE, data)
    set_active_connection(user_id, chat_id)

def remove_user_connection(user_id: int, chat_id: int = None) -> bool:
    data  = load(USER_CONNECTIONS_FILE)
    key   = str(user_id)
    chats = get_connected_chats(user_id)
    if chat_id is not None:
        cid = int(chat_id)
        if cid not in chats:
            return False
        chats.remove(cid)
        data[key] = chats
        save(USER_CONNECTIONS_FILE, data)
        active = get_active_connection(user_id)
        if active == cid:
            if chats:
                set_active_connection(user_id, chats[0])
            else:
                active_data = load(ACTIVE_CONN_FILE)
                active_data.pop(str(user_id), None)
                save(ACTIVE_CONN_FILE, active_data)
        return True
    else:
        data.pop(key, None)
        save(USER_CONNECTIONS_FILE, data)
        active_data = load(ACTIVE_CONN_FILE)
        active_data.pop(str(user_id), None)
        save(ACTIVE_CONN_FILE, active_data)
        return True

def get_connected_chat(user_id: int) -> int | None:
    active = get_active_connection(user_id)
    if active:
        return active
    chats = get_connected_chats(user_id)
    return chats[0] if chats else None

def connect_user(user_id: int, chat_id: int) -> bool:
    add_user_connection(user_id, chat_id)
    return True

def disconnect_user(user_id: int) -> bool:
    return remove_user_connection(user_id)


async def connected(bot: Client, chat: dict, user_id: int, need_admin: bool = True):
    """FIX-A: Uses Bot API getChatMember instead of Pyrogram to avoid peer-cache errors."""
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return False
    conn_id = get_active_connection(user_id)
    if not conn_id:
        chats = get_connected_chats(user_id)
        if chats:
            conn_id = chats[0]
            set_active_connection(user_id, conn_id)
        else:
            return False

    data, err = await api_get_chat_member(conn_id, user_id)
    if err:
        # Bot API failure — keep stored connection rather than silently dropping it.
        log_msg(
            f"Cannot verify member {user_id} in {conn_id} right now ({err}); "
            "keeping stored connection.",
            "WARNING",
        )
        return conn_id  # trust stored data; worst case Telegram rejects the action

    status = data.get("status", "")
    is_admin_status = status in _BOT_API_ADMIN_STATUSES
    is_member_status = status in _BOT_API_MEMBER_STATUSES

    if not is_admin_status and not (allow_connect_to_chat(conn_id) and is_member_status):
        return False
    if need_admin and not is_admin_status:
        return False

    try:
        create_group_defaults(conn_id)
    except Exception:
        pass
    return conn_id


async def allow_connections(bot: Client, msg: dict, args: list[str]) -> str:
    chat = msg.get("chat", {})
    if chat.get("type") == "private":
        return "Please enter on/yes/off/no in group!"
    user    = msg.get("from", {})
    user_id = user.get("id")
    if not user_id:
        return "Please enter on/yes/off/no in group!"
    if not (await is_chat_admin(bot, chat["id"], user_id) or user_id == OWNER_ID):
        return "Only group admins can change this setting."
    if len(args) < 1:
        return "Please enter on/yes/off/no in group!"
    var = args[0].lower()
    if var in ("no", "off"):
        set_allow_connect_to_chat(chat["id"], False)
        return "Disabled connections to this chat for users"
    if var in ("yes", "on"):
        set_allow_connect_to_chat(chat["id"], True)
        return "Enabled connections to this chat for users"
    return "Please enter on/yes/off/no in group!"


async def connect_chat(bot: Client, msg: dict, args: list[str]) -> str:
    chat    = msg.get("chat", {})
    user    = msg.get("from", {})
    user_id = user.get("id")
    if chat.get("type") != "private":
        return "Use this in PM to connect a chat by ID."
    if not user_id:
        return "Invalid user!"
    if len(args) < 1:
        return "Input chat ID to connect!"
    try:
        connect_id = int(args[0])
    except ValueError:
        return "Invalid Chat ID provided!"
    message = await connect_user_to_chat(bot, user_id, connect_id)
    return message or "Connection failed!"


async def connect_user_to_chat(bot: Client, user_id: int, connect_id: int) -> str | None:
    """FIX-A: Uses Bot API getChatMember instead of Pyrogram."""
    data, err = await api_get_chat_member(connect_id, user_id)
    if err == "UserNotParticipant":
        return "Connections to this chat not allowed!"
    if err:
        return "Invalid Chat ID provided!"

    status = data.get("status", "")
    allowed = status in _BOT_API_ADMIN_STATUSES
    if not allowed:
        allowed = allow_connect_to_chat(connect_id) and status in _BOT_API_MEMBER_STATUSES

    if not allowed:
        return "Connections to this chat not allowed!"

    try:
        target_chat = await bot.get_chat(connect_id)
        title = target_chat.title or f"Chat {connect_id}"
        cache_chat_title(connect_id, title)
        add_user_connection(user_id, connect_id)
        try:
            create_group_defaults(connect_id)
        except Exception:
            pass
        chats = get_connected_chats(user_id)
        count = len(chats)
        return (
            f"✅ Connected to *{title}*\n"
            f"📌 Set as active group\n"
            f"🔗 Total connected: `{count}`\n\n"
            f"Use /connections to view & switch groups."
        )
    except Exception:
        return "Connection failed!"


async def disconnect_user_from_chat(bot: Client, user_id: int, disconnect_id: int) -> str | None:
    chats = get_connected_chats(user_id)
    if disconnect_id not in chats:
        return "❌ You were not connected to that group."
    removed = remove_user_connection(user_id, disconnect_id)
    if not removed:
        return "❌ You were not connected to that group."
    remaining = get_connected_chats(user_id)
    if remaining:
        new_active = get_active_connection(user_id)
        try:
            chat_obj = await bot.get_chat(new_active)
            return f"✅ Disconnected. Active group switched to *{chat_obj.title}*."
        except Exception:
            return f"✅ Disconnected. Active group: `{new_active}`."
    return "✅ Disconnected from active group."


async def disconnect_chat(bot: Client, msg: dict, args: list[str]) -> str:
    chat    = msg.get("chat", {})
    user    = msg.get("from", {})
    user_id = user.get("id")
    if chat.get("type") != "private":
        return "Use this in PM to disconnect a chat by ID."
    if not user_id:
        return "Invalid user!"
    if args and args[0].lower() == "all":
        remove_user_connection(user_id)
        return "✅ Disconnected from all groups."
    if args:
        try:
            cid = int(args[0])
            message = await disconnect_user_from_chat(bot, user_id, cid)
            return message or "❌ You were not connected to that group."
        except ValueError:
            return "❌ Invalid chat ID."
    active = get_active_connection(user_id)
    if active:
        message = await disconnect_user_from_chat(bot, user_id, active)
        return message or "✅ Disconnected from active group."
    return "You are not connected to any group."

# =========================================================
# NOTES SYSTEM
# =========================================================

def _notes_for_chat(chat_id: int) -> dict:
    cache_key = f"notes:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data   = load(NOTES_FILE)
    result = data.get(str(chat_id), {})
    _cache.set(cache_key, result)
    return result

def _save_notes_for_chat(chat_id: int, notes: dict):
    data = load(NOTES_FILE)
    data[str(chat_id)] = notes
    save(NOTES_FILE, data)
    _cache.invalidate(f"notes:{chat_id}")

def note_get(chat_id: int, name: str) -> dict | None:
    return _notes_for_chat(chat_id).get(name.lower().strip())

def note_save(
    chat_id: int, name: str, content: str,
    note_type: str = "text", file_id: str = None,
    created_by: int = None,
    entities: list = None,
):
    notes = _notes_for_chat(chat_id)
    notes[name.lower().strip()] = {
        "content":    content,
        "type":       note_type,
        "file_id":    file_id,
        "created_by": created_by,
        "entities":   entities,
        "updated_at": str(datetime.now()),
    }
    _save_notes_for_chat(chat_id, notes)

def note_delete(chat_id: int, name: str) -> bool:
    notes = _notes_for_chat(chat_id)
    key   = name.lower().strip()
    if key not in notes:
        return False
    del notes[key]
    _save_notes_for_chat(chat_id, notes)
    return True

def note_list(chat_id: int) -> list[str]:
    return sorted(_notes_for_chat(chat_id).keys())

def note_count(chat_id: int) -> int:
    return len(_notes_for_chat(chat_id))

# =========================================================
# FILTERS SYSTEM
# =========================================================

def _filters_for_chat(chat_id: int) -> dict:
    cache_key = f"filters:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data   = load(FILTERS_FILE)
    result = data.get(str(chat_id), {})
    _cache.set(cache_key, result)
    return result

def _save_filters_for_chat(chat_id: int, filters: dict):
    data = load(FILTERS_FILE)
    data[str(chat_id)] = filters
    save(FILTERS_FILE, data)
    _cache.invalidate(f"filters:{chat_id}")

def filter_add(
    chat_id: int, keyword: str, response: str,
    match_type: str = "contains", created_by: int = None,
):
    filters = _filters_for_chat(chat_id)
    filters[keyword.lower().strip()] = {
        "response":   response,
        "match_type": match_type,
        "created_by": created_by,
        "updated_at": str(datetime.now()),
    }
    _save_filters_for_chat(chat_id, filters)

def filter_remove(chat_id: int, keyword: str) -> bool:
    filters = _filters_for_chat(chat_id)
    key     = keyword.lower().strip()
    if key not in filters:
        return False
    del filters[key]
    _save_filters_for_chat(chat_id, filters)
    return True

def filter_list(chat_id: int) -> list[str]:
    return sorted(_filters_for_chat(chat_id).keys())

def filter_count(chat_id: int) -> int:
    return len(_filters_for_chat(chat_id))

def filter_check(chat_id: int, text: str) -> tuple[str | None, dict | None]:
    if not text:
        return None, None
    filters    = _filters_for_chat(chat_id)
    text_lower = text.lower()
    for keyword, fdata in filters.items():
        match_type = fdata.get("match_type", "contains")
        matched = False
        if match_type == "exact":
            matched = text_lower == keyword
        elif match_type == "startswith":
            matched = text_lower.startswith(keyword)
        elif match_type == "regex":
            try:
                matched = bool(re.search(keyword, text, re.IGNORECASE))
            except re.error:
                pass
        else:
            matched = keyword in text_lower
        if matched:
            return keyword, fdata
    return None, None


def create_group_defaults(chat_id: int):
    files_defaults = {
        NOTES_FILE:       {},
        FILTERS_FILE:     {},
        BLOCKLIST_FILE:   {},
        WARN_FILE:        {},
        WARN_CONFIG_FILE: {"threshold": 3, "action": "mute", "duration": 3600},
        WELCOME_FILE:     {},
        GOODBYE_FILE:     {},
        RULES_FILE:       {},
    }
    for f, default in files_defaults.items():
        data = load(f)
        if isinstance(data, dict) and str(chat_id) not in data:
            data[str(chat_id)] = default
            save(f, data)
    bm = load(BLOCKLIST_MODE_FILE)
    if str(chat_id) not in bm:
        bm[str(chat_id)] = "warn"
        save(BLOCKLIST_MODE_FILE, bm)


def group_load(file: str, chat_id: int):
    data = load(file)
    if isinstance(data, dict):
        return data.get(str(chat_id), {})
    return {}


def group_save(file: str, chat_id: int, group_data):
    data = load(file)
    if not isinstance(data, dict):
        data = {}
    data[str(chat_id)] = group_data
    save(file, data)


def group_delete(file: str, chat_id: int):
    data = load(file)
    if not isinstance(data, dict):
        return False
    if str(chat_id) in data:
        data.pop(str(chat_id), None)
        save(file, data)
        return True
    return False

# =========================================================
# BLOCKLISTS SYSTEM
# =========================================================

def _blocklists_for_chat(chat_id: int) -> dict:
    cache_key = f"blocklists:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data = load(BLOCKLIST_FILE)
    result = data.get(str(chat_id), {})
    _cache.set(cache_key, result)
    return result

def _save_blocklists_for_chat(chat_id: int, blocks: dict):
    data = load(BLOCKLIST_FILE)
    data[str(chat_id)] = blocks
    save(BLOCKLIST_FILE, data)
    _cache.invalidate(f"blocklists:{chat_id}")

def blocklist_add(chat_id: int, keyword: str, created_by: int = None):
    blocks = _blocklists_for_chat(chat_id)
    blocks[keyword.lower().strip()] = {"created_by": created_by, "updated_at": str(datetime.now())}
    _save_blocklists_for_chat(chat_id, blocks)

def blocklist_remove(chat_id: int, keyword: str) -> bool:
    blocks = _blocklists_for_chat(chat_id)
    key = keyword.lower().strip()
    if key not in blocks:
        return False
    del blocks[key]
    _save_blocklists_for_chat(chat_id, blocks)
    return True

def blocklist_list(chat_id: int) -> list[str]:
    return sorted(_blocklists_for_chat(chat_id).keys())

def blocklist_count(chat_id: int) -> int:
    return len(_blocklists_for_chat(chat_id))

def blocklist_check(chat_id: int, text: str) -> str | None:
    if not text:
        return None
    blocks = _blocklists_for_chat(chat_id)
    text_lower = text.lower()
    for keyword in blocks.keys():
        if keyword in text_lower:
            return keyword
    return None

def get_blocklist_mode(chat_id: int) -> str:
    data = load(BLOCKLIST_MODE_FILE)
    return data.get(str(chat_id), "warn")

def set_blocklist_mode(chat_id: int, mode: str):
    data = load(BLOCKLIST_MODE_FILE)
    data[str(chat_id)] = mode
    save(BLOCKLIST_MODE_FILE, data)

# =========================================================
# CHAT TITLE CACHE
# =========================================================

def get_cached_chat_title(chat_id: int) -> str | None:
    data = load(CHAT_TITLES_FILE)
    return data.get(str(chat_id)) if isinstance(data, dict) else None

def cache_chat_title(chat_id: int, title: str):
    if not title:
        return
    data = load(CHAT_TITLES_FILE)
    if not isinstance(data, dict):
        data = {}
    data[str(chat_id)] = title
    save(CHAT_TITLES_FILE, data)

 # =========================================================
# BOT ON/OFF STATUS (per-chat)
# =========================================================

def is_bot_enabled(chat_id: int) -> bool:
    """Return True if the bot is active in this chat (default: True)."""
    cache_key = f"bot_enabled:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data   = load(BOT_STATUS_FILE)
    result = data.get(str(chat_id), True)   # default ON
    _cache.set(cache_key, result)
    return result

def set_bot_enabled(chat_id: int, enabled: bool):
    """Enable or disable the bot for a specific chat."""
    data = load(BOT_STATUS_FILE)
    data[str(chat_id)] = enabled
    save(BOT_STATUS_FILE, data)
    _cache.invalidate(f"bot_enabled:{chat_id}")   

# =========================================================
# WELCOME / GOODBYE / RULES / LOCKS
# =========================================================

def _escape_markdown(text: str) -> str:
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _user_display_name(user: dict) -> str:
    if not isinstance(user, dict):
        return "User"
    first = (user.get("first_name") or "").strip()
    last  = (user.get("last_name") or "").strip()
    full  = " ".join(x for x in (first, last) if x).strip()
    if full:
        return full
    uname = (user.get("username") or "").strip()
    return uname or "User"


def _format_welcome_text(template: str, user: dict) -> str:
    uid  = user.get("id") if isinstance(user, dict) else None
    name = _user_display_name(user)
    safe_name = _escape_markdown(name)
    mention = safe_name
    if isinstance(uid, int) and uid > 0:
        mention = f"[{safe_name}](tg://user?id={uid})"
    text = template or ""
    return (
        text.replace("{mention}", mention)
            .replace("{name}", safe_name)
            .replace("{id}", str(uid or ""))
    )


def _welcome_for_chat(chat_id: int) -> dict:
    cache_key = f"welcome:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data  = load(WELCOME_FILE)
    entry = data.get(str(chat_id), {}) if isinstance(data, dict) else {}
    _cache.set(cache_key, entry)
    return entry


def _save_welcome_for_chat(chat_id: int, entry: dict | None):
    data = load(WELCOME_FILE)
    if not isinstance(data, dict):
        data = {}
    if entry:
        data[str(chat_id)] = entry
    else:
        data.pop(str(chat_id), None)
    save(WELCOME_FILE, data)
    _cache.invalidate(f"welcome:{chat_id}")


def _goodbye_for_chat(chat_id: int) -> dict:
    cache_key = f"goodbye:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data  = load(GOODBYE_FILE)
    entry = data.get(str(chat_id), {}) if isinstance(data, dict) else {}
    _cache.set(cache_key, entry)
    return entry


def _save_goodbye_for_chat(chat_id: int, entry: dict | None):
    data = load(GOODBYE_FILE)
    if not isinstance(data, dict):
        data = {}
    if entry:
        data[str(chat_id)] = entry
    else:
        data.pop(str(chat_id), None)
    save(GOODBYE_FILE, data)
    _cache.invalidate(f"goodbye:{chat_id}")


def _rules_for_chat(chat_id: int) -> dict:
    cache_key = f"rules:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data  = load(RULES_FILE)
    entry = data.get(str(chat_id), {}) if isinstance(data, dict) else {}
    _cache.set(cache_key, entry)
    return entry


def _save_rules_for_chat(chat_id: int, entry: dict | None):
    data = load(RULES_FILE)
    if not isinstance(data, dict):
        data = {}
    if entry:
        data[str(chat_id)] = entry
    else:
        data.pop(str(chat_id), None)
    save(RULES_FILE, data)
    _cache.invalidate(f"rules:{chat_id}")


def _lock_for_chat(chat_id: int) -> dict | None:
    cache_key = f"lock:{chat_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data  = load(LOCKS_FILE)
    entry = data.get(str(chat_id)) if isinstance(data, dict) else None
    _cache.set(cache_key, entry)
    return entry


def _save_lock_for_chat(chat_id: int, entry: dict | None):
    data = load(LOCKS_FILE)
    if not isinstance(data, dict):
        data = {}
    if entry:
        data[str(chat_id)] = entry
    else:
        data.pop(str(chat_id), None)
    save(LOCKS_FILE, data)
    _cache.invalidate(f"lock:{chat_id}")

# =========================================================
# MISC HELPERS
# =========================================================

def _normalize_protected_store(raw) -> dict:
    data: dict[str, dict] = {}
    global_map: dict[str, bool] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key == "__global__" and isinstance(value, dict):
                for uid, flag in value.items():
                    if flag:
                        global_map[str(uid)] = True
                continue
            if isinstance(value, dict):
                cleaned = {str(uid): True for uid, flag in value.items() if flag}
                if cleaned:
                    data[str(key)] = cleaned
            else:
                if value:
                    global_map[str(key)] = True
    elif isinstance(raw, list):
        for uid in raw:
            global_map[str(uid)] = True
    if global_map:
        data["__global__"] = global_map
    return data


def _load_protected_store() -> dict:
    return _normalize_protected_store(load(PROTECT_FILE))


def is_protected(uid: int, chat_id: int | None = None) -> bool:
    cache_key = f"protected:{chat_id}:{uid}" if chat_id is not None else f"protected:{uid}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    data   = _load_protected_store()
    key    = str(uid)
    result = False
    if chat_id is not None:
        group = data.get(str(chat_id))
        if isinstance(group, dict) and key in group:
            result = True
    if not result:
        global_map = data.get("__global__")
        if isinstance(global_map, dict) and key in global_map:
            result = True
    _cache.set(cache_key, result)
    return result

def make_mention(user: dict) -> str:
    if not isinstance(user, dict):
        return "User"
    uid  = user.get("id")
    name = ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip() or "User"
    return f"[{name}](tg://user?id={uid})" if uid else name

def _message_link(chat: dict, message_id: int) -> str | None:
    if not isinstance(chat, dict) or not message_id:
        return None
    username = chat.get("username")
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = chat.get("id")
    if isinstance(chat_id, int):
        cid = str(chat_id)
        if cid.startswith("-100"):
            return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None

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
    sender_chat = reply.get("sender_chat")
    if isinstance(sender_chat, dict):
        return sender_chat, None
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
    if isinstance(reply, dict) and reply.get("sender_chat"):
        return {}, None, "❌ Channel messages are not supported for moderation commands."
    if tid:
        return target, tid, None
    if len(args) > idx:
        uid = parse_positive_user_id(args[idx])
        if uid:
            return {"id": uid, "first_name": "User"}, uid, None
        return {}, None, "❌ Invalid user ID."
    return {}, None, "❌ Reply to a user or pass their user ID."

async def resolve_target_ext(
    bot: Client, reply, args, idx
) -> tuple[dict, int | None, str | None]:
    target, tid = extract_reply_user(reply)
    if isinstance(reply, dict) and reply.get("sender_chat"):
        return {}, None, "❌ Channel messages are not supported for moderation commands."
    if tid:
        return target, tid, None
    if len(args) > idx:
        val = args[idx]
        if val.startswith("@"):
            username = val[1:]
            try:
                user = await bot.get_user(username)
                return (
                    {"id": user.id, "first_name": user.first_name or "User",
                     "last_name": user.last_name or ""},
                    user.id,
                    None,
                )
            except Exception as e:
                return {}, None, f"❌ User {val} not found: {e}"
        uid = parse_positive_user_id(val)
        if uid:
            return {"id": uid, "first_name": "User"}, uid, None
        return {}, None, "❌ Invalid user ID or username."
    return {}, None, "❌ Reply to a user or pass their user ID / @username."

def extract_reason(args, start, default="No Reason") -> str:
    return " ".join(args[start:]).strip() or default if len(args) > start else default

def create_case(action, moderator, target, reason, extra: dict | None = None) -> str:
    cases = load(CASE_FILE)
    cid   = str(len(cases) + 1)
    cases[cid] = {
        "action": action, "moderator": moderator,
        "target": target, "reason": reason,
        "time":   str(datetime.now()),
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

def warn(user_id: int, chat_id: int, reason: str, warner: str | None = None) -> dict:
    warns = load(WARN_FILE)
    key   = f"{chat_id}:{user_id}"
    if key not in warns and str(user_id) in warns:
        warns[key] = warns.pop(str(user_id))
    warns.setdefault(key, 0)
    warns[key] += 1
    save(WARN_FILE, warns)
    cfg           = get_warn_config()
    threshold     = int(cfg.get("threshold", 3))
    auto_action   = cfg.get("action", "mute")
    moderator_tag = warner or "Automated warn filter"
    num           = warns[key]
    if num >= threshold:
        warns[key] = 0
        save(WARN_FILE, warns)
        reply = f"{threshold} warnings — auto-{auto_action}!"
        log   = (
            f"#WARN_BAN\nAdmin: {moderator_tag}\nUser: {user_id}\n"
            f"Reason: {reason}\nCounts: {num}/{threshold}"
        )
        return {"action": auto_action, "reply": reply, "log": log, "num_warns": 0, "threshold": threshold}
    else:
        reply = f"User {user_id} has {num}/{threshold} warnings."
        if reason:
            reply += f" Reason: {reason}"
        log = (
            f"#WARN\nAdmin: {moderator_tag}\nUser: {user_id}\n"
            f"Reason: {reason}\nCounts: {num}/{threshold}"
        )
        return {"action": None, "reply": reply, "log": log, "num_warns": num, "threshold": threshold}

def reset_warns(user_id: int, chat_id: int, admin_tag: str | None = None) -> str:
    warns = load(WARN_FILE)
    key   = f"{chat_id}:{user_id}"
    if key not in warns and str(user_id) in warns:
        warns[key] = warns.pop(str(user_id))
    warns.pop(key, None)
    save(WARN_FILE, warns)
    actor = admin_tag or "system"
    return f"#RESETWARNS\nAdmin: {actor}\nUser: {user_id}"

def warns_for(user_id: int, chat_id: int) -> dict:
    warns = load(WARN_FILE)
    key   = f"{chat_id}:{user_id}"
    if key not in warns and str(user_id) in warns:
        warns[key] = warns.get(str(user_id), 0)
    num = int(warns.get(key, 0))
    return {"num_warns": num, "reasons": []}

def save_warn_config(config: dict):
    save(WARN_CONFIG_FILE, config)

def schedule_message_delete(chat_id: int, message_id: int, delay: int = 60):
    actions = load_temp_actions()
    actions.append({
        "type":      "delete",
        "chat_id":   chat_id,
        "target_id": message_id,
        "until_ts":  int(time.time()) + delay,
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

def _chat_permissions_payload(chat) -> dict:
    perms = getattr(chat, "permissions", None)
    if perms is None:
        return _FULL_PERMISSIONS
    fields = [
        "can_send_messages", "can_send_audios", "can_send_documents",
        "can_send_photos", "can_send_videos", "can_send_video_notes",
        "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
        "can_add_web_page_previews", "can_invite_users",
    ]
    return {field: bool(getattr(perms, field, True)) for field in fields}

def grant_all_permissions() -> dict:
    return {perm: True for perm in sorted(VALID_PERMISSIONS)}

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

async def scan_zombies(bot: Client, chat_id: int, bot_id: int) -> tuple[int, int, list[str]]:
    kicked_deleted = 0
    kicked_bots    = 0
    failures: list[str] = []
    async for member in bot.get_chat_members(chat_id):
        user = getattr(member, "user", None)
        if not user:
            continue
        if member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR):
            continue
        if user.id == bot_id:
            continue
        first_name = (getattr(user, "first_name", "") or "").strip()
        is_deleted = (
            bool(getattr(user, "is_deleted", False))
            or first_name.lower() == "deleted account"
            or first_name.lower().startswith("deleted")
        )
        if not is_deleted:
            continue
        ok, err = await api_kick(chat_id, user.id)
        if ok:
            kicked_deleted += 1
        else:
            failures.append(f"{user.id}: {err}")
    return kicked_deleted, kicked_bots, failures

def schedule_temp_action(
    action_type: str, chat_id: int, target_id: int,
    until_ts: int, set_by: int, reason: str, case_id: str | None = None,
    extra: dict | None = None,
):
    actions = load_temp_actions()
    action  = {
        "type": action_type, "chat_id": chat_id,
        "target_id": target_id, "until_ts": until_ts,
        "set_by": set_by, "reason": reason,
    }
    if case_id:
        action["case_id"] = case_id
    if isinstance(extra, dict):
        action.update(extra)
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
            "`/hunauth <user_id>` - Remove moderator status\n"
            "`/hrevoke <perm|all> <user_id>` - Remove permission(s)\n"
            "`/hgrant <user_id>` - Grant all permissions\n"
            "`/hgrant <perm> <user_id>` - Grant one permission\n"
            "`/hfreeze <user_id>` - Freeze moderator account\n"
            "`/hunfreeze <user_id>` - Unfreeze moderator account\n"
            "`/hbadge <user_id> <badge text>` - Set mod badge/title\n\n"
            "⚙️ **Warn Configuration:**\n"
            "`/hwarnconfig threshold <n>` - Set warn threshold\n"
            "`/hwarnconfig action <ban|mute|kick>` - Set auto-action\n"
            "`/hwarnconfig duration <e.g. 1h>` - Set auto-action duration\n"
            "`/hwarnconfig show` - Show current config\n\n"
            "📋 **Notes (group-scoped):**\n"
            "`/save <name> <text>` - Save a text note\n"
            "`/save <name>` (reply) - Save replied message as note\n"
            "`/get <name>` or `#name` - Retrieve a note\n"
            "`/clear <name>` - Delete a note\n"
            "`/notes` - List all notes\n\n"
            "🔍 **Filters (auto-reply keywords):**\n"
            "`/filter <keyword> <response>` - Add a filter\n"
            "`/filter -regex <pattern> <response>` - Regex filter\n"
            "`/filter -exact <keyword> <response>` - Exact-match filter\n"
            "`/stop <keyword>` - Remove a filter\n"
            "`/filters` - List all filters\n\n"
            "🛡️ **Protection Commands:**\n"
            "`/hprotect <user_id>` - Protect user from moderation\n"
            "`/hunprotect <user_id>` - Remove user protection\n"
            "`/hprotected` - List protected users\n\n"
            "🎉 **Welcome / Goodbye:**\n"
            "`/setwelcome <text>` - Set welcome message\n"
            "`/setgoodbye <text>` - Set goodbye message\n"
            "`/welcome on|off` - Toggle welcome on/off\n"
            "`/goodbye on|off` - Toggle goodbye on/off\n"
            "`/setrules <text>` - Set group rules\n"
            "`/rules on|off` - Toggle rules command\n\n"
            "🚨 **Report / Lock:**\n"
            "`/report [reason]` - Report a message to admins\n"
            "`/hlock [duration]` - Lock the chat\n"
            "`/hunlock` - Unlock the chat\n\n"
            "📋 **Moderation Commands:**\n"
            "`/hban [user_id/@user] [duration] [reason]` - Ban user\n"
            "`/hkick <user_id/@user> [reason]` - Kick user from group\n"
            "`/hmute [user_id/@user] [duration] [reason]` - Mute user\n"
            "`/hunban [user_id/@user] [reason]` - Unban a user\n"
            "`/hunmute [user_id/@user] [reason]` - Unmute a user\n"
            "`/hstats` - Show moderation stats\n"
            "`/hmod list` - List authorized moderators\n"
            "`/hwarn [user_id/@user] [reason]` - Warn user\n"
            "`/hdel` - Delete replied message\n"
            "`/hcase <case_id>` - View case details\n"
            "`/hmodinfo [user_id]` - View moderator info\n\n"
            "🔗 **Connections (PM multi-group):**\n"
            "`/allowconnections yes|no` - Allow PM connection to this group\n"
            "`/connect <chat_id>` - Connect PM to a group\n"
            "`/connections` - List & switch connected groups\n"
            "`/disconnect [chat_id|all]` - Disconnect from a group\n\n"
            "🎮 **Games:**\n"
            "`/ttt [user_id]` - Start Tic-Tac-Toe\n"
            "`/tttleaderboard` - Show top players\n"
            "`/tttmystats` - Show your game stats\n"
            "`/tttend` - Forfeit active game\n\n"
            "⏱️ **Duration Format:** 30m, 2h, 1d\n"
            "💡 **Tip:** Reply to a message to target without ID\n"
        )
    if is_authorized(uid):
        return (
            "╔════════════════════════════════════════╗\n"
            "║  👮 MODERATOR COMMAND REFERENCE       ║\n"
            "╚════════════════════════════════════════╝\n\n"
            "🚫 **Moderation Commands:**\n"
            "`/hban [user_id/@user] [duration] [reason]` - Ban user\n"
            "`/hkick <user_id/@user> [reason]` - Kick user from group\n"
            "`/hmute [user_id/@user] [duration] [reason]` - Mute user\n"
            "`/hunban [user_id/@user] [reason]` - Unban a user\n"
            "`/hunmute [user_id/@user] [reason]` - Unmute a user\n"
            "`/hstats` - Show moderation stats\n"
            "`/hwarn [user_id/@user] [reason]` - Warn user\n"
            "`/hdel` - Delete replied message\n\n"
            "📋 **Notes:**\n"
            "`/save <name> <text>` - Save a note\n"
            "`/get <name>` or `#name` - Get a note\n"
            "`/clear <name>` - Delete a note\n"
            "`/notes` - List all notes\n\n"
            "🔍 **Filters:**\n"
            "`/filter <keyword> <response>` - Add keyword auto-reply\n"
            "`/stop <keyword>` - Remove a filter\n"
            "`/filters` - List all filters\n\n"
            "📋 **Information Commands:**\n"
            "`/hcase <case_id>` - View case details\n"
            "`/hmod list` - List authorized moderators\n"
            "`/hmodinfo` - View your moderator info\n"
            "`/hr` - Get user information\n\n"
            "🔗 **Connections:**\n"
            "`/connect <chat_id>` - Connect PM to a group\n"
            "`/connections` - List & switch connected groups\n"
            "`/disconnect [chat_id|all]` - Disconnect\n\n"
            "🎮 **Games:**\n"
            "`/ttt [user_id]` - Start Tic-Tac-Toe\n"
            "`/tttleaderboard` - Show top players\n\n"
            "⏱️ **Duration Format:** 30m, 2h, 1d\n"
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
        "📋 **Notes:**\n"
        "`/get <name>` or `#name` - Get a saved note\n"
        "`/notes` - List available notes\n\n"
        "📢 **Appeals:**\n"
        "`/happeal <case_id> <message>` in bot DM\n\n"
        "📞 **Support:**\n"
        "Contact your group administrator for assistance\n"
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


# FIX-C: removed dead unreachable code block that appeared after the returns
def moderation_help_markup(section: str = "home") -> dict:
    base_rows = (
        (("🚫 Ban", "cb:help_ban"), ("🔇 Mute", "cb:help_mute"), ("⚠ Warn", "cb:help_warn")),
        (("👢 Kick", "cb:help_kick"), ("🛡 Protect", "cb:help_protect"), ("📋 Notes", "cb:help_notes")),
        (("🔍 Filters", "cb:help_filters"), ("🔒 Blocklist", "cb:help_blocklist"), ("🔗 Connections", "cb:help_connections")),
        (("🔐 Authorization", "cb:help_auth"), ("📊 Stats", "cb:help_stats"), ("🎮 Games", "cb:help_games")),
        (("🎉 Welcome", "cb:help_welcome"), ("📜 Rules", "cb:help_rules"), ("🚨 Report", "cb:help_report")),
        (("🔒 Lock", "cb:help_lock"),),
    )
    if section == "home":
        return build_markup(*base_rows)
    return build_markup(*base_rows, (("⬅️ Back", "cb:help_home"),))


def moderation_help_text(section: str, uid: int) -> str:
    role = "OWNER" if is_owner(uid) else "MODERATOR"
    if section == "ban":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🚫 **Ban System**\n\n"
            "`/hban [user_id/@user] [duration] [reason]` - Ban a user\n"
            "`/hunban [user_id/@user] [reason]` - Unban a user\n"
            "`/hbans` - List all bans in this group\n\n"
            "**Usage**\n"
            "• Reply to a user's message OR pass user ID/username\n"
            "• Duration: Optional (default permanent)\n"
            "  Examples: `30m`, `2h`, `1d`, `7d`\n"
            "• Reason: Logged in case file\n"
            "• Example: `/hban @user 2h spam`\n\n"
            "**Notes**\n"
            "• Protected users cannot be banned\n"
            "• Temporary bans auto-expire and remove restriction\n"
            "• Use `/hunban` to manually unban before expiry\n"
        )
    if section == "mute":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🔇 **Mute System**\n\n"
            "`/hmute [user_id/@user] [duration] [reason]` - Mute a user\n"
            "`/hunmute [user_id/@user] [reason]` - Unmute a user\n"
            "`/hmutes` - List all mutes in this group\n\n"
            "**Usage**\n"
            "• Reply to a user's message OR pass user ID/username\n"
            "• Duration: Optional (default permanent)\n"
            "  Examples: `30m`, `2h`, `1d`, `7d`\n"
            "• Muted users cannot send messages\n"
            "• Example: `/hmute @user 1h flooding`\n\n"
            "**Notes**\n"
            "• Protected users cannot be muted\n"
            "• Temporary mutes auto-expire and restore permissions\n"
            "• Admin can always override mute\n"
        )
    if section == "warn":
        return (
            f"👮 **{role} Help Center**\n\n"
            "⚠ **Warn System**\n\n"
            "`/hwarn [user_id/@user] [reason]` - Issue a warning to a user\n"
            "`/warns [user_id/@user]` - Check total warnings for a user\n"
            "`/resetwarns [user_id/@user]` - Reset warnings for a user\n"
            "`/hwarnconfig` - Configure auto-action thresholds\n\n"
            "**Usage**\n"
            "• Reply to a user's message or pass their ID/username.\n"
            "• Warn system can auto-mute/ban after threshold warnings.\n"
            "• Example: `/hwarn @user off-topic`\n"
        )
    if section == "kick":
        return (
            f"👮 **{role} Help Center**\n\n"
            "👢 **Kick System**\n\n"
            "`/hkick <user_id/@user> [reason]` - Kick a user from group\n\n"
            "**Usage**\n"
            "• Reply to a user's message OR pass user ID/username\n"
            "• User is immediately removed from group\n"
            "• User can rejoin unless also banned\n"
            "• Example: `/hkick @user rule violation`\n\n"
            "**Features**\n"
            "• Instant removal - no waiting\n"
            "• Different from ban - user can rejoin\n"
            "• Often used with warning before ban\n"
            "• Protected users cannot be kicked\n"
        )
    if section == "protect":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🛡 **Protection Commands**\n\n"
            "`/hprotect <user_id>` - Protect a user from moderation\n"
            "`/hunprotect <user_id>` - Remove user protection\n"
            "`/hprotected` - List all protected users\n\n"
            "**Usage**\n"
            "• Owner-only commands.\n"
            "• Protected users cannot be banned, muted, kicked, or warned.\n"
            "• Use for bots, admins, or trusted members.\n"
            "• Example: `/hprotect 123456789`\n"
        )
    if section == "welcome":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🎉 **Welcome & Goodbye**\n\n"
            "`/setwelcome <text>` — Set welcome message\n"
            "`/setgoodbye <text>` — Set goodbye message\n"
            "`/welcome on|off` — Enable or disable welcome\n"
            "`/goodbye on|off` — Enable or disable goodbye\n"
            "`/welcome` — View current welcome message & status\n"
            "`/goodbye` — View current goodbye message & status\n\n"
            "**Variables**\n"
            "• `{mention}` — Clickable user mention\n"
            "• `{name}` — User display name\n"
            "• `{id}` — User ID\n\n"
            "**Examples**\n"
            "• `/setwelcome Hello {mention}, welcome to the group! 👋`\n"
            "• `/setgoodbye Goodbye {name}, we'll miss you!`\n"
        )
    if section == "rules":
        return (
            f"👮 **{role} Help Center**\n\n"
            "📜 **Rules**\n\n"
            "`/setrules <text>` — Set or update group rules\n"
            "`/rules` — Show current rules (members)\n"
            "`/rules on|off` — Enable or disable the rules command\n\n"
            "**Usage**\n"
            "• Use `/setrules off` to clear rules entirely.\n"
            "• Use `/rules off` to hide rules without deleting them.\n"
            "• Example: `/setrules 1) Be respectful 2) No spam 3) Stay on topic`\n"
        )
    if section == "report":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🚨 **Report System**\n\n"
            "`/report [reason]` — Alert all admins and the log group\n\n"
            "**Usage**\n"
            "• Reply to the offending message, then run `/report`.\n"
            "• Optionally include a reason: `/report posting scam links`\n"
            "• The report is sent privately to every admin and to the log group.\n"
            "• You'll see a confirmation message when the report is sent.\n"
        )
    if section == "lock":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🔒 **Chat Lock**\n\n"
            "`/hlock` — Lock chat until manually unlocked\n"
            "`/hlock <duration>` — Lock for a fixed time, then auto-unlock\n"
            "`/hunlock` — Restore original chat permissions\n\n"
            "**Duration Format:** `10m`, `2h`, `1d`\n\n"
            "**Notes**\n"
            "• Original permissions are saved and fully restored on unlock.\n"
            "• Admins are unaffected.\n"
            "• If the bot restarts during a timed lock, the lock expires "
            "automatically when the temp-action worker next runs.\n"
        )
    if section == "notes":
        return (
            f"👮 **{role} Help Center**\n\n"
            "📋 **Notes System**\n\n"
            "`/notes` - List all saved notes for this group\n"
            "`/save <name> <text>` - Save a note\n"
            "`/save <name>` (reply) - Save replied content as a note\n"
            "`/get <name>` or `#name` - Retrieve a note\n"
            "`/clear <name>` - Delete a note\n\n"
            "**Usage**\n"
            "• Use notes to store group rules, FAQs, or information.\n"
            "• Notes are group-specific and persistent.\n"
            "• Example: `/save rules We have 3 main rules...`\n"
            "• Retrieve: `/get rules` or `#rules`\n"
        )
    if section == "filters":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🔍 **Auto-Response Filters**\n\n"
            "`/filters` - List all active filters\n"
            "`/filter <keyword> <response>` - Auto-reply on keyword match\n"
            "`/filter -exact <keyword> <response>` - Exact word match only\n"
            "`/filter -start <keyword> <response>` - Starts with keyword\n"
            "`/filter -regex <pattern> <response>` - Regex pattern matching\n"
            "`/stop <keyword>` - Remove a filter\n\n"
            "**Usage**\n"
            "• Filters auto-respond when keywords are mentioned.\n"
            "• Example: `/filter hello Hello there! 👋`\n"
        )
    if section == "connections":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🔗 **Group Connections (Multi-Group Management)**\n\n"
            "`/connect <chat_id>` - Connect PM to manage a group\n"
            "`/connections` - View all connected groups and switch\n"
            "`/disconnect [chat_id|all]` - Disconnect from a group\n"
            "`/allowconnections yes|no` - Allow/block PM connections\n\n"
            "**Usage**\n"
            "• Connect PMs to manage multiple groups from one bot instance.\n"
            "• Each connection has isolated storage (notes, filters, warns, etc.).\n"
            "• Example: `/connect -1001234567890`\n"
            "• Get chat_id using `/hr` in the target group.\n"
        )
    if section == "blocklist":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🔒 **Blocklist System**\n\n"
            "`/addblocklist <keyword>` - Add a keyword to this group's blocklist\n"
            "`/deleteblocklist <keyword>` - Remove a blocklist keyword\n"
            "`/blocklists` - View all blocked keywords for this group\n"
            "`/blocklistmode [warn|mute|ban]` - Get or set action for blocked keywords\n\n"
            "**Usage**\n"
            "• Blocked messages are auto-deleted.\n"
            "• Action can be: warn (default), mute, or ban user.\n"
            "• Example: `/addblocklist spam` then `/blocklistmode mute`\n"
        )
    if section == "games":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🎮 **Games & Entertainment**\n\n"
            "`/ttt [user_id]` - Start Tic-Tac-Toe with a user\n"
            "`/ttt [user_id] 5` - 5x5 board (default 3x3)\n"
            "`/tttleaderboard` - Show top Tic-Tac-Toe players\n"
            "`/tttmystats` - Show your game statistics\n"
            "`/tttend` - Forfeit your active game\n\n"
            "**Features**\n"
            "• Play against other users with inline buttons.\n"
            "• Rankings and stats tracking.\n"
            "• Games are group-wide or PM-based.\n"
        )
    if section == "stats":
        return (
            f"👮 **{role} Help Center**\n\n"
            "📊 **Moderation Stats & Info**\n\n"
            "`/hstats` - Show moderation statistics (bans, mutes, warns, etc.)\n"
            "`/hmod list` - List all authorized moderators\n"
            "`/hmodinfo [user_id]` - View specific moderator's info\n"
            "`/hcase <case_id>` - View details of a specific case\n"
            "`/hdel` - Delete the replied message\n"
            "`/hr [@user/user_id]` - Get user information & moderation history\n\n"
            "**Features**\n"
            "• Track all mod actions in case logs.\n"
            "• View user info and ban/mute history.\n"
            "• Monitor moderator activity.\n"
        )
    if section == "auth":
        return (
            f"👮 **{role} Help Center**\n\n"
            "🔐 **Authorization & Permissions**\n\n"
            "`/hauth <user_id>` - Make someone a moderator (Owner only)\n"
            "`/hunauth <user_id>` - Remove moderator status (Owner only)\n"
            "`/hgrant <user_id> <perm>` - Grant a specific permission\n"
            "`/hrevoke <user_id> <perm>` - Revoke a specific permission\n"
            "`/hfreeze <user_id>` - Freeze a moderator's permissions\n"
            "`/hunfreeze <user_id>` - Unfreeze a moderator\n"
            "`/hbadge <user_id> <badge_text>` - Set custom moderator badge\n"
            "`/hwarnconfig` - Configure auto-action thresholds\n\n"
            "**Permissions**\n"
            "• ban, unban, mute, unmute, kick, warn, protect, auth, notes, filters\n"
        )
    return (
        f"👮 **{role} Help Center**\n\n"
        "**Welcome to Moderation Commands!**\n\n"
        "Select a category below to learn about each feature:\n\n"
        "🔨 **Moderation**\n"
        "  • Ban, Mute, Kick, Warn\n\n"
        "🛠️ **Management**\n"
        "  • Protect, Notes, Filters, Blocklist\n\n"
        "🔧 **Advanced**\n"
        "  • Connections, Authorization, Stats, Games\n\n"
        "💡 **Tips:**\n"
        "  • Reply to a message to avoid typing user IDs\n"
        "  • Duration: `30m`, `2h`, `1d`, etc.\n"
        "  • All data is group-specific and persistent\n"
    )

# =========================================================
# PYROGRAM CLIENT
# =========================================================

_bot: Client       = None
_bot_id: int       = 0
bot_ready: bool    = False
_temp_worker_task  = None
_games_worker_task = None

async def get_bot() -> Client:
    global _bot, bot_ready, _bot_id
    if _bot is None or not _bot.is_connected:
        log_msg("Initializing Pyrogram client...", "INFO")
        _bot = Client(
            name="modbot", api_id=API_ID, api_hash=API_HASH,
            bot_token=BOT_TOKEN, in_memory=True, no_updates=True,
        )
        await _bot.start()
        bot_ready = True
        me = await _bot.get_me()
        _bot_id = me.id
        log_msg(f"✅ Authenticated as @{me.username} (id={_bot_id})", "INFO")
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
    _log_group_id = groups[0]
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

async def anti_nuke(chat_id: int, reply_to: int, uid: int, is_anon: bool = False) -> bool:
    total = track_action(uid)
    if total < 10:
        return False
    if not is_anon:
        auth = load(AUTH_FILE)
        if str(uid) in auth:
            auth[str(uid)]["frozen"] = True
            save(AUTH_FILE, auth)
    lg = get_log_group()
    actor_label = f"Anon Admin (chat {chat_id})" if is_anon else f"`{uid}`"
    if lg:
        await tg_send(
            lg,
            f"🚨 **ANTI-NUKE ACTIVATED**\n\n"
            f"Actor: {actor_label}\n"
            f"Actions in 60 sec: `{total}`\n"
            f"{'⚠️ Anonymous admin — cannot freeze' if is_anon else 'Moderator frozen automatically.'}",
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

    chat_to_send = None
    try:
        if isinstance(source_chat, dict):
            chat_to_send = source_chat.get("id")
        else:
            chat_to_send = int(source_chat)
    except Exception:
        chat_to_send = None

    resp = await tg_send(chat_to_send if chat_to_send is not None else source_chat, text, reply_to=reply_to, markup=markup)
    if resp.get("ok") and resp.get("result"):
        msg_id = resp["result"].get("message_id")
        if msg_id and chat_to_send is not None:
            schedule_message_delete(chat_to_send, msg_id, ACTION_LOG_AUTO_DELETE)

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
        until_ts = int(action.get("until_ts", 0))
        if until_ts > now_ts:
            pending.append(action)
            continue
        chat_id_raw   = action.get("chat_id")
        target_id_raw = action.get("target_id")
        try:
            chat_id = int(chat_id_raw)
        except Exception:
            log_msg(f"Skipping temp action with invalid chat_id: {chat_id_raw}", "WARNING")
            continue
        try:
            target_id = int(target_id_raw)
        except Exception:
            log_msg(f"Skipping temp action with invalid target_id: {target_id_raw}", "WARNING")
            continue
        atype = action["type"]
        try:
            if atype == "mute":
                permissions = _FULL_PERMISSIONS
                try:
                    chat = await bot.get_chat(chat_id)
                    permissions = _chat_permissions_payload(chat)
                except Exception as perm_err:
                    log_msg(f"temp unmute permission load failed for {chat_id}: {perm_err}", "WARNING")
                ok, err = await api_unmute(chat_id, target_id, permissions=permissions)
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
            elif atype == "lock":
                restore_perms = action.get("permissions") if isinstance(action.get("permissions"), dict) else _FULL_PERMISSIONS
                ok, err = await api_set_chat_permissions(chat_id, restore_perms)
                if ok:
                    _save_lock_for_chat(chat_id, None)
                    msg_text = f"🔓 Chat unlocked\n⏰ Expired: {format_timestamp(until_ts)}"
                    try:
                        await tg_send(chat_id, msg_text)
                    except Exception as send_err:
                        log_msg(f"temp lock update failed for {chat_id}: {send_err}", "WARNING")
                else:
                    pending.append(action)
                    log_msg(f"auto-unlock failed for {chat_id}: {err}", "WARNING")
                    continue
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
        text       = msg.get("text", "")
        chat_id    = msg["chat"]["id"]
        msg_id     = msg["message_id"]
        is_private = msg.get("chat", {}).get("type") == "private"
        uid, err, is_anon_admin = extract_actor_user_id(msg)
        reply      = msg.get("reply_to_message") or {}

        if not is_private:
            new_members = msg.get("new_chat_members") or []
            if not new_members and msg.get("new_chat_member"):
                new_members = [msg.get("new_chat_member")]
            if new_members:
                welcome_entry = _welcome_for_chat(chat_id)
                welcome_text  = welcome_entry.get("text") if isinstance(welcome_entry, dict) else None
                welcome_on    = welcome_entry.get("enabled", True) if isinstance(welcome_entry, dict) else False
                if welcome_text and welcome_on:
                    for member in new_members:
                        if not isinstance(member, dict):
                            continue
                        if member.get("id") == _bot_id:
                            continue
                        rendered = _format_welcome_text(welcome_text, member)
                        await tg_send(chat_id, rendered, reply_to=msg_id)

            left_member = msg.get("left_chat_member")
            if isinstance(left_member, dict):
                if left_member.get("id") != _bot_id:
                    goodbye_entry = _goodbye_for_chat(chat_id)
                    goodbye_text  = goodbye_entry.get("text") if isinstance(goodbye_entry, dict) else None
                    goodbye_on    = goodbye_entry.get("enabled", True) if isinstance(goodbye_entry, dict) else False
                    if goodbye_text and goodbye_on:
                        rendered = _format_welcome_text(goodbye_text, left_member)
                        await tg_send(chat_id, rendered, reply_to=msg_id)

        # ── Non-command messages: filters + #note triggers ────────────────
        if not text.startswith("/"):
            if not is_private and text:
                # 1) #notename trigger
                hashtags = re.findall(r'#(\w+)', text)
                for tag in hashtags:
                    note = note_get(chat_id, tag.lower())
                    if note:
                        content = note.get("content", "")
                        if note.get("entities"):
                            await tg_send(chat_id, content, reply_to=msg_id, parse_mode=None, entities=note.get("entities"))
                        else:
                            if re.search(r"\[.+?\]\(https?://[^\s)]+\)", content):
                                await tg_send(chat_id, content, reply_to=msg_id, parse_mode="Markdown")
                            elif "<a " in content:
                                await tg_send(chat_id, content, reply_to=msg_id, parse_mode="HTML")
                            else:
                                await tg_send(chat_id, content, reply_to=msg_id, parse_mode=None)
                        return

                # 2a) Blocklist check
                bkw = blocklist_check(chat_id, text)
                if bkw:
                    if not is_protected(uid, chat_id) and uid != OWNER_ID:
                        mode = get_blocklist_mode(chat_id)
                        try:
                            await api_delete_msg(chat_id, msg_id)
                        except Exception:
                            pass
                        actor       = 0
                        case_reason = f"Blocked keyword: {bkw}"
                        if mode == "ban":
                            ok, _ = await api_ban(chat_id, uid)
                            if ok:
                                cid = create_case("BAN", actor, uid, case_reason)
                                await send_action_log(chat_id, msg_id, "BAN", {"id": uid}, case_reason, cid, {"badge": "🔒 Blocklist", "mod_id": "BLOCKLIST"})
                        elif mode == "mute":
                            ok, _ = await api_mute(chat_id, uid)
                            if ok:
                                cid = create_case("MUTE", actor, uid, case_reason)
                                await send_action_log(chat_id, msg_id, "MUTE", {"id": uid}, case_reason, cid, {"badge": "🔒 Blocklist", "mod_id": "BLOCKLIST"})
                        else:
                            res = warn(uid, chat_id, case_reason, warner="BLOCKLIST")
                            cid = create_case("WARN", actor, uid, case_reason)
                            await send_action_log(chat_id, msg_id, "WARN", {"id": uid}, case_reason, cid, {"badge": "🔒 Blocklist", "mod_id": "BLOCKLIST"}, extra=f"📊 Total Warns: {res.get('num_warns', 0)}/{res.get('threshold', 0)}")
                    return

                # 2b) Filters check
                keyword, fdata = filter_check(chat_id, text)
                if keyword and fdata:
                    await tg_send(chat_id, fdata["response"], reply_to=msg_id, parse_mode=None)
            return

        # ── Parse command ─────────────────────────────────────────────────
        parts   = text.split(None, 1)
        raw_cmd = parts[0].split("@")[0].lstrip("/").lower()
        args    = parts[1].split() if len(parts) > 1 else []

        actor_label = f"anon_admin:{chat_id}" if is_anon_admin else str(uid)
        log_msg(f"/{raw_cmd} from actor={actor_label} chat={chat_id}", "INFO")

        async def reply_text(t: str, markup: dict = None, parse_mode: str = "Markdown"):
            sent = await tg_send(chat_id, t, reply_to=msg_id, markup=markup, parse_mode=parse_mode)
            if not is_private and sent.get("ok"):
                rmid = sent.get("result", {}).get("message_id")
                if rmid:
                    schedule_message_delete(chat_id, rmid)

        if not is_private:
            schedule_message_delete(chat_id, msg_id)

        if err:
            await reply_text(err)
            return

        if OWNER_DEBUG_NOTIFICATIONS and raw_cmd not in ("start", "help"):
            await notify_owner(f"📨 /{raw_cmd}\nActor: `{actor_label}`\nChat: `{chat_id}`")

        action_chat_id = chat_id
        if is_private and raw_cmd in MODERATION_COMMANDS:
            resolved = await connected(bot, msg.get("chat", {}), uid, need_admin=False)
            if not resolved:
                return await reply_text(
                    "❌ You are not connected to any group.\n"
                    "Use `/connect <chat_id>` to connect."
                )
            action_chat_id = resolved

      # ── Bot ON/OFF guard ──────────────────────────────────────────────
        if raw_cmd != "bot":
            effective_chat = chat_id
            _caller_is_owner = (not is_anon_admin) and is_owner(uid)
            if not is_bot_enabled(effective_chat) and not _caller_is_owner:
                return

        # ── Actor helpers ─────────────────────────────────────────────────
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

        # ── /allowconnections ─────────────────────────────────────────────
        if raw_cmd == "allowconnections":
            message = await allow_connections(bot, msg, args)
            if message:
                await reply_text(message)
            return

        # ── /connect ──────────────────────────────────────────────────────
        if raw_cmd == "connect":
            if is_private:
                message = await connect_chat(bot, msg, args)
                if message:
                    await reply_text(message)
            else:
                try:
                    me = await bot.get_me()
                    pm_link = f"https://t.me/{me.username}?start=connect_{chat_id}"
                    try:
                        chat_obj = await bot.get_chat(chat_id)
                        title = chat_obj.title
                    except Exception:
                        title = None
                    if title:
                        msg_text  = f"🔗 Connect *{title}* (`{chat_id}`) in PM — tap the button below."
                        btn_label = f"Connect to {title[:30]}"
                    else:
                        msg_text  = "🔗 Tap the button below to connect this group in PM."
                        btn_label = "Connect to PM"
                    await tg_send(
                        chat_id, msg_text, reply_to=msg_id,
                        markup=build_markup([(btn_label, f"url:{pm_link}")]),
                    )
                except Exception:
                    await reply_text("❌ Could not create the PM connect link.")
            return

        # ── /disconnect ───────────────────────────────────────────────────
        if raw_cmd == "disconnect":
            if is_private:
                message = await disconnect_chat(bot, msg, args)
                if message:
                    await reply_text(message)
            else:
                try:
                    me = await bot.get_me()
                    pm_link = f"https://t.me/{me.username}?start=disconnect_{chat_id}"
                    try:
                        chat_obj = await bot.get_chat(chat_id)
                        title = chat_obj.title
                    except Exception:
                        title = None
                    if title:
                        msg_text  = f"🔗 Disconnect *{title}* (`{chat_id}`) in PM — tap the button below."
                        btn_label = f"Disconnect {title[:30]}"
                    else:
                        msg_text  = "🔗 Tap the button below to disconnect this group in PM."
                        btn_label = "Disconnect from PM"
                    await tg_send(
                        chat_id, msg_text, reply_to=msg_id,
                        markup=build_markup([(btn_label, f"url:{pm_link}")]),
                    )
                except Exception:
                    await reply_text("❌ Could not create the PM disconnect link.")
            return

        # ── /connections ──────────────────────────────────────────────────
        if raw_cmd == "connections":
            if not is_private:
                return await reply_text("Use /connections in bot DM.")
            chats  = get_connected_chats(uid)
            active = get_active_connection(uid)
            if not chats:
                return await reply_text(
                    "You are not connected to any group.\n"
                    "Use `/connect <chat_id>` to connect."
                )
            lines = ["🔗 **Your Connected Groups**\n"]
            rows  = []
            for cid in chats:
                title = None
                try:
                    chat_obj = await bot.get_chat(cid)
                    title = getattr(chat_obj, "title", None)
                    if title:
                        cache_chat_title(cid, title)
                except Exception:
                    title = get_cached_chat_title(cid)
                star         = " ⭐" if cid == active else ""
                display_id   = f"`{cid}`"
                if title:
                    lines.append(f"• *{title}*{star} ({display_id})")
                    display_label = title[:20]
                else:
                    lines.append(f"• {display_id}{star}")
                    display_label = str(cid)
                btn_text = "✅ Active" if cid == active else f"Switch → {display_label}"
                rows.append([(btn_text, f"cb:setactive_{cid}")])
            markup = build_markup(*rows)
            await tg_send(
                chat_id,
                "\n".join(lines) + "\n\n⭐ = currently active group",
                reply_to=msg_id,
                markup=markup,
            )
            return

        # ── /start ────────────────────────────────────────────────────────
        if raw_cmd == "start":
            if args and args[0].startswith("connect_"):
                try:
                    connect_id = int(args[0].split("_", 1)[1])
                except ValueError:
                    return await reply_text("❌ Invalid connection link.")
                message = await connect_user_to_chat(bot, uid, connect_id)
                if message:
                    await reply_text(message)
                return
            if args and args[0].startswith("disconnect_"):
                try:
                    disconnect_id = int(args[0].split("_", 1)[1])
                except ValueError:
                    return await reply_text("❌ Invalid disconnect link.")
                message = await disconnect_user_from_chat(bot, uid, disconnect_id)
                if message:
                    await reply_text(message)
                return
            await reply_text(
                "🛡️ **SentriX Prime**\n\n"
                "Elite moderation + advanced utility tools for your group.\n\n"
                "**Quick Commands**\n"
                "• `/help` - Role-based commands\n"
                "• `/hr` - User insights\n"
                "• `/notes` - Group notes\n"
                "• `/filters` - Active filters\n"
                "• `/ttt` - Tic-Tac-Toe battle\n"
                "• `/happeal` - Appeal in DM\n\n"
                "👨‍💻 Developed by @dreamm\_ca\n"
                "⚙️ Sudo Dev: @developer\_hr\n\n"
                "⚡ Active. Secure. Ready."
            )
            return

        # ── /help ─────────────────────────────────────────────────────────
        if raw_cmd == "help":
            if is_anon_admin or is_authorized(uid):
                await reply_text(
                    moderation_help_text("home", uid),
                    markup=moderation_help_markup("home"),
                )
            else:
                await reply_text(role_help_text(uid))
            return

        # ── /setwelcome ───────────────────────────────────────────────────
        if raw_cmd == "setwelcome":
            if not is_authorized_actor():
                return await security_fail()
            raw_text = parts[1].strip() if len(parts) > 1 else ""
            if not raw_text and reply:
                raw_text = (reply.get("text") or reply.get("caption") or "").strip()
            if not raw_text:
                return await reply_text(
                    "❌ Usage: `/setwelcome <text>`\n"
                    "Variables: `{mention}`, `{name}`, `{id}`\n"
                    "Toggle: `/welcome on` or `/welcome off`"
                )
            if raw_text.lower() in ("off", "disable", "clear", "none"):
                _save_welcome_for_chat(action_chat_id, None)
                return await reply_text("✅ Welcome message cleared.")
            existing = _welcome_for_chat(action_chat_id) or {}
            _save_welcome_for_chat(
                action_chat_id,
                {
                    "text":       raw_text,
                    "enabled":    existing.get("enabled", True),
                    "set_by":     uid,
                    "updated_at": str(datetime.now()),
                },
            )
            await reply_text(
                f"✅ Welcome message saved!\n"
                f"Status: {'🟢 On' if existing.get('enabled', True) else '🔴 Off'}\n\n"
                f"Preview:\n{_format_welcome_text(raw_text, {'id': uid, 'first_name': 'User'})}"
            )
            return

        if raw_cmd == "welcome":
            if not is_authorized_actor():
                return await security_fail()
            entry = _welcome_for_chat(action_chat_id)
            if not args:
                if isinstance(entry, dict) and entry.get("text"):
                    status = "🟢 On" if entry.get("enabled", True) else "🔴 Off"
                    return await reply_text(
                        f"🎉 **Welcome Message**\nStatus: {status}\n\n"
                        f"{entry['text']}\n\n"
                        f"Toggle: `/welcome on` or `/welcome off`\n"
                        f"Change text: `/setwelcome <text>`"
                    )
                return await reply_text(
                    "🎉 No welcome message set.\nUse `/setwelcome <text>` to add one."
                )
            flag = args[0].lower()
            if flag not in ("on", "off", "yes", "no", "enable", "disable"):
                return await reply_text("❌ Usage: `/welcome on` or `/welcome off`")
            enabled = flag in ("on", "yes", "enable")
            if not isinstance(entry, dict) or not entry.get("text"):
                return await reply_text(
                    "❌ No welcome message saved yet.\n"
                    "Use `/setwelcome <text>` first."
                )
            entry["enabled"]    = enabled
            entry["updated_at"] = str(datetime.now())
            _save_welcome_for_chat(action_chat_id, entry)
            status_icon = "🟢" if enabled else "🔴"
            return await reply_text(f"{status_icon} Welcome message turned **{'on' if enabled else 'off'}**.")

        if raw_cmd == "setgoodbye":
            if not is_authorized_actor():
                return await security_fail()
            raw_text = parts[1].strip() if len(parts) > 1 else ""
            if not raw_text and reply:
                raw_text = (reply.get("text") or reply.get("caption") or "").strip()
            if not raw_text:
                return await reply_text(
                    "❌ Usage: `/setgoodbye <text>`\n"
                    "Variables: `{mention}`, `{name}`, `{id}`\n"
                    "Toggle: `/goodbye on` or `/goodbye off`"
                )
            if raw_text.lower() in ("off", "disable", "clear", "none"):
                _save_goodbye_for_chat(action_chat_id, None)
                return await reply_text("✅ Goodbye message cleared.")
            existing = _goodbye_for_chat(action_chat_id) or {}
            _save_goodbye_for_chat(
                action_chat_id,
                {
                    "text":       raw_text,
                    "enabled":    existing.get("enabled", True),
                    "set_by":     uid,
                    "updated_at": str(datetime.now()),
                },
            )
            await reply_text(
                f"✅ Goodbye message saved!\n"
                f"Status: {'🟢 On' if existing.get('enabled', True) else '🔴 Off'}"
            )
            return

        if raw_cmd == "goodbye":
            if not is_authorized_actor():
                return await security_fail()
            entry = _goodbye_for_chat(action_chat_id)
            if not args:
                if isinstance(entry, dict) and entry.get("text"):
                    status = "🟢 On" if entry.get("enabled", True) else "🔴 Off"
                    return await reply_text(
                        f"👋 **Goodbye Message**\nStatus: {status}\n\n"
                        f"{entry['text']}\n\n"
                        f"Toggle: `/goodbye on` or `/goodbye off`\n"
                        f"Change text: `/setgoodbye <text>`"
                    )
                return await reply_text(
                    "👋 No goodbye message set.\nUse `/setgoodbye <text>` to add one."
                )
            flag = args[0].lower()
            if flag not in ("on", "off", "yes", "no", "enable", "disable"):
                return await reply_text("❌ Usage: `/goodbye on` or `/goodbye off`")
            enabled = flag in ("on", "yes", "enable")
            if not isinstance(entry, dict) or not entry.get("text"):
                return await reply_text(
                    "❌ No goodbye message saved yet.\n"
                    "Use `/setgoodbye <text>` first."
                )
            entry["enabled"]    = enabled
            entry["updated_at"] = str(datetime.now())
            _save_goodbye_for_chat(action_chat_id, entry)
            status_icon = "🟢" if enabled else "🔴"
            return await reply_text(f"{status_icon} Goodbye message turned **{'on' if enabled else 'off'}**.")

        if raw_cmd == "setrules":
            if not is_authorized_actor():
                return await security_fail()
            raw_text = parts[1].strip() if len(parts) > 1 else ""
            if not raw_text and reply:
                raw_text = (reply.get("text") or reply.get("caption") or "").strip()
            if not raw_text:
                return await reply_text(
                    "❌ Usage: `/setrules <text>`\n"
                    "Toggle: `/rules on` or `/rules off`"
                )
            if raw_text.lower() in ("off", "disable", "clear", "none"):
                _save_rules_for_chat(action_chat_id, None)
                return await reply_text("✅ Rules cleared.")
            existing = _rules_for_chat(action_chat_id) or {}
            _save_rules_for_chat(
                action_chat_id,
                {
                    "text":       raw_text,
                    "enabled":    existing.get("enabled", True),
                    "set_by":     uid,
                    "updated_at": str(datetime.now()),
                },
            )
            return await reply_text("✅ Rules saved.")

        # FIX-D: single unified /rules handler (removed duplicate dead-code block)
        if raw_cmd == "rules":
            entry = _rules_for_chat(action_chat_id if is_private else chat_id)
            if is_authorized_actor() and args:
                flag = args[0].lower()
                if flag not in ("on", "off", "yes", "no", "enable", "disable"):
                    return await reply_text("❌ Usage: `/rules on` or `/rules off`")
                enabled = flag in ("on", "yes", "enable")
                if not isinstance(entry, dict) or not entry.get("text"):
                    return await reply_text(
                        "❌ No rules saved yet.\nUse `/setrules <text>` first."
                    )
                entry["enabled"]    = enabled
                entry["updated_at"] = str(datetime.now())
                _save_rules_for_chat(action_chat_id if is_private else chat_id, entry)
                status_icon = "🟢" if enabled else "🔴"
                return await reply_text(f"{status_icon} Rules turned **{'on' if enabled else 'off'}**.")
            # Public display
            if is_private:
                return await reply_text("Use /rules in a group.")
            rules_text = entry.get("text") if isinstance(entry, dict) else None
            rules_on   = entry.get("enabled", True) if isinstance(entry, dict) else True
            if not rules_text:
                return await reply_text("📜 No rules set for this group yet.")
            if not rules_on:
                return await reply_text("📜 Rules are currently disabled for this group.")
            await reply_text(f"📜 **Group Rules**\n\n{rules_text}")
            return

        if raw_cmd == "report":
            if is_private:
                return await reply_text("Use /report in a group.")
            reporter      = msg.get("from", {})
            reason        = " ".join(args).strip() or "No reason provided"
            target        = None
            target_id     = None
            target_msg_id = msg_id
            if reply:
                target        = reply.get("from", {})
                target_id     = target.get("id")
                target_msg_id = reply.get("message_id") or msg_id
            chat       = msg.get("chat", {})
            chat_title = chat.get("title") or "Group"
            link       = _message_link(chat, target_msg_id)
            report_lines = [
                "🚨 **New Report**",
                f"Chat: *{_escape_markdown(chat_title)}* (`{chat_id}`)",
                f"Reporter: {make_mention(reporter)} (`{uid}`)",
                f"Reason: {reason}",
            ]
            if target_id:
                report_lines.append(f"Reported User: {make_mention(target)} (`{target_id}`)")
            if link:
                report_lines.append(f"Message: {link}")
            report_text = "\n".join(report_lines)
            lg = get_log_group()
            if lg:
                await tg_send(lg, report_text)
            try:
                async for member in bot.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
                    user = getattr(member, "user", None)
                    if not user or getattr(user, "is_bot", False):
                        continue
                    try:
                        await tg_send(user.id, report_text)
                    except Exception:
                        pass
            except Exception as admin_err:
                log_msg(f"report admin notify failed: {admin_err}", "WARNING")
            await reply_text("✅ Report sent to admins.")
            return

        # FIX-E: hlock/hunlock now work from PM when a group is connected
        if raw_cmd in ("hlock", "lock"):
            if not await check_mod("mute"):
                return
            # Only block when truly no connected group (action_chat_id == chat_id in PM)
            if is_private and action_chat_id == chat_id:
                return await reply_text("Use /hlock in a group or via a connected group.")
            dur = parse_duration_token(args[0]) if args else None
            if args and dur is None:
                return await reply_text("❌ Invalid duration. Examples: 10m, 1h, 1d")
            lock_entry = _lock_for_chat(action_chat_id)
            if lock_entry and lock_entry.get("locked"):
                if dur:
                    until_ts = int(time.time()) + dur
                    lock_entry["until_ts"] = until_ts
                    _save_lock_for_chat(action_chat_id, lock_entry)
                    cancel_temp_action("lock", action_chat_id, action_chat_id)
                    schedule_temp_action(
                        "lock", action_chat_id, action_chat_id, until_ts, uid,
                        "Chat lock", extra={"permissions": lock_entry.get("permissions")},
                    )
                    return await reply_text(f"🔒 Chat lock extended to {format_duration(dur)}.")
                return await reply_text("🔒 Chat is already locked.")
            original_perms = _FULL_PERMISSIONS
            try:
                chat_obj = await bot.get_chat(action_chat_id)
                original_perms = _chat_permissions_payload(chat_obj)
            except Exception as perm_err:
                log_msg(f"lock permissions load failed for {action_chat_id}: {perm_err}", "WARNING")
            ok, err = await api_set_chat_permissions(action_chat_id, _MUTE_PERMISSIONS)
            if not ok:
                return await reply_text(f"❌ Lock failed: {err}")
            until_ts = int(time.time()) + dur if dur else 0
            _save_lock_for_chat(
                action_chat_id,
                {
                    "locked":      True,
                    "set_by":      uid,
                    "set_at":      str(datetime.now()),
                    "until_ts":    until_ts,
                    "permissions": original_perms,
                },
            )
            if dur:
                schedule_temp_action(
                    "lock", action_chat_id, action_chat_id, until_ts, uid,
                    "Chat lock", extra={"permissions": original_perms},
                )
                return await reply_text(f"🔒 Chat locked for {format_duration(dur)}.")
            return await reply_text("🔒 Chat locked until manually unlocked.")

        if raw_cmd in ("hunlock", "unlock"):
            if not await check_mod("mute"):
                return
            # Only block when truly no connected group (action_chat_id == chat_id in PM)
            if is_private and action_chat_id == chat_id:
                return await reply_text("Use /hunlock in a group or via a connected group.")
            lock_entry    = _lock_for_chat(action_chat_id)
            restore_perms = None
            if isinstance(lock_entry, dict):
                restore_perms = lock_entry.get("permissions")
            if restore_perms is None:
                restore_perms = _FULL_PERMISSIONS
            ok, err = await api_set_chat_permissions(action_chat_id, restore_perms)
            if not ok:
                return await reply_text(f"❌ Unlock failed: {err}")
            _save_lock_for_chat(action_chat_id, None)
            cancel_temp_action("lock", action_chat_id, action_chat_id)
            return await reply_text("🔓 Chat unlocked.")

        # ── /bot on | /bot off ────────────────────────────────────────────
        if raw_cmd == "bot":
            if not is_owner_actor():
                return await reply_text("❌ Only the bot owner can toggle bot status.")

            if not args:
                current = is_bot_enabled(action_chat_id)
                status  = "🟢 ON" if current else "🔴 OFF"
                return await reply_text(
                    f"🤖 **Bot Status**\n\n"
                    f"Current status: {status}\n\n"
                    f"Usage:\n"
                    f"`/bot on` — enable bot for moderators\n"
                    f"`/bot off` — disable bot (owner-only mode)"
                )

            flag = args[0].lower()
            if flag not in ("on", "off", "yes", "no", "enable", "disable"):
                return await reply_text("❌ Usage: `/bot on` or `/bot off`")

            enabled = flag in ("on", "yes", "enable")
            set_bot_enabled(action_chat_id, enabled)

            status_icon = "🟢" if enabled else "🔴"
            status_word = "ON" if enabled else "OFF"

            msg_text = (
                f"{status_icon} **Bot turned {status_word}**\n\n"
                + (
                    "✅ Moderators can use all their commands again."
                    if enabled else
                    "🔒 Only the owner can use commands until bot is turned back on."
                )
            )
            await reply_text(msg_text)

            lg = get_log_group()
            if lg:
                await tg_send(
                    lg,
                    f"🤖 Bot status changed\n\n"
                    f"Chat: `{action_chat_id}`\n"
                    f"Status: {status_icon} {status_word}\n"
                    f"Changed by: `{uid}`",
                )
            return

        # FIX-G: locktype / locktypes stub
        if raw_cmd in ("locktype", "locktypes"):
            if not is_authorized_actor():
                return await security_fail()
            return await reply_text(
                "🔒 **Lock Types**\n\n"
                "Use `/hlock [duration]` to lock the entire chat.\n"
                "Use `/hunlock` to restore original permissions.\n\n"
                "Duration examples: `10m`, `2h`, `1d`"
            )

        # ── /hr ───────────────────────────────────────────────────────────
        if raw_cmd == "hr":
            try:
                if not args and not reply and not is_private:
                    return await reply_text(f"📌 Group ID: `{chat_id}`")
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
                elif args and args[0].lstrip("-").isdigit():
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

                response = f"👤 **User Profile**\n\n🆔 ID: `{target_id}`\n📝 Name: {first_name}"
                if last_name:
                    response += f" {last_name}"
                response += "\n"
                if uname:
                    response += f"🔗 Username: @{uname}\n"
                response += f"📌 Link: [Profile](tg://user?id={target_id})"
                if not is_private:
                    response += f"\n\nGroup ID: `{chat_id}`"

                if is_owner_actor() and not is_private:
                    await notify_owner(response)
                else:
                    await reply_text(response)
                return
            except Exception as e:
                log_msg(f"Error in /hr: {e}\n{traceback.format_exc()}", "ERROR")
                return await reply_text(f"❌ Error: {e}")

        # ── /ttt commands ─────────────────────────────────────────────────
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

        # ── /happeal ──────────────────────────────────────────────────────
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
            asyncio.create_task(upload_backup("appeals", load(APPEALS_FILE)))
            await reply_text(f"✅ Appeal submitted. Appeal ID: #{aid}")
            await notify_owner(f"📨 New appeal #{aid}\nCase: #{case_id}\nUser: `{uid}`\n{appeal_msg}")
            return

        # ══════════════════════════════════════════════════════════════════
        # NOTES COMMANDS
        # ══════════════════════════════════════════════════════════════════

        if raw_cmd == "save":
            if not is_authorized_actor():
                return await security_fail()
            if not args:
                return await reply_text(
                    "❌ Usage:\n"
                    "`/save <name> <content>` — save text as note\n"
                    "`/save <name>` + reply — save replied message as note"
                )
            note_name = args[0].lower().strip()
            if len(note_name) > 64:
                return await reply_text("❌ Note name too long (max 64 chars).")
            entities  = None
            note_type = "text"
            file_id   = None
            if len(args) > 1:
                content = " ".join(args[1:]).strip()
            elif reply:
                content = reply.get("caption") or reply.get("text") or ""
                if not content and not any(k in reply for k in ("photo","document","video","audio","voice","sticker","animation")):
                    return await reply_text("❌ Replied message has no text or media to save.")
                entities = reply.get("caption_entities") or reply.get("entities")
                if "photo" in reply and isinstance(reply.get("photo"), list) and reply["photo"]:
                    note_type = "photo";   file_id = reply["photo"][-1].get("file_id")
                elif "document" in reply and isinstance(reply.get("document"), dict):
                    note_type = "document"; file_id = reply["document"].get("file_id")
                elif "video" in reply and isinstance(reply.get("video"), dict):
                    note_type = "video";   file_id = reply["video"].get("file_id")
                elif "audio" in reply and isinstance(reply.get("audio"), dict):
                    note_type = "audio";   file_id = reply["audio"].get("file_id")
                elif "voice" in reply and isinstance(reply.get("voice"), dict):
                    note_type = "voice";   file_id = reply["voice"].get("file_id")
                elif "sticker" in reply and isinstance(reply.get("sticker"), dict):
                    note_type = "sticker"; file_id = reply["sticker"].get("file_id")
                elif "animation" in reply and isinstance(reply.get("animation"), dict):
                    note_type = "animation"; file_id = reply["animation"].get("file_id")
            else:
                return await reply_text(
                    "❌ Provide content after the name, or reply to a message.\n"
                    "Example: `/save rules No spamming!`"
                )
            note_save(action_chat_id, note_name, content, note_type, file_id, created_by=uid, entities=entities if reply else None)
            await reply_text(f"📋 Note `{note_name}` saved! Get it with `/get {note_name}` or `#{note_name}`.")
            return

        if raw_cmd == "get":
            if not args:
                return await reply_text("Usage: `/get <name>`")
            note_name = args[0].lower().strip()
            note = note_get(action_chat_id, note_name)
            if not note:
                return await reply_text(f"❌ Note `{note_name}` not found.\nUse `/notes` to see all saved notes.")
            if note.get("type") and note.get("type") != "text" and note.get("file_id"):
                await tg_send_media(chat_id, note, reply_to=msg_id)
            else:
                content = note.get("content", "")
                if note.get("entities"):
                    await tg_send(chat_id, content, reply_to=msg_id, parse_mode=None, entities=note.get("entities"))
                else:
                    if re.search(r"\[.+?\]\(https?://[^\s)]+\)", content):
                        await tg_send(chat_id, content, reply_to=msg_id, parse_mode="Markdown")
                    elif "<a " in content:
                        await tg_send(chat_id, content, reply_to=msg_id, parse_mode="HTML")
                    else:
                        await tg_send(chat_id, content, reply_to=msg_id, parse_mode=None)
            return

        if raw_cmd == "clear":
            if not is_authorized_actor():
                return await security_fail()
            if not args:
                return await reply_text("Usage: `/clear <name>`")
            note_name = args[0].lower().strip()
            if note_delete(action_chat_id, note_name):
                await reply_text(f"🗑️ Note `{note_name}` deleted.")
            else:
                await reply_text(f"❌ Note `{note_name}` not found.")
            return

        if raw_cmd == "notes":
            names = note_list(action_chat_id)
            if not names:
                return await reply_text(
                    "📋 No notes saved in this group yet.\n"
                    "Use `/save <name> <text>` to add one."
                )
            note_rows = []
            row = []
            for i, name in enumerate(names):
                cb_name = name[:35]
                row.append((f"#{name}", f"cb:getnote_{action_chat_id}_{cb_name}"))
                if len(row) == 3:
                    note_rows.append(row)
                    row = []
            if row:
                note_rows.append(row)
            markup = build_markup(*note_rows)
            await tg_send(
                chat_id,
                f"📋 **Notes in this group** — `{len(names)}` saved\n\n"
                + "\n".join(f"• `#{n}`" for n in names)
                + "\n\nTap a button or type `#notename` to retrieve.",
                reply_to=msg_id,
                markup=markup,
            )
            return

        # ══════════════════════════════════════════════════════════════════
        # FILTERS COMMANDS
        # ══════════════════════════════════════════════════════════════════

        if raw_cmd == "filter":
            if not is_authorized_actor():
                return await security_fail()
            if len(args) < 2:
                return await reply_text(
                    "❌ Usage:\n"
                    "`/filter <keyword> <response>` — contains match (default)\n"
                    "`/filter -exact <keyword> <response>` — exact message match\n"
                    "`/filter -start <keyword> <response>` — message starts with\n"
                    "`/filter -regex <pattern> <response>` — regex match\n\n"
                    "Example: `/filter spam You cannot spam here!`"
                )
            match_type = "contains"
            arg_start  = 0
            if args[0].startswith("-"):
                flag     = args[0].lower()
                flag_map = {
                    "-exact":    "exact",
                    "-start":    "startswith",
                    "-regex":    "regex",
                    "-contains": "contains",
                }
                if flag in flag_map:
                    match_type = flag_map[flag]
                    arg_start  = 1
                else:
                    return await reply_text(f"❌ Unknown flag `{flag}`. Valid: -exact, -start, -regex, -contains")
            if len(args) < arg_start + 2:
                return await reply_text("❌ Provide both a keyword and a response.")
            keyword  = args[arg_start].lower().strip()
            response = " ".join(args[arg_start + 1:]).strip()
            if len(keyword) > 128:
                return await reply_text("❌ Keyword too long (max 128 chars).")
            if not response:
                return await reply_text("❌ Response text cannot be empty.")
            if match_type == "regex":
                try:
                    re.compile(keyword)
                except re.error as e:
                    return await reply_text(f"❌ Invalid regex pattern: `{e}`")
            filter_add(action_chat_id, keyword, response, match_type=match_type, created_by=uid)
            await reply_text(
                f"✅ Filter added!\n"
                f"🔑 Keyword: `{keyword}`\n"
                f"🔍 Match: `{match_type}`\n"
                f"💬 Response: {response[:80]}{'...' if len(response) > 80 else ''}"
            )
            return

        if raw_cmd == "stop":
            if not is_authorized_actor():
                return await security_fail()
            if not args:
                return await reply_text("Usage: `/stop <keyword>`")
            keyword = args[0].lower().strip()
            if filter_remove(action_chat_id, keyword):
                await reply_text(f"✅ Filter `{keyword}` removed.")
            else:
                await reply_text(f"❌ No filter found for `{keyword}`.\nUse `/filters` to see all active filters.")
            return

        if raw_cmd == "filters":
            keywords = filter_list(action_chat_id)
            if not keywords:
                return await reply_text(
                    "🔍 No filters active in this group.\n"
                    "Use `/filter <keyword> <response>` to add one."
                )
            fdata_all = _filters_for_chat(action_chat_id)
            lines     = [f"🔍 **Active Filters** — `{len(keywords)}`\n"]
            for kw in keywords:
                fd   = fdata_all.get(kw, {})
                mt   = fd.get("match_type", "contains")
                resp = fd.get("response", "")[:50]
                lines.append(f"• `{kw}` [{mt}] → _{resp}_")
            await reply_text("\n".join(lines) + "\n\nUse `/stop <keyword>` to remove a filter.")
            return

        if raw_cmd == "addblocklist":
            if not is_authorized_actor():
                return await security_fail()
            keyword = None
            if args:
                keyword = args[0].lower().strip()
            elif reply:
                kw_text = reply.get("text") or reply.get("caption") or ""
                if kw_text:
                    keyword = kw_text.strip().lower()[:128]
            if not keyword:
                return await reply_text("❌ Usage: /addblocklist <keyword>")
            if len(keyword) > 128:
                return await reply_text("❌ Keyword too long (max 128 chars).")
            blocklist_add(action_chat_id, keyword, created_by=uid)
            await reply_text(f"✅ Blocklist keyword `{keyword}` added for this group.")
            return

        if raw_cmd in ("deleteblocklist", "removeblocklist"):
            if not is_authorized_actor():
                return await security_fail()
            if not args:
                return await reply_text("❌ Usage: /deleteblocklist <keyword>")
            keyword = args[0].lower().strip()
            if blocklist_remove(action_chat_id, keyword):
                await reply_text(f"✅ Blocklist keyword `{keyword}` removed.")
            else:
                await reply_text(f"❌ No blocklist entry for `{keyword}`.")
            return

        if raw_cmd == "blocklists":
            keys = blocklist_list(action_chat_id)
            if not keys:
                return await reply_text("🔒 No blocklist keywords set for this group.")
            lines = [f"🔒 **Blocklist** — `{len(keys)}` keywords"]
            for k in keys:
                lines.append(f"• `{k}`")
            await reply_text("\n".join(lines))
            return

        if raw_cmd == "blocklistmode":
            if not is_authorized_actor():
                return await security_fail()
            if not args:
                cur = get_blocklist_mode(action_chat_id)
                return await reply_text(f"🔒 Blocklist mode for this group: `{cur}`")
            mode = args[0].lower()
            if mode not in ("warn", "mute", "ban"):
                return await reply_text("❌ Invalid mode. Valid: warn, mute, ban")
            set_blocklist_mode(action_chat_id, mode)
            await reply_text(f"✅ Blocklist mode set to `{mode}` for this group.")
            return

        # ══════════════════════════════════════════════════════════════════
        # OWNER COMMANDS
        # ══════════════════════════════════════════════════════════════════

        if raw_cmd in ("hauth", "ha"):
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
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

        # FIX-F: /hunauth — remove moderator status (was completely missing)
        if raw_cmd == "hunauth":
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunauth <user_id>")
            data = load(AUTH_FILE)
            key  = str(tid)
            if key not in data:
                return await reply_text(f"ℹ️ {make_mention(target)} is not a moderator.")
            del data[key]
            await save_and_backup(AUTH_FILE, data)
            await reply_text(f"✅ {make_mention(target)} has been removed as moderator.")
            lg = get_log_group()
            if lg:
                await tg_send(lg, f"🗑 Moderator removed\n👤 {make_mention(target)} (`{tid}`)\n🛡 By: `{uid}`")
            return

        if raw_cmd in ("hgrant", "hg"):
            if not is_owner_actor():
                return
            if not args:
                return await reply_text(
                    f"Usage: /hgrant <user_id> | /hgrant <permission> <user_id>\n"
                    f"Valid: {', '.join(sorted(VALID_PERMISSIONS))}"
                )
            perm   = None
            target = {}
            tid    = None
            terr   = None
            if args[0].lower() in VALID_PERMISSIONS:
                perm = args[0].lower()
                target, tid, terr = await resolve_target_ext(bot, reply, args, 1)
                if not tid:
                    return await reply_text(terr or "❌ Reply to a user or pass their user ID.")
            else:
                target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
                if not tid:
                    return await reply_text(terr)
                if len(args) > 1 and args[1].lower() not in VALID_PERMISSIONS:
                    return await reply_text(f"❌ Invalid permission. Valid: {', '.join(sorted(VALID_PERMISSIONS))}")
                perm = "all"
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User not authorized. Run /hauth first.")
            if perm == "all":
                data[str(tid)]["permissions"] = grant_all_permissions()
            else:
                data[str(tid)]["permissions"][perm] = True
            await save_and_backup(AUTH_FILE, data)
            case_id = create_case("GRANT", uid, tid, f"Granted: {perm}")
            await send_grant_log(chat_id, msg_id, uid, target, perm, case_id)
            return

        if raw_cmd in ("hrevoke", "hrev"):
            if not is_owner_actor():
                return
            if not args:
                return await reply_text(
                    f"Usage: /hrevoke <permission|all> <user_id>\n"
                    f"Valid: {', '.join(sorted(VALID_PERMISSIONS))}, all"
                )
            perm = args[0].lower()
            if perm not in VALID_PERMISSIONS and perm != "all":
                return await reply_text(f"❌ Invalid. Valid: {', '.join(sorted(VALID_PERMISSIONS))}, all")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 1)
            if not tid:
                return await reply_text(terr)
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User is not a moderator.")
            if perm == "all":
                data[str(tid)]["permissions"] = {p: False for p in VALID_PERMISSIONS}
            else:
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

        if raw_cmd == "hfreeze":
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hfreeze <user_id>")
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User is not a moderator.")
            if data[str(tid)].get("frozen"):
                return await reply_text(f"ℹ️ {make_mention(target)} is already frozen.")
            data[str(tid)]["frozen"] = True
            await save_and_backup(AUTH_FILE, data)
            await reply_text(f"🧊 {make_mention(target)} has been frozen.")
            lg = get_log_group()
            if lg:
                await tg_send(lg, f"🧊 Moderator frozen\n👤 {make_mention(target)} (`{tid}`)\n🛡 By: `{uid}`")
            return

        if raw_cmd == "hunfreeze":
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunfreeze <user_id>")
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User is not a moderator.")
            if not data[str(tid)].get("frozen"):
                return await reply_text(f"ℹ️ {make_mention(target)} is not frozen.")
            data[str(tid)]["frozen"] = False
            await save_and_backup(AUTH_FILE, data)
            await reply_text(f"🔥 {make_mention(target)} has been unfrozen.")
            lg = get_log_group()
            if lg:
                await tg_send(lg, f"🔥 Moderator unfrozen\n👤 {make_mention(target)} (`{tid}`)\n🛡 By: `{uid}`")
            return

        if raw_cmd == "hbadge":
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hbadge <user_id> <badge text>")
            badge_start = 0 if reply else 1
            badge_text  = extract_reason(args, badge_start, "").strip()
            if not badge_text:
                return await reply_text("❌ Provide a badge text.\nUsage: /hbadge <user_id> <badge text>")
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User is not a moderator.")
            data[str(tid)]["badge"] = badge_text
            await save_and_backup(AUTH_FILE, data)
            await reply_text(f"🏷️ Badge set to `{badge_text}` for {make_mention(target)}")
            return

        if raw_cmd == "hwarnconfig":
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            config = get_warn_config()
            if not args or args[0].lower() == "show":
                return await reply_text(
                    f"⚙️ **Warn Configuration**\n\n"
                    f"🔢 Threshold: `{config['threshold']}`\n"
                    f"⚔️ Auto-action: `{config['action']}`\n"
                    f"⏱️ Duration: `{format_duration(config['duration'])}`\n\n"
                    f"Usage:\n"
                    f"`/hwarnconfig threshold <n>` - Set warn limit\n"
                    f"`/hwarnconfig action <ban|mute|kick>` - Set action\n"
                    f"`/hwarnconfig duration <e.g. 1h>` - Set duration"
                )
            sub = args[0].lower()
            if sub == "threshold":
                if len(args) < 2 or not args[1].isdigit() or int(args[1]) < 1:
                    return await reply_text("❌ Usage: /hwarnconfig threshold <number ≥ 1>")
                config["threshold"] = int(args[1])
                save_warn_config(config)
                return await reply_text(f"✅ Warn threshold set to `{config['threshold']}`")
            elif sub == "action":
                if len(args) < 2 or args[1].lower() not in ("ban", "mute", "kick"):
                    return await reply_text("❌ Usage: /hwarnconfig action <ban|mute|kick>")
                config["action"] = args[1].lower()
                save_warn_config(config)
                return await reply_text(f"✅ Auto-action set to `{config['action']}`")
            elif sub == "duration":
                if len(args) < 2:
                    return await reply_text("❌ Usage: /hwarnconfig duration <e.g. 30m, 2h, 1d>")
                dur = parse_duration_token(args[1])
                if not dur:
                    return await reply_text("❌ Invalid duration. Examples: 30m, 2h, 1d")
                config["duration"] = dur
                save_warn_config(config)
                return await reply_text(f"✅ Auto-action duration set to `{format_duration(dur)}`")
            else:
                return await reply_text("❌ Unknown option. Use: threshold / action / duration / show")

        # ══════════════════════════════════════════════════════════════════
        # MODERATION COMMANDS
        # ══════════════════════════════════════════════════════════════════

        if raw_cmd in ("hban", "hb"):
            if not await check_mod("ban"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hban <user_id/@user> [duration] [reason]")
            if not is_anon_admin and tid == uid:
                return await reply_text("❌ You cannot ban yourself.")
            # FIX-A: use Bot API member check
            member, gm_err = await get_chat_member_safe(bot, action_chat_id, tid)
            if gm_err:
                if gm_err != "UserNotParticipant":
                    log_msg(f"get_chat_member failed for ban check: {gm_err}", "WARNING")
            else:
                if member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR):
                    return await reply_text("❌ Cannot ban an admin or the group owner.")
            rs = 0 if reply else 1
            dur, reason = parse_duration_and_reason(args, rs)
            if is_protected(tid, action_chat_id):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            until_ts = int(time.time()) + dur if dur else None
            ok, err  = await api_ban(action_chat_id, tid, until_date=until_ts)
            if not ok:
                return await reply_text(f"❌ Ban failed: {err}")
            if dur:
                reason = f"{reason} | Duration: {format_duration(dur)}"
            case_id = create_case("BAN", uid, tid, reason, extra={
                "temporary": bool(dur), "duration": dur, "expires_at": until_ts,
            })
            if dur:
                schedule_temp_action("ban", action_chat_id, tid, until_ts, uid, reason, case_id=case_id)
            await send_action_log(chat_id, msg_id, "BAN", target, reason, case_id, actor_mod_info())
            return

        if raw_cmd in ("hkick", "hk"):
            if not await check_mod("kick"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hkick <user_id/@user> [reason]")
            if not is_anon_admin and tid == uid:
                return await reply_text("❌ You cannot kick yourself.")
            # FIX-A: use Bot API member check
            member, gm_err = await get_chat_member_safe(bot, action_chat_id, tid)
            if gm_err:
                if gm_err == "UserNotParticipant":
                    return await reply_text("❌ This user is not in the chat.")
                else:
                    log_msg(f"get_chat_member failed for kick check: {gm_err}", "WARNING")
            else:
                if member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR):
                    return await reply_text("❌ Cannot kick an admin or the group owner.")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No Reason")
            if is_protected(tid, action_chat_id):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            ok, err = await api_kick(action_chat_id, tid)
            if not ok:
                return await reply_text(f"❌ Kick failed: {err}")
            case_id = create_case("KICK", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "KICK", target, reason, case_id, actor_mod_info())
            return

        if raw_cmd in ("hmute", "hm"):
            if not await check_mod("mute"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hmute <user_id/@user> [duration] [reason]")
            rs = 0 if reply else 1
            dur, reason = parse_duration_and_reason(args, rs)
            # FIX-A: use Bot API member check
            member, gm_err = await get_chat_member_safe(bot, action_chat_id, tid)
            if gm_err:
                if gm_err == "UserNotParticipant":
                    return await reply_text("This user isn't in the chat!")
                else:
                    return await reply_text(gm_err)
            if member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR):
                return await reply_text("Afraid I can't stop an admin from talking!")
            if tid == _bot_id:
                return await reply_text("I'm not muting myself!")
            if is_protected(tid, action_chat_id):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            until_ts = int(time.time()) + dur if dur else None
            ok, err  = await api_mute(action_chat_id, tid, until_date=until_ts)
            if not ok:
                return await reply_text(f"❌ Mute failed: {err}")
            if dur:
                reason = f"{reason} | Duration: {format_duration(dur)}"
            case_id = create_case("MUTE", uid, tid, reason, extra={
                "temporary": bool(dur), "duration": dur, "expires_at": until_ts,
            })
            if dur:
                schedule_temp_action("mute", action_chat_id, tid, until_ts, uid, reason, case_id=case_id)
            await send_action_log(chat_id, msg_id, "MUTE", target, reason, case_id, actor_mod_info())
            return

        if raw_cmd in ("hunban", "hub"):
            if not await check_mod("unban"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunban <user_id/@user> [reason]")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No reason given")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            ok, err = await api_unban(action_chat_id, tid)
            if not ok:
                return await reply_text(f"❌ Unban failed: {err}")
            cancel_temp_action("ban", action_chat_id, tid)
            case_id = create_case("UNBAN", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "UNBAN", target, reason, case_id, actor_mod_info())
            return

        if raw_cmd in ("hunmute", "hum"):
            if not await check_mod("unmute"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunmute <user_id/@user> [reason]")
            rs = 0 if reply else 1
            reason = extract_reason(args, rs, "No reason given")
            # FIX-A: use Bot API member check
            member, gm_err = await get_chat_member_safe(bot, action_chat_id, tid)
            if gm_err:
                if gm_err == "UserNotParticipant":
                    return await reply_text("This user isn't even in the chat!")
                else:
                    return await reply_text(gm_err)
            if member.status in (enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR):
                return await reply_text("This user already has the right to speak.")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            ok, err = await api_unmute(action_chat_id, tid)
            if not ok:
                return await reply_text(f"❌ Unmute failed: {err}")
            cancel_temp_action("mute", action_chat_id, tid)
            case_id = create_case("UNMUTE", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "UNMUTE", target, reason, case_id, actor_mod_info())
            return

        if raw_cmd == "pin":
            if not await check_mod("pin"):
                return
            if not reply:
                return await reply_text("❌ Reply to the message you want to pin.")
            reply_msg_id = reply.get("message_id")
            if not reply_msg_id:
                return await reply_text("❌ Could not determine the replied message.")
            ok, err = await api_pin(action_chat_id, reply_msg_id)
            if not ok:
                return await reply_text(f"❌ Pin failed: {err}")
            await reply_text("📌 Message pinned.")
            return

        if raw_cmd == "unpin":
            if not await check_mod("pin"):
                return
            ok, err = await api_unpin(action_chat_id)
            if not ok:
                return await reply_text(f"❌ Unpin failed: {err}")
            await reply_text("📍 Pinned message removed.")
            return

        if raw_cmd == "adminlist":
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            await reply_text(await build_admin_list_text(bot, action_chat_id))
            return

        if raw_cmd == "zombies":
            if not await check_mod("kick"):
                return
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            await reply_text("🔎 Scanning for deleted accounts...")
            kicked_deleted, kicked_bots, failures = await scan_zombies(bot, action_chat_id, _bot_id)
            summary = (
                f"🧟 Zombie scan complete\n"
                f"• Deleted accounts kicked: `{kicked_deleted}`"
            )
            if failures:
                summary += f"\n• Failures: `{len(failures)}`"
            await reply_text(summary)
            return

        if raw_cmd == "hstats":
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            await reply_text(build_stats_text())
            return

        if raw_cmd == "hmod":
            if not is_authorized_actor():
                return await reply_text("❌ Moderator access required.")
            if not args or args[0].lower() != "list":
                return await reply_text("Usage: /hmod list")
            await reply_text(build_moderator_list_text())
            return

        if raw_cmd in ("hwarn", "hw"):
            if not await check_mod("warn"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hwarn <user_id/@user> [reason]")
            rs     = 0 if reply else 1
            reason = extract_reason(args, rs, "No reason given")
            if is_protected(tid, action_chat_id):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            warner_tag = actor_mod_info().get("mod_id", str(uid))
            res = warn(tid, action_chat_id, reason, warner=warner_tag)
            case_id = create_case("WARN", uid, tid, reason)
            await send_action_log(
                chat_id, msg_id, "WARN", target, reason, case_id, actor_mod_info(),
                extra=f"📊 Total Warns: {res.get('num_warns', 0)}/{res.get('threshold', 0)}",
            )
            action = res.get("action")
            if action:
                ok  = False
                err = ""
                until_ts     = int(time.time()) + get_warn_config().get("duration", 3600)
                action_label = action.upper()
                if action == "ban":
                    ok, err = await api_ban(action_chat_id, tid, until_date=until_ts)
                elif action == "kick":
                    ok, err = await api_kick(action_chat_id, tid)
                else:
                    ok, err = await api_mute(action_chat_id, tid, until_date=until_ts)
                if ok:
                    auto_reason = f"Warn threshold ({res.get('threshold')}) reached"
                    auto_case   = create_case(action_label, uid, tid, auto_reason)
                    await send_action_log(
                        chat_id, msg_id, action_label, target,
                        auto_reason, auto_case, actor_mod_info(),
                        extra="⚡ Auto-action on warn threshold",
                    )
                    if action in ("ban", "mute"):
                        schedule_temp_action(action, action_chat_id, tid, until_ts, uid, auto_reason, case_id=auto_case)
            return

        if raw_cmd == "resetwarns":
            if not await check_mod("warn"):
                return
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /resetwarns <user_id/@user>")
            reset_warns(tid, action_chat_id, admin_tag=actor_mod_info().get("mod_id", str(uid)))
            await reply_text("Warnings have been reset!")
            case_id = create_case("RESETWARNS", uid, tid, "Warnings reset")
            await send_action_log(chat_id, msg_id, "RESETWARNS", target, "Warnings reset", case_id, actor_mod_info())
            return

        if raw_cmd == "warns":
            explicit    = False
            user_lookup = uid
            if args:
                explicit = True
                target_tmp, tid_tmp, terr_tmp = await resolve_target_ext(bot, reply, args, 0)
                if tid_tmp:
                    user_lookup = tid_tmp
                else:
                    if reply:
                        _, rid = extract_reply_user(reply)
                        if rid:
                            user_lookup = rid
                        else:
                            return await reply_text(f"{terr_tmp}")
                    else:
                        return await reply_text(f"{terr_tmp}")
            elif reply:
                explicit = True
                _, rid = extract_reply_user(reply)
                if rid:
                    user_lookup = rid
            threshold = get_warn_config().get("threshold", 3)
            if explicit:
                warns_data = load(WARN_FILE)
                total = 0
                per_chat: dict[int, int] = {}
                for k, v in warns_data.items():
                    try:
                        if isinstance(k, str) and k == str(user_lookup):
                            total += int(v)
                        elif isinstance(k, str) and k.endswith(f":{user_lookup}"):
                            parts2 = k.split(":", 1)
                            cid    = int(parts2[0])
                            cnt    = int(v)
                            total += cnt
                            per_chat[cid] = per_chat.get(cid, 0) + cnt
                    except Exception:
                        continue
                if total > 0:
                    text_out = f"Total warnings for `{user_lookup}`: {total}/{threshold}\n"
                    if per_chat:
                        for cid, cnt in per_chat.items():
                            text_out += f" - Chat `{cid}`: {cnt}\n"
                    await reply_text(text_out)
                else:
                    await reply_text("This user hasn't got any warnings!")
            else:
                result    = warns_for(user_lookup, action_chat_id)
                num_warns = result.get("num_warns", 0)
                reasons   = result.get("reasons", [])
                if num_warns > 0:
                    text_out = f"This user has {num_warns}/{threshold} warnings."
                    for r in reasons:
                        text_out += f"\n - {r}"
                    await reply_text(text_out)
                else:
                    await reply_text("This user hasn't got any warnings!")
            return

        if raw_cmd in ("hdel", "hd"):
            if not await check_mod("delete"):
                return
            if not reply:
                return
            target, tid = extract_reply_user(reply)
            if not tid:
                return
            if is_protected(tid, action_chat_id):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid, is_anon=is_anon_admin):
                return
            reply_msg_id = reply.get("message_id")
            ok, err = await api_delete_msg(action_chat_id, reply_msg_id)
            if not ok:
                return await reply_text(f"❌ Delete failed: {err}")
            case_id = create_case("DELETE", uid, tid, "Message Deleted")
            await send_action_log(chat_id, msg_id, "DELETE", target, "Message Deleted", case_id, actor_mod_info())
            return

        if raw_cmd in ("hprotect", "hp", "protect"):
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hprotect <user_id/@user>")
            data     = _load_protected_store()
            chat_key = str(action_chat_id)
            group    = data.get(chat_key)
            if not isinstance(group, dict):
                group = {}
            key = str(tid)
            if key in group:
                return await reply_text(f"ℹ️ {make_mention(target)} is already protected.")
            group[key]       = True
            data[chat_key]   = group
            await save_and_backup(PROTECT_FILE, data)
            await reply_text(f"🛡 {make_mention(target)} is now protected.")
            return

        if raw_cmd in ("hunprotect", "hup", "unprotect"):
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            target, tid, terr = await resolve_target_ext(bot, reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hunprotect <user_id/@user>")
            data       = _load_protected_store()
            chat_key   = str(action_chat_id)
            group      = data.get(chat_key)
            if not isinstance(group, dict):
                group = {}
            global_map = data.get("__global__") if isinstance(data.get("__global__"), dict) else {}
            key = str(tid)
            if key not in group and key not in global_map:
                return await reply_text(f"ℹ️ {make_mention(target)} is not protected.")
            group.pop(key, None)
            global_map.pop(key, None)
            if group:
                data[chat_key] = group
            else:
                data.pop(chat_key, None)
            if global_map:
                data["__global__"] = global_map
            else:
                data.pop("__global__", None)
            await save_and_backup(PROTECT_FILE, data)
            await reply_text(f"🔓 Protection removed from {make_mention(target)}.")
            return

        if raw_cmd in ("hprotected", "protected"):
            if not is_owner_actor():
                return await reply_text("❌ Owner only.")
            data       = _load_protected_store()
            chat_key   = str(action_chat_id)
            group      = data.get(chat_key)
            if not isinstance(group, dict):
                group = {}
            global_map = data.get("__global__") if isinstance(data.get("__global__"), dict) else {}
            group_ids  = sorted(group.keys(), key=lambda x: int(x) if x.lstrip("-").isdigit() else x)
            lines = [f"🛡 **Protected Users** — `{len(group_ids)}`"]
            if group_ids:
                for uid_str in group_ids[:50]:
                    lines.append(f"• `{uid_str}`")
                if len(group_ids) > 50:
                    lines.append(f"…and `{len(group_ids) - 50}` more")
            else:
                lines.append("No protected users for this group.")
            if global_map:
                global_ids = sorted(global_map.keys(), key=lambda x: int(x) if x.lstrip("-").isdigit() else x)
                lines.append("")
                lines.append(f"🌐 **Global Protected** — `{len(global_ids)}`")
                for uid_str in global_ids[:20]:
                    lines.append(f"• `{uid_str}`")
                if len(global_ids) > 20:
                    lines.append(f"…and `{len(global_ids) - 20}` more")
            await reply_text("\n".join(lines))
            return

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
            if reply or args:
                target_tmp, tid_tmp, terr_tmp = await resolve_target_ext(bot, reply, args, 0)
                if not tid_tmp:
                    return await reply_text(terr_tmp)
                lookup = tid_tmp
            mod = get_mod_info(lookup)
            if not mod:
                return await reply_text("❌ That user is not a moderator.")
            perms     = mod.get("permissions", {})
            perm_list = "\n".join(
                f"  {'✅' if v else '❌'} {k}" for k, v in sorted(perms.items())
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

        def _mod_info() -> dict:
            return get_mod_info(uid) or {"badge": "🛡 Moderator", "mod_id": str(uid)}

        # TTT callbacks
        for prefix in TTT_CALLBACK_PREFIXES:
            if data.startswith(prefix):
                await handle_ttt_callback(cb_id, data, uid, from_user, chat_id, message)
                return

        # setactive_<chat_id>
        if data.startswith("setactive_"):
            try:
                new_active = int(data.split("_", 1)[1])
            except ValueError:
                return await tg_answer_cb(cb_id, "❌ Invalid chat ID.", alert=True)
            chats = get_connected_chats(uid)
            if new_active not in chats:
                return await tg_answer_cb(cb_id, "❌ You are not connected to that group.", alert=True)
            set_active_connection(uid, new_active)
            try:
                chat_obj = await bot.get_chat(new_active)
                await tg_answer_cb(cb_id, f"✅ Switched to: {chat_obj.title}", alert=False)
            except Exception:
                await tg_answer_cb(cb_id, f"✅ Active group set to {new_active}.", alert=False)
            return

        # getnote_<chat_id>_<name>
        if data.startswith("getnote_"):
            rest         = data[len("getnote_"):]
            note_chat_id = chat_id
            note_name    = rest
            underscore_pos = rest.find("_")
            if underscore_pos > 0:
                potential_id = rest[:underscore_pos]
                try:
                    note_chat_id = int(potential_id)
                    note_name    = rest[underscore_pos + 1:]
                except ValueError:
                    pass
            note = note_get(note_chat_id, note_name)
            if note:
                content = note.get("content", "")
                if note.get("entities"):
                    await tg_send(chat_id, content, parse_mode=None, entities=note.get("entities"))
                else:
                    if re.search(r"\[.+?\]\(https?://[^\s)]+\)", content):
                        await tg_send(chat_id, content, parse_mode="Markdown")
                    elif "<a " in content:
                        await tg_send(chat_id, content, parse_mode="HTML")
                    else:
                        await tg_send(chat_id, content, parse_mode=None)
                await tg_answer_cb(cb_id, f"📋 Note: #{note_name}")
            else:
                await tg_answer_cb(cb_id, "❌ Note not found.", alert=True)
            return

        if data.startswith("help_"):
            if not is_authorized(uid):
                await tg_answer_cb(cb_id, "⛔ Only moderators can use this.", alert=True)
                return
            message_id = message.get("message_id")
            if not message_id:
                await tg_answer_cb(cb_id, "❌ Could not update help message.", alert=True)
                return
            section = data.split("_", 1)[1]
            await tg_edit_text(
                chat_id, message_id,
                moderation_help_text(section, uid),
                markup=moderation_help_markup(section),
            )
            await tg_answer_cb(cb_id, "✅ Help updated.")
            return

        # Mod-only callbacks
        if not is_authorized(uid):
            await tg_answer_cb(cb_id, "⛔ Only moderators can use this.", alert=True)
            return

        if data.startswith("unban_"):
            if not has_permission(uid, "unban"):
                return await tg_answer_cb(cb_id, "❌ No unban permission.", alert=True)
            tid     = int(data.split("_", 1)[1])
            ok, err = await api_unban(chat_id, tid)
            if ok:
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
                cancel_temp_action("mute", chat_id, tid)
                await tg_answer_cb(cb_id, "✅ User unmuted.")
            else:
                await tg_answer_cb(cb_id, f"❌ {err}", alert=True)

        elif data.startswith("warn_"):
            if not has_permission(uid, "warn"):
                return await tg_answer_cb(cb_id, "❌ No warn permission.", alert=True)
            tid        = int(data.split("_", 1)[1])
            warner_tag = _mod_info().get("mod_id", str(uid))
            res        = warn(tid, chat_id, "Warned via button", warner=warner_tag)
            case_id    = create_case("WARN", uid, tid, "Warned via button")
            target     = {"id": tid}
            await send_action_log(
                chat_id, message.get("message_id"), "WARN", target,
                "Warned via button", case_id, _mod_info(),
                extra=f"📊 Total Warns: {res.get('num_warns', 0)}/{res.get('threshold', 0)}",
            )
            action = res.get("action")
            if action:
                ok  = False
                err = ""
                until_ts     = int(time.time()) + get_warn_config().get("duration", 3600)
                action_label = action.upper()
                if action == "ban":
                    ok, err = await api_ban(chat_id, tid, until_date=until_ts)
                elif action == "kick":
                    ok, err = await api_kick(chat_id, tid)
                else:
                    ok, err = await api_mute(chat_id, tid, until_date=until_ts)
                if ok:
                    auto_reason = f"Warn threshold ({res.get('threshold')}) reached"
                    auto_case   = create_case(action_label, uid, tid, auto_reason)
                    await send_action_log(
                        chat_id, message.get("message_id"), action_label, target,
                        auto_reason, auto_case, _mod_info(),
                        extra="⚡ Auto-action on warn threshold",
                    )
                    if action in ("ban", "mute"):
                        schedule_temp_action(action, chat_id, tid, until_ts, uid, auto_reason, case_id=auto_case)
            await tg_answer_cb(cb_id, f"✅ {res.get('reply', 'User warned.')}")

        elif data.startswith("unwarn_"):
            if not has_permission(uid, "warn"):
                return await tg_answer_cb(cb_id, "❌ No warn permission.", alert=True)
            tid = int(data.split("_", 1)[1])
            reset_warns(tid, chat_id, admin_tag=_mod_info().get("mod_id", str(uid)))
            case_id = create_case("RESETWARNS", uid, tid, "Unwarn via button")
            target  = {"id": tid}
            await send_action_log(chat_id, message.get("message_id"), "RESETWARNS", target, "Warnings reset via button", case_id, _mod_info())
            await tg_answer_cb(cb_id, "✅ Warnings reset.")

        elif data.startswith("removewarn_"):
            if not has_permission(uid, "warn"):
                return await tg_answer_cb(cb_id, "❌ No warn permission.", alert=True)
            tid        = int(data.split("_", 1)[1])
            warns_data = load(WARN_FILE)
            key        = f"{chat_id}:{tid}"
            if key in warns_data and warns_data[key] > 0:
                warns_data[key] -= 1
                save(WARN_FILE, warns_data)
            await tg_answer_cb(cb_id, f"✅ Warning removed. Total: {warns_data.get(key, 0)}")

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
        verify_storage_restored()
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
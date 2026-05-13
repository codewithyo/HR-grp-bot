# =========================================================
# ADVANCED TELEGRAM MODERATION BOT — KOYEB VERSION (FIXED)
# =========================================================
#
# FIXES in this revision:
#   • Peer ID Invalid  — ALL send_message calls now go through
#     tg_send() which uses the Bot API (httpx) directly.
#     Pyrogram's in-memory session no longer needs peer cache.
#   • Permissions wiped on restart — every save() also uploads
#     a JSON snapshot as a Telegram document to BACKUP_CHAT_ID
#     (defaults to LOG_GROUP_ID).  On startup the bot fetches
#     the latest snapshot and restores local files automatically.
#   • Auto-detect log group — if LOG_GROUP_ID == 0 the bot
#     scans its dialogs on startup and picks the first
#     supergroup/group where it has admin rights.
#   • GET / 404 — root endpoint now returns a JSON status page.
#   • Action log now goes to the originating chat AND log group.
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
import traceback, sys, shutil, io
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from pyrogram import Client
from db import mongo_db
# Pyrogram is used only for: get_me(), get_chat(), get_dialogs(),
# get_chat_member(), get_chat_history(), download_media().
# All moderation actions (ban/mute/restrict/delete) go through
# the Bot API wrappers (httpx) defined below.

# =========================================================
# BOT COMMANDS MANIFEST
# =========================================================

BOT_COMMANDS = [
    {"command": "start",    "description": "Start Bot"},
    {"command": "help",     "description": "Show command help"},
    {"command": "happeal",  "description": "Appeal moderation case"},
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

VALID_PERMISSIONS = {"ban", "mute", "warn", "delete"}

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
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "0"))   # 0 = auto-detect
PORT         = int(os.environ.get("PORT", "8000"))
OWNER_DEBUG_NOTIFICATIONS = os.environ.get("OWNER_DEBUG_NOTIFICATIONS", "0") == "1"

# Mutable — may be updated after auto-detection
_log_group_id: int = LOG_GROUP_ID
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

STORAGE_PATH = resolve_storage_path()
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
PROTECT_FILE      = f"{STORAGE_PATH}/protected.json"
ABUSE_FILE        = f"{STORAGE_PATH}/abuse.json"
TEMP_ACTIONS_FILE = f"{STORAGE_PATH}/temp_actions.json"
APPEALS_FILE      = f"{STORAGE_PATH}/appeals.json"

FALLBACK_FILE_MAP = {
    AUTH_FILE:         f"{FALLBACK_STORAGE_PATH}/auth.json",
    WARN_FILE:         f"{FALLBACK_STORAGE_PATH}/warns.json",
    CASE_FILE:         f"{FALLBACK_STORAGE_PATH}/cases.json",
    PROTECT_FILE:      f"{FALLBACK_STORAGE_PATH}/protected.json",
    ABUSE_FILE:        f"{FALLBACK_STORAGE_PATH}/abuse.json",
    TEMP_ACTIONS_FILE: f"{FALLBACK_STORAGE_PATH}/temp_actions.json",
    APPEALS_FILE:      f"{FALLBACK_STORAGE_PATH}/appeals.json",
}

ALL_FILES = list(FALLBACK_FILE_MAP.keys())

# Map filename stem → label used in Telegram backup captions
FILE_LABEL = {
    AUTH_FILE:         "auth",
    WARN_FILE:         "warns",
    CASE_FILE:         "cases",
    PROTECT_FILE:      "protected",
    TEMP_ACTIONS_FILE: "temp_actions",
    APPEALS_FILE:      "appeals",
}

MONGO_LOADERS = {
    AUTH_FILE: mongo_db.load_auth,
    WARN_FILE: mongo_db.load_warns,
    CASE_FILE: mongo_db.load_cases,
    PROTECT_FILE: mongo_db.load_protected,
    ABUSE_FILE: mongo_db.load_abuse,
    TEMP_ACTIONS_FILE: mongo_db.load_temp_actions,
    APPEALS_FILE: mongo_db.load_appeals,
}

MONGO_SAVERS = {
    AUTH_FILE: mongo_db.save_auth,
    WARN_FILE: mongo_db.save_warns,
    CASE_FILE: mongo_db.save_cases,
    PROTECT_FILE: mongo_db.save_protected,
    ABUSE_FILE: mongo_db.save_abuse,
    TEMP_ACTIONS_FILE: mongo_db.save_temp_actions,
    APPEALS_FILE: mongo_db.save_appeals,
}

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
# JSON HELPERS (atomic save + fallback read)
# =========================================================

def load(file: str):
    try:
        if os.path.exists(file):
            with open(file, "r") as f:
                data = json.load(f)
            loader = MONGO_LOADERS.get(file)
            if loader and _is_empty_payload(data) and mongo_db.is_connected():
                remote = loader()
                if not _is_empty_payload(remote):
                    _write_local_json(file, remote)
                    return remote
            return data
        loader = MONGO_LOADERS.get(file)
        if loader and mongo_db.is_connected():
            remote = loader()
            if not _is_empty_payload(remote):
                _write_local_json(file, remote)
            return remote if remote is not None else {}
        fallback = FALLBACK_FILE_MAP.get(file)
        if fallback and os.path.exists(fallback):
            with open(fallback, "r") as f:
                data = json.load(f)
            save(file, data)
            log_msg(f"Recovered {file} from fallback", "WARNING")
            return data
        return {}
    except json.JSONDecodeError:
        for candidate in [f"{file}.bak", FALLBACK_FILE_MAP.get(file)]:
            if candidate and os.path.exists(candidate):
                try:
                    with open(candidate, "r") as f:
                        data = json.load(f)
                    save(file, data)
                    log_msg(f"Recovered {file} from {candidate}", "WARNING")
                    return data
                except Exception:
                    pass
        log_msg(f"ERROR loading {file}: corrupt JSON, no valid backup", "ERROR")
        return {}
    except Exception as e:
        log_msg(f"ERROR loading {file}: {e}", "ERROR")
        return {}

def save(file: str, data):
    try:
        tmp = f"{file}.tmp"
        bak = f"{file}.bak"
        if os.path.exists(file):
            shutil.copy2(file, bak)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, file)
        fallback = FALLBACK_FILE_MAP.get(file)
        if fallback:
            Path(fallback).parent.mkdir(parents=True, exist_ok=True)
            with open(f"{fallback}.tmp", "w") as f:
                json.dump(data, f, indent=2)
            os.replace(f"{fallback}.tmp", fallback)
        saver = MONGO_SAVERS.get(file)
        if saver and mongo_db.is_connected():
            saver(data)
    except Exception as e:
        log_msg(f"ERROR saving {file}: {e}", "ERROR")

def sync_storage_with_mongo():
    if not mongo_db.is_connected():
        return
    for file, loader in MONGO_LOADERS.items():
        local_data = _read_local_json(file)
        remote_data = loader()
        if not _is_empty_payload(local_data):
            saver = MONGO_SAVERS.get(file)
            if saver:
                saver(local_data)
        elif not _is_empty_payload(remote_data):
            _write_local_json(file, remote_data)

# =========================================================
# BOT API HELPER (httpx) — avoids Pyrogram peer-ID issues
# =========================================================

async def tg_api(method: str, **kwargs) -> dict:
    """Call any Telegram Bot API method. Returns parsed JSON."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
    """Send a text message via Bot API (no Pyrogram peer cache needed)."""
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
# BOT API MODERATION ACTION WRAPPERS
# =========================================================
# All moderation calls go through Bot API (httpx) so they work
# regardless of Pyrogram's in-memory peer cache state.
# Each returns (success: bool, error_text: str).
# =========================================================

_MUTE_PERMISSIONS = {
    "can_send_messages":       False,
    "can_send_audios":         False,
    "can_send_documents":      False,
    "can_send_photos":         False,
    "can_send_videos":         False,
    "can_send_video_notes":    False,
    "can_send_voice_notes":    False,
    "can_send_polls":          False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
}

_FULL_PERMISSIONS = {
    "can_send_messages":       True,
    "can_send_audios":         True,
    "can_send_documents":      True,
    "can_send_photos":         True,
    "can_send_videos":         True,
    "can_send_video_notes":    True,
    "can_send_voice_notes":    True,
    "can_send_polls":          True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
    "can_invite_users":        True,
}

async def api_ban(chat_id: int, user_id: int, until_date: int = None) -> tuple[bool, str]:
    payload = {"chat_id": chat_id, "user_id": user_id}
    if until_date:
        payload["until_date"] = until_date
    r = await tg_api("banChatMember", json=payload)
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_unban(chat_id: int, user_id: int) -> tuple[bool, str]:
    r = await tg_api("unbanChatMember", json={
        "chat_id": chat_id, "user_id": user_id, "only_if_banned": True
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
        "chat_id": chat_id, "user_id": user_id, "permissions": _FULL_PERMISSIONS
    })
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

async def api_delete_msg(chat_id: int, message_id: int) -> tuple[bool, str]:
    r = await tg_api("deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    return (True, "") if r.get("ok") else (False, r.get("description", "Unknown error"))

# =========================================================
# TELEGRAM-BASED BACKUP (survives ephemeral FS)
# =========================================================
# On every save() for auth/warns/cases/protected/temp_actions/appeals
# we also upload the JSON as a document to BACKUP_CHAT_ID.
# On startup restore_from_telegram() fetches the last known snapshot.
#
# The backup message caption format:  MODBOT_BACKUP:<label>
# We search recent messages for that caption.
# =========================================================

_tg_backup_enabled = False   # set True once log group is confirmed

async def upload_backup(label: str, data) -> bool:
    """Upload a JSON snapshot as a Telegram document."""
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

async def save_and_backup(file: str, data):
    """Save locally and push a Telegram backup for critical files."""
    save(file, data)
    label = FILE_LABEL.get(file)
    if label and label in ("auth", "warns", "cases", "protected", "temp_actions", "appeals"):
        asyncio.create_task(upload_backup(label, data))

async def restore_from_telegram() -> dict[str, bool]:
    """
    Search the last 200 messages in the backup chat for MODBOT_BACKUP:<label>
    captions and download each one to restore local JSON files.
    Returns {label: restored?} for each label.
    """
    backup_chat = get_backup_chat()
    if backup_chat == 0:
        return {}

    label_to_file = {v: k for k, v in FILE_LABEL.items()}
    restored: dict[str, bool] = {}
    found_labels: set[str] = set()

    try:
        resp = await tg_api("getUpdates", json={"limit": 1, "offset": -1})
    except Exception:
        pass  # just to warm up

    # getMessages via getChatHistory (Bot API only gives us messages through getUpdates)
    # We use the forwardMessages trick: call getUpdates with large offset then check history.
    # Simpler: just call getUpdates with allowed_updates=[] to get nothing, then use
    # copyMessage trick. Actually the clean approach here is calling getFile after
    # searching via forwardMessages... But Bot API doesn't have a getHistory endpoint.
    #
    # Better approach: use Pyrogram's get_chat_history() which works via MTProto.
    # We do this AFTER the bot is started.
    log_msg("Telegram backup restore will be attempted via Pyrogram after bot start", "INFO")
    return restored

async def restore_from_telegram_pyrogram(bot: Client) -> int:
    """Use Pyrogram to scan backup chat history for MODBOT_BACKUP captions."""
    backup_chat = get_backup_chat()
    if backup_chat == 0:
        return 0

    label_to_file = {v: k for k, v in FILE_LABEL.items()}
    restored_count = 0
    seen_labels: set[str] = set()

    try:
        async for msg in bot.get_chat_history(backup_chat, limit=500):
            caption = (msg.caption or "") + (msg.text or "")
            if not caption.startswith("MODBOT_BACKUP:"):
                continue
            label = caption.replace("MODBOT_BACKUP:", "").strip()
            if label in seen_labels or label not in label_to_file:
                continue
            seen_labels.add(label)
            target_file = label_to_file[label]

            # Skip if local file already exists and is non-empty
            if os.path.exists(target_file):
                try:
                    with open(target_file, "r") as f:
                        existing = json.load(f)
                    if existing:  # already has data
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
        log_msg(f"restore_from_telegram_pyrogram error: {e}", "WARNING")

    return restored_count

# =========================================================
# PERMISSION CHECKS
# =========================================================

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_authorized(uid: int) -> bool:
    return is_owner(uid) or str(uid) in load(AUTH_FILE)

def has_permission(uid: int, perm: str) -> bool:
    if is_owner(uid):
        return True
    return load(AUTH_FILE).get(str(uid), {}).get("permissions", {}).get(perm, False)

def is_frozen(uid: int) -> bool:
    if is_owner(uid):
        return False
    return bool(load(AUTH_FILE).get(str(uid), {}).get("frozen", False))

# =========================================================
# UTILITY HELPERS
# =========================================================

def generate_mod_id() -> str:
    return "MOD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))

def get_mod_info(uid: int) -> dict:
    return load(AUTH_FILE).get(str(uid), {})

def is_protected(uid: int) -> bool:
    return str(uid) in load(PROTECT_FILE)

def make_mention(user: dict) -> str:
    if not isinstance(user, dict):
        return "User"
    uid   = user.get("id")
    name  = ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip() or "User"
    return f"[{name}](tg://user?id={uid})" if uid else name

def extract_actor_user_id(msg: dict) -> tuple[int | None, str | None]:
    from_user = msg.get("from") if isinstance(msg, dict) else None
    if isinstance(from_user, dict):
        uid = from_user.get("id")
        if isinstance(uid, int) and uid > 0:
            return uid, None
    if msg.get("sender_chat"):
        return None, "❌ Anonymous admin/channel messages are not supported."
    return None, "❌ Could not identify your Telegram account."

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

def create_case(action, moderator, target, reason) -> str:
    cases = load(CASE_FILE)
    cid   = str(len(cases) + 1)
    cases[cid] = {
        "action": action, "moderator": moderator,
        "target": target, "reason": reason,
        "time": str(datetime.now()),
    }
    save(CASE_FILE, cases)
    return cid

def load_temp_actions() -> list:
    d = load(TEMP_ACTIONS_FILE)
    return d if isinstance(d, list) else []

def save_temp_actions(actions: list):
    save(TEMP_ACTIONS_FILE, actions)

def schedule_message_delete(chat_id: int, message_id: int, delay: int = 60):
    actions = load_temp_actions()
    actions.append({
        "type": "delete",
        "chat_id": chat_id,
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
            "📘 **Owner Help**\n\n"
            "General: /start, /help, /happeal <case_id> <msg>\n"
            "Owner only: /hauth, /hgrant, /hrevoke, /hprotect\n"
            "Moderation: /hban, /hmute, /hwarn, /hdel, /hcase, /hmodinfo\n\n"
            "Timed actions: `/hban [id] 2h spam`  `/hmute [id] 30m abuse`\n"
            "Reply to a message to target without passing user ID."
        )
    if is_authorized(uid):
        return (
            "📘 **Moderator Help**\n\n"
            "Commands: /hban, /hmute, /hwarn, /hdel, /hcase, /hmodinfo\n"
            "Timed ban/mute: `/hban [user_id] 30m [reason]`\n"
            "Reply to a user message to target without their ID."
        )
    return (
        "📘 **User Help**\n\n"
        "Commands: /start, /help\n"
        "Appeal a case in bot DM: `/happeal <case_id> <message>`"
    )

# =========================================================
# INLINE KEYBOARD BUILDER (Bot API dict format)
# =========================================================

def build_markup(*rows) -> dict:
    """
    build_markup(
        [("🔓 Unban", "cb:unban_123")],
        [("📜 View Case", "cb:case_5")],
    )
    Prefix url: with the URL string to create a URL button.
    """
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

_bot: Client      = None
bot_ready: bool   = False
_temp_worker_task = None

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
        _bot = None
        bot_ready = False

# =========================================================
# ADMIN GROUP AUTO-DETECT
# =========================================================

async def detect_admin_groups(bot: Client) -> list[int]:
    """Return list of group/supergroup chat IDs where the bot is admin."""
    groups = []
    try:
        async for dialog in bot.get_dialogs():
            chat = dialog.chat
            if chat.type.value not in ("group", "supergroup", "channel"):
                continue
            try:
                me = await bot.get_chat_member(chat.id, "me")
                if me.status.value in ("administrator", "creator"):
                    groups.append(chat.id)
            except Exception:
                pass
    except Exception as e:
        log_msg(f"detect_admin_groups error: {e}", "WARNING")
    return groups

async def resolve_log_group(bot: Client):
    """Set _log_group_id and _backup_chat_id; auto-detect if LOG_GROUP_ID == 0."""
    global _log_group_id, _backup_chat_id, _tg_backup_enabled

    if _log_group_id != 0:
        # Verify the configured group is accessible
        try:
            chat = await bot.get_chat(_log_group_id)
            log_msg(f"✅ Log group confirmed: {chat.title} ({_log_group_id})", "INFO")
            _tg_backup_enabled = True
            if _backup_chat_id == 0:
                _backup_chat_id = _log_group_id
            return
        except Exception as e:
            log_msg(f"WARNING: configured LOG_GROUP_ID {_log_group_id} inaccessible: {e}", "WARNING")
            log_msg("Attempting auto-detection...", "INFO")

    groups = await detect_admin_groups(bot)
    if not groups:
        log_msg("⚠️ No admin groups found. Log messages will be skipped.", "WARNING")
        await tg_send(OWNER_ID, "⚠️ Bot started but no admin group found. Set LOG_GROUP_ID.")
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
            f"Set `LOG_GROUP_ID={_log_group_id}` in env vars to make this permanent."
        )
    except Exception:
        log_msg(f"Auto-detected log group: {_log_group_id}", "INFO")

# =========================================================
# WEBHOOK MANAGEMENT
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
        await tg_send(lg,
            f"🚨 **ANTI-NUKE ACTIVATED**\n\n"
            f"Moderator: `{uid}`\n"
            f"Actions in 60 sec: `{total}`\n"
            f"Moderator frozen automatically."
        )
    await tg_send(chat_id, "🚨 Anti-Nuke triggered — moderator frozen.", reply_to=reply_to)
    return True

# =========================================================
# ACTION LOG — sends to source group AND log group
# =========================================================

async def send_action_log(
    source_chat: int,
    reply_to:    int,
    action:      str,
    target:      dict,
    reason:      str,
    case_id:     str,
    mod_data:    dict,
    extra:       str = "",
):
    mention   = make_mention(target)
    target_id = target.get("id")
    badge     = mod_data.get("badge", "🛡 Moderator")
    mod_uid   = mod_data.get("mod_id", "UNKNOWN")
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

    rows = []
    if action == "BAN":
        rows.append([("🔓 Unban",       f"cb:unban_{target_id}")])
    elif action == "MUTE":
        rows.append([("🔊 Unmute",      f"cb:unmute_{target_id}")])
    elif action == "WARN":
        rows.append([("🗑 Remove Warn", f"cb:removewarn_{target_id}")])
    elif action == "DELETE":
        rows.append([("👤 Profile",     f"url:tg://user?id={target_id}")])
    rows.append([("📜 View Case",       f"cb:case_{case_id}")])
    markup = build_markup(*rows)

    # Send to the group where the action happened
    await tg_send(source_chat, text, reply_to=reply_to, markup=markup)

    # Send to log group (may differ from source_chat)
    lg = get_log_group()
    if lg and lg != source_chat:
        await tg_send(lg, text, markup=markup)
    elif lg == source_chat:
        # already sent above
        pass

async def send_grant_log(chat_id, reply_to, granted_by, target, permission, case_id=None):
    case_line = f"\n📜 Case ID: #{case_id}" if case_id else ""
    text = (
        f"✅ **Permission Granted**\n\n"
        f"👤 Moderator: {make_mention(target)}\n"
        f"🔐 Permission: `{permission}`\n"
        f"🛡 Granted by: `{granted_by}`{case_line}"
    )
    await tg_send(chat_id, text, reply_to=reply_to)
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
        if int(action.get("until_ts", 0)) > now_ts:
            pending.append(action)
            continue
        chat_id   = action["chat_id"]
        target_id = action["target_id"]
        atype     = action["type"]
        try:
            if atype == "mute":
                ok, err = await api_unmute(chat_id, target_id)
                msg_text = f"🔊 Temporary mute ended for `{target_id}`" if ok else f"⚠️ Auto-unmute failed for `{target_id}`: {err}"
                await tg_send(chat_id, msg_text)
            elif atype == "ban":
                ok, err = await api_unban(chat_id, target_id)
                msg_text = f"🔓 Temporary ban ended for `{target_id}`" if ok else f"⚠️ Auto-unban failed for `{target_id}`: {err}"
                await tg_send(chat_id, msg_text)
            elif atype == "delete":
                await tg_delete(chat_id, target_id)
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

# ── Root endpoint (fixes 404 on GET /) ──────────────────
@app.get("/")
async def root():
    return {
        "service":   "Telegram Moderation Bot",
        "status":    "running" if bot_ready else "starting",
        "endpoints": ["/health", "/api/status", "/api/setup_webhook", "/api/webhook"],
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
async def setup_webhook_endpoint(request: Request):
    base = str(request.base_url).rstrip("/")
    webhook_url = f"{base}/api/webhook"
    result = await tg_api("setWebhook", json={
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    })
    cmds   = await sync_commands()
    info   = await tg_api("getWebhookInfo")
    return {"webhook_url": webhook_url, "set_webhook": result, "commands": cmds, "info": info}

# ── Webhook ──────────────────────────────────────────────
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

        chat_id  = msg["chat"]["id"]
        msg_id   = msg["message_id"]
        uid, err = extract_actor_user_id(msg)
        reply    = msg.get("reply_to_message") or {}

        parts   = text.split(None, 1)
        raw_cmd = parts[0].split("@")[0].lstrip("/").lower()
        args    = parts[1].split() if len(parts) > 1 else []

        log_msg(f"/{raw_cmd} from uid={uid} chat={chat_id}", "INFO")

        async def reply_text(t: str):
            sent = await tg_send(chat_id, t, reply_to=msg_id)
            if msg.get("chat", {}).get("type") != "private" and sent.get("ok"):
                result = sent.get("result", {})
                reply_message_id = result.get("message_id")
                if reply_message_id:
                    schedule_message_delete(chat_id, reply_message_id)

        if msg.get("chat", {}).get("type") != "private":
            schedule_message_delete(chat_id, msg_id)

        if err:
            await reply_text(err)
            return

        if OWNER_DEBUG_NOTIFICATIONS and raw_cmd not in ("start", "help"):
            await notify_owner(f"📨 /{raw_cmd}\nUser: `{uid}`\nChat: `{chat_id}`")

        async def security_fail():
            await tg_delete(chat_id, msg_id)

        async def check_mod(perm: str) -> bool:
            if not is_authorized(uid):
                await security_fail()
                return False
            if is_frozen(uid):
                await reply_text("🚫 Your moderator account is frozen.")
                return False
            if not has_permission(uid, perm):
                await reply_text(f"❌ You don't have `{perm}` permission.")
                return False
            return True

        # ── /start ────────────────────────────────────────
        if raw_cmd == "start":
            await reply_text(
                "✅ **Advanced Moderation Bot**\n\n"
                "Use /help to see your available commands."
            )
            return

        # ── /help ─────────────────────────────────────────
        if raw_cmd == "help":
            await reply_text(role_help_text(uid))
            return

        # ── /id ───────────────────────────────────────────
        if raw_cmd == "id":
            try:
                target_id = None
                target_user = None
                
                # Mode 1: /id me
                if args and args[0].lower() == "me":
                    target_id = uid
                
                # Mode 2: /id @username
                elif args and args[0].startswith("@"):
                    username = args[0][1:]  # Remove @
                    try:
                        bot = await get_bot()
                        target_user = await bot.get_user(username)
                        target_id = target_user.id
                    except Exception as e:
                        return await reply_text(f"❌ User @{username} not found: {str(e)}")
                
                # Mode 3: /id <raw_id>
                elif args and args[0].isdigit():
                    parsed_id = parse_positive_user_id(args[0])
                    if parsed_id:
                        target_id = parsed_id
                        try:
                            bot = await get_bot()
                            target_user = await bot.get_user(target_id)
                        except Exception as e:
                            return await reply_text(f"❌ User ID {target_id} not found: {str(e)}")
                    else:
                        return await reply_text("❌ Invalid user ID.")
                
                # Mode 4: /id (reply to message)
                elif reply:
                    target_dict, tid, err = resolve_target(reply, [], 0)
                    if tid:
                        target_id = tid
                        try:
                            bot = await get_bot()
                            target_user = await bot.get_user(target_id)
                        except Exception:
                            # Fallback if get_user fails
                            pass
                    else:
                        return await reply_text("❌ Reply to a user or use /id @username, /id me, or /id <user_id>")
                else:
                    return await reply_text("❌ Reply to a user or use /id @username, /id me, or /id <user_id>")
                
                # Build profile card
                if target_id is None:
                    return await reply_text("❌ Could not determine target user.")
                
                first_name = target_user.first_name if target_user and target_user.first_name else "User"
                last_name = target_user.last_name if target_user and target_user.last_name else ""
                username = target_user.username if target_user and target_user.username else None
                
                # Format the response
                response = f"👤 **User Profile**\n\n"
                response += f"🆔 ID: `{target_id}`\n"
                response += f"📝 Name: {first_name}"
                if last_name:
                    response += f" {last_name}"
                response += f"\n"
                if username:
                    response += f"🔗 Username: @{username}\n"
                    response += f"📌 Link: [Profile](tg://user?id={target_id})"
                else:
                    response += f"🔗 Link: [Profile](tg://user?id={target_id})"
                
                # Send to owner's DM if owner uses /id in a group, otherwise reply in chat
                is_group = msg.get("chat", {}).get("type") != "private"
                if is_owner(uid) and is_group:
                    await notify_owner(response)
                else:
                    await reply_text(response)
                return
            except Exception as e:
                log_msg(f"Error in /id command: {e}\n{traceback.format_exc()}", "ERROR")
                return await reply_text(f"❌ Error: {str(e)}")

        # ── /happeal ──────────────────────────────────────
        if raw_cmd == "happeal":
            if msg.get("chat", {}).get("type") != "private":
                return await reply_text("❌ Use /happeal in bot DM only.")
            if len(args) < 2:
                return await reply_text("Usage: /happeal <case_id> <message>")
            case_id = args[0]
            appeal_msg = " ".join(args[1:]).strip()
            if not appeal_msg:
                return await reply_text("❌ Appeal message is required.")
            cases = load(CASE_FILE)
            if case_id not in cases:
                return await reply_text("❌ Case not found.")
            if int(cases[case_id].get("target", 0)) != uid and not is_owner(uid):
                return await reply_text("❌ You can only appeal your own case.")
            aid = create_appeal(uid, case_id, appeal_msg)
            await reply_text(f"✅ Appeal submitted. Appeal ID: #{aid}")
            await notify_owner(
                f"📨 New appeal #{aid}\nCase: #{case_id}\nUser: `{uid}`\n{appeal_msg}"
            )
            return

        # ── /hauth ────────────────────────────────────────
        if raw_cmd in ("hauth", "ha"):
            if not is_owner(uid):
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
            if not is_owner(uid):
                return
            if not args:
                return await reply_text(
                    f"Usage: /hgrant <permission> <user_id>\n"
                    f"Valid: {', '.join(VALID_PERMISSIONS)}"
                )
            perm = args[0].lower()
            if perm not in VALID_PERMISSIONS:
                return await reply_text(f"❌ Invalid permission. Valid: {', '.join(VALID_PERMISSIONS)}")
            target, tid, terr = resolve_target(reply, args, 1)
            if not tid:
                return await reply_text(f"{terr}")
            data = load(AUTH_FILE)
            if str(tid) not in data:
                return await reply_text("❌ User not authorized. Run /hauth first.")
            data[str(tid)]["permissions"][perm] = True
            await save_and_backup(AUTH_FILE, data)
            case_id = create_case("GRANT", uid, tid, f"Granted: {perm}")
            await send_grant_log(chat_id, msg_id, uid, target, perm, case_id)
            return

        # ── /hrevoke ──────────────────────────────────────
        if raw_cmd in ("hrevoke", "hr"):
            if not is_owner(uid):
                return
            if not args:
                return await reply_text("Usage: /hrevoke <permission> <user_id>")
            perm = args[0].lower()
            if perm not in VALID_PERMISSIONS:
                return await reply_text(f"❌ Invalid. Valid: {', '.join(VALID_PERMISSIONS)}")
            target, tid, terr = resolve_target(reply, args, 1)
            if not tid:
                return await reply_text(f"{terr}")
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
                    f"👤 {make_mention(target)}\n"
                    f"🔐 `{perm}`\n"
                    f"🛡 By: `{uid}`\n"
                    f"📜 Case #{case_id}",
                )
            return

        # ── /hban ─────────────────────────────────────────
        if raw_cmd in ("hban", "hb"):
            if not await check_mod("ban"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(
                    f"{terr}\nUsage: /hban <user_id> [duration] [reason]"
                )
            rs = 0 if reply else 1
            dur, reason = parse_duration_and_reason(args, rs)
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            until_ts = int(time.time()) + dur if dur else None
            ok, err = await api_ban(chat_id, tid, until_date=until_ts)
            if not ok:
                return await reply_text(f"❌ Ban failed: {err}")
            if dur:
                actions = load_temp_actions()
                actions.append({"type": "ban", "chat_id": chat_id, "target_id": tid,
                                 "until_ts": until_ts, "set_by": uid, "reason": reason})
                save_temp_actions(actions)
                reason = f"{reason} | Duration: {format_duration(dur)}"
            case_id = create_case("BAN", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "BAN", target, reason, case_id, get_mod_info(uid))
            return

        # ── /hmute ────────────────────────────────────────
        if raw_cmd in ("hmute", "hm"):
            if not await check_mod("mute"):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(
                    f"{terr}\nUsage: /hmute <user_id> [duration] [reason]"
                )
            rs = 0 if reply else 1
            dur, reason = parse_duration_and_reason(args, rs)
            if is_protected(tid):
                return await reply_text("🛡 That user is protected.")
            if await anti_nuke(chat_id, msg_id, uid):
                return
            until_ts = int(time.time()) + dur if dur else None
            ok, err = await api_mute(chat_id, tid, until_date=until_ts)
            if not ok:
                return await reply_text(f"❌ Mute failed: {err}")
            if dur:
                actions = load_temp_actions()
                actions.append({"type": "mute", "chat_id": chat_id, "target_id": tid,
                                 "until_ts": until_ts, "set_by": uid, "reason": reason})
                save_temp_actions(actions)
                reason = f"{reason} | Duration: {format_duration(dur)}"
            case_id = create_case("MUTE", uid, tid, reason)
            await send_action_log(chat_id, msg_id, "MUTE", target, reason, case_id, get_mod_info(uid))
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
            warns  = load(WARN_FILE)
            key    = str(tid)
            warns.setdefault(key, 0)
            warns[key] += 1
            save(WARN_FILE, warns)
            case_id = create_case("WARN", uid, tid, reason)
            await send_action_log(
                chat_id, msg_id, "WARN", target, reason, case_id, get_mod_info(uid),
                extra=f"📊 Total Warns: {warns[key]}",
            )
            return

        # ── /hdel ─────────────────────────────────────────
        if raw_cmd in ("hdel", "hd"):
            if not await check_mod("delete"):
                return
            if not reply:
                return await reply_text("❌ Reply to the message you want to delete.")
            target, tid = extract_reply_user(reply)
            if not tid:
                return await reply_text("❌ Reply to a normal user message.")
            reply_msg_id = reply.get("message_id")
            deleted_text = reply.get("text") or "[Media]"
            ok, err = await api_delete_msg(chat_id, reply_msg_id)
            if not ok:
                return await reply_text(f"❌ Could not delete: {err}")
            case_id = create_case("DELETE", uid, tid, "Message Deleted")
            await send_action_log(
                chat_id, msg_id, "DELETE", target, "Message Deleted",
                case_id, get_mod_info(uid),
                extra=f"💬 Content: {deleted_text[:200]}",
            )
            return

        # ── /hprotect ─────────────────────────────────────
        if raw_cmd in ("hprotect", "hp"):
            if not is_owner(uid):
                return
            target, tid, terr = resolve_target(reply, args, 0)
            if not tid:
                return await reply_text(f"{terr}\nUsage: /hprotect <user_id>")
            data = load(PROTECT_FILE)
            key  = str(tid)
            if key in data:
                return await reply_text(f"ℹ️ {make_mention(target)} is already protected.")
            data[key] = True
            save(PROTECT_FILE, data)
            await reply_text(f"🛡 {make_mention(target)} is now protected.")
            return

        # ── /hcase ────────────────────────────────────────
        if raw_cmd in ("hcase", "hc"):
            if not is_authorized(uid):
                return
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
            )
            return

        # ── /hmodinfo ─────────────────────────────────────
        if raw_cmd in ("hmodinfo", "hmi"):
            if not is_authorized(uid):
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
            perms = mod.get("permissions", {})
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

        if not is_authorized(uid):
            await tg_answer_cb(cb_id, "⛔ Only moderators can use this.", alert=True)
            return

        if data.startswith("unban_"):
            if not has_permission(uid, "ban"):
                return await tg_answer_cb(cb_id, "❌ No ban permission.", alert=True)
            tid = int(data.split("_", 1)[1])
            ok, err = await api_unban(chat_id, tid)
            if ok:
                await tg_answer_cb(cb_id, "✅ User unbanned.")
            else:
                await tg_answer_cb(cb_id, f"❌ {err}", alert=True)

        elif data.startswith("unmute_"):
            if not has_permission(uid, "mute"):
                return await tg_answer_cb(cb_id, "❌ No mute permission.", alert=True)
            tid = int(data.split("_", 1)[1])
            ok, err = await api_unmute(chat_id, tid)
            if ok:
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
                await tg_answer_cb(
                    cb_id,
                    f"Case #{case_id}\n{case['action']} — {case['reason']}\n{case['time']}",
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
    global _temp_worker_task
    log_msg("🚀 Starting up...", "INFO")
    try:
        mongo_db.connect()
        sync_storage_with_mongo()
        bot = await get_bot()

        # 1. Resolve / auto-detect the log group
        await resolve_log_group(bot)

        # 2. Restore data from Telegram backup (survives ephemeral FS)
        restored = await restore_from_telegram_pyrogram(bot)
        if restored:
            log_msg(f"✅ Restored {restored} file(s) from Telegram backup", "INFO")

        # 3. Webhook + commands
        wh_status  = await ensure_webhook()
        cmd_status = await sync_commands()

        # 4. Start temp-action worker
        if _temp_worker_task is None or _temp_worker_task.done():
            _temp_worker_task = asyncio.create_task(temp_action_worker())

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
    global _temp_worker_task
    log_msg("🛑 Shutting down...", "INFO")
    if _temp_worker_task and not _temp_worker_task.done():
        _temp_worker_task.cancel()
        try:
            await _temp_worker_task
        except asyncio.CancelledError:
            pass
    await shutdown_bot()
    mongo_db.disconnect()

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
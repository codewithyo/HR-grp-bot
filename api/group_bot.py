# =========================================================
# ADVANCED TELEGRAM MODERATION BOT
# FINAL SECURE VERSION
# =========================================================

# INSTALL:
# pip install pyrogram tgcrypto

# =========================================================
# CONFIG
# =========================================================

API_ID = 123456
API_HASH = "YOUR_API_HASH"
BOT_TOKEN = "YOUR_BOT_TOKEN"

OWNER_ID = 123456789
LOG_GROUP_ID = -100123456789

# =========================================================
# IMPORTS
# =========================================================

from pyrogram import Client, filters, idle
from pyrogram.types import (
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand
)

from datetime import datetime
import json
import os
import time
import random
import string

# =========================================================
# BOT START
# =========================================================

app = Client(
    "advanced_mod_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# =========================================================
# FILES
# =========================================================

AUTH_FILE = "auth.json"
WARN_FILE = "warns.json"
CASE_FILE = "cases.json"
PROTECT_FILE = "protected.json"
ABUSE_FILE = "abuse.json"

FILES = [
    AUTH_FILE,
    WARN_FILE,
    CASE_FILE,
    PROTECT_FILE,
    ABUSE_FILE
]

for file in FILES:
    if not os.path.exists(file):
        with open(file, "w") as f:
            json.dump({}, f)

# =========================================================
# JSON HELPERS
# =========================================================

def load(file):
    with open(file, "r") as f:
        return json.load(f)

def save(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

# =========================================================
# CHECKS
# =========================================================

def is_owner(user_id):
    return user_id == OWNER_ID

def is_authorized(user_id):

    if user_id == OWNER_ID:
        return True

    data = load(AUTH_FILE)

    return str(user_id) in data

def has_permission(user_id, permission):

    if is_owner(user_id):
        return True

    data = load(AUTH_FILE)

    user = data.get(str(user_id))

    if not user:
        return False

    return user.get(
        "permissions",
        {}
    ).get(permission, False)

# =========================================================
# GENERATE MOD ID
# =========================================================

def generate_mod_id():

    chars = (
        string.ascii_uppercase +
        string.digits
    )

    return "MOD-" + ''.join(
        random.choice(chars)
        for _ in range(5)
    )

# =========================================================
# GET MOD INFO
# =========================================================

def get_mod_info(user_id):

    data = load(AUTH_FILE)

    return data.get(str(user_id), {})

# =========================================================
# PROTECTED USER
# =========================================================

def is_protected(user_id):

    data = load(PROTECT_FILE)

    return str(user_id) in data

# =========================================================
# CREATE CASE
# =========================================================

def create_case(
    action,
    moderator,
    target,
    reason
):

    cases = load(CASE_FILE)

    case_id = str(len(cases) + 1)

    cases[case_id] = {
        "action": action,
        "moderator": moderator,
        "target": target,
        "reason": reason,
        "time": str(datetime.now())
    }

    save(CASE_FILE, cases)

    return case_id

# =========================================================
# TRACK ACTIONS
# =========================================================

def track_action(user_id):

    data = load(ABUSE_FILE)

    uid = str(user_id)

    now = time.time()

    if uid not in data:
        data[uid] = []

    data[uid].append(now)

    data[uid] = [
        x for x in data[uid]
        if now - x <= 60
    ]

    save(ABUSE_FILE, data)

    return len(data[uid])

# =========================================================
# ANTI NUKE
# =========================================================

async def anti_nuke(
    message,
    user_id
):

    total = track_action(user_id)

    if total >= 10:

        auth = load(AUTH_FILE)

        if str(user_id) in auth:

            auth[str(user_id)][
                "frozen"
            ] = True

            save(AUTH_FILE, auth)

        await app.send_message(
            LOG_GROUP_ID,
            f"""
🚨 ANTI-NUKE ACTIVATED

Moderator: `{user_id}`
Actions in 60 sec: `{total}`

Moderator Frozen Automatically.
"""
        )

        await message.reply(
            "🚨 Anti-Nuke Triggered.\n"
            "Moderator Frozen."
        )

        return True

    return False

# =========================================================
# ACTION LOG
# =========================================================

async def send_action_log(
    message,
    action,
    target,
    reason,
    case_id,
    moderator_data,
    extra=""
):

    badge = moderator_data.get(
        "badge",
        "🛡 Moderator"
    )

    mod_unique = moderator_data.get(
        "mod_id",
        "UNKNOWN"
    )

    time_now = datetime.now().strftime(
        "%d %b %Y • %I:%M %p"
    )

    text = f"""
╭━━━〔 🚨 MODERATION ACTION 〕━━━╮

👤 User: {target.mention}
🆔 User ID: `{target.id}`

⚔ Action: {action}
📝 Reason: {reason}

👮 Moderator:
{badge} | {mod_unique}

⏰ Time: {time_now}
📜 Case ID: #{case_id}

{extra}

╰━━━━━━━━━━━━━━━━━━━━━━╯
"""

    buttons = []

    if action == "BAN":

        buttons.append(
            [
                InlineKeyboardButton(
                    "🔓 Unban",
                    callback_data=f"unban_{target.id}"
                )
            ]
        )

    elif action == "MUTE":

        buttons.append(
            [
                InlineKeyboardButton(
                    "🔊 Unmute",
                    callback_data=f"unmute_{target.id}"
                )
            ]
        )

    elif action == "WARN":

        buttons.append(
            [
                InlineKeyboardButton(
                    "🗑 Remove Warn",
                    callback_data=f"removewarn_{target.id}"
                )
            ]
        )

    elif action == "DELETE":

        buttons.append(
            [
                InlineKeyboardButton(
                    "👤 Profile",
                    url=f"tg://user?id={target.id}"
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "📜 View Case",
                callback_data=f"case_{case_id}"
            )
        ]
    )

    reply_markup = InlineKeyboardMarkup(
        buttons
    )

    await message.reply(
        text,
        reply_markup=reply_markup
    )

    await app.send_message(
        LOG_GROUP_ID,
        text,
        reply_markup=reply_markup
    )

# =========================================================
# STARTUP
# =========================================================

async def startup():

    commands = [

        BotCommand("hauth", "Authorize Moderator"),
        BotCommand("hgrant", "Grant Permission"),
        BotCommand("hrevoke", "Revoke Permission"),

        BotCommand("hban", "Ban User"),
        BotCommand("hmute", "Mute User"),
        BotCommand("hwarn", "Warn User"),
        BotCommand("hdel", "Delete Message"),

        BotCommand("hprotect", "Protect User"),
        BotCommand("hcase", "View Case"),
        BotCommand("hmodinfo", "Moderator Info")
    ]

    await app.set_bot_commands(
        commands
    )

    await app.send_message(
        OWNER_ID,
        """
🚀 Advanced Moderation Bot Started

✅ Commands Loaded
✅ Anti-Nuke Active
✅ Action Logs Active
✅ Buttons Active
✅ Moderation System Online
"""
    )

# =========================================================
# AUTH
# =========================================================

@app.on_message(
    filters.command(["hauth", "ha"])
)
async def auth(client, message):

    if not is_owner(
        message.from_user.id
    ):
        return

    if not message.reply_to_message:
        return await message.reply(
            "Reply to user."
        )

    target = (
        message.reply_to_message
        .from_user
    )

    data = load(AUTH_FILE)

    if str(target.id) not in data:

        data[str(target.id)] = {
            "permissions": {},
            "mod_id": generate_mod_id(),
            "badge": "🛡 Moderator",
            "frozen": False
        }

    save(AUTH_FILE, data)

    await message.reply(
        f"✅ Authorized "
        f"{target.mention}"
    )

# =========================================================
# GRANT
# =========================================================

@app.on_message(
    filters.command(["hgrant", "hg"])
)
async def grant(client, message):

    if not is_owner(
        message.from_user.id
    ):
        return

    if not message.reply_to_message:
        return

    args = message.text.split()

    if len(args) < 2:
        return

    permission = args[1].lower()

    target = (
        message.reply_to_message
        .from_user
    )

    data = load(AUTH_FILE)

    data[str(target.id)][
        "permissions"
    ][permission] = True

    save(AUTH_FILE, data)

    await message.reply(
        f"✅ Granted {permission}"
    )

# =========================================================
# REVOKE
# =========================================================

@app.on_message(
    filters.command(["hrevoke", "hr"])
)
async def revoke(client, message):

    if not is_owner(
        message.from_user.id
    ):
        return

    if not message.reply_to_message:
        return

    args = message.text.split()

    if len(args) < 2:
        return

    permission = args[1].lower()

    target = (
        message.reply_to_message
        .from_user
    )

    data = load(AUTH_FILE)

    data[str(target.id)][
        "permissions"
    ][permission] = False

    save(AUTH_FILE, data)

    await message.reply(
        f"❌ Revoked {permission}"
    )

# =========================================================
# COMMON SECURITY
# =========================================================

async def security_check(message):

    if not is_authorized(
        message.from_user.id
    ):

        try:
            await message.delete()
        except:
            pass

        return False

    return True

# =========================================================
# BAN
# =========================================================

@app.on_message(
    filters.command(["hban", "hb"])
)
async def ban(client, message):

    if not await security_check(message):
        return

    mod_id = message.from_user.id

    if not has_permission(
        mod_id,
        "ban"
    ):
        return await message.reply(
            "❌ No Permission"
        )

    data = get_mod_info(mod_id)

    if data.get("frozen"):
        return await message.reply(
            "🚫 Moderator Frozen"
        )

    if not message.reply_to_message:
        return

    target = (
        message.reply_to_message
        .from_user
    )

    if is_protected(target.id):
        return await message.reply(
            "🛡 Protected User"
        )

    args = message.text.split(
        None,
        1
    )

    reason = (
        args[1]
        if len(args) > 1
        else "No Reason"
    )

    triggered = await anti_nuke(
        message,
        mod_id
    )

    if triggered:
        return

    await client.ban_chat_member(
        message.chat.id,
        target.id
    )

    case_id = create_case(
        "BAN",
        mod_id,
        target.id,
        reason
    )

    await send_action_log(
        message,
        "BAN",
        target,
        reason,
        case_id,
        data
    )

# =========================================================
# MUTE
# =========================================================

@app.on_message(
    filters.command(["hmute", "hm"])
)
async def mute(client, message):

    if not await security_check(message):
        return

    mod_id = message.from_user.id

    if not has_permission(
        mod_id,
        "mute"
    ):
        return await message.reply(
            "❌ No Permission"
        )

    data = get_mod_info(mod_id)

    if data.get("frozen"):
        return await message.reply(
            "🚫 Moderator Frozen"
        )

    if not message.reply_to_message:
        return

    target = (
        message.reply_to_message
        .from_user
    )

    if is_protected(target.id):
        return await message.reply(
            "🛡 Protected User"
        )

    args = message.text.split(
        None,
        1
    )

    reason = (
        args[1]
        if len(args) > 1
        else "No Reason"
    )

    triggered = await anti_nuke(
        message,
        mod_id
    )

    if triggered:
        return

    await client.restrict_chat_member(
        message.chat.id,
        target.id,
        ChatPermissions()
    )

    case_id = create_case(
        "MUTE",
        mod_id,
        target.id,
        reason
    )

    await send_action_log(
        message,
        "MUTE",
        target,
        reason,
        case_id,
        data
    )

# =========================================================
# WARN
# =========================================================

@app.on_message(
    filters.command(["hwarn", "hw"])
)
async def warn(client, message):

    if not await security_check(message):
        return

    mod_id = message.from_user.id

    if not has_permission(
        mod_id,
        "warn"
    ):
        return await message.reply(
            "❌ No Permission"
        )

    data = get_mod_info(mod_id)

    if data.get("frozen"):
        return await message.reply(
            "🚫 Moderator Frozen"
        )

    if not message.reply_to_message:
        return

    target = (
        message.reply_to_message
        .from_user
    )

    warns = load(WARN_FILE)

    uid = str(target.id)

    if uid not in warns:
        warns[uid] = 0

    warns[uid] += 1

    save(WARN_FILE, warns)

    case_id = create_case(
        "WARN",
        mod_id,
        target.id,
        "Warning"
    )

    await send_action_log(
        message,
        "WARN",
        target,
        f"Warning #{warns[uid]}",
        case_id,
        data,
        extra=f"📊 Total Warns: {warns[uid]}"
    )

# =========================================================
# DELETE
# =========================================================

@app.on_message(
    filters.command(["hdel", "hd"])
)
async def delete_msg(client, message):

    if not await security_check(message):
        return

    mod_id = message.from_user.id

    if not has_permission(
        mod_id,
        "delete"
    ):
        return await message.reply(
            "❌ No Permission"
        )

    data = get_mod_info(mod_id)

    if not message.reply_to_message:
        return

    target = (
        message.reply_to_message
        .from_user
    )

    deleted_text = (
        message.reply_to_message.text
        or "Media Message"
    )

    await (
        message.reply_to_message
        .delete()
    )

    case_id = create_case(
        "DELETE",
        mod_id,
        target.id,
        "Message Deleted"
    )

    await send_action_log(
        message,
        "DELETE",
        target,
        "Message Deleted",
        case_id,
        data,
        extra=f"💬 Deleted Text: {deleted_text}"
    )

# =========================================================
# CALLBACKS
# =========================================================

@app.on_callback_query()
async def callbacks(
    client,
    callback_query
):

    data = callback_query.data

    if (
        callback_query
        .from_user.id
        != OWNER_ID
    ):

        return await (
            callback_query.answer(
                "Only Owner Can Use This",
                show_alert=True
            )
        )

    if data.startswith("unban_"):

        user_id = int(
            data.split("_")[1]
        )

        await client.unban_chat_member(
            callback_query.message.chat.id,
            user_id
        )

        await callback_query.answer(
            "User Unbanned"
        )

    elif data.startswith("unmute_"):

        user_id = int(
            data.split("_")[1]
        )

        await client.restrict_chat_member(
            callback_query.message.chat.id,
            user_id,
            ChatPermissions(
                can_send_messages=True
            )
        )

        await callback_query.answer(
            "User Unmuted"
        )

# =========================================================
# START
# =========================================================

@app.on_message(
    filters.command("start")
)
async def start(client, message):

    if not is_authorized(
        message.from_user.id
    ):
        return

    await message.reply(
        "✅ Advanced Moderation Bot Running"
    )

# =========================================================
# MAIN
# =========================================================

async def main():

    await app.start()

    print("BOT STARTED")

    await startup()

    await idle()

# =========================================================
# RUN
# =========================================================

app.run(main())
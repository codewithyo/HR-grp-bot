"""
Tic-Tac-Toe Game Module for Telegram Bot
Uses httpx (no Pyrogram) + asyncio
Commands: /ttt, /tttleaderboard, /tttmystats, /tttend
"""

import asyncio
import json
import re
import random
import string
import time
from pathlib import Path
from typing import Callable, Optional, Dict

import httpx

# ─────────────────────────────────────────────────────────
# CONFIGURATION & GLOBALS
# ─────────────────────────────────────────────────────────

BOT_TOKEN = ""
SCORES_FILE = "data/ttt_scores.json"
STATE_FILE = "data/ttt_state.json"
SAVE_FN: Optional[Callable] = None
LOAD_FN: Optional[Callable] = None
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None

# Game storage
GAMES: Dict[str, dict] = {}  # game_id → game state
CHALLENGES: Dict[str, dict] = {}  # challenge_id → challenge
PLAYER_GAMES: Dict[int, Dict[int, str]] = {}  # uid → {chat_id: game_id}

CHALLENGE_TIMEOUT = 60  # seconds
MOVE_TIMEOUT = 120  # seconds per move
CLEANUP_INTERVAL = 10  # check for timeouts every N seconds
FINISHED_GAME_CACHE = 300  # keep finished games for 5 min (rematch)
BOT_PLAYER_ID = 0
BOT_PLAYER_NAME = "HR SentriX 🤖"

TTT_CALLBACK_PREFIXES = (
    "ttt_accept_",
    "ttt_decline_",
    "ttt_move_",
    "ttt_rematch_",
    "ttt_noop_",
    "ttt_leaderboard",
)


async def _get_http_client() -> httpx.AsyncClient:
    """Reuse a single HTTP client for better latency and connection pooling."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _HTTP_CLIENT


async def shutdown_games():
    """Close shared HTTP resources."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        await _HTTP_CLIENT.aclose()
        _HTTP_CLIENT = None


def _load_data(file_path: str, default):
    """Load data via injected loader (Mongo-aware in main app) or local JSON fallback."""
    try:
        if LOAD_FN:
            data = LOAD_FN(file_path)
            return data if data not in (None, "") else default
        with open(file_path, "r") as f:
            data = json.load(f)
        return data if data not in (None, "") else default
    except Exception:
        return default


def _save_data(file_path: str, data):
    """Save data via injected saver (Mongo-aware in main app) or local JSON fallback."""
    try:
        if SAVE_FN:
            SAVE_FN(file_path, data)
            return
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[TTT] Error saving {file_path}: {e}")


def _persist_state():
    """Persist all in-memory game structures."""
    payload = {
        "games": GAMES,
        "challenges": CHALLENGES,
        "player_games": {
            str(uid): {str(cid): gid for cid, gid in games.items()}
            for uid, games in PLAYER_GAMES.items()
        },
        "saved_at": time.time(),
    }
    _save_data(STATE_FILE, payload)


def _restore_state():
    """Restore in-memory game structures from storage."""
    global GAMES, CHALLENGES, PLAYER_GAMES
    payload = _load_data(STATE_FILE, {})
    if not isinstance(payload, dict):
        return

    games = payload.get("games", {})
    challenges = payload.get("challenges", {})
    GAMES = games if isinstance(games, dict) else {}

    # Normalize challenge keys as strings (challenge IDs are strings).
    normalized_challenges = {}
    if isinstance(challenges, dict):
        for key, value in challenges.items():
            if key is None:
                continue
            normalized_challenges[str(key)] = value
    CHALLENGES = normalized_challenges

    # Rebuild per-user game index from active games for consistency.
    PLAYER_GAMES = {}
    if isinstance(GAMES, dict):
        for game_id, game in GAMES.items():
            if not isinstance(game, dict):
                continue
            if game.get("status") != "active":
                continue
            chat_id = game.get("chat_id")
            if not isinstance(chat_id, int):
                continue
            for pid in (game.get("player_x"), game.get("player_o")):
                if _is_trackable_player(pid):
                    PLAYER_GAMES.setdefault(pid, {})[chat_id] = game_id

    # Drop stale challenge records after restart.
    now = time.time()
    expired = [
        mid for mid, item in CHALLENGES.items()
        if now - float(item.get("created_at", 0)) > CHALLENGE_TIMEOUT
    ]
    for mid in expired:
        CHALLENGES.pop(mid, None)

    if expired:
        _persist_state()


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def generate_game_id() -> str:
    """Generate unique game ID."""
    return "TTT-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def generate_challenge_id() -> str:
    """Generate compact challenge ID for callback payload."""
    return "C" + "".join(random.choices(string.ascii_uppercase + string.digits, k=7))


def _name_from_user(user: dict, default: str = "Player") -> str:
    """Prefer Telegram first+last name, fallback to username/default."""
    if not isinstance(user, dict):
        return default
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full = " ".join(x for x in (first, last) if x).strip()
    if full:
        return full
    uname = (user.get("username") or "").strip()
    return uname or default


def _is_trackable_player(uid: Optional[int]) -> bool:
    return isinstance(uid, int) and uid > 0


def _release_game_players(game: dict):
    """Remove active-game index for human players only."""
    chat_id = game.get("chat_id")
    for pid in (game.get("player_x"), game.get("player_o")):
        if not _is_trackable_player(pid):
            continue
        games_for_user = PLAYER_GAMES.get(pid)
        if not isinstance(games_for_user, dict) or not isinstance(chat_id, int):
            continue
        if games_for_user.get(chat_id) == game.get("game_id"):
            games_for_user.pop(chat_id, None)
            if not games_for_user:
                PLAYER_GAMES.pop(pid, None)


def _line_winning_move(board: list[str], symbol: str) -> Optional[int]:
    combos = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]
    for a, b, c in combos:
        line = [board[a], board[b], board[c]]
        if line.count(symbol) == 2 and line.count(" ") == 1:
            if board[a] == " ":
                return a
            if board[b] == " ":
                return b
            if board[c] == " ":
                return c
    return None


def _choose_bot_move(board: list[str], bot_symbol: str) -> Optional[int]:
    """Simple bot strategy: win, block, center, corner, edge."""
    enemy_symbol = "O" if bot_symbol == "X" else "X"

    win_idx = _line_winning_move(board, bot_symbol)
    if win_idx is not None:
        return win_idx

    block_idx = _line_winning_move(board, enemy_symbol)
    if block_idx is not None:
        return block_idx

    if board[4] == " ":
        return 4

    corners = [i for i in (0, 2, 6, 8) if board[i] == " "]
    if corners:
        return random.choice(corners)

    edges = [i for i in (1, 3, 5, 7) if board[i] == " "]
    if edges:
        return random.choice(edges)

    return None


def load_scores() -> dict:
    """Load leaderboard from file or via injected load_fn."""
    data = _load_data(SCORES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_scores(scores: dict):
    """Save leaderboard to file or via injected save_fn."""
    _save_data(SCORES_FILE, scores)


def check_win(board: list[str]) -> tuple[bool, Optional[str], list[int]]:
    """
    Check if there's a winner.
    Returns (is_winner, symbol, winning_indices).
    """
    combos = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],  # rows
        [0, 3, 6], [1, 4, 7], [2, 5, 8],  # cols
        [0, 4, 8], [2, 4, 6],  # diags
    ]
    for combo in combos:
        a, b, c = combo
        if board[a] == board[b] == board[c] and board[a] in ("X", "O"):
            return True, board[a], combo
    return False, None, []


def is_board_full(board: list[str]) -> bool:
    """Check if board is full (draw)."""
    return all(cell in ("X", "O") for cell in board)


def render_board(
    board: list[str],
    player_x_name: str,
    player_o_name: str,
    status: str,
    winner: Optional[str] = None,
    winning_line: Optional[list] = None,
    turn: Optional[str] = None,
    move_count: int = 0,
) -> str:
    """Render board caption with game state."""
    emoji_map = {" ": "⬜", "X": "❌", "O": "⭕"}
    if winning_line:
        # Highlight winners
        for idx in winning_line:
            if board[idx] == "X":
                emoji_map[board[idx]] = "🟥"
            elif board[idx] == "O":
                emoji_map[board[idx]] = "🟦"

    # Build board grid
    board_lines = []
    for i in range(3):
        row = board[i * 3 : i * 3 + 3]
        board_lines.append(" ".join(emoji_map[cell] for cell in row))

    # Status line
    if status == "active":
        turn_player = player_x_name if turn == "X" else player_o_name
        turn_emoji = "❌" if turn == "X" else "⭕"
        status_line = f"🎯 {turn_emoji} *{turn_player}* — your turn!\n⏳ _120s per move_"
    elif status == "finished":
        if winner == "X":
            status_line = f"🏆 *{player_x_name} wins!* Congratulations!"
        elif winner == "O":
            status_line = f"🏆 *{player_o_name} wins!* Congratulations!"
        else:
            status_line = "🤝 *It's a draw!* Well played both."
    else:
        status_line = "⏸ Game paused"

    caption = (
        f"🎮 *Tic-Tac-Toe*\n\n"
        f"❌  {player_x_name}\n"
        f"⭕  {player_o_name}\n\n"
        + "\n".join(board_lines)
        + f"\n\n{status_line}"
    )
    return caption


# ─────────────────────────────────────────────────────────
# TELEGRAM API WRAPPERS
# ─────────────────────────────────────────────────────────

async def tg_send_message(
    chat_id: int,
    text: str,
    reply_to: Optional[int] = None,
    markup: Optional[dict] = None,
    parse_mode: Optional[str] = "Markdown",
) -> Optional[dict]:
    """Send a message via Bot API."""
    try:
        client = await _get_http_client()
        payload = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        if markup:
            payload["reply_markup"] = markup
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
        )
        data = resp.json()
        if resp.status_code != 200 or not data.get("ok", False):
            print(f"[TTT] sendMessage failed ({resp.status_code}): {data}")
            return None
        return data
    except Exception as e:
        print(f"[TTT] Error sending message: {e}")
        return None


def _escape_markdown(text: str) -> str:
    """Escape Telegram Markdown v1 control characters for usernames and dynamic labels."""
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _to_plain_text(text: str) -> str:
    """Strip simple markdown markers for fallback plain text send."""
    if text is None:
        return ""
    return str(text).replace("*", "").replace("`", "").replace("_", "")


async def _send_markdown_with_fallback(chat_id: int, text: str, reply_to: Optional[int] = None):
    """Try Markdown first; if Telegram rejects formatting, fallback to plain text."""
    sent = await tg_send_message(chat_id, text, reply_to=reply_to, parse_mode="Markdown")
    if sent:
        return sent
    return await tg_send_message(chat_id, _to_plain_text(text), reply_to=reply_to, parse_mode=None)


async def tg_edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    markup: Optional[dict] = None,
    parse_mode: str = "Markdown",
) -> bool:
    """Edit an existing message via Bot API."""
    try:
        client = await _get_http_client()
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if markup:
            payload["reply_markup"] = markup
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json=payload,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[TTT] Error editing message: {e}")
        return False


async def tg_delete_message(chat_id: int, message_id: int) -> bool:
    """Delete a message via Bot API."""
    try:
        client = await _get_http_client()
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[TTT] Error deleting message: {e}")
        return False


async def tg_answer_callback(
    callback_id: str,
    text: str = "",
    alert: bool = False,
) -> bool:
    """Answer a callback query."""
    try:
        client = await _get_http_client()
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": alert,
            },
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[TTT] Error answering callback: {e}")
        return False


# ─────────────────────────────────────────────────────────
# BOARD RENDERING & MARKUP
# ─────────────────────────────────────────────────────────

def build_board_markup(board: list[str], game_id: str, winning_line: Optional[list] = None) -> dict:
    """Build inline keyboard for board."""
    keyboard = []
    for row_idx in range(3):
        row = []
        for col_idx in range(3):
            idx = row_idx * 3 + col_idx
            cell = board[idx]
            
            if cell == " ":
                # Empty cell
                text = "⬜"
                callback = f"ttt_move_{game_id}_{idx}"
            else:
                # Filled cell (highlight if winning)
                if winning_line and idx in winning_line:
                    text = "🟥" if cell == "X" else "🟦"
                else:
                    text = "❌" if cell == "X" else "⭕"
                callback = f"ttt_noop_{idx}"
        
            row.append({"text": text, "callback_data": callback})
        keyboard.append(row)
    
    return {"inline_keyboard": keyboard}


def build_challenge_markup(challenge_id: str) -> dict:
    """Build challenge accept/decline buttons."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": f"ttt_accept_{challenge_id}"},
                {"text": "❌ Decline", "callback_data": f"ttt_decline_{challenge_id}"},
            ]
        ]
    }


def build_rematch_markup(game_id: str) -> dict:
    """Build rematch + leaderboard buttons."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔁 Rematch", "callback_data": f"ttt_rematch_{game_id}"},
                {"text": "📊 Leaderboard", "callback_data": "ttt_leaderboard"},
            ]
        ]
    }


# ─────────────────────────────────────────────────────────
# GAME MANAGEMENT
# ─────────────────────────────────────────────────────────

def init_games(
    save_fn: Optional[Callable] = None,
    load_fn: Optional[Callable] = None,
    scores_file: str = "data/ttt_scores.json",
    bot_token: str = "",
):
    """Initialize game module with persistence functions."""
    global BOT_TOKEN, SCORES_FILE, STATE_FILE, SAVE_FN, LOAD_FN
    BOT_TOKEN = bot_token
    SCORES_FILE = scores_file
    STATE_FILE = str(Path(scores_file).with_name("ttt_state.json"))
    SAVE_FN = save_fn
    LOAD_FN = load_fn
    _restore_state()
    print("[TTT] Game module initialized")


def create_game(
    player_x_id: int,
    player_x_name: str,
    player_o_id: int,
    player_o_name: str,
    chat_id: int,
) -> dict:
    """Create a new game state."""
    game_id = generate_game_id()
    game = {
        "game_id": game_id,
        "chat_id": chat_id,
        "board": [" "] * 9,
        "player_x": player_x_id,
        "player_o": player_o_id,
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "turn": "X",
        "status": "active",
        "winner": None,
        "winning_line": [],
        "board_msg_id": None,
        "created_at": time.time(),
        "last_move_at": time.time(),
        "move_count": 0,
    }
    GAMES[game_id] = game
    if _is_trackable_player(player_x_id):
        PLAYER_GAMES.setdefault(player_x_id, {})[chat_id] = game_id
    if _is_trackable_player(player_o_id):
        PLAYER_GAMES.setdefault(player_o_id, {})[chat_id] = game_id
    _persist_state()
    return game


def get_game_for_player(uid: int, chat_id: Optional[int] = None) -> Optional[dict]:
    """Get active game for player (optionally scoped to a chat)."""
    games_for_user = PLAYER_GAMES.get(uid)
    if not isinstance(games_for_user, dict) or not games_for_user:
        return None
    if chat_id is not None:
        game_id = games_for_user.get(chat_id)
        if not game_id:
            return None
        game = GAMES.get(game_id)
        if game and game.get("status") == "active":
            return game
        games_for_user.pop(chat_id, None)
        if not games_for_user:
            PLAYER_GAMES.pop(uid, None)
        _persist_state()
        return None
    for game_id in list(games_for_user.values()):
        game = GAMES.get(game_id)
        if game and game.get("status") == "active":
            return game
    return None


def _apply_player_name(scores: dict, uid: int, name: Optional[str]):
    """Keep leaderboard names synced with latest Telegram first/last names."""
    if not name:
        return
    clean_name = " ".join(str(name).split()).strip()
    if not clean_name:
        return
    key = str(uid)
    if key in scores:
        scores[key]["name"] = clean_name


def _ensure_score_entry(scores: dict, uid: int, name: Optional[str]):
    key = str(uid)
    if key not in scores:
        scores[key] = {"wins": 0, "losses": 0, "draws": 0, "uid": uid, "name": "Unknown"}
    _apply_player_name(scores, uid, name)


def record_game(
    winner_id: Optional[int],
    loser_id: Optional[int],
    is_draw: bool = False,
    winner_name: Optional[str] = None,
    loser_name: Optional[str] = None,
):
    """Update scores in leaderboard."""
    scores = load_scores()

    winner_trackable = _is_trackable_player(winner_id)
    loser_trackable = _is_trackable_player(loser_id)

    if is_draw and winner_id is not None and loser_id is not None:
        # Draw
        if winner_trackable:
            _ensure_score_entry(scores, winner_id, winner_name)
            scores[str(winner_id)]["draws"] += 1
        if loser_trackable and loser_id != winner_id:
            _ensure_score_entry(scores, loser_id, loser_name)
            scores[str(loser_id)]["draws"] += 1
    elif winner_id is not None and loser_id is not None:
        # Winner
        if winner_trackable:
            _ensure_score_entry(scores, winner_id, winner_name)
            scores[str(winner_id)]["wins"] += 1
        
        # Loser
        if loser_trackable:
            _ensure_score_entry(scores, loser_id, loser_name)
            scores[str(loser_id)]["losses"] += 1
    
    save_scores(scores)


def finish_game(game_id: str, winner: Optional[str] = None):
    """Mark game as finished and record scores."""
    if game_id not in GAMES:
        return
    
    game = GAMES[game_id]
    game["status"] = "finished"
    game["winner"] = winner
    
    if winner == "X":
        record_game(
            game["player_x"],
            game["player_o"],
            is_draw=False,
            winner_name=game.get("player_x_name"),
            loser_name=game.get("player_o_name"),
        )
    elif winner == "O":
        record_game(
            game["player_o"],
            game["player_x"],
            is_draw=False,
            winner_name=game.get("player_o_name"),
            loser_name=game.get("player_x_name"),
        )
    elif winner == "draw":
        record_game(
            game["player_x"],
            game["player_o"],
            is_draw=True,
            winner_name=game.get("player_x_name"),
            loser_name=game.get("player_o_name"),
        )

    # Auto-end: immediately free both players for new games when this one completes.
    _release_game_players(game)
    _persist_state()


async def maybe_play_bot_turn(game: dict, chat_id: int, board_message_id: int):
    """If it's bot turn, play one move and update board."""
    if game.get("status") != "active":
        return

    current_turn = game.get("turn")
    current_player = game.get("player_x") if current_turn == "X" else game.get("player_o")
    if current_player != BOT_PLAYER_ID:
        return

    bot_idx = _choose_bot_move(game["board"], current_turn)
    if bot_idx is None:
        return

    game["board"][bot_idx] = current_turn
    game["last_move_at"] = time.time()
    game["move_count"] += 1
    _persist_state()

    is_win, winner, winning_line = check_win(game["board"])
    is_draw = is_board_full(game["board"]) and not is_win

    if is_win:
        finish_game(game["game_id"], winner=winner)
        board_caption = render_board(
            game["board"],
            game["player_x_name"],
            game["player_o_name"],
            status="finished",
            winner=winner,
            winning_line=winning_line,
        )
        await tg_edit_message(
            chat_id,
            board_message_id,
            board_caption,
            markup=build_rematch_markup(game["game_id"]),
        )
        _release_game_players(game)
        _persist_state()
        return

    if is_draw:
        finish_game(game["game_id"], winner="draw")
        board_caption = render_board(
            game["board"],
            game["player_x_name"],
            game["player_o_name"],
            status="finished",
            winner="draw",
        )
        await tg_edit_message(
            chat_id,
            board_message_id,
            board_caption,
            markup=build_rematch_markup(game["game_id"]),
        )
        _release_game_players(game)
        _persist_state()
        return

    game["turn"] = "O" if current_turn == "X" else "X"
    _persist_state()
    board_caption = render_board(
        game["board"],
        game["player_x_name"],
        game["player_o_name"],
        status="active",
        turn=game["turn"],
        move_count=game["move_count"],
    )
    await tg_edit_message(
        chat_id,
        board_message_id,
        board_caption,
        markup=build_board_markup(game["board"], game["game_id"]),
    )


def cleanup_old_games():
    """Remove finished games older than FINISHED_GAME_CACHE."""
    now = time.time()
    expired = []
    for game_id, game in list(GAMES.items()):
        if (
            game["status"] == "finished"
            and now - game["created_at"] > FINISHED_GAME_CACHE
        ):
            expired.append(game_id)
    
    for game_id in expired:
        game = GAMES.pop(game_id, {})
        _release_game_players(game)
    if expired:
        _persist_state()


# ─────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────

async def handle_ttt_command(
    bot,
    msg: dict,
    args: list,
    reply: Optional[dict],
    uid: int,
    chat_id: int,
    msg_id: int,
):
    """Handle /ttt <@user or user_id> challenge."""
    try:
        chat_type = msg.get("chat", {}).get("type")
        challenger_info = msg.get("from", {})
        challenger_name = _name_from_user(challenger_info)
        
        # DM mode: play against bot
        if chat_type == "private":
            if get_game_for_player(uid, chat_id):
                await tg_send_message(
                    chat_id,
                    "❌ You already have an active game. Use /tttend first.",
                    reply_to=msg_id,
                )
                return

            game = create_game(uid, challenger_name, BOT_PLAYER_ID, BOT_PLAYER_NAME, chat_id)
            board_caption = render_board(
                game["board"],
                game["player_x_name"],
                game["player_o_name"],
                status="active",
                turn="X",
                move_count=0,
            )
            result = await tg_send_message(
                chat_id,
                board_caption,
                reply_to=msg_id,
                markup=build_board_markup(game["board"], game["game_id"]),
            )
            if result and result.get("ok"):
                game["board_msg_id"] = result["result"]["message_id"]
                _persist_state()

            # If future rematches swap sides and bot is X, allow immediate bot turn.
            if game["player_x"] == BOT_PLAYER_ID and game.get("board_msg_id"):
                await maybe_play_bot_turn(game, chat_id, game["board_msg_id"])
            return
        
        # Check: target provided by reply or argument
        if not args and not reply:
            await tg_send_message(
                chat_id,
                "❌ Reply to a user or provide @username/user_id.",
                reply_to=msg_id,
            )
            return
        
        # Resolve challenger
        challenger_id = uid
        
        # Resolve opponent (reply, @username/plain username, or user_id)
        opponent_id = None
        opponent_name = "Player"
        if reply:
            from_reply = reply.get("from", {})
            opponent_id = from_reply.get("id")
            opponent_name = _name_from_user(from_reply)
        elif args:
            arg = (args[0] or "").strip()
            lookup = arg[1:] if arg.startswith("@") else arg
            if not lookup:
                return await tg_send_message(
                    chat_id,
                    "❌ Invalid target. Use reply, @username, or user_id.",
                    reply_to=msg_id,
                )

            if lookup.isdigit():
                opponent_id = int(lookup)
                try:
                    target_user = await bot.get_users(opponent_id)
                    opponent_name = _name_from_user(
                        {
                            "first_name": getattr(target_user, "first_name", ""),
                            "last_name": getattr(target_user, "last_name", ""),
                            "username": getattr(target_user, "username", ""),
                        }
                    )
                except Exception:
                    opponent_name = str(opponent_id)
            else:
                try:
                    target_user = await bot.get_users(lookup)
                    opponent_id = getattr(target_user, "id", None)
                    opponent_name = _name_from_user(
                        {
                            "first_name": getattr(target_user, "first_name", ""),
                            "last_name": getattr(target_user, "last_name", ""),
                            "username": getattr(target_user, "username", ""),
                        }
                    )
                except Exception:
                    return await tg_send_message(
                        chat_id,
                        "❌ Could not find that username. Try reply or user_id.",
                        reply_to=msg_id,
                    )
        
        if not opponent_id:
            await tg_send_message(
                chat_id,
                "❌ Could not resolve opponent.",
                reply_to=msg_id,
            )
            return
        
        # Check: self-challenge
        if opponent_id == challenger_id:
            await tg_send_message(
                chat_id,
                "❌ You can't challenge yourself!",
                reply_to=msg_id,
            )
            return
        
        # Check: either player already in a game
        if get_game_for_player(challenger_id, chat_id) or get_game_for_player(opponent_id, chat_id):
            await tg_send_message(
                chat_id,
                "❌ One or both players are already in a game.",
                reply_to=msg_id,
            )
            return

        challenge_id = generate_challenge_id()
        
        # Send challenge
        challenge_text = (
            f"🎮 *{_escape_markdown(challenger_name)}* challenges *{_escape_markdown(opponent_name)}* to Tic-Tac-Toe!\n\n"
            "⏳ _60 seconds to respond_"
        )
        result = await tg_send_message(
            chat_id,
            challenge_text,
            reply_to=None,
            markup=build_challenge_markup(challenge_id),
        )
        
        if result and result.get("ok"):
            challenge_msg_id = result["result"]["message_id"]
            CHALLENGES[challenge_id] = {
                "challenger_id": challenger_id,
                "challenger_name": challenger_name,
                "opponent_id": opponent_id,
                "opponent_name": opponent_name,
                "chat_id": chat_id,
                "challenge_msg_id": challenge_msg_id,
                "created_at": time.time(),
            }
            _persist_state()
            
            # Auto-expire challenge
            async def expire_challenge():
                await asyncio.sleep(CHALLENGE_TIMEOUT)
                challenge = CHALLENGES.get(challenge_id)
                if challenge:
                    del CHALLENGES[challenge_id]
                    _persist_state()
                    try:
                        await tg_delete_message(chat_id, challenge.get("challenge_msg_id", challenge_msg_id))
                    except Exception:
                        pass
            
            asyncio.create_task(expire_challenge())
    
    except Exception as e:
        print(f"[TTT] Error in handle_ttt_command: {e}")


async def handle_ttt_leaderboard(chat_id: int, msg_id: Optional[int] = None):
    """Handle /tttleaderboard."""
    try:
        scores = load_scores()
        
        if not scores:
            await _send_markdown_with_fallback(
                chat_id,
                "📊 *Leaderboard*\n\nNo games recorded yet.",
                reply_to=msg_id,
            )
            return
        
        # Sort by wins descending
        player_rows = [row for row in scores.values() if isinstance(row, dict)]
        sorted_players = sorted(
            player_rows,
            key=lambda x: x.get("wins", 0),
            reverse=True,
        )[:10]
        
        lines = ["📊 *Top 10 Players*\n"]
        for idx, player in enumerate(sorted_players, start=1):
            wins = player.get("wins", 0)
            losses = player.get("losses", 0)
            draws = player.get("draws", 0)
            total = wins + losses + draws
            wr = (wins / total * 100) if total > 0 else 0
            name = _escape_markdown(player.get("name", "Unknown"))
            lines.append(
                f"{idx}. *{name}* — "
                f"W:{wins} L:{losses} D:{draws} ({wr:.0f}%)"
            )
        
        text = "\n".join(lines)
        await _send_markdown_with_fallback(chat_id, text, reply_to=msg_id)
    
    except Exception as e:
        print(f"[TTT] Error in handle_ttt_leaderboard: {e}")


async def handle_ttt_mystats(uid: int, chat_id: int, msg_id: int):
    """Handle /tttmystats."""
    try:
        scores = load_scores()
        player_score = scores.get(str(uid), {})
        
        if not player_score:
            await _send_markdown_with_fallback(
                chat_id,
                "📊 *Your Stats*\n\nNo games yet. Play one to see stats!",
                reply_to=msg_id,
            )
            return
        
        wins = player_score.get("wins", 0)
        losses = player_score.get("losses", 0)
        draws = player_score.get("draws", 0)
        total = wins + losses + draws
        wr = (wins / total * 100) if total > 0 else 0
        
        text = (
            f"📊 *Your Stats*\n\n"
            f"✅ Wins: `{wins}`\n"
            f"❌ Losses: `{losses}`\n"
            f"🤝 Draws: `{draws}`\n"
            f"🎯 Win Rate: `{wr:.1f}%`\n"
            f"🧮 Total: `{total}`"
        )
        await _send_markdown_with_fallback(chat_id, text, reply_to=msg_id)
    
    except Exception as e:
        print(f"[TTT] Error in handle_ttt_mystats: {e}")


async def handle_ttt_end(uid: int, chat_id: int, msg_id: int, is_owner: bool = False):
    """Handle /tttend (forfeit)."""
    try:
        game = get_game_for_player(uid, chat_id)
        if not game:
            await tg_send_message(
                chat_id,
                "❌ You're not in an active game.",
                reply_to=msg_id,
            )
            return
        
        # Determine winner
        opponent_id = game["player_o"] if game["player_x"] == uid else game["player_x"]
        opponent_name = game["player_o_name"] if game["player_x"] == uid else game["player_x_name"]
        
        finish_game(game["game_id"], winner="X" if game["player_x"] == opponent_id else "O")
        
        # Update board message
        board_caption = render_board(
            game["board"],
            game["player_x_name"],
            game["player_o_name"],
            status="finished",
            winner="X" if game["player_x"] == opponent_id else "O",
        )
        board_caption += f"\n\n🏳️ *{(game['player_x_name'] if uid == game['player_o'] else game['player_o_name'])} forfeited.*"
        
        if game["board_msg_id"]:
            await tg_edit_message(
                chat_id,
                game["board_msg_id"],
                board_caption,
                markup=build_rematch_markup(game["game_id"]),
            )
        
        # Cleanup
        _release_game_players(game)
        _persist_state()
        
        await tg_send_message(
            chat_id,
            f"🏳️ Forfeit recorded. *{opponent_name}* wins!",
            reply_to=msg_id,
        )
    
    except Exception as e:
        print(f"[TTT] Error in handle_ttt_end: {e}")


async def handle_ttt_callback(
    cb_id: str,
    data: str,
    uid: int,
    from_user: dict,
    chat_id: int,
    message: dict,
):
    """Handle all ttt_* callbacks."""
    try:
        # Parse callback data
        if data.startswith("ttt_accept_"):
            challenge_id = data.replace("ttt_accept_", "", 1)
            await handle_accept_challenge(cb_id, challenge_id, uid, from_user, chat_id, message)
        
        elif data.startswith("ttt_decline_"):
            challenge_id = data.replace("ttt_decline_", "", 1)
            await handle_decline_challenge(cb_id, challenge_id, uid, chat_id)
        
        elif data.startswith("ttt_move_"):
            parts = data.replace("ttt_move_", "").split("_")
            if len(parts) >= 2:
                game_id = "_".join(parts[:-1])
                idx = int(parts[-1])
                await handle_move(cb_id, game_id, idx, uid, chat_id, message)
        
        elif data.startswith("ttt_rematch_"):
            game_id = data.replace("ttt_rematch_", "")
            await handle_rematch(cb_id, game_id, uid, chat_id, message)
        
        elif data.startswith("ttt_noop_"):
            await tg_answer_callback(cb_id, "❌ Not your turn!", alert=False)
        
        elif data == "ttt_leaderboard":
            await tg_answer_callback(cb_id)
            await handle_ttt_leaderboard(chat_id, None)
    
    except Exception as e:
        print(f"[TTT] Error in handle_ttt_callback: {e}")


async def handle_accept_challenge(
    cb_id: str,
    challenge_id: str,
    uid: int,
    from_user: dict,
    chat_id: int,
    message: dict,
):
    """Accept challenge and start game."""
    try:
        challenge = CHALLENGES.get(challenge_id)
        if not challenge:
            await tg_answer_callback(cb_id, "⏳ Challenge expired.", alert=True)
            return

        if challenge.get("chat_id") != chat_id:
            await tg_answer_callback(cb_id, "❌ Invalid challenge context.", alert=True)
            return
        
        # Check: opponent accepting
        if uid != challenge["opponent_id"]:
            await tg_answer_callback(cb_id, "❌ Only opponent can accept.", alert=True)
            return

        # Re-check player availability to avoid race conditions.
        if get_game_for_player(challenge["challenger_id"], chat_id) or get_game_for_player(challenge["opponent_id"], chat_id):
            await tg_answer_callback(cb_id, "❌ One player is already in another game.", alert=True)
            return
        
        # Create game
        opponent_name = _name_from_user(from_user, challenge.get("opponent_name", "Player"))
        game = create_game(
            challenge["challenger_id"],
            challenge["challenger_name"],
            challenge["opponent_id"],
            opponent_name,
            chat_id,
        )
        
        # Delete challenge message
        await tg_delete_message(chat_id, challenge.get("challenge_msg_id", 0))
        del CHALLENGES[challenge_id]
        _persist_state()
        
        # Send board
        board_caption = render_board(
            game["board"],
            game["player_x_name"],
            game["player_o_name"],
            status="active",
            turn="X",
            move_count=0,
        )
        result = await tg_send_message(
            chat_id,
            board_caption,
            reply_to=None,
            markup=build_board_markup(game["board"], game["game_id"]),
        )
        
        if result and result.get("ok"):
            game["board_msg_id"] = result["result"]["message_id"]
            _persist_state()
        
        await tg_answer_callback(cb_id, "🎮 Game started!")
    
    except Exception as e:
        print(f"[TTT] Error in handle_accept_challenge: {e}")


async def handle_decline_challenge(cb_id: str, challenge_id: str, uid: int, chat_id: int):
    """Decline challenge."""
    try:
        challenge = CHALLENGES.get(challenge_id)
        if not challenge:
            await tg_answer_callback(cb_id, "⏳ Challenge expired.", alert=True)
            return

        if challenge.get("chat_id") != chat_id:
            await tg_answer_callback(cb_id, "❌ Invalid challenge context.", alert=True)
            return
        
        if uid != challenge["opponent_id"]:
            await tg_answer_callback(cb_id, "❌ Only opponent can decline.", alert=True)
            return
        
        await tg_delete_message(chat_id, challenge.get("challenge_msg_id", 0))
        del CHALLENGES[challenge_id]
        _persist_state()
        await tg_answer_callback(cb_id, "👋 Challenge declined.")
    
    except Exception as e:
        print(f"[TTT] Error in handle_decline_challenge: {e}")


async def handle_move(cb_id: str, game_id: str, idx: int, uid: int, chat_id: int, message: dict):
    """Handle board move."""
    try:
        if game_id not in GAMES:
            await tg_answer_callback(cb_id, "❌ Game not found.", alert=True)
            return
        
        game = GAMES[game_id]
        
        # Check: active game
        if game["status"] != "active":
            await tg_answer_callback(cb_id, "Game is finished.", alert=False)
            return
        
        # Check: correct player turn
        if game["turn"] == "X" and uid != game["player_x"]:
            await tg_answer_callback(cb_id, "❌ Not your turn!", alert=False)
            return
        if game["turn"] == "O" and uid != game["player_o"]:
            await tg_answer_callback(cb_id, "❌ Not your turn!", alert=False)
            return
        
        # Check: cell empty
        if game["board"][idx] != " ":
            await tg_answer_callback(cb_id, "❌ Cell taken.", alert=False)
            return
        
        # Make move
        game["board"][idx] = game["turn"]
        game["last_move_at"] = time.time()
        game["move_count"] += 1
        _persist_state()
        
        # Check win
        is_win, winner, winning_line = check_win(game["board"])
        is_draw = is_board_full(game["board"]) and not is_win
        
        if is_win:
            finish_game(game_id, winner=winner)
            board_caption = render_board(
                game["board"],
                game["player_x_name"],
                game["player_o_name"],
                status="finished",
                winner=winner,
                winning_line=winning_line,
            )
            await tg_edit_message(
                chat_id,
                message["message_id"],
                board_caption,
                markup=build_rematch_markup(game_id),
            )
            await tg_answer_callback(cb_id, f"🎉 {game['player_x_name'] if winner == 'X' else game['player_o_name']} wins!")
        
        elif is_draw:
            finish_game(game_id, winner="draw")
            board_caption = render_board(
                game["board"],
                game["player_x_name"],
                game["player_o_name"],
                status="finished",
                winner="draw",
            )
            await tg_edit_message(
                chat_id,
                message["message_id"],
                board_caption,
                markup=build_rematch_markup(game_id),
            )
            await tg_answer_callback(cb_id, "🤝 It's a draw!")
        
        else:
            # Switch turn
            game["turn"] = "O" if game["turn"] == "X" else "X"
            _persist_state()

            # Human move accepted; if bot turn now, let bot play immediately.
            await tg_answer_callback(cb_id, "✅ Move recorded!")
            next_player = game["player_x"] if game["turn"] == "X" else game["player_o"]
            if next_player == BOT_PLAYER_ID:
                await maybe_play_bot_turn(game, chat_id, message["message_id"])
                return

            board_caption = render_board(
                game["board"],
                game["player_x_name"],
                game["player_o_name"],
                status="active",
                turn=game["turn"],
                move_count=game["move_count"],
            )
            await tg_edit_message(
                chat_id,
                message["message_id"],
                board_caption,
                markup=build_board_markup(game["board"], game_id),
            )
    
    except Exception as e:
        print(f"[TTT] Error in handle_move: {e}")


async def handle_rematch(cb_id: str, game_id: str, uid: int, chat_id: int, message: dict):
    """Start a rematch."""
    try:
        if game_id not in GAMES:
            await tg_answer_callback(cb_id, "Game not found.", alert=True)
            return
        
        old_game = GAMES[game_id]
        if old_game["status"] != "finished":
            await tg_answer_callback(cb_id, "Game still active.", alert=True)
            return

        if uid not in (old_game.get("player_x"), old_game.get("player_o")):
            await tg_answer_callback(cb_id, "❌ Only players can start rematch.", alert=True)
            return
        
        # Ensure neither player is in another active game in this chat.
        if get_game_for_player(old_game.get("player_x"), chat_id) or get_game_for_player(old_game.get("player_o"), chat_id):
            await tg_answer_callback(cb_id, "❌ One player is already in another game.", alert=True)
            return

        # Create new game with swapped sides
        new_game = create_game(
            old_game["player_o"],  # Swapped
            old_game["player_o_name"],
            old_game["player_x"],  # Swapped
            old_game["player_x_name"],
            chat_id,
        )
        
        board_caption = render_board(
            new_game["board"],
            new_game["player_x_name"],
            new_game["player_o_name"],
            status="active",
            turn="X",
        )
        
        await tg_edit_message(
            chat_id,
            message["message_id"],
            board_caption,
            markup=build_board_markup(new_game["board"], new_game["game_id"]),
        )

        if new_game["player_x"] == BOT_PLAYER_ID:
            await maybe_play_bot_turn(new_game, chat_id, message["message_id"])

        await tg_answer_callback(cb_id, "🎮 Rematch started! Sides swapped.")
    
    except Exception as e:
        print(f"[TTT] Error in handle_rematch: {e}")


# ─────────────────────────────────────────────────────────
# TIMEOUT WORKER
# ─────────────────────────────────────────────────────────

async def games_cleanup_worker():
    """Periodically check for idle/expired games and challenges."""
    while True:
        try:
            now = time.time()
            
            # Check active games for timeouts
            for game_id, game in list(GAMES.items()):
                if game["status"] != "active":
                    continue
                
                # Forfeit if idle > 120s
                if now - game["last_move_at"] > MOVE_TIMEOUT:
                    # Determine loser (idle player)
                    idle_player = game["player_x"] if game.get("turn") == "X" else game["player_o"]
                    
                    # Record
                    if idle_player == game["player_x"]:
                        finish_game(game_id, winner="O")
                    else:
                        finish_game(game_id, winner="X")
                    
                    # Update message
                    board_caption = render_board(
                        game["board"],
                        game["player_x_name"],
                        game["player_o_name"],
                        status="finished",
                        winner="O" if idle_player == game["player_x"] else "X",
                    ) + "\n\n⏱️ *Timeout — idle player forfeited.*"
                    
                    if game["board_msg_id"]:
                        await tg_edit_message(
                            game["chat_id"],
                            game["board_msg_id"],
                            board_caption,
                            markup=build_rematch_markup(game_id),
                        )
                    
                    # Cleanup
                    _release_game_players(game)
                    _persist_state()
            
            # Cleanup old finished games and expired challenges
            cleanup_old_games()
            
            expired_challenges = [
                msg_id for msg_id, ch in CHALLENGES.items()
                if now - ch["created_at"] > CHALLENGE_TIMEOUT
            ]
            for msg_id in expired_challenges:
                del CHALLENGES[msg_id]
            if expired_challenges:
                _persist_state()
            
            await asyncio.sleep(CLEANUP_INTERVAL)
        
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[TTT] Error in games_cleanup_worker: {e}")
            await asyncio.sleep(CLEANUP_INTERVAL)


# ─────────────────────────────────────────────────────────
# INTEGRATION COMMENT
# ─────────────────────────────────────────────────────────

"""
INTEGRATION INTO main.py (index.py)
═══════════════════════════════════════════════════════════

1. Import at top of index.py:
   from games import (
       init_games,
       handle_ttt_command,
       handle_ttt_leaderboard,
       handle_ttt_mystats,
       handle_ttt_end,
       handle_ttt_callback,
       games_cleanup_worker,
       TTT_CALLBACK_PREFIXES,
   )

2. In startup_event(), after other initialization:
   init_games(
       save_fn=save,        # from db.py / your save function
       load_fn=load,        # from db.py / your load function
       scores_file=TTT_SCORES_FILE,
       bot_token=BOT_TOKEN,
   )
   # Start cleanup worker
   asyncio.create_task(games_cleanup_worker())

3. In handle_message(), add command handlers:
   if raw_cmd in ("ttt", "ttt_game"):
       args = text.split()[1:] if len(text.split()) > 1 else []
       await handle_ttt_command(bot, msg, args, reply, uid, chat_id, msg_id)
   elif raw_cmd == "tttleaderboard":
       await handle_ttt_leaderboard(chat_id, msg_id)
   elif raw_cmd == "tttmystats":
       await handle_ttt_mystats(uid, chat_id, msg_id)
   elif raw_cmd == "tttend":
       await handle_ttt_end(uid, chat_id, msg_id, is_owner=is_owner)

4. In handle_callback(), add:
   for prefix in TTT_CALLBACK_PREFIXES:
       if cb_data.startswith(prefix):
           await handle_ttt_callback(cb_id, cb_data, uid, from_user, chat_id, message)
           return

5. Add /ttt commands to BOT_COMMANDS:
   {"command": "ttt", "description": "🎮 Challenge someone to Tic-Tac-Toe"},
   {"command": "tttleaderboard", "description": "📊 View top players"},
   {"command": "tttmystats", "description": "📈 Your Tic-Tac-Toe stats"},
   {"command": "tttend", "description": "🏳️ Forfeit current game"},

═══════════════════════════════════════════════════════════
"""

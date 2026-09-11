#!/usr/bin/env python3
# telegram_hosting_panel.py
# Telegram Hosting Panel — complete implementation of bot.txt specification.
# Single-file production bot using python-telegram-bot.

import asyncio
import ast
import base64
import datetime
import hashlib
import importlib.util
import json
import logging
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import aiosqlite
import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# =====================================================================
# CONFIG
# =====================================================================

BASE_DIR = Path(__file__).resolve().parent

_ENC_TOKEN = "gAAAAABqlObC5p8ZFOGDRCuz9-YqO7JI7cuqqMGIL7_v9eD4DjxA_9n-SzmOuQTk0hbNRNOl00hXS08ffoJPDaPL1ZXpWzH7BuQ-HRvFF2K3jzo9umOjAkIsjZ5UdVvmp22GFYx6g60O"


def _decrypt_token() -> str:
    # FF to plaintext `HOSTING_BOT_TOKEN` first; otherwise decrypt the embedded
    # Fernet ciphertext with the key from HOSTING_TOKEN_KEY (never committed).
    direct = (os.environ.get("HOSTING_BOT_TOKEN") or "").strip()
    if direct:
        return direct
    key = (os.environ.get("HOSTING_TOKEN_KEY") or "").strip()
    if not key or not _ENC_TOKEN:
        return ""
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode()).decrypt(_ENC_TOKEN.encode()).decode().strip()
    except Exception:
        return ""


TOKEN = _decrypt_token()
OWNER_ID = int(os.environ.get("HOSTING_OWNER_ID", "8799679469") or "8799679469")
SUB_LINK = os.environ.get("HOSTING_SUB_LINK", "https://t.me/Duifioookn2").strip()
PAID_TEXT = (
    "🤖 VIP VPS Panel Hosting\n\n"
    "🚫 This bot is a PAID SERVICE.\n"
    "❌ Access Denied — Dashboard is locked.\n\n"
    "💎 Subscription Benefits\n"
    "• 📂 Multi-File Management — upload, replace, rename, delete & download multiple files\n"
    "• 📦 One-tap Deploy — single .py, ZIP or import from old hosting\n"
    "• 🕐 24/7 Online Hosting on a VPS\n"
    "• 🔄 Auto Restart & Crash Recovery\n"
    "• 💾 Backups + version restore\n"
    "• 🌐 Env Vars, Scheduler, Logs & more\n\n"
    "👉 To get access, message the owner for subscription."
)

DATA_DIR = Path(os.environ.get("HOSTING_DATA_DIR", str(BASE_DIR / "hosting_data"))).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
HOST_LOGS_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "hosting.db"

LOG_FILE = HOST_LOGS_DIR / "hosting.log"

NAME_MAX = 30
NAME_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-\.]{0,29}$")
ILLEGAL_CHARS = r'/\\:*?"<>|'
MAX_UPLOAD_BYTES = 48 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 48 * 1024 * 1024
FS_CHUNK_SIZE = 2 * 1024 * 1024  # per-chunk BLOB size for the TiDB file mirror
MAX_ZIP_ENTRIES = 4000
MAX_ZIP_TOTAL_BYTES = 128 * 1024 * 1024
FILES_PER_PAGE = 8
PROJECTS_PER_PAGE = 7
BACKUPS_PER_PAGE = 6
NOTIFS_PER_PAGE = 8
LIST_PER_PAGE = 10

LOG_TAIL_LINES = 40
LOG_LINE_LIMIT = 3000

LIVE_UPDATE_INTERVAL = 10
LIVE_MIN_GAP = 9
MONITOR_INTERVAL = 15
SCHEDULE_INTERVAL = 30
STORAGE_WARN_PCT = 90
STORAGE_CRIT_PCT = 95

STATUS_ICON = {
    "running": "🟢 Running",
    "starting": "🟡 Starting",
    "restarting": "🟠 Restarting",
    "stopped": "🔴 Stopped",
    "error": "⚫ Error",
}

LANG_BY_EXT = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".json": "JSON",
    ".txt": "Text",
    ".md": "Markdown",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".sh": "Shell",
    ".sql": "SQL",
    ".cfg": "Config",
    ".ini": "Config",
    ".env": "Env",
    ".csv": "CSV",
    ".log": "Log",
    ".db": "Database",
    ".sqlite": "Database",
    ".sqlite3": "Database",
    ".lock": "Lock",
    ".toml": "TOML",
    ".c": "C",
    ".cpp": "C++",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".php": "PHP",
    ".rb": "Ruby",
    ".png": "Image",
    ".jpg": "Image",
    ".jpeg": "Image",
    ".gif": "Image",
    ".webp": "Image",
    ".ico": "Image",
    ".svg": "Image",
    ".mp3": "Audio",
    ".mp4": "Video",
    ".zip": "Archive",
    ".tar": "Archive",
    ".gz": "Archive",
    ".7z": "Archive",
}

# =====================================================================
# LOGGING
# =====================================================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hosting_panel")
logger.setLevel(logging.INFO)

try:
    HOST_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
except Exception:
    pass


def log_host(message: str):
    logger.info("HOST: %s", message)
    try:
        if _loop and _loop.is_running():
            asyncio.run_coroutine_threadsafe(db_system_log(message), _loop)
    except Exception:
        pass


# =====================================================================
# TIME / FORMAT UTILITIES
# =====================================================================

def now() -> float:
    return time.time()


def fmt_dt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def fmt_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d %b %Y")


def day_str(ts: float = None) -> str:
    d = datetime.datetime.fromtimestamp(ts or time.time())
    return d.strftime("%Y-%m-%d")


def relative_time(ts: float) -> str:
    if not ts:
        return "Never"
    delta = time.time() - ts
    if delta < 5:
        return "Just Now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 604800:
        return f"{int(delta // 86400)}d ago"
    if delta < 31536000:
        return f"{int(delta // 604800)}w ago"
    return f"{delta / 31536000:.1f}y ago"


def human_size(num: float) -> str:
    if num is None:
        return "N/A"
    num = float(num)
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024:
            if num >= 100:
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
    return f"{num:.1f} PB"


def human_duration(seconds: float) -> str:
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        return f"{h}h {rem // 60:02d}m"
    if seconds < 604800:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    return f"{seconds // 604800}w {seconds % 604800 // 86400}d"


def duration_hhmmss(seconds: float) -> str:
    if seconds is None:
        return "00:00:00"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[\W_]+", "_", name.strip()).strip("_")
    return cleaned[:60] or "project"


def sanitize_filename(name: str) -> str:
    name = name.strip()
    for ch in ILLEGAL_CHARS + "\x00":
        name = name.replace(ch, "_")
    if name in (".", ".."):
        return "_"
    return name[:120] or "_"


def paginate(items: list, page: int, per_page: int):
    total_pages = max(1, math.ceil(len(items) / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return items[start : start + per_page], page, total_pages


# =====================================================================
# EVENT LOOP REFERENCE (set in main)
# =====================================================================

_loop = None
_application = None


def app():
    return _application


# =====================================================================
# CALLBACK TOKEN REGISTRY
# =====================================================================

class CbTokens:
    def __init__(self):
        self.map = {}
        self.rev = {}

    def _gen(self) -> str:
        while True:
            tok = base64.urlsafe_b64encode(os.urandom(5)).decode().rstrip("=")
            if tok not in self.map:
                return tok

    def put(self, handler: str, params: dict) -> str:
        key = (handler, json.dumps(params, sort_keys=True, default=str))
        if key in self.rev:
            return self.rev[key]
        tok = self._gen()
        self.map[tok] = (key, time.time())
        self.rev[key] = tok
        return tok

    def get(self, tok: str):
        entry = self.map.get(tok)
        if not entry:
            return None, {}
        key, _ts = entry
        handler, params_json = key
        try:
            params = json.loads(params_json)
        except Exception:
            return None, {}
        return handler, params

    def cleanup(self, max_age: float = 86400):
        cutoff = time.time() - max_age
        stale = [t for t, (k, ts) in self.map.items() if ts < cutoff]
        for t in stale:
            entry = self.map.pop(t, None)
            if entry:
                self.rev.pop(entry[0], None)


CB = CbTokens()


def cb(handler: str, **params) -> str:
    return CB.put(handler, params)


# =====================================================================
# SCREEN INFRASTRUCTURE
# =====================================================================

LIVE_SCREENS = {
    "home",
    "my_projects",
    "project_dash",
    "project_info",
    "deploy_history",
    "recovery",
    "files",
    "file_info",
    "logs",
    "resources",
    "resource_monitor",
    "backups_menu",
    "backup_list",
    "env_vars",
    "requirements",
    "notifications",
    "system_dash",
    "live_resources",
    "storage_info",
    "statistics",
    "system_info",
    "usage_history",
    "system_logs",
    "activity",
    "health",
    "distribution",
    "global_backups",
    "settings_menu",
    "deploy_done",
}


class Screen:
    def __init__(self, name: str, params: dict = None):
        self.name = name
        self.params = params or {}

    def token(self) -> str:
        return cb("nav", screen=self.name, params=self.params)


def go(name: str, **params) -> str:
    return Screen(name, params).token()


class OpenScreen:
    def __init__(self):
        self.entries = {}

    def set(self, user_id, screen, params, chat_id, message_id, text, markup):
        self.entries[user_id] = {
            "screen": screen,
            "params": params,
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "markup": markup,
            "last_edit": 0,
        }

    def get(self, user_id):
        return self.entries.get(user_id)

    def pop(self, user_id, default=None):
        return self.entries.pop(user_id, default)


OPEN_SCREENS = OpenScreen()


# =====================================================================
# HELPERS: render + send/edit
# =====================================================================

def build_markup(rows) -> InlineKeyboardMarkup:
    from telegram import InlineKeyboardButton
    keyboard = []
    for row in rows:
        btn_row = []
        for btn in row:
            if btn.get("url"):
                btn_row.append(InlineKeyboardButton(btn["label"], url=btn["url"]))
            else:
                btn_row.append(InlineKeyboardButton(btn["label"], callback_data=btn["cb"]))
        keyboard.append(btn_row)
    return InlineKeyboardMarkup(keyboard)


def BU(label, url):
    return {"label": label, "url": url}


def paid_screen():
    return PAID_TEXT, [[BU("💬 DM for Subscription", SUB_LINK)]]


async def show_paid_if_not_owner(context, update):
    user = update.effective_user
    if await require(user.id, "admin"):
        return True
    chat = update.effective_chat.id
    try:
        q = update.callback_query
        if q is not None:
            await q.answer("⛔ This bot is a paid service.", show_alert=False)
            await q.edit_message_text(text=PAID_TEXT, reply_markup=build_markup([[BU("💬 DM for Subscription", SUB_LINK)]]))
            return False
    except TelegramError:
        pass
    text, rows = paid_screen()
    await _deliver(context, chat, text, build_markup(rows))
    return False


async def render(name: str, params: dict):
    fn = SCREEN_RENDERERS.get(name)
    if fn is None:
        return "⚠ Unknown screen.", []
    return await fn(params)


def B(label, handler, **params):
    return {"label": label, "cb": cb(handler, **params)}


async def _deliver(context, chat_id, text, markup, edit_message_id=None):
    if edit_message_id:
        try:
            return await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=edit_message_id,
                text=text,
                reply_markup=markup,
            )
        except Forbidden:
            raise
        except TelegramError:
            return None
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except Forbidden:
        raise
    except TelegramError:
        return None


async def show_screen(context, user_id, chat_id, screen, params=None, edit_message_id=None, register=True):
    text, rows = await render(screen, params or {})
    markup = build_markup(rows)
    sent = await _deliver(context, chat_id, text, markup, edit_message_id)
    if sent:
        if register and screen in LIVE_SCREENS:
            OPEN_SCREENS.set(
                user_id,
                screen,
                params or {},
                chat_id,
                sent.message_id,
                text,
                text_of_markup(markup),
            )
        else:
            OPEN_SCREENS.pop(user_id)
    return sent


def text_of_markup(markup) -> str:
    if not markup or not markup.inline_keyboard:
        return ""
    parts = []
    for row in markup.inline_keyboard:
        parts.append("|".join(b.callback_data or "" for b in row))
    return "\n".join(parts)


async def toast(context, chat_id, text):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except TelegramError:
        pass


# =====================================================================
# DB LAYER
# =====================================================================

_db_lock = asyncio.Lock()
_db = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    main_file TEXT NOT NULL,
    python TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped',
    auto_restart INTEGER NOT NULL DEFAULT 0,
    req_install INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_deploy REAL,
    last_backup REAL,
    last_restart REAL,
    started_at REAL,
    pid INTEGER,
    error TEXT,
    deploy_count INTEGER NOT NULL DEFAULT 0,
    req_hash TEXT,
    req_status TEXT,
    req_error TEXT,
    crash_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS env_vars (
    project_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    name TEXT,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    day TEXT NOT NULL,
    time TEXT NOT NULL,
    project_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT
);
CREATE TABLE IF NOT EXISTS deploy_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    type TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS day_metrics (
    day TEXT PRIMARY KEY,
    deploys INTEGER NOT NULL DEFAULT 0,
    restarts INTEGER NOT NULL DEFAULT 0,
    backups INTEGER NOT NULL DEFAULT 0,
    replacements INTEGER NOT NULL DEFAULT 0,
    cpu_peak REAL NOT NULL DEFAULT 0,
    ram_peak INTEGER NOT NULL DEFAULT 0,
    storage_initial INTEGER NOT NULL DEFAULT 0
);
"""


async def db_init():
    global _db
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    HOST_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(str(DB_PATH))
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _migrate_db()
    await _db.commit()
    await _ensure_owner_user()
    removed = await asyncio.to_thread(sweep_tmp)
    if removed:
        log_host(f"TMP cleanup removed {removed} stale item(s)")
    return _db


async def _migrate_db():
    """Additive column migration for existing databases."""
    additions = {
        "projects": {
            "req_hash": "TEXT",
            "req_status": "TEXT",
            "req_error": "TEXT",
            "crash_count": "INTEGER NOT NULL DEFAULT 0",
        }
    }
    for table, cols in additions.items():
        for col, ddl in cols.items():
            try:
                cur = await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                await _close_cur(cur)
            except Exception:
                pass


async def _close_cur(cur):
    """Close a cursor (aiosqlite or TiDB pool cursor). Releasing a TiDB pool
    cursor returns its connection to the pool; aiosqlite cursors are closed
    after fetching. Safe to call on any cursor-like object."""
    if cur is None:
        return
    closer = getattr(cur, "close_real", None) or getattr(cur, "close", None)
    if closer is None:
        return
    try:
        res = closer()
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        pass


async def db_exec(sql, params=()):
    cur = await _db.execute(sql, params)
    try:
        await _db.commit()
    finally:
        await _close_cur(cur)
    return cur


async def db_fetchall(sql, params=()):
    cur = await _db.execute(sql, params)
    try:
        return await cur.fetchall()
    finally:
        await _close_cur(cur)


async def db_fetchone(sql, params=()):
    cur = await _db.execute(sql, params)
    try:
        return await cur.fetchone()
    finally:
        await _close_cur(cur)


async def db_system_log(message: str):
    try:
        await db_exec("INSERT INTO system_logs (message, created_at) VALUES (?, ?)", (message, now()))
    except Exception:
        pass


def _db_row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


# =====================================================================
# META / COUNTERS / METRICS
# =====================================================================

async def meta_get(key, default=None):
    row = await db_fetchone("SELECT value FROM meta WHERE key=?", (key,))
    return row[0] if row else default


async def meta_set(key, value):
    await db_exec(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


async def counter_incr(key, by=1):
    await db_exec(
        "INSERT INTO counters (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
        (key, by),
    )


async def counter_get(key, default=0):
    row = await db_fetchone("SELECT value FROM counters WHERE key=?", (key,))
    return row[0] if row else default


async def metrics_today() -> dict:
    d = day_str()
    row = await db_fetchone("SELECT * FROM day_metrics WHERE day=?", (d,))
    if row is None:
        return {"day": d, "deploys": 0, "restarts": 0, "backups": 0, "replacements": 0,
                "cpu_peak": 0.0, "ram_peak": 0, "storage_initial": 0}
    return dict(row)


async def metrics_incr(day, field, by=1):
    d = day_str()
    await db_exec(
        f"INSERT INTO day_metrics (day, {field}) VALUES (?, ?) "
        f"ON CONFLICT(day) DO UPDATE SET {field}={field}+excluded.{field}",
        (d, by),
    )


async def metrics_peak(day, field, value):
    d = day_str()
    await db_exec(
        f"INSERT INTO day_metrics (day, {field}) VALUES (?, ?) "
        f"ON CONFLICT(day) DO UPDATE SET {field}=MAX({field}, excluded.{field})",
        (d, value),
    )


async def storage_initial_set(day, value):
    d = day_str()
    await db_exec(
        "INSERT INTO day_metrics (day, storage_initial) VALUES (?, ?) "
        "ON CONFLICT(day) DO UPDATE SET storage_initial=excluded.storage_initial",
        (d, value),
    )


# =====================================================================
# USERS / PERMISSIONS
# =====================================================================

ROLE_RANK = {"viewer": 1, "admin": 2, "owner": 3}
MIN_ROLE = {"viewer": 1, "admin": 2, "owner": 3}


async def _ensure_owner_user():
    if OWNER_ID:
        await db_exec(
            "INSERT OR IGNORE INTO users (telegram_id, name, role) VALUES (?, ?, ?)",
            (OWNER_ID, "Owner", "owner"),
        )


_ROLE_CACHE = {}       # telegram_id -> (role, ts)
_ROLE_CACHE_TTL = 300  # seconds
_OWNER_NAME_CACHE = {}  # telegram_id -> last name we persisted


def _role_cache_get(tid: int):
    hit = _ROLE_CACHE.get(tid)
    if hit and time.time() - hit[1] < _ROLE_CACHE_TTL:
        return hit[0]
    return None


def role_cache_clear(tid: int = None):
    if tid is None:
        _ROLE_CACHE.clear()
    else:
        _ROLE_CACHE.pop(tid, None)


async def register_user(telegram_id: int, name: str):
    if OWNER_ID and telegram_id == OWNER_ID:
        if name and _OWNER_NAME_CACHE.get(telegram_id) != name:
            _OWNER_NAME_CACHE[telegram_id] = name
            await db_exec("UPDATE users SET name=? WHERE telegram_id=?", (name, telegram_id))
        return
    if _role_cache_get(telegram_id) is not None:
        return
    row = await db_fetchone("SELECT role FROM users WHERE telegram_id=?", (telegram_id,))
    if row is not None:
        _ROLE_CACHE[telegram_id] = (row[0], time.time())
        return
    role = "viewer"
    await db_exec("INSERT INTO users (telegram_id, name, role) VALUES (?, ?, ?)", (telegram_id, name or str(telegram_id), role))
    _ROLE_CACHE[telegram_id] = (role, time.time())


async def role_of(telegram_id: int) -> str:
    if OWNER_ID and telegram_id == OWNER_ID:
        return "owner"
    cached = _role_cache_get(telegram_id)
    if cached is not None:
        return cached
    row = await db_fetchone("SELECT role FROM users WHERE telegram_id=?", (telegram_id,))
    if row is None:
        role = "viewer"
    else:
        role = row[0]
    _ROLE_CACHE[telegram_id] = (role, time.time())
    return role


async def require(telegram_id: int, role: str) -> bool:
    user_role = await role_of(telegram_id)
    return ROLE_RANK.get(user_role, 0) >= ROLE_RANK.get(role, 0)


async def is_owner(telegram_id: int) -> bool:
    return await require(telegram_id, "owner")


async def list_users() -> list:
    return [dict(r) for r in await db_fetchall("SELECT telegram_id, name, role FROM users ORDER BY role DESC, telegram_id")]


# =====================================================================
# STORAGE / DISK HELPERS
# =====================================================================

def dir_size(path: str) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


async def adir_size(path: str) -> int:
    """Non-blocking wrapper for dir_size (offloads disk walk to a thread)."""
    return await asyncio.to_thread(dir_size, path)


def disk_usage_of(path: str):
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


def list_files_in(path: str) -> list:
    if not os.path.isdir(path):
        return []
    files = []
    for entry in os.scandir(path):
        try:
            if entry.is_file():
                files.append(entry.name)
        except OSError:
            continue
    return sorted(files, key=lambda x: x.lower())


def count_lines(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def file_modified(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


# =====================================================================
# PYTHON VERSIONS
# =====================================================================

_PY_CACHE = {"ts": 0, "data": []}


def available_python_versions() -> list:
    now_ts = time.time()
    if now_ts - _PY_CACHE["ts"] < 30 and _PY_CACHE["data"]:
        return _PY_CACHE["data"]
    versions = []
    candidates = [("python3", None)]
    for i in range(14):
        candidates.append((f"python3.{i}", f"3.{i}"))
    seen = set()
    for exe, ver in candidates:
        p = shutil.which(exe)
        if p and ver not in seen:
            seen.add(ver)
            try:
                out = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
                label = (out.stdout or out.stderr or "").strip()
                m = re.search(r"(\d+\.\d+)", label)
                if m:
                    ver = m.group(1)
            except Exception:
                pass
            versions.append((ver or "3", p))
    if not versions:
        versions = [("3", sys.executable)]
    _PY_CACHE["ts"] = time.time()
    _PY_CACHE["data"] = versions
    return versions


async def resolve_python(requested: str) -> str:
    avail = available_python_versions()
    if not avail:
        return sys.executable
    if requested:
        for ver, exe in avail:
            if ver == requested:
                return exe
        if "." not in requested:
            for ver, exe in avail:
                if ver.startswith(requested + "."):
                    return exe
    return avail[0][1]


# =====================================================================
# PROJECT STORE
# =====================================================================

def project_dir(pid: int) -> Path:
    return PROJECTS_DIR / f"p{pid}"


def project_files_dir(pid: int) -> Path:
    return project_dir(pid)


def project_logs_dir(pid: int) -> Path:
    return project_dir(pid) / "host_logs"


def project_backups_dir(pid: int) -> Path:
    return project_dir(pid) / "backups"


def project_versions_dir(pid: int) -> Path:
    return project_dir(pid) / "._versions"


# =====================================================================
# TIDB FILE STORE (persistent project files)
# =====================================================================
#
# HF free-tier /data is EPHEMERAL: it is wiped whenever the Space restarts or
# is stopped (48h idle auto-sleep, redeploys, infra moves). Project rows live
# in TiDB (persistent) but project FILES used to live only on /data, so after a
# restart every project hit "Main File Not Found" with an empty Files section.
# This layer mirrors every project's source files into TiDB (chunked BLOBs) at
# deploy/upload/replace/rename/delete time and re-materializes them onto the
# local disk at startup (or on demand right before a project starts). It goes
# through the same db_exec/db_fetchall path as everything else, so the
# dual-account TiDB failover applies automatically. When TiDB is inactive
# (local SQLite fallback) every function here is a no-op and files simply stay
# on the local disk.


def fs_enabled() -> bool:
    """True when the TiDB failover layer is active (project files should be
    mirrored there). Always False on the local-SQLite fallback."""
    try:
        import tidb_aiosqlite
        return getattr(tidb_aiosqlite, "_manager", None) is not None
    except Exception:
        return False


def _fs_rel(relpath: str) -> str:
    """Normalize a stored file path and reject anything that escapes the
    project root (".", "..", absolute paths). Returns "" when unsafe."""
    rel = str(relpath).replace("\\", "/").lstrip("/")
    if not rel or rel in (".", ".."):
        return ""
    try:
        if ".." in Path(rel).parts:
            return ""
    except Exception:
        return ""
    return rel


async def fs_save_file(pid: int, relpath: str, data: bytes):
    """Mirror one project file into TiDB as a chunked BLOB (replaces old copy)."""
    rel = _fs_rel(relpath)
    if not rel or not fs_enabled():
        return
    try:
        await db_exec("DELETE FROM project_files WHERE project_id=? AND path=?", (pid, rel))
    except Exception as exc:
        logger.warning("fs_save_file delete failed %s: %s", rel, exc)
        return
    if not data:
        return
    ts = now()
    size = len(data)
    chunks = [data[i:i + FS_CHUNK_SIZE] for i in range(0, size, FS_CHUNK_SIZE)]
    try:
        if len(chunks) == 1:
            await db_exec(
                "INSERT INTO project_files (project_id, path, chunk_idx, data, file_size, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pid, rel, 0, chunks[0], size, ts),
            )
            return
        placeholders = ",".join(["(?, ?, ?, ?, ?, ?)"] * len(chunks))
        params = []
        for idx, chunk in enumerate(chunks):
            params += [pid, rel, idx, chunk, size, ts]
        await db_exec(
            "INSERT INTO project_files (project_id, path, chunk_idx, data, file_size, updated_at) "
            "VALUES " + placeholders,
            tuple(params),
        )
    except Exception as exc:
        logger.warning("fs_save_file failed %s: %s", rel, exc)


async def fs_load_file(pid: int, relpath: str) -> bytes:
    """Read a project file back from TiDB (None when absent)."""
    rel = _fs_rel(relpath)
    if not rel or not fs_enabled():
        return None
    try:
        rows = await db_fetchall(
            "SELECT chunk_idx, data FROM project_files WHERE project_id=? AND path=? ORDER BY chunk_idx",
            (pid, rel),
        )
    except Exception as exc:
        logger.warning("fs_load_file failed %s: %s", rel, exc)
        return None
    if not rows:
        return None
    return b"".join(row["data"] for row in rows)


async def fs_delete_file(pid: int, relpath: str):
    rel = _fs_rel(relpath)
    if not rel or not fs_enabled():
        return
    try:
        await db_exec("DELETE FROM project_files WHERE project_id=? AND path=?", (pid, rel))
    except Exception as exc:
        logger.warning("fs_delete_file failed %s: %s", rel, exc)


async def fs_rename_file(pid: int, old: str, new: str):
    old_rel, new_rel = _fs_rel(old), _fs_rel(new)
    if not old_rel or not new_rel or not fs_enabled():
        return
    try:
        await db_exec("UPDATE project_files SET path=? WHERE project_id=? AND path=?",
                      (new_rel, pid, old_rel))
    except Exception as exc:
        logger.warning("fs_rename_file failed %s -> %s: %s", old_rel, new_rel, exc)


async def fs_delete_all(pid: int):
    if not fs_enabled():
        return
    try:
        await db_exec("DELETE FROM project_files WHERE project_id=?", (pid,))
    except Exception as exc:
        logger.warning("fs_delete_all failed %s: %s", pid, exc)


async def fs_project_file_paths(pid: int) -> list:
    if not fs_enabled():
        return []
    try:
        rows = await db_fetchall("SELECT DISTINCT path FROM project_files WHERE project_id=?", (pid,))
    except Exception as exc:
        logger.warning("fs_project_file_paths failed %s: %s", pid, exc)
        return []
    return [row["path"] for row in rows]


async def fs_save_project(pid: int, pdir: Path = None) -> int:
    """Mirror every source file of a project into TiDB (deploy / backup-restore
    snapshot). Runtime-only dirs (logs, backups, versions, caches) are skipped."""
    if not fs_enabled():
        return 0
    pdir = pdir or project_dir(pid)
    if not pdir.exists():
        return 0
    try:
        await db_exec("DELETE FROM project_files WHERE project_id=?", (pid,))
    except Exception as exc:
        logger.warning("fs_save_project clear failed %s: %s", pid, exc)
        return 0
    n = 0
    for root, dirs, files in os.walk(str(pdir)):
        dirs[:] = [d for d in dirs if d not in BACKUP_EXCLUDE_DIRS]
        for fname in files:
            fp = os.path.join(root, fname)
            rel = os.path.relpath(fp, str(pdir)).replace(os.sep, "/")
            try:
                with open(fp, "rb") as fh:
                    await fs_save_file(pid, rel, fh.read())
                n += 1
            except OSError:
                continue
    return n


async def fs_restore_project(pid: int, pdir: Path = None) -> int:
    """Recreate a project's files on disk from TiDB (used when /data was wiped)."""
    if not fs_enabled():
        return 0
    pdir = pdir or project_dir(pid)
    paths = await fs_project_file_paths(pid)
    if not paths:
        return 0
    try:
        pdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    n = 0
    for rel in paths:
        data = await fs_load_file(pid, rel)
        if data is None:
            continue
        try:
            dest = _resolve_within(pdir, rel)
        except ValueError:
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            n += 1
        except OSError:
            continue
    return n


async def fs_restore_all_missing() -> int:
    """At startup: for every project, make the on-disk folder and the TiDB
    mirror consistent:
      - files in TiDB but not on disk  -> materialize onto disk (restore)
      - files on disk but not in TiDB  -> backfill into TiDB (adopt existing
        projects created before this feature, so their files survive the next
        /data wipe)
    Returns the number of projects synced."""
    if not fs_enabled():
        return 0
    try:
        rows = await db_fetchall("SELECT id FROM projects")
    except Exception as exc:
        logger.warning("fs_restore_all_missing list failed: %s", exc)
        return 0
    synced = 0
    for row in rows:
        pid = row["id"]
        pdir = project_dir(pid)
        try:
            has_disk = pdir.exists() and any(p.is_file() for p in pdir.iterdir())
        except OSError:
            has_disk = False
        paths = await fs_project_file_paths(pid)
        try:
            if paths and not has_disk:
                n = await fs_restore_project(pid, pdir)
                if n:
                    synced += 1
                    log_host(f"TiDB file restore: project #{pid} files materialized ({n} file(s))")
            elif not paths and has_disk:
                n = await fs_save_project(pid, pdir)
                if n:
                    synced += 1
                    log_host(f"TiDB file backfill: project #{pid} files mirrored ({n} file(s))")
        except Exception as exc:
            logger.warning("fs_sync project %s failed: %s", pid, exc)
    return synced


def _fs_persist_sync(pid: int, relpath: str, data: bytes):
    """Persist a file to TiDB from a synchronous context (auto-requirements
    writes). Fire-and-forget inside the running event loop."""
    if not fs_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(fs_save_file(pid, relpath, data))
    except Exception:
        pass


# =====================================================================
# DEPENDENCY ENGINE
# =====================================================================

STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ()))

# import-name -> pip package name mapping for well-known differences.
IMPORT_TO_PKG = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "Crypto": "pycryptodome",
    "jwt": "PyJWT",
    "telegram": "python-telegram-bot",
    "bson": "pymongo",
    "google": None,
}


def _auto_req_file(pid: int) -> Path:
    return project_dir(pid) / "requirements.auto.txt"


def _req_sources(pid: int) -> list:
    out = []
    for rel in ("requirements.txt", "requirements.auto.txt"):
        p = project_dir(pid) / rel
        if p.exists():
            out.append(p)
    return out


def _combined_req_fingerprint(pid: int, interpreter: str) -> str:
    h = hashlib.sha256()
    for p in _req_sources(pid):
        try:
            h.update(rel_name_of(p).encode("utf-8", "replace"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        except OSError:
            pass
    h.update(interpreter.encode("utf-8", "replace"))
    return h.hexdigest()


# package-name (as written in requirements) -> top-level import name. Reverse of
# IMPORT_TO_PKG plus common extra cases; used to verify a requirements install
# actually made the modules importable (a plain pip rc=0 can lie after a Space
# rebuild wipes site-packages while TiDB still holds req_status="ok").
_PKG_TO_IMPORT = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4"}
for _imp, _pkg in IMPORT_TO_PKG.items():
    if _pkg:
        _PKG_TO_IMPORT.setdefault(_pkg.lower(), _imp)


def _req_import_names(pid: int) -> list:
    """Extract top-level import names that requirements files promise."""
    names = []
    for rel in ("requirements.txt", "requirements.auto.txt"):
        p = project_dir(pid) / rel
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith(("-", "--", "git+", "http", "https")):
                continue
            # skip options like -r, -e, --index-url handled above; strip extras.
            if "://" in line or line.startswith("@"):
                continue
            name = re.split(r"[<>=!;\[\s]+", line)[0].strip()
            if not name or name.startswith("_"):
                continue
            imp = _PKG_TO_IMPORT.get(name.lower(), name.replace("-", "_").lower())
            if imp not in names:
                names.append(imp)
    return names


async def _requirements_installed(pid: int, interpreter: str) -> bool:
    """Verify the promised top-level imports actually resolve in `interpreter`.
    Returns True when there is nothing to verify or everything imports."""
    names = _req_import_names(pid)
    if not names:
        return True
    code = "import " + ", ".join(names)
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter, "-c", code,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


def rel_name_of(path) -> str:
    return str(Path(path).name)


def _module_installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def scan_python_imports(pdir: Path) -> list:
    """Return missing third-party import names found in project .py files.

    Standard-library modules, local project modules and already-installed
    packages are excluded. Top-level import bases only.
    """
    found = set()
    for root, dirs, files in os.walk(str(pdir)):
        dirs[:] = [d for d in dirs if d not in BACKUP_EXCLUDE_DIRS and d != "host_logs"]
        for f in files:
            if not f.lower().endswith(".py"):
                continue
            try:
                with open(os.path.join(root, f), "r", encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read(), filename=f)
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        base = a.name.split(".")[0]
                        if base:
                            found.add(base)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        base = node.module.split(".")[0]
                        if base:
                            found.add(base)
    local = set()
    for root, dirs, files in os.walk(str(pdir)):
        dirs[:] = [d for d in dirs if d not in BACKUP_EXCLUDE_DIRS and d != "host_logs"]
        rel_root = os.path.relpath(root, str(pdir))
        at_top = rel_root == "."
        for f in files:
            if f.lower().endswith(".py"):
                local.add(f[:-3])
        for d in dirs:
            if d == "__pycache__":
                continue
            if at_top and os.path.exists(os.path.join(root, d, "__init__.py")):
                local.add(d)
    missing = []
    for name in sorted(found):
        if name in STDLIB_MODULES:
            continue
        if name in local:
            continue
        if name.startswith("_"):
            continue
        if IMPORT_TO_PKG.get(name) is None and name == "google":
            continue
        if _module_installed(name):
            continue
        missing.append(name)
    return missing


async def ensure_auto_requirements(proj: dict) -> list:
    """Auto-detect missing third-party imports and record them when no
    requirements.txt is present. requirements.txt always has priority."""
    pid = proj["id"]
    pdir = project_dir(pid)
    if (pdir / "requirements.txt").exists():
        return []
    auto = _auto_req_file(pid)
    missing = scan_python_imports(pdir)
    if not missing:
        return []
    pkgs = sorted({IMPORT_TO_PKG.get(m, m) for m in missing if m})
    existing = set()
    if auto.exists():
        try:
            for line in auto.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    existing.add(line)
        except OSError:
            pass
    merged = sorted(existing | set(pkgs))
    try:
        auto.write_text("\n".join(merged) + "\n", encoding="utf-8")
    except OSError:
        pass
    _fs_persist_sync(pid, "requirements.auto.txt", ("\n".join(merged) + "\n").encode("utf-8"))
    return merged


def classify_missing_module(reason: str):
    m = re.search(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]", reason)
    if not m:
        m = re.search(r"ImportError: No module named ['\"]([\w.]+)['\"]", reason)
    if m:
        return m.group(1).split(".")[0]
    return None


def extract_pip_failure(out: str) -> str:
    if not out:
        return "pip install failed"
    reason = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            reason = line[6:].strip()
        elif "No matching distribution found for" in line:
            m = re.search(r"for\s+([\w\-\.\[\]]+)", line)
            reason = f"Package not found: {m.group(1) if m else '?'}"
        elif "Could not find a version" in line:
            reason = line
    if not reason:
        for line in out.splitlines():
            if line.strip():
                reason = line.strip()
                break
    return truncate(reason, 300)


async def get_project(pid: int) -> dict:
    row = await db_fetchone("SELECT * FROM projects WHERE id=?", (pid,))
    return _db_row_to_dict(row)


async def get_project_by_name(name: str) -> dict:
    row = await db_fetchone("SELECT * FROM projects WHERE name=?", (name,))
    return _db_row_to_dict(row)


async def list_projects() -> list:
    rows = await db_fetchall("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
    return [dict(r) for r in rows]


async def enrich_project(proj: dict) -> dict:
    p = dict(proj)
    pdir = project_dir(p["id"])
    size, files = await asyncio.gather(
        adir_size(str(pdir)),
        asyncio.to_thread(list_files_in, str(pdir)),
    )
    p["storage"] = size
    p["files_count"] = len(files)
    if p["status"] == "running" and p.get("started_at"):
        p["uptime"] = time.time() - p["started_at"]
    else:
        p["uptime"] = None
    return p


async def set_project_field(pid: int, field: str, value):
    await db_exec(f"UPDATE projects SET {field}=? WHERE id=?", (value, pid))


async def set_project_status(pid: int, status: str):
    await db_exec("UPDATE projects SET status=? WHERE id=?", (status, pid))


async def project_activity(project_id, action, detail=""):
    await db_exec(
        "INSERT INTO activity (project_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (project_id, action, detail, now()),
    )


async def project_deploy_history(pid: int) -> list:
    rows = await db_fetchall(
        "SELECT * FROM deploy_history WHERE project_id=? ORDER BY id DESC LIMIT 30", (pid,)
    )
    return [dict(r) for r in rows]


async def record_deploy(pid: int, dtype: str, detail: str):
    proj = await get_project(pid)
    count = (proj.get("deploy_count") or 0) + 1
    await db_exec(
        "INSERT INTO deploy_history (project_id, version, type, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (pid, count, dtype, detail, now()),
    )
    await set_project_field(pid, "deploy_count", count)
    await set_project_field(pid, "last_deploy", now())
    await counter_incr("deploys_total")
    await metrics_incr(day_str(), "deploys")


async def notify(kind: str, message: str):
    await db_exec("INSERT INTO notifications (type, message, created_at) VALUES (?, ?, ?)",
                  (kind, message, now()))
    await push_notification_live(kind, message)


# =====================================================================
# GLOBAL STATS HELPERS
# =====================================================================

async def global_stats() -> dict:
    projects, mt, backups_total, replacements_total, restarts_total, projects_created = await asyncio.gather(
        list_projects(),
        metrics_today(),
        counter_get("backups_total"),
        counter_get("replacements_total"),
        counter_get("restarts_total"),
        counter_get("projects_created"),
    )
    running = sum(1 for p in projects if p["status"] == "running")
    starting = sum(1 for p in projects if p["status"] == "starting")
    restarting = sum(1 for p in projects if p["status"] == "restarting")
    stopped = sum(1 for p in projects if p["status"] == "stopped")
    error = sum(1 for p in projects if p["status"] == "error")
    total_storage = sum(await asyncio.gather(*[adir_size(str(project_dir(p["id"]))) for p in projects])) if projects else 0
    return {
        "total": len(projects),
        "running": running,
        "starting": starting,
        "restarting": restarting,
        "stopped": stopped,
        "error": error,
        "storage": total_storage,
        "deploys_today": mt["deploys"],
        "restarts_today": mt["restarts"],
        "backups_created": backups_total,
        "file_replacements": replacements_total,
        "projects_created": projects_created,
        "restarts_total": restarts_total,
        "uptime_host": time.time() - HOST_START,
    }


HOST_START = time.time()

# =====================================================================
# PROCESS MANAGER
# =====================================================================

PROCS = {}      # pid -> asyncio subprocess
PROC_LOCKS = {} # pid -> asyncio.Lock (to serialise lifecycle ops)


def proc_lock(pid: int):
    return PROC_LOCKS.setdefault(pid, asyncio.Lock())


async def env_vars_for(pid: int) -> dict:
    rows = await db_fetchall("SELECT key, value FROM env_vars WHERE project_id=?", (pid,))
    return {r["key"]: r["value"] for r in rows}


async def runtime_env(proj: dict) -> dict:
    env = dict(os.environ)
    env["HOSTING_PROJECT_ID"] = str(proj["id"])
    env["HOSTING_PROJECT_NAME"] = proj["name"]
    vars_map = await env_vars_for(proj["id"])
    env.update(vars_map)
    return env


LOG_TYPES = {"runtime", "error", "deploy"}


def _log_path(pid: int, logname: str) -> Path:
    if logname not in LOG_TYPES:
        raise ValueError("Invalid log type.")
    return project_logs_dir(pid) / f"{logname}.log"


def _tail_file(path: str, lines: int, char_cap: int) -> str:
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            block = 8192
            data = b""
            pos = size
            while pos > 0 and len(data) < block * 64:
                read_start = max(0, pos - block)
                fh.seek(read_start)
                data = fh.read(pos - read_start) + data
                pos = read_start
                if data.count(b"\n") >= lines:
                    break
            text = data.decode("utf-8", errors="replace")
    except OSError:
        return "(unreadable)"
    out_lines = text.splitlines()[-lines:]
    joined = "\n".join(out_lines)
    if len(joined) > char_cap:
        joined = joined[-char_cap:]
    return joined or "(empty)"


async def tail_log(pid: int, logname: str, lines: int = LOG_TAIL_LINES) -> str:
    try:
        path = _log_path(pid, logname)
    except ValueError:
        return "(invalid)"
    if not path.exists():
        return "(empty)"
    return await asyncio.to_thread(_tail_file, str(path), lines, LOG_LINE_LIMIT)


async def clear_log(pid: int, logname: str):
    try:
        path = _log_path(pid, logname)
    except ValueError:
        return
    try:
        if path.exists():
            path.write_text("", encoding="utf-8")
        await project_activity(pid, "Log Cleared", logname)
    except OSError:
        pass


async def download_log(pid: int, logname: str) -> str:
    try:
        path = _log_path(pid, logname)
    except ValueError:
        return str(project_logs_dir(pid) / f"{sanitize_filename(logname)}.log")
    return str(path)


async def prepare_runtime_dir(pid: int):
    pdir = project_dir(pid)
    (pdir / "host_logs").mkdir(parents=True, exist_ok=True)
    (pdir / "backups").mkdir(parents=True, exist_ok=True)
    (pdir / "._versions").mkdir(parents=True, exist_ok=True)


LOG_FILE_CAP = 2 * 1024 * 1024


def _trim_log_file(path: Path):
    try:
        keep = LOG_FILE_CAP // 2
        with open(path, "r+b") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size <= LOG_FILE_CAP:
                return
            fh.seek(max(0, size - keep))
            rest = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(rest)
            fh.write(b"\n--- log trimmed ---\n")
    except OSError:
        pass


def _append_capped(path: Path, text: str):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
        _trim_log_file(path)
    except OSError:
        pass


def _write_runtime_error(pid: int, text: str):
    _append_capped(project_logs_dir(pid) / "error.log", text)


def _write_runtime_log(pid: int, text: str):
    _append_capped(project_logs_dir(pid) / "runtime.log", text)


def _write_deploy_log(pid: int, text: str):
    _append_capped(project_logs_dir(pid) / "deploy.log", text)


async def _run_start_locked(proj: dict) -> dict:
    pid = proj["id"]
    pdir = project_dir(pid)
    await prepare_runtime_dir(pid)
    main_path = pdir / proj["main_file"]

    if not main_path.exists():
        # /data is ephemeral on HF free tier; pull the files back from the
        # TiDB mirror before declaring the project broken.
        try:
            await fs_restore_project(pid, pdir)
        except Exception:
            logger.exception("pre-start TiDB restore failed for project %s", pid)
        main_path = pdir / proj["main_file"]

    if not main_path.exists():
        await set_project_status(pid, "error")
        await set_project_field(pid, "error", "Main File Not Found")
        _write_runtime_error(pid, "ERROR: Main File Not Found: " + proj["main_file"])
        return {"ok": False, "error": "Main File Not Found", "hint": "Please select the correct startup file in Settings → Startup File."}

    interpreter = await resolve_python(proj["python"])
    avail = available_python_versions()
    avail_versions = [v for v, _ in avail]
    if not interpreter or not os.path.isfile(interpreter):
        await set_project_status(pid, "error")
        await set_project_field(pid, "error", "Python Version Not Supported")
        _write_runtime_error(pid, f"ERROR: Python Version Not Supported: {proj['python']}")
        return {"ok": False, "error": "Python Version Not Supported",
                "hint": f"Requested Python {proj['python']} is not installed. Available: {', '.join(avail_versions) or 'python3'}."}

    if proj.get("req_install"):
        await ensure_auto_requirements(proj)
        inst = await _install_requirements(proj, interpreter)
        if not inst.get("ok") and not inst.get("skipped"):
            await set_project_status(pid, "error")
            await set_project_field(pid, "error", "Dependency Installation Failed")
            _write_runtime_error(pid, f"ERROR: Dependency installation failed: {inst.get('error')}")
            return {"ok": False, "error": "Dependency Installation Failed",
                    "hint": f"{inst.get('error')}\n\nCheck the Deploy Log. Disable 'Requirements Install' in Settings to start anyway."}

    env = await runtime_env(proj)
    rt_log = open(str(project_logs_dir(pid) / "runtime.log"), "ab")
    err_log = open(str(project_logs_dir(pid) / "error.log"), "ab")
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter,
            proj["main_file"],
            cwd=str(pdir),
            env=env,
            stdout=rt_log,
            stderr=err_log,
            start_new_session=True,
        )
    except FileNotFoundError:
        _write_runtime_error(pid, f"ERROR: Python interpreter not found: {interpreter}")
        await set_project_status(pid, "error")
        await set_project_field(pid, "error", "Python Version Not Supported")
        return {"ok": False, "error": "Python Version Not Supported", "hint": "Interpreter missing."}

    PROCS[proc.pid] = proc
    await set_project_field(pid, "pid", proc.pid)
    await set_project_field(pid, "started_at", now())
    await set_project_field(pid, "last_restart", now())
    await set_project_status(pid, "starting")

    _write_runtime_log(pid, f"[{fmt_time(now())}] Project starting (pid={proc.pid}) interpreter={interpreter}")

    asyncio.create_task(_monitor_project(pid, proc))
    await asyncio.sleep(3)

    proj_now = await get_project(pid)
    if proj_now["status"] == "starting" and await is_process_alive(proc.pid):
        await set_project_status(pid, "running")
        await set_project_field(pid, "crash_count", 0)
        return {"ok": True, "pid": proc.pid}

    if proj_now["status"] == "error" and proj_now.get("error"):
        return {"ok": False, "error": proj_now["error"], "hint": "Check Logs for details."}

    return {"ok": False, "error": "Process Exited", "hint": "Check Logs for details."}


async def run_start(proj: dict) -> dict:
    pid = proj["id"]
    async with proc_lock(pid):
        return await _run_start_locked(proj)


async def install_requirements(proj: dict, interpreter: str, force: bool = False) -> dict:
    pid = proj["id"]
    async with proc_lock(pid):
        fresh = await get_project(pid)
        if fresh is None:
            return {"ok": False, "error": "Project not found"}
        return await _install_requirements(fresh, interpreter, force=force)


async def _install_requirements(proj: dict, interpreter: str, force: bool = False) -> dict:
    pid = proj["id"]
    sources = _req_sources(pid)
    if not sources:
        return {"ok": True, "skipped": True}
    try:
        content_hash = _combined_req_fingerprint(pid, interpreter)
    except Exception:
        content_hash = ""
    if not force:
        if proj.get("req_hash") == content_hash and proj.get("req_status") == "ok":
            # Trust the cached result only if the promised modules actually
            # import with this interpreter right now. A Space rebuild wipes
            # site-packages while TiDB keeps req_status="ok", which silently
            # skipped every future install -> ModuleNotFoundError at start.
            if await _requirements_installed(pid, interpreter):
                return {"ok": True, "skipped": True}
            _write_deploy_log(pid, "[install] cached ok but modules missing; reinstalling")
    _write_deploy_log(pid, "[install] Installing requirements.txt ...")
    await set_project_field(pid, "req_status", "installing")
    await set_project_field(pid, "req_error", None)
    args = [interpreter, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    for src in sources:
        args += ["-r", str(src)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(project_dir(pid)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        _write_deploy_log(pid, "[install] TIMEOUT after 900s")
        await set_project_field(pid, "req_status", "failed")
        await set_project_field(pid, "req_error", "pip install timed out")
        return {"ok": False, "error": "pip install timed out"}
    except Exception as e:
        _write_deploy_log(pid, f"[install] error: {e}")
        await set_project_field(pid, "req_status", "failed")
        await set_project_field(pid, "req_error", str(e))
        return {"ok": False, "error": str(e)}
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    _write_deploy_log(pid, f"[install] done rc={proc.returncode}")
    if out:
        _write_deploy_log(pid, out[-3000:])
    if proc.returncode == 0:
        await set_project_field(pid, "req_hash", content_hash)
        if await _requirements_installed(pid, interpreter):
            await set_project_field(pid, "req_status", "ok")
            await set_project_field(pid, "req_error", None)
            _write_deploy_log(pid, "[install] verified: all requirements import successfully")
            return {"ok": True}
        reason = "pip reported success but imports still fail: " + ", ".join(_req_import_names(pid))
        _write_deploy_log(pid, f"[install] verify failed: {reason}")
        await set_project_field(pid, "req_status", "failed")
        await set_project_field(pid, "req_error", reason)
        await project_activity(pid, "Dependency Install Failed", reason)
        return {"ok": False, "error": reason}
    reason = extract_pip_failure(out)
    await set_project_field(pid, "req_status", "failed")
    await set_project_field(pid, "req_error", reason)
    await project_activity(pid, "Dependency Install Failed", reason)
    return {"ok": False, "error": reason}


async def _monitor_project(pid: int, proc: asyncio.subprocess.Process):
    try:
        rc = await proc.wait()
    except Exception:
        return
    PROCS.pop(proc.pid, None)
    await _handle_project_exit(pid, proc, rc)


async def _handle_project_exit(pid: int, proc, rc):
    proj = await get_project(pid)
    if proj is None:
        return
    status = proj["status"]
    if status in ("stopped", "restarting"):
        return
    err_tail = await tail_log(pid, "error", 12)
    reason = err_tail.strip() or "Process exited"
    reason = truncate(reason, 300)
    crash_count = (proj.get("crash_count") or 0) + 1
    await set_project_field(pid, "crash_count", crash_count)
    if rc == 0:
        await set_project_status(pid, "stopped")
        await set_project_field(pid, "error", None)
        await set_project_field(pid, "pid", None)
        await set_project_field(pid, "crash_count", 0)
        await notify("Project Stopped", f"⏹ {proj['name']} stopped cleanly (exit 0).")
        return

    await set_project_status(pid, "error")
    await set_project_field(pid, "error", reason)
    await set_project_field(pid, "pid", None)
    await counter_incr("crashes_total")
    msg = f"⚠ {proj['name']} Crashed\n\nTime: {fmt_time(now())}\n\nReason:\n{reason}\n\nView Logs"
    await notify("Crash Detected", msg)

    missing = classify_missing_module(reason)
    if proj.get("auto_restart"):
        if crash_count > 5:
            await notify("Crash Detected", f"⚠ Auto-restart disabled for {proj['name']}: crashed {crash_count} times in a row. Manual restart required.")
            await _send_crash_alert(pid, msg)
            return
        await set_project_status(pid, "restarting")
        await _auto_restart(proj, crash_count)
        return
    if missing and proj.get("req_install") and crash_count <= 2:
        await set_project_status(pid, "restarting")
        await _recover_missing_module(proj, missing, crash_count)
        return
    await _send_crash_alert(pid, msg)


async def _recover_missing_module(proj: dict, missing_name: str, crash_count: int):
    pid = proj["id"]
    pkg = IMPORT_TO_PKG.get(missing_name) or missing_name
    _write_deploy_log(pid, f"[install] Missing module '{missing_name}' detected; installing {pkg} ...")
    await asyncio.sleep(1)
    async with proc_lock(pid):
        fresh = await get_project(pid)
        if fresh is None or fresh["status"] != "restarting":
            return
        auto = _auto_req_file(pid)
        try:
            existing = set()
            if auto.exists():
                for line in auto.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        existing.add(line)
            existing.add(pkg)
            auto.write_text("\n".join(sorted(existing)) + "\n", encoding="utf-8")
        except OSError:
            pass
        _fs_persist_sync(pid, "requirements.auto.txt", ("\n".join(sorted(existing)) + "\n").encode("utf-8"))
        await set_project_field(pid, "req_hash", None)
        await set_project_field(pid, "req_status", None)
        interpreter = await resolve_python(fresh["python"])
        res = await _install_requirements(fresh, interpreter, force=True)
        if not res.get("ok"):
            await notify("Crash Detected", f"⚠ Auto-install failed for {proj['name']}: {res.get('error')}")
            await _send_crash_alert(pid, f"⚠ {proj['name']} Crashed\n\nMissing module '{missing_name}' could not be installed: {res.get('error')}")
            return
        fresh2 = await get_project(pid)
        if fresh2 is None or fresh2["status"] != "restarting":
            return
        result = await _run_start_locked(fresh2)
        if result["ok"]:
            await set_project_field(pid, "crash_count", 0)
            await counter_incr("restarts_total")
            await metrics_incr(day_str(), "restarts")
            await notify("Restart Complete", f"🔄 {proj['name']} recovered automatically (installed {pkg}).")
        else:
            await notify("Crash Detected", f"⚠ Auto-recovery restart failed for {proj['name']}: {result['error']}")
            await _send_crash_alert(pid, f"⚠ {proj['name']} restart after install failed: {result['error']}")


async def _send_crash_alert(pid: int, text: str):
    if not _application:
        return
    rows = [
        [B("📜 Logs", "nav", screen="log_view", params={"pid": pid, "logtype": "error"}),
         B("🔄 Restart", "start_proj", pid=pid)],
        [B("📂 Dashboard", "nav", screen="project_dash", params={"pid": pid})],
    ]
    markup = build_markup(rows)
    for chat in ([OWNER_ID] if OWNER_ID else []):
        try:
            await _application.bot.send_message(chat_id=chat, text=text, reply_markup=markup)
        except TelegramError:
            pass


async def _auto_restart(proj: dict, crash_count: int = 0):
    pid = proj["id"]
    await asyncio.sleep(2)
    fresh = await get_project(pid)
    if fresh is None or fresh["status"] != "restarting":
        return
    result = await run_start(fresh)
    if result["ok"]:
        await set_project_field(pid, "crash_count", 0)
        await counter_incr("restarts_total")
        await metrics_incr(day_str(), "restarts")
        await notify("Restart Complete", f"🔄 Project Restarted Successfully: {fresh['name']}")
    else:
        await notify("Crash Detected", f"⚠ Auto-restart failed for {fresh['name']}: {result['error']}")


async def _wait_pid_dead(pid: int, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not await is_process_alive(pid):
            return
        await asyncio.sleep(0.3)


def _pid_belongs_to_project(db_pid: int, started_at) -> bool:
    """Safely confirm an OS pid belongs to a hosted project before killing."""
    try:
        p = psutil.Process(db_pid)
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return False
        if started_at:
            return abs(p.create_time() - started_at) < 60
        return True
    except Exception:
        return False


async def stop_project(proj: dict) -> bool:
    pid = proj["id"]
    async with proc_lock(pid):
        return await _stop_project_locked(proj)


async def _stop_project_locked(proj: dict) -> bool:
    pid = proj["id"]
    await set_project_status(pid, "stopped")
    proc = PROCS.get(proj.get("pid"))
    if proc is not None:
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                proc.send_signal(signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        except ProcessLookupError:
            pass
        except Exception:
            pass
        PROCS.pop(proc.pid, None)
        await set_project_field(pid, "pid", None)
        return True
    # Not tracked in this session: handle an orphan/stale pid from the OS.
    db_pid = proj.get("pid")
    if db_pid and await is_process_alive(db_pid) and _pid_belongs_to_project(db_pid, proj.get("started_at")):
        try:
            os.kill(db_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(_wait_pid_dead(db_pid, 8), timeout=10)
        except Exception:
            try:
                os.kill(db_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(_wait_pid_dead(db_pid, 3), timeout=5)
            except Exception:
                pass
    await set_project_field(pid, "pid", None)
    return True


async def restart_project(proj: dict) -> dict:
    pid = proj["id"]
    async with proc_lock(pid):
        await set_project_status(pid, "restarting")
        await _stop_project_locked(proj)
        await asyncio.sleep(1.5)
        fresh = await get_project(pid)
        if fresh is None:
            return {"ok": False, "error": "Project not found"}
        result = await _run_start_locked(fresh)
        if result["ok"]:
            await set_project_field(pid, "crash_count", 0)
            await counter_incr("restarts_total")
            await metrics_incr(day_str(), "restarts")
        return result


async def is_process_alive(pid: int) -> bool:
    try:
        ps = psutil.Process(pid)
        return ps.is_running() and ps.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


async def process_rss(pid: int) -> int:
    try:
        return psutil.Process(pid).memory_info().rss
    except Exception:
        return 0


async def process_cpu(pid: int) -> float:
    try:
        return psutil.Process(pid).cpu_percent(interval=None)
    except Exception:
        return 0.0


_CPU_CACHE = 0.0


def _cpu_sampler_loop():
    """Dedicated thread samples CPU every 2s into _CPU_CACHE so the event loop
    is never blocked by psutil.cpu_percent(interval=...)."""
    global _CPU_CACHE
    try:
        while True:
            try:
                _CPU_CACHE = psutil.cpu_percent(interval=None)
            except Exception:
                pass
            time.sleep(2)
    except Exception:
        pass


def _start_cpu_sampler():
    try:
        threading.Thread(target=_cpu_sampler_loop, daemon=True).start()
    except Exception:
        pass


def system_cpu() -> float:
    return _CPU_CACHE


def system_ram():
    try:
        vm = psutil.virtual_memory()
        return vm.used, vm.total
    except Exception:
        return None, None


def hosting_storage():
    du = disk_usage_of(str(DATA_DIR))
    if du is None:
        return None
    total, used, free = du.total, du.used, du.free
    pct = (used / total) * 100 if total else 0
    return total, used, free, pct


# =====================================================================
# LIVE INDICATORS (Part 5)
# =====================================================================

async def db_status() -> bool:
    try:
        row = await db_fetchone("SELECT 1")
        return row is not None
    except Exception:
        return False


async def reconcile_startup_statuses():
    """On startup the OS processes are gone (Space restart / /data wipe) but TiDB
    may still say 'running'. Reconcile so no project ever shows 🟢 Running for a
    process that was never spawned in this session."""
    try:
        projects = await list_projects()
    except Exception:
        return
    for proj in projects:
        if proj.get("status") != "running":
            continue
        pid = proj.get("pid")
        # A pid only "keeps" running if this session actually spawned it.
        if pid and PROCS.get(pid) is not None and await is_process_alive(pid):
            continue
        await set_project_status(proj["id"], "stopped")
        await set_project_field(proj["id"], "pid", None)
        await set_project_field(proj["id"], "error", None)
        logger.info("startup reconcile: project %s ('%s') reset from running to stopped",
                    proj["id"], proj.get("name"))


def bot_polling_alive() -> bool:
    """True only when the panel bot's application is actually started. In
    webhook mode there is no polling updater, so we check the application is
    initialized and its asyncio loop is still alive."""
    try:
        a = _application
        if a is None:
            return False
        if getattr(a, "running", False):
            return True
        updater = a.updater
        if updater is not None and bool(getattr(updater, "running", False)):
            return True
        return False
    except Exception:
        return False


def health_db_ok() -> bool:
    """Synchronous DB health check safe to call from the Flask (web) thread.
    Runs the async SELECT 1 on the bot's event loop."""
    loop = _loop
    if loop is None or loop.is_closed():
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(db_status(), loop)
        return bool(fut.result(timeout=5))
    except Exception:
        return False


async def network_status() -> str:
    try:
        import socket
        loop = asyncio.get_running_loop()
        t0 = time.time()
        conn = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: socket.create_connection(("api.telegram.org", 443), timeout=3)),
            timeout=5,
        )
        conn.close()
        elapsed = time.time() - t0
        if elapsed > 1.5:
            return "Slow"
        return "Stable"
    except Exception:
        return "Offline"


async def backend_status() -> str:
    db_ok = await db_status()
    du = hosting_storage()
    if not db_ok:
        return "Offline"
    if du and du[3] >= STORAGE_CRIT_PCT:
        return "Limited"
    return "Healthy"


async def security_status() -> str:
    du = hosting_storage()
    db_ok = await db_status()
    if du and du[3] >= STORAGE_CRIT_PCT:
        return "Risk Detected"
    if not db_ok:
        return "Warning"
    return "Protected"


async def runtime_indicator() -> str:
    stats = await global_stats()
    if stats["running"] > 0:
        return "Active"
    return "Stopped"


async def hosting_status_line() -> str:
    if not bot_polling_alive():
        return "Restarting"
    if not await db_status():
        return "Degraded"
    return "Online"


async def build_indicators(scope: str, proj: dict = None) -> list:
    lines = []
    if scope == "home":
        host = await hosting_status_line()
        host_icon = {"Online": "🟢", "Degraded": "🟡", "Restarting": "🟠"}.get(host, "🔴")
        lines.append(f"🖥 Hosting Status: {host_icon} {host}")
        du = hosting_storage()
        if du:
            used = await adir_size(str(DATA_DIR))
            free = du[2]
            total_usable = used + free
            pct = (used / total_usable * 100) if total_usable else 0
            lines.append(f"💾 Panel Data: {bar(pct)} {human_size(used)} ({pct:.0f}%)")
        lines.append(f"🔒 Security: 🟢 {await security_status()}")
    elif scope == "project":
        status = STATUS_ICON.get((proj or {}).get("status", "stopped"), "🔴 Stopped")
        lines.append(f"📌 Status: {status}")
        lines.append(f"💾 Storage: {human_size((proj or {}).get('storage', 0))}")
        lines.append("🚀 Runtime: 🟢 Active" if (proj or {}).get("status") == "running" else "🚀 Runtime: 🔴 Stopped")
        db_ok = await db_status()
        lines.append("🗄 Database: 🟢 Connected" if db_ok else "🗄 Database: 🔴 Disconnected")
    elif scope == "system":
        host = await hosting_status_line()
        host_icon = {"Online": "🟢", "Degraded": "🟡", "Restarting": "🟠"}.get(host, "🔴")
        lines.append(f"🖥 Hosting: {host_icon} {host}")
        du = hosting_storage()
        if du:
            used = await adir_size(str(DATA_DIR))
            free = du[2]
            total_usable = used + free
            pct = (used / total_usable * 100) if total_usable else 0
            lines.append(f"💾 Panel Data: {bar(pct)} {human_size(used)} ({pct:.0f}%)")
        ram_used, ram_total = system_ram()
        if ram_total:
            lines.append(f"🧠 RAM: {bar(ram_used / ram_total * 100)} {human_size(ram_used)} / {human_size(ram_total)}")
        lines.append(f"⚙ CPU: {bar(system_cpu())} {system_cpu():.0f}%")
        net = await network_status()
        lines.append(f"📡 Network: {'🟢 Stable' if net == 'Stable' else ('🟡 Slow' if net == 'Slow' else '🔴 Offline')}")
        db_ok = await db_status()
        lines.append(f"🗄 Database: {'🟢 Connected' if db_ok else '🔴 Disconnected'}")
        sec = await security_status()
        icon = "🟢 Protected" if sec == "Protected" else ("🟡 Warning" if sec == "Warning" else "🔴 Risk Detected")
        lines.append(f"🔒 Security: {icon}")
        backend = await backend_status()
        bicon = {"Healthy": "🟢 Healthy", "Limited": "🟡 Limited", "Offline": "🔴 Offline"}.get(backend, backend)
        lines.append(f"☁ Backend: {bicon}")
    return lines


# =====================================================================
# RESOURCE MONITOR (global)
# =====================================================================

async def resource_summary() -> list:
    projects = await list_projects()
    out = []
    for p in projects:
        mem = 0
        cpu = 0.0
        if p["status"] == "running" and p.get("pid"):
            mem = await process_rss(p["pid"])
            cpu = await process_cpu(p["pid"])
        out.append({"id": p["id"], "name": p["name"], "ram": mem, "cpu": cpu,
                    "storage": await adir_size(str(project_dir(p["id"]))), "status": p["status"]})
    return out


# =====================================================================
# NOTIFICATIONS / LIVE PUSH
# =====================================================================

NOTIF_ICONS = {
    "Deploy Complete": "📤",
    "Restart Complete": "🔄",
    "Backup Complete": "💾",
    "Storage Warning": "⚠",
    "Crash Detected": "🚨",
    "Project Deleted": "🗑",
    "Project Started": "▶",
    "Project Stopped": "⏹",
    "File Replaced": "🔄",
    "Restore Complete": "♻",
    "Backup Restored": "♻",
}


async def notification_list(page: int = 1, per_page: int = NOTIFS_PER_PAGE):
    rows = await db_fetchall(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT ? OFFSET ?",
        (per_page, (page - 1) * per_page),
    )
    total_row = await db_fetchone("SELECT COUNT(*) AS c FROM notifications")
    total = total_row["c"] if total_row else 0
    return [dict(r) for r in rows], total


async def push_notification_live(kind: str, message: str):
    entries = list(OPEN_SCREENS.entries.items())
    for uid, entry in entries:
        if entry["screen"] == "notifications":
            text, rows = await render("notifications", {"page": 1})
            markup = build_markup(rows)
            try:
                await _application.bot.edit_message_text(
                    chat_id=entry["chat_id"],
                    message_id=entry["message_id"],
                    text=text,
                    reply_markup=markup,
                )
            except TelegramError:
                pass
            entry["last_edit"] = time.time()


async def delete_all_notifications():
    await db_exec("DELETE FROM notifications")


# =====================================================================
# BACKUP MANAGER
# =====================================================================

BACKUP_EXCLUDE_DIRS = {"backups", "._versions", "__pycache__", ".venv", "venv", ".git", "host_logs"}


def _walk_backup(src: Path) -> list:
    items = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in BACKUP_EXCLUDE_DIRS]
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, src)
            items.append((fp, rel))
    return items


async def create_backup(pid: int) -> dict:
    pdir = project_dir(pid)
    backups_dir = project_backups_dir(pid)
    backups_dir.mkdir(parents=True, exist_ok=True)
    name = f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    zpath = backups_dir / name
    zf = zipfile.ZipFile(str(zpath), "w", zipfile.ZIP_DEFLATED)
    try:
        for fp, rel in _walk_backup(pdir):
            try:
                zf.write(fp, rel)
            except OSError:
                continue
    finally:
        zf.close()
    size = os.path.getsize(zpath)
    await db_exec("INSERT INTO backups (project_id, name, size, created_at) VALUES (?, ?, ?, ?)",
                  (pid, name, size, now()))
    await set_project_field(pid, "last_backup", now())
    await counter_incr("backups_total")
    await metrics_incr(day_str(), "backups")
    await project_activity(pid, "Backup Created", name)
    await notify("Backup Complete", f"💾 Backup Created for project #{pid} ({human_size(size)})")
    return {"name": name, "size": size, "path": str(zpath)}


async def list_backups(pid: int, page: int = 1, per_page: int = BACKUPS_PER_PAGE):
    rows = await db_fetchall(
        "SELECT * FROM backups WHERE project_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (pid, per_page, (page - 1) * per_page),
    )
    total_row = await db_fetchone("SELECT COUNT(*) AS c FROM backups WHERE project_id=?", (pid,))
    total = total_row["c"] if total_row else 0
    return [dict(r) for r in rows], total


async def get_backup(bid: int):
    row = await db_fetchone("SELECT * FROM backups WHERE id=?", (bid,))
    return _db_row_to_dict(row)


def backup_path(pid: int, name: str) -> Path:
    return _resolve_within(project_backups_dir(pid), name)


async def delete_backup(pid: int, bid: int) -> bool:
    b = await get_backup(bid)
    if not b:
        return False
    p = backup_path(pid, b["name"])
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass
    await db_exec("DELETE FROM backups WHERE id=?", (bid,))
    await project_activity(pid, "Backup Deleted", b["name"])
    return True


async def restore_backup(pid: int, bid: int) -> dict:
    b = await get_backup(bid)
    if not b:
        return {"ok": False, "error": "Backup not found"}
    zpath = backup_path(pid, b["name"])
    if not zpath.exists():
        return {"ok": False, "error": "Backup file missing on disk"}
    pdir = project_dir(pid)
    tmp_dir = TMP_DIR / f"restore_{pid}_{int(time.time())}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(zpath), "r") as zf:
            err = _validate_zip_members(zf)
            if err:
                return {"ok": False, "error": err}
            zf.extractall(str(tmp_dir))
        for fp, rel in _walk_backup(tmp_dir):
            dest = pdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, dest)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    await fs_save_project(pid, pdir)
    await project_activity(pid, "Backup Restored", b["name"])
    await notify("Backup Restored", f"♻ Restored {b['name']} into project #{pid}")
    return {"ok": True}


# =====================================================================
# EXPORT PROJECT
# =====================================================================

async def export_project(pid: int) -> Path:
    pdir = project_dir(pid)
    tmp = TMP_DIR / f"export_{pid}_{int(time.time())}.zip"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    zf = zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED)
    try:
        for fp, rel in _walk_backup(pdir):
            try:
                zf.write(fp, rel)
            except OSError:
                continue
        settings_file = pdir / "._panel_settings.json"
        if settings_file.exists():
            try:
                zf.write(str(settings_file), "._panel_settings.json")
            except OSError:
                pass
        try:
            env_rows = await db_fetchall("SELECT key, value FROM env_vars WHERE project_id=?", (pid,))
            env_json = json.dumps({r["key"]: r["value"] for r in env_rows}, indent=2)
            zf.writestr("._panel_env.json", env_json)
        except Exception:
            pass
    finally:
        zf.close()
    return tmp


# =====================================================================
# FILE MANAGER OPERATIONS
# =====================================================================

def _resolve_within(root: Path, name: str) -> Path:
    """Resolve a user-supplied name against root, rejecting absolute paths,
    traversal (..), NUL bytes, and symlinks that escape the root."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("Invalid path.")
    if name.startswith("/") or name.startswith("\\") or name.startswith("\\\\"):
        raise ValueError("Invalid path.")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Invalid path.")
    root_real = root.resolve()
    cand = (root / rel).resolve()
    if cand != root_real and root_real not in cand.parents:
        raise ValueError("Path escapes project folder.")
    return root / rel


def file_path_for(pid: int, filename: str) -> Path:
    root = project_dir(pid)
    return _resolve_within(root, filename)


def file_exists(pid: int, filename: str) -> bool:
    try:
        p = file_path_for(pid, filename)
    except ValueError:
        return False
    return p.exists() and p.is_file()


def validate_new_filename(name: str) -> str:
    if not name:
        return "Name cannot be empty."
    if len(name) > 120:
        return "Name is too long."
    if any(ch in ILLEGAL_CHARS + "\x00" for ch in name):
        return "Name contains illegal characters."
    if name in (".", ".."):
        return "Invalid name."
    return ""


async def replace_file(pid: int, filename: str, new_content: bytes) -> dict:
    try:
        target = file_path_for(pid, filename)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not target.parent.exists():
        return {"ok": False, "error": "Project folder missing"}
    vdir = project_versions_dir(pid)
    vdir.mkdir(parents=True, exist_ok=True)
    version_name = f"{filename}.{int(time.time())}.bak"
    try:
        if target.exists():
            shutil.copy2(str(target), str(vdir / version_name))
    except OSError as e:
        return {"ok": False, "error": f"Failed to preserve old version: {e}"}
    tmp = TMP_DIR / f"replace_{pid}_{int(time.time())}.tmp"
    try:
        tmp.write_bytes(new_content)
        os.replace(str(tmp), str(target))
    except OSError as e:
        return {"ok": False, "error": f"Failed to write new file: {e}"}
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    await fs_save_file(pid, filename, new_content)
    await counter_incr("replacements_total")
    await metrics_incr(day_str(), "replacements")
    await project_activity(pid, "File Replaced", filename)
    await notify("File Replaced", f"🔄 {filename} replaced in project #{pid}")
    return {"ok": True, "version": version_name}


async def upload_file(pid: int, filename: str, content: bytes) -> dict:
    if file_exists(pid, filename):
        return {"ok": False, "error": "A file with this name already exists."}
    try:
        target = file_path_for(pid, filename)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        target.write_bytes(content)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    await fs_save_file(pid, filename, content)
    await project_activity(pid, "File Uploaded", filename)
    return {"ok": True}


async def delete_file(pid: int, filename: str) -> bool:
    try:
        target = file_path_for(pid, filename)
    except ValueError:
        return False
    try:
        target.unlink()
    except OSError:
        return False
    await fs_delete_file(pid, filename)
    await project_activity(pid, "File Deleted", filename)
    return True


async def rename_file(pid: int, old: str, new: str) -> dict:
    try:
        src = file_path_for(pid, old)
        dst = file_path_for(pid, new)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not src.exists():
        return {"ok": False, "error": "Source file not found."}
    if dst.exists():
        return {"ok": False, "error": "A file with this name already exists."}
    try:
        os.replace(str(src), str(dst))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    await fs_rename_file(pid, old, new)
    proj = await get_project(pid)
    if proj and proj["main_file"] == old:
        await set_project_field(pid, "main_file", new)
    await project_activity(pid, "File Renamed", f"{old} → {new}")
    return {"ok": True}


async def list_previous_versions(pid: int, filename: str) -> list:
    vdir = project_versions_dir(pid)
    if not vdir.exists():
        return []
    pattern = re.compile(r"^" + re.escape(filename) + r"\.(\d+)\.bak$")
    out = []
    for f in vdir.iterdir():
        m = pattern.match(f.name)
        if m:
            out.append({"name": f.name, "path": str(f), "ts": float(m.group(1)),
                        "size": f.stat().st_size if f.exists() else 0})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


async def restore_previous_version(pid: int, filename: str, version_name: str) -> dict:
    vdir = project_versions_dir(pid)
    try:
        vp = _resolve_within(vdir, version_name)
    except ValueError:
        return {"ok": False, "error": "Invalid version name."}
    if vp.name != version_name or not vp.name.endswith(".bak"):
        return {"ok": False, "error": "Invalid version name."}
    if not vp.exists() or not vp.is_file():
        return {"ok": False, "error": "Version file missing."}
    try:
        target = file_path_for(pid, filename)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        shutil.copy2(str(vp), str(target))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    await fs_save_file(pid, filename, vp.read_bytes())
    await project_activity(pid, "Version Restored", version_name)
    return {"ok": True}


async def delete_cache(pid: int) -> dict:
    pdir = project_dir(pid)
    removed = 0
    freed = 0
    for root, dirs, files in os.walk(pdir):
        dirs[:] = [d for d in dirs if d not in BACKUP_EXCLUDE_DIRS and d != "host_logs"]
        if os.path.basename(root) == "__pycache__":
            for f in files:
                fp = os.path.join(root, f)
                try:
                    freed += os.path.getsize(fp)
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
            shutil.rmtree(root, ignore_errors=True)
            continue
        for f in files:
            low = f.lower()
            if low.endswith((".pyc", ".pyo", ".tmp", ".temp", "~")):
                fp = os.path.join(root, f)
                try:
                    freed += os.path.getsize(fp)
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
    await project_activity(pid, "Cache Cleared", f"{removed} files, {human_size(freed)}")
    return {"removed": removed, "freed": freed}


async def delete_project(pid: int) -> dict:
    """Permanently delete a project: stop it, remove its folder and all
    project-scoped rows (env_vars, backups, history, activity, schedules)."""
    proj = await get_project(pid)
    if not proj:
        return {"ok": False, "error": "Project not found."}
    async with proc_lock(pid):
        try:
            if proj["status"] in ("running", "starting", "restarting"):
                await stop_project(proj)
        except Exception:
            logger.exception("stop during project delete failed")
        proc = PROCS.pop(pid, None)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        pdir = project_dir(pid)
        try:
            if pdir.exists():
                shutil.rmtree(str(pdir))
        except OSError as e:
            return {"ok": False, "error": f"Failed to remove project folder: {e}"}
        for table in ("env_vars", "backups", "deploy_history", "activity", "project_files"):
            await db_exec(f"DELETE FROM {table} WHERE project_id=?", (pid,))
        await db_exec("DELETE FROM schedules WHERE project_id=?", (pid,))
        await db_exec("DELETE FROM projects WHERE id=?", (pid,))
        PROC_LOCKS.pop(pid, None)
    await db_system_log(f"Project deleted: {proj['name']} (#{pid})")
    await notify("Project Deleted", f"🗑 Project deleted: {proj['name']}")
    await counter_incr("projects_deleted")
    return {"ok": True, "name": proj["name"]}


# =====================================================================
# DEPLOY MANAGER
# =====================================================================

async def validate_project_name(name: str) -> str:
    name = name.strip()
    if not name:
        return "Please enter a project name."
    if len(name) > NAME_MAX:
        return f"Project name is too long (max {NAME_MAX} characters)."
    if not NAME_ALLOWED.match(name):
        return "Project name contains illegal characters. Use letters, numbers, space, _, -, ."
    existing = await get_project_by_name(name)
    if existing:
        return f"A project named '{name}' already exists. Please choose another name."
    return ""


def _parse_env_file(content: bytes) -> dict:
    """Parse KEY=VALUE lines from a .env file (skips comments/blanks)."""
    env = {}
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and "\x00" not in key and "=" not in key and len(key) <= 50:
            env[key] = val
    return env


async def create_project(name: str, files: dict, main_file: str, python: str, dtype: str) -> dict:
    pdir = None
    pid = None
    try:
        await db_exec(
            "INSERT INTO projects (name, path, main_file, python, status, created_at, last_deploy, deploy_count) "
            "VALUES (?, ?, ?, ?, 'stopped', ?, ?, 1)",
            (name, "", main_file, python, now(), now()),
        )
        row = await db_fetchone("SELECT id FROM projects WHERE name=?", (name,))
        pid = row["id"]
        pdir = project_dir(pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "host_logs").mkdir(parents=True, exist_ok=True)
        (pdir / "backups").mkdir(parents=True, exist_ok=True)
        (pdir / "._versions").mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            fname = sanitize_filename(fname)
            if not fname:
                continue
            if fname == ".env":
                env_map = _parse_env_file(content if isinstance(content, bytes)
                                          else content.encode("utf-8", errors="replace"))
                if env_map:
                    for k, v in env_map.items():
                        await db_exec(
                            "INSERT OR REPLACE INTO env_vars (project_id, key, value) VALUES (?, ?, ?)",
                            (pid, k, v),
                        )
                    await project_activity(pid, "Env Imported", f"{len(env_map)} variables from .env")
                continue
            dest = pdir / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                dest.write_bytes(content.encode("utf-8"))
            else:
                dest.write_bytes(content)
        await fs_save_project(pid, pdir)
        await set_project_field(pid, "path", str(pdir))
        await set_project_field(pid, "main_file", main_file)
        await set_project_field(pid, "python", python)
        await db_exec(
            "INSERT INTO deploy_history (project_id, version, type, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (pid, 1, dtype, f"Initial deploy of {main_file}", now()),
        )
        await counter_incr("projects_created")
        await counter_incr("deploys_total")
        await metrics_incr(day_str(), "deploys")
        await project_activity(pid, "Project Deployed", f"{dtype} — {name}")
        await notify("Deploy Complete", f"📤 Project Created Successfully: {name}")
        return {"ok": True, "pid": pid}
    except Exception as e:
        if pid and pdir:
            shutil.rmtree(str(pdir), ignore_errors=True)
            await db_exec("DELETE FROM projects WHERE id=?", (pid,))
        logger.exception("create_project failed")
        return {"ok": False, "error": str(e)}


def validate_python_file(content: bytes) -> bool:
    try:
        content.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def _validate_zip_members(zf: zipfile.ZipFile) -> str:
    """Return an error string, or None when the archive is safe to unpack."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        return "Archive contains too many files."
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or name.startswith("\\\\"):
            return "Unsafe ZIP archive rejected."
        if "\x00" in name:
            return "Unsafe ZIP archive rejected."
        mp = Path(name)
        if mp.is_absolute() or ".." in mp.parts:
            return "Unsafe ZIP archive rejected."
        total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            return "Archive is too large to unpack."
    return None


def zip_safe_extract(zf: zipfile.ZipFile, target: Path) -> bool:
    err = _validate_zip_members(zf)
    if err:
        return False
    try:
        zf.extractall(str(target))
        return True
    except Exception:
        return False


def _reroot_dir(base: Path) -> Path:
    """If the extracted tree is wrapped in a single top-level folder that
    holds the actual project, re-root into it (up to 3 levels deep)."""
    cur = base
    for _ in range(3):
        try:
            entries = list(cur.iterdir())
        except OSError:
            break
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        root_py = [f for f in files if f.name.lower().endswith(".py")]
        if len(dirs) == 1 and not root_py:
            cur = dirs[0]
            continue
        break
    return cur


async def scan_zip_archive(zpath: Path) -> dict:
    tmp = TMP_DIR / f"scan_{int(time.time())}_{random.randint(1000, 9999)}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(zpath), "r") as zf:
            err = _validate_zip_members(zf)
            if err:
                return {"ok": False, "error": err, "tmp": str(tmp)}
            if not zip_safe_extract(zf, tmp):
                return {"ok": False, "error": "Failed to unpack archive.", "tmp": str(tmp)}
        base = _reroot_dir(tmp)
        root_files = list_files_in(str(base))
        py_files = [f for f in root_files if f.lower().endswith(".py")]
        has_requirements = "requirements.txt" in root_files
        has_env = ".env" in root_files
        has_plugins = any(os.path.isdir(os.path.join(str(base), d))
                          for d in os.scandir(str(base)) if d.is_dir())
        found = []
        for pref in ("main.py", "run.py", "app.py", "bot.py"):
            if pref in root_files:
                found.append(pref)
        main_file = "main.py" if "main.py" in root_files else (found[0] if found else None)
        return {
            "ok": True,
            "tmp": str(tmp),
            "base": str(base),
            "files": root_files,
            "py_files": py_files,
            "has_requirements": has_requirements,
            "has_env": has_env,
            "has_plugins": has_plugins,
            "main_file": main_file,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tmp": str(tmp)}


async def load_project_files(tmpdir: str, main_file: str) -> dict:
    files = {}
    src = Path(tmpdir)
    try:
        entries = list(src.rglob("*"))
    except OSError:
        return files
    for fp in entries:
        if not fp.is_file():
            continue
        try:
            rel = os.path.relpath(str(fp), str(src))
        except ValueError:
            continue
        if ".." in Path(rel).parts:
            continue
        try:
            with open(str(fp), "rb") as fh:
                files[rel] = fh.read()
        except OSError:
            continue
    return files


def cleanup_tmp(path: str):
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def sweep_tmp(max_age_sec: float = 6 * 3600):
    """Remove stale files/dirs from TMP_DIR (leftovers from aborted flows)."""
    if not TMP_DIR.exists():
        return 0
    cutoff = time.time() - max_age_sec
    removed = 0
    for entry in list(TMP_DIR.iterdir()):
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0
        if mtime < cutoff:
            try:
                if entry.is_dir():
                    shutil.rmtree(str(entry), ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


# =====================================================================
# SCHEDULER
# =====================================================================

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_KEYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def list_schedules():
    rows = await db_fetchall("SELECT * FROM schedules ORDER BY id")
    return [dict(r) for r in rows]


async def add_schedule(jtype: str, day: str, time_str: str, project_id=None) -> dict:
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        return {"ok": False, "error": "Invalid time. Use HH:MM format."}
    try:
        h, m = time_str.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            return {"ok": False, "error": "Invalid time."}
    except ValueError:
        return {"ok": False, "error": "Invalid time."}
    await db_exec(
        "INSERT INTO schedules (type, day, time, project_id) VALUES (?, ?, ?, ?)",
        (jtype, day, time_str, project_id),
    )
    return {"ok": True}


async def delete_schedule(sid: int):
    await db_exec("DELETE FROM schedules WHERE id=?", (sid,))


def schedule_due(sched: dict) -> bool:
    day_key = datetime.datetime.now().strftime("%a")
    now_t = datetime.datetime.now()
    try:
        h, m = sched["time"].split(":")
        target = now_t.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    except Exception:
        return False
    if sched["day"] == "daily":
        return now_t >= target and (now_t - target).total_seconds() < SCHEDULE_INTERVAL + 5 and sched.get("last_run") != day_str()
    idx = WEEKDAY_KEYS.index(day_key)
    if WEEKDAYS[idx] != sched["day"]:
        return False
    return now_t >= target and (now_t - target).total_seconds() < SCHEDULE_INTERVAL + 5 and sched.get("last_run") != day_str()


async def scheduler_tick():
    schedules = await list_schedules()
    for s in schedules:
        if not s["enabled"]:
            continue
        if not schedule_due(s):
            continue
        await db_exec("UPDATE schedules SET last_run=? WHERE id=?", (day_str(), s["id"]))
        try:
            if s["type"] == "restart":
                proj = await get_project(s["project_id"]) if s["project_id"] else None
                if proj:
                    await restart_project(proj)
                    await notify("Restart Complete", f"🔄 Scheduled restart: {proj['name']}")
            elif s["type"] == "backup":
                if s["project_id"]:
                    proj = await get_project(s["project_id"])
                    if proj:
                        await create_backup(proj["id"])
                else:
                    for proj in await list_projects():
                        await create_backup(proj["id"])
        except Exception as e:
            logger.error("scheduler tick error: %s", e)


# =====================================================================
# SCREEN RENDERERS
# =====================================================================

async def owner_display_name() -> str:
    if OWNER_ID:
        row = await db_fetchone("SELECT name FROM users WHERE telegram_id=?", (OWNER_ID,))
        if row and row[0]:
            return row[0]
    return "Owner"


async def r_home(p):
    stats, indicators, owner = await asyncio.gather(
        global_stats(), build_indicators("home"), owner_display_name()
    )
    du = hosting_storage()
    lines = ["🚀 Telegram Hosting Panel", "━━━━━━━━━━━━━━━━━━━"]
    lines.append(f"👤 {owner} — Personal")
    lines.append("")
    lines.extend(indicators)
    lines.append("")
    lines.append(f"📦 Total Projects: {stats['total']}")
    lines.append(f"🟢 Running: {stats['running']}")
    lines.append(f"🔴 Stopped: {stats['stopped']}")
    if du:
        lines.append(f"💾 Total Storage: {human_size(du[1])} | Used: {human_size(du[2])}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("Choose an option below.")
    rows = [
        [B("📤 Deploy Project", "deploy_menu_open"), B("📂 My Projects", "nav", screen="my_projects", params={"page": 1})],
        [B("📊 Resource Monitor", "nav", screen="resource_monitor", params={}), B("💾 Backups", "nav", screen="global_backups", params={})],
        [B("🔔 Notifications", "nav", screen="notifications", params={"page": 1}), B("🖥 System Dashboard", "nav", screen="system_dash", params={})],
        [B("⚙ Settings", "nav", screen="global_settings", params={})],
    ]
    return "\n".join(lines), rows


async def r_deploy_menu(p):
    text = ("📤 Deploy Project\n\nSelect Deployment Type:\n\n"
            "📄 Single File — deploy one Python file\n"
            "📦 ZIP Project — deploy a full project archive\n"
            "📥 Import Project — import a ZIP from another hosting")
    rows = [
        [B("📄 Single File", "deploy_ask", dtype="single"), B("📦 ZIP Project", "deploy_ask", dtype="zip")],
        [B("📥 Import Project", "deploy_ask", dtype="import")],
        [B("⬅ Back", "nav", screen="home", params={})],
    ]
    return text, rows


async def r_my_projects(p):
    page = int(p.get("page", 1))
    projects = await list_projects()
    items = []
    for pr in projects:
        label = f"{pr['name']} {STATUS_ICON.get(pr['status'], '🔴 Stopped')}"
        items.append(B(label, "project_open", pid=pr["id"]))
    page_items, page, total_pages = paginate(items, page, PROJECTS_PER_PAGE)
    lines = ["📂 My Projects", ""]
    if not projects:
        lines.append("No projects deployed yet.")
        lines.append("Use 📤 Deploy Project to create one.")
    else:
        lines.append(f"Page {page} / {total_pages}")
        lines.append("────────────")
        for it in page_items:
            lines.append(it["label"])
        lines.append("────────────")
    rows = [[B("🔍 Search Project", "search_ask"), B("🔧 Batch", "batch_menu")]]
    for it in page_items:
        rows.append([it])
    if total_pages > 1:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "nav", screen="my_projects", params={"page": page - 1}))
        navrow.append(B(f"📄 Page {page} / {total_pages}", "noop"))
        if page < total_pages:
            navrow.append(B("➡ Next", "nav", screen="my_projects", params={"page": page + 1}))
        rows.append(navrow)
    rows.append([B("⬅ Back", "nav", screen="home", params={})])
    return "\n".join(lines), rows


async def r_batch_menu(p):
    lines = ["🔧 Batch Operations", "", "Choose a batch action to run on multiple projects:"]
    rows = [
        [B("🔄 Restart Selected", "batch_select", action="restart", page=1, selected=[])],
        [B("💾 Backup Selected", "batch_select", action="backup", page=1, selected=[])],
        [B("⬅ Back", "nav", screen="my_projects", params={"page": 1})],
    ]
    return "\n".join(lines), rows


async def r_batch_select(p):
    action = p.get("action", "restart")
    selected = list(p.get("selected", []))
    page = int(p.get("page", 1))
    projects = await list_projects()
    items = []
    for pr in projects:
        mark = "✅ " if pr["id"] in selected else ""
        items.append(B(f"{mark}{pr['name']} {STATUS_ICON.get(pr['status'], '🔴 Stopped')}",
                       "batch_toggle", action=action, pid=pr["id"], selected=selected, page=page))
    page_items, page, total_pages = paginate(items, page, PROJECTS_PER_PAGE)
    label = "🔄 Restart" if action == "restart" else "💾 Backup"
    lines = [f"{label} — Select Projects", "", f"Selected: {len(selected)}", f"Page {page} / {total_pages}"]
    rows = []
    for it in page_items:
        rows.append([it])
    if total_pages > 1:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "batch_page", action=action, selected=selected, page=page - 1))
        navrow.append(B(f"📄 {page} / {total_pages}", "noop"))
        if page < total_pages:
            navrow.append(B("➡ Next", "batch_page", action=action, selected=selected, page=page + 1))
        rows.append(navrow)
    rows.append([B(f"✅ {label} ({len(selected)})", "batch_run_ask", action=action, selected=selected)])
    rows.append([B("⬅ Back", "batch_menu")])
    return "\n".join(lines), rows


async def r_project_dash(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    proj = await enrich_project(proj)
    indicators = await build_indicators("project", proj)
    lines = [f"📦 {proj['name']} Dashboard", "━━━━━━━━━━━━"]
    lines.append(f"Status: {STATUS_ICON.get(proj['status'], '🔴 Stopped')}")
    lines.append(f"Python: {proj['python']}")
    lines.append(f"Main File: {proj['main_file']}")
    lines.append(f"Storage: {human_size(proj['storage'])}")
    lines.append(f"Uptime: {human_duration(proj['uptime']) if proj['uptime'] else 'N/A'}")
    lines.append(f"Last Restart: {relative_time(proj['last_restart'])}")
    req_status = proj.get("req_status")
    if req_status:
        req_icon = {"ok": "✅ Installed", "failed": "❌ Failed",
                    "installing": "🔄 Installing"}.get(req_status, req_status)
        lines.append(f"Requirements: {req_icon}")
    lines.append("")
    lines.extend(indicators)
    rows = [
        [B("▶ Start", "start_proj", pid=pid), B("⏹ Stop", "stop_ask", pid=pid)],
        [B("🔄 Restart", "restart_proj", pid=pid), B("📁 Files", "nav", screen="files", params={"pid": pid, "page": 1})],
        [B("📜 Logs", "nav", screen="logs", params={"pid": pid}), B("📊 Resources", "nav", screen="resources", params={"pid": pid})],
        [B("⚙ Settings", "nav", screen="settings_menu", params={"pid": pid}), B("💾 Backup", "nav", screen="backups_menu", params={"pid": pid})],
        [B("📁 Project Info", "nav", screen="project_info", params={"pid": pid}), B("🗑 Delete", "delete_project_ask", pid=pid)],
        [B("⬅ Back", "nav", screen="my_projects", params={"page": 1})],
    ]
    return "\n".join(lines), rows


async def r_project_info(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    lines = [f"📁 {proj['name']} — Project Information", ""]
    lines.append(f"Project Name: {proj['name']}")
    lines.append(f"Python: {proj['python']}")
    lines.append(f"Main File: {proj['main_file']}")
    lines.append(f"Created: {fmt_date(proj['created_at'])}")
    lines.append(f"Last Deploy: {relative_time(proj['last_deploy'])}")
    lines.append(f"Last Backup: {relative_time(proj['last_backup'])}")
    lines.append(f"Status: {STATUS_ICON.get(proj['status'], '🔴 Stopped')}")
    rows = [
        [B("📦 Deploy History", "nav", screen="deploy_history", params={"pid": pid}),
         B("♻ Recovery", "nav", screen="recovery", params={"pid": pid})],
        [B("⬅ Back", "nav", screen="project_dash", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_deploy_history(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    entries = await project_deploy_history(pid)
    lines = [f"📦 {proj['name']} — Deploy History", ""]
    if not entries:
        lines.append("No deploy history yet.")
    for e in entries:
        lines.append(f"v{e['version']} — {fmt_dt(e['created_at'])} ({e['type']})")
        if e["detail"]:
            lines.append(f"   {e['detail']}")
    rows = [[B("⬅ Back", "nav", screen="project_info", params={"pid": pid})]]
    return "\n".join(lines), rows


async def r_recovery(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    fname = proj["main_file"]
    versions = await list_previous_versions(pid, fname)
    lines = [f"♻ Recovery — {proj['name']}", "",
             f"Restore a previous version of {fname}:", ""]
    if not versions:
        lines.append("No previous versions available for the main file.")
    else:
        for v in versions[:8]:
            lines.append(f"{relative_time(v['ts'])} — {human_size(v['size'])}")
    lines.append("")
    lines.append("Or restore the whole project from the latest backup.")
    rows = []
    for v in versions[:8]:
        rows.append([B(f"♻ {relative_time(v['ts'])} — {human_size(v['size'])}",
                       "recovery_ask", pid=pid, fname=fname, version=v["name"])])
    if versions:
        rows.append([B("📦 Latest Backup", "recovery_latest_ask", pid=pid)])
    rows.append([B("⬅ Back", "nav", screen="project_info", params={"pid": pid})])
    return "\n".join(lines), rows


async def r_files(p):
    pid = int(p["pid"])
    page = int(p.get("page", 1))
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    files = list_files_in(str(project_dir(pid)))
    items = [B(f"📄 {f}", "file_open", pid=pid, fname=f) for f in files]
    page_items, page, total_pages = paginate(items, page, FILES_PER_PAGE)
    lines = [f"📁 {proj['name']} Files", "Choose the file you want to manage.", ""]
    if not files:
        lines.append("No files in this project yet.")
    else:
        lines.append(f"Page {page} / {total_pages}")
    rows = []
    for it in page_items:
        rows.append([it])
    rows.append([B("📤 Upload", "upload_ask", pid=pid)])
    if total_pages > 1:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "nav", screen="files", params={"pid": pid, "page": page - 1}))
        navrow.append(B(f"📄 Page {page} / {total_pages}", "noop"))
        if page < total_pages:
            navrow.append(B("➡ Next", "nav", screen="files", params={"pid": pid, "page": page + 1}))
        rows.append(navrow)
    rows.append([B("⬅ Back", "nav", screen="project_dash", params={"pid": pid})])
    return "\n".join(lines), rows


async def r_file_info(p):
    pid = int(p["pid"])
    fname = p["fname"]
    path = file_path_for(pid, fname)
    ext = os.path.splitext(fname)[1].lower()
    lang = LANG_BY_EXT.get(ext, "Unknown")
    lines_val = count_lines(str(path)) if path.exists() else 0
    size = os.path.getsize(str(path)) if path.exists() else 0
    mtime = file_modified(str(path))
    lines = [f"📄 {fname}", ""]
    lines.append(f"Language: {lang}")
    lines.append(f"Lines: {lines_val}")
    lines.append(f"Size: {human_size(size)}")
    lines.append(f"Last Modified: {relative_time(mtime)}")
    rows = [
        [B("🔄 Replace", "replace_ask", pid=pid, fname=fname), B("⬇ Download", "file_download", pid=pid, fname=fname)],
        [B("✏ Rename", "rename_ask", pid=pid, fname=fname), B("🗑 Delete", "file_delete_ask", pid=pid, fname=fname)],
        [B("⬅ Back", "nav", screen="files", params={"pid": pid, "page": 1})],
    ]
    return "\n".join(lines), rows


async def r_logs(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    lines = [f"📜 {proj['name']} — Logs", "", "Select a log type:"]
    rows = [
        [B("🟢 Runtime Log", "nav", screen="log_view", params={"pid": pid, "logtype": "runtime"}),
         B("🔴 Error Log", "nav", screen="log_view", params={"pid": pid, "logtype": "error"})],
        [B("📦 Deploy Log", "nav", screen="log_view", params={"pid": pid, "logtype": "deploy"})],
        [B("⬅ Back", "nav", screen="project_dash", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_log_view(p):
    pid = int(p["pid"])
    logtype = p.get("logtype", "runtime")
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    names = {"runtime": "Runtime", "error": "Error", "deploy": "Deploy"}
    content = await tail_log(pid, logtype)
    lines = [f"📜 {proj['name']} — {names.get(logtype, logtype)} Log", "```"]
    for line in content.splitlines():
        lines.append(truncate(line, 400))
    lines.append("```")
    rows = [
        [B("🔄 Refresh", "log_refresh", pid=pid, logtype=logtype), B("⬇ Download", "log_download", pid=pid, logtype=logtype)],
        [B("🗑 Clear", "log_clear_ask", pid=pid, logtype=logtype), B("⬅ Back", "nav", screen="logs", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_resources(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    proj = await enrich_project(proj)
    mem = 0
    cpu = 0.0
    if proj["status"] == "running" and proj.get("pid"):
        mem = await process_rss(proj["pid"])
        cpu = await process_cpu(proj["pid"])
    lines = [f"📊 {proj['name']} — Resources", ""]
    lines.append(f"CPU: {cpu:.0f}%")
    lines.append(f"RAM: {human_size(mem)}")
    lines.append(f"Storage: {human_size(proj['storage'])}")
    lines.append(f"Runtime: Python {proj['python']}")
    lines.append(f"Uptime: {human_duration(proj['uptime']) if proj['uptime'] else 'N/A'}")
    rows = [
        [B("🔄 Refresh", "nav", screen="resources", params={"pid": pid}), B("⬅ Back", "nav", screen="project_dash", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_resource_monitor(p):
    summary = await resource_summary()
    lines = ["📊 Resource Monitor", ""]
    if not summary:
        lines.append("No projects yet.")
    for i, item in enumerate(summary):
        lines.append(f"{item['name']}")
        lines.append(f"RAM: {human_size(item['ram'])}")
        lines.append(f"CPU: {item['cpu']:.0f}%")
        if i != len(summary) - 1:
            lines.append("────────────")
    rows = [
        [B("🔄 Refresh", "nav", screen="resource_monitor", params={}), B("⬅ Back", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


async def r_backups_menu(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    backups, total = await list_backups(pid, 1, 100)
    latest = backups[0] if backups else None
    lines = [f"💾 {proj['name']} — Backup", ""]
    if latest:
        lines.append(f"Latest Backup: {relative_time(latest['created_at'])}")
        lines.append(f"Size: {human_size(latest['size'])}")
    else:
        lines.append("Latest Backup: Never")
    lines.append(f"Total Backups: {total}")
    rows = [
        [B("💾 Create Backup", "backup_create", pid=pid), B("♻ Restore", "backup_list", pid=pid, action="restore", page=1)],
        [B("⬇ Download", "backup_list", pid=pid, action="download", page=1), B("🗑 Delete", "backup_list", pid=pid, action="delete", page=1)],
        [B("📤 Export Project", "export_project", pid=pid)],
        [B("⬅ Back", "nav", screen="project_dash", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_backup_list(p):
    pid = int(p["pid"])
    action = p.get("action", "restore")
    page = int(p.get("page", 1))
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    backups, total = await list_backups(pid, page, BACKUPS_PER_PAGE)
    labels = {"restore": "♻ Restore", "download": "⬇ Download", "delete": "🗑 Delete"}
    lines = [f"{labels.get(action, action)} — {proj['name']}", "", "Select a backup:"]
    if not backups:
        lines.append("No backups yet.")
    else:
        lines.append(f"Page {page} / {max(1, math.ceil(total / BACKUPS_PER_PAGE))}")
    handler = {"restore": "backup_restore_ask", "download": "backup_download_pick", "delete": "backup_delete_ask"}[action]
    rows = []
    for b in backups:
        rows.append([B(f"📦 {b['name']} — {human_size(b['size'])} — {relative_time(b['created_at'])}",
                       handler, pid=pid, bid=b["id"])])
    if total > BACKUPS_PER_PAGE:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "backup_page", pid=pid, action=action, page=page - 1))
        navrow.append(B(f"📄 {page} / {max(1, math.ceil(total / BACKUPS_PER_PAGE))}", "noop"))
        if page * BACKUPS_PER_PAGE < total:
            navrow.append(B("➡ Next", "backup_page", pid=pid, action=action, page=page + 1))
        rows.append(navrow)
    rows.append([B("⬅ Back", "nav", screen="backups_menu", params={"pid": pid})])
    return "\n".join(lines), rows


async def r_settings_menu(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    lines = [f"⚙ {proj['name']} — Settings", ""]
    lines.append(f"Auto Restart: {'ON' if proj['auto_restart'] else 'OFF'}")
    lines.append(f"Python Version: {proj['python']}")
    lines.append(f"Startup File: {proj['main_file']}")
    lines.append(f"Requirements Install: {'ON' if proj['req_install'] else 'OFF'}")
    rows = [
        [B("🌐 Environment Variables", "nav", screen="env_vars", params={"pid": pid}), B("🔄 Auto Restart", "auto_restart_toggle", pid=pid)],
        [B("🐍 Python Version", "nav", screen="py_versions", params={"pid": pid}), B("🚀 Startup File", "nav", screen="startup_file", params={"pid": pid})],
        [B("📦 Requirements", "nav", screen="requirements", params={"pid": pid}), B("🧹 Delete Cache", "cache_ask", pid=pid)],
        [B("⬅ Back", "nav", screen="project_dash", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_env_vars(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    rows_map = await env_vars_for(pid)
    lines = [f"🌐 {proj['name']} — Environment Variables", ""]
    if not rows_map:
        lines.append("No environment variables set.")
    for k, v in rows_map.items():
        lines.append(f"{k} = {'••••' if v else '(empty)'}")
    rows = [[B("➕ Add Variable", "env_add_ask", pid=pid)]]
    for k in sorted(rows_map.keys()):
        rows.append([B(f"🌐 {k}", "env_var_open", pid=pid, key=k)])
    rows.append([B("⬅ Back", "nav", screen="settings_menu", params={"pid": pid})])
    return "\n".join(lines), rows


async def r_env_var_action(p):
    pid = int(p["pid"])
    key = p["key"]
    rows_map = await env_vars_for(pid)
    value = rows_map.get(key, "")
    lines = [f"🌐 {key}", "", f"Value: {'••••' if value else '(empty)'}"]
    rows = [
        [B("✏ Edit Value", "env_edit_ask", pid=pid, key=key), B("🗑 Delete", "env_var_delete_ask", pid=pid, key=key)],
        [B("⬅ Back", "nav", screen="env_vars", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_py_versions(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    avail = available_python_versions()
    lines = [f"🐍 {proj['name']} — Python Version", "",
             f"Current: {proj['python']}", "", "Select a version:"]
    rows = []
    for ver, _exe in avail:
        mark = "✅ " if ver == proj["python"] else ""
        rows.append([B(f"{mark}Python {ver}", "py_ver_select", pid=pid, ver=ver)])
    rows.append([B("⬅ Back", "nav", screen="settings_menu", params={"pid": pid})])
    return "\n".join(lines), rows


async def r_startup_file(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    files = list_files_in(str(project_dir(pid)))
    py_files = [f for f in files if f.lower().endswith(".py")]
    lines = [f"🚀 {proj['name']} — Startup File", "", f"Current: {proj['main_file']}", ""]
    rows = []
    for f in py_files:
        mark = "✅ " if f == proj["main_file"] else ""
        rows.append([B(f"{mark}🐍 {f}", "startup_select", pid=pid, fname=f)])
    if not py_files:
        for f in files:
            rows.append([B(f"📄 {f}", "startup_select", pid=pid, fname=f)])
    if not rows:
        lines.append("No files found in this project.")
    rows.append([B("⬅ Back", "nav", screen="settings_menu", params={"pid": pid})])
    return "\n".join(lines), rows


async def r_requirements(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    req_exists = (project_dir(pid) / "requirements.txt").exists()
    lines = [f"📦 {proj['name']} — Requirements", ""]
    if req_exists:
        lines.append("requirements.txt: Present")
    else:
        lines.append("requirements.txt: Missing")
    lines.append(f"Auto Install on Start: {'ON' if proj['req_install'] else 'OFF'}")
    rows = [
        [B("🔄 Toggle Auto Install", "req_toggle", pid=pid), B("📥 Install Now", "req_install_now", pid=pid)],
        [B("⬅ Back", "nav", screen="settings_menu", params={"pid": pid})],
    ]
    return "\n".join(lines), rows


async def r_notifications(p):
    page = int(p.get("page", 1))
    notifs, total = await notification_list(page)
    total_pages = max(1, math.ceil(total / NOTIFS_PER_PAGE))
    lines = ["🔔 Notifications", ""]
    if not notifs:
        lines.append("No notifications yet.")
    else:
        lines.append(f"Page {page} / {total_pages}")
        for n in notifs:
            icon = NOTIF_ICONS.get(n["type"], "📌")
            lines.append(f"[{fmt_time(n['created_at'])}] {icon} {n['message']}")
    rows = [[B("🗑 Clear All", "notif_clear_ask")]]
    if total_pages > 1:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "nav", screen="notifications", params={"page": page - 1}))
        navrow.append(B(f"📄 {page} / {total_pages}", "noop"))
        if page < total_pages:
            navrow.append(B("➡ Next", "nav", screen="notifications", params={"page": page + 1}))
        rows.append(navrow)
    rows.append([B("⬅ Back", "nav", screen="home", params={})])
    return "\n".join(lines), rows


async def r_system_dash(p):
    stats = await global_stats()
    indicators = await build_indicators("system")
    lines = ["🖥 System Dashboard", "━━━━━━━━━━━━━━━━"]
    lines.append(f"🟢 Hosting Status: {await hosting_status_line()}")
    lines.append(f"⏱ Uptime: {human_duration(stats['uptime_host'])}")
    lines.append(f"📦 Total Projects: {stats['total']}")
    lines.append(f"🟢 Running: {stats['running']}")
    lines.append(f"🔴 Stopped: {stats['stopped']}")
    lines.append("")
    lines.extend(indicators)
    rows = [
        [B("📊 Live Resources", "nav", screen="live_resources", params={}), B("📂 Storage", "nav", screen="storage_info", params={})],
        [B("📈 Statistics", "nav", screen="statistics", params={}), B("📋 System Logs", "nav", screen="system_logs", params={"page": 1})],
        [B("📈 Usage History", "nav", screen="usage_history", params={}), B("💡 Health", "nav", screen="health", params={})],
        [B("📦 Distribution", "nav", screen="distribution", params={}), B("📈 Activity", "nav", screen="activity", params={"page": 1})],
        [B("⚙ System Info", "nav", screen="system_info", params={}), B("🔄 Refresh", "sys_refresh", screen="system_dash")],
        [B("⬅ Back", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


async def r_live_resources(p):
    cpu = system_cpu()
    ram_used, ram_total = system_ram()
    du = hosting_storage()
    net = await network_status()
    lines = ["📊 Live Resources", ""]
    lines.append(f"CPU: {bar(cpu)} {cpu:.0f}%")
    if ram_total:
        lines.append(f"RAM: {bar(ram_used / ram_total * 100)} {human_size(ram_used)} / {human_size(ram_total)}")
    else:
        lines.append("RAM: N/A")
    if du:
        # Panel data dir real usage + host volume free space.
        used = await adir_size(str(DATA_DIR))
        free = du[2]
        total_usable = used + free
        pct = (used / total_usable * 100) if total_usable else 0
        lines.append(f"Panel Data: {bar(pct)} {human_size(used)}")
        lines.append(f"Host Volume: {human_size(free)} free")
    else:
        lines.append("Storage: N/A")
    net_icon = {"Stable": "🟢 Connected", "Slow": "🟡 Slow", "Offline": "🔴 Offline"}.get(net, net)
    lines.append(f"Network: {net_icon}")
    rows = [
        [B("🔄 Refresh", "nav", screen="live_resources", params={}), B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_storage_info(p):
    order = p.get("order", "desc")
    du = hosting_storage()
    projects = await list_projects()
    sized = []
    for pr in projects:
        sized.append((pr["name"], await adir_size(str(project_dir(pr["id"])))))
    sized.sort(key=lambda x: x[1], reverse=(order == "desc"))
    lines = ["📂 Storage Information", ""]
    if du:
        used = await adir_size(str(DATA_DIR))
        free = du[2]
        lines.append(f"Panel Data Used: {human_size(used)}")
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"Host Free Space: {human_size(free)}")
    lines.append("")
    lines.append("Largest Projects:")
    for name, size in sized:
        lines.append(f"{name} — {human_size(size)}")
    rows = [
        [B("🔃 Sort By Size", "sort_by_size", order="asc" if order == "desc" else "desc"),
         B("🔄 Refresh", "nav", screen="storage_info", params={"order": order})],
        [B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_statistics(p):
    stats = await global_stats()
    mt = await metrics_today()
    lines = ["📈 Statistics", ""]
    lines.append(f"Projects Created: {stats['projects_created']}")
    lines.append(f"Running: {stats['running']}")
    lines.append(f"Stopped: {stats['stopped']}")
    lines.append(f"Restart Today: {mt['restarts']}")
    lines.append(f"Backups Created: {stats['backups_created']}")
    lines.append(f"File Replacements: {stats['file_replacements']}")
    lines.append(f"Deploys Today: {stats['deploys_today']}")
    rows = [
        [B("🔄 Refresh", "nav", screen="statistics", params={}), B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_system_info(p):
    db_ok = await db_status()
    du = hosting_storage()
    lines = ["⚙ System Information", ""]
    lines.append(f"Python: {sys.version.split()[0]}")
    lines.append(f"Hosting Backend: {'🟢 Online' if await backend_status() != 'Offline' else '🔴 Offline'}")
    lines.append(f"Database: {'🟢 Connected' if db_ok else '🔴 Offline'}")
    lines.append(f"Storage: {'🟢 Healthy' if (du and du[3] < STORAGE_WARN_PCT) else ('🟡 Warning' if du else 'N/A')}")
    lines.append(f"Runtime: {'🟢 Running' if await runtime_indicator() == 'Active' else '🔴 Stopped'}")
    rows = [
        [B("🔄 Refresh", "nav", screen="system_info", params={}), B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_usage_history(p):
    mt = await metrics_today()
    lines = ["📈 Usage History — Today", ""]
    lines.append(f"CPU Peak: {mt['cpu_peak']:.0f}%")
    lines.append(f"RAM Peak: {human_size(mt['ram_peak'])}")
    initial = mt["storage_initial"]
    projs = await list_projects()
    current = sum(await asyncio.gather(*[adir_size(str(project_dir(pr["id"]))) for pr in projs])) if projs else 0
    increased = max(0, current - initial) if initial else 0
    lines.append(f"Storage Increased: {human_size(increased)}")
    lines.append(f"Deploys: {mt['deploys']}")
    lines.append(f"Restarts: {mt['restarts']}")
    rows = [
        [B("🔄 Refresh", "nav", screen="usage_history", params={}), B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_system_logs(p):
    page = int(p.get("page", 1))
    rows_db = await db_fetchall(
        "SELECT * FROM system_logs ORDER BY id DESC LIMIT ? OFFSET ?",
        (LIST_PER_PAGE, (page - 1) * LIST_PER_PAGE),
    )
    total_row = await db_fetchone("SELECT COUNT(*) AS c FROM system_logs")
    total = total_row["c"] if total_row else 0
    total_pages = max(1, math.ceil(total / LIST_PER_PAGE))
    lines = ["📋 System Logs", ""]
    if not rows_db:
        lines.append("No system logs yet.")
    else:
        lines.append(f"Page {page} / {total_pages}")
        for r in rows_db:
            lines.append(f"[{fmt_dt(r['created_at'])}] {truncate(r['message'], 150)}")
    rows = []
    if total_pages > 1:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "nav", screen="system_logs", params={"page": page - 1}))
        navrow.append(B(f"📄 {page} / {total_pages}", "noop"))
        if page < total_pages:
            navrow.append(B("➡ Next", "nav", screen="system_logs", params={"page": page + 1}))
        rows.append(navrow)
    rows.append([B("⬅ Back", "nav", screen="system_dash", params={})])
    return "\n".join(lines), rows


async def r_activity(p):
    page = int(p.get("page", 1))
    rows_db = await db_fetchall(
        "SELECT * FROM activity ORDER BY id DESC LIMIT ? OFFSET ?",
        (LIST_PER_PAGE, (page - 1) * LIST_PER_PAGE),
    )
    total_row = await db_fetchone("SELECT COUNT(*) AS c FROM activity")
    total = total_row["c"] if total_row else 0
    total_pages = max(1, math.ceil(total / LIST_PER_PAGE))
    lines = ["📈 Activity History", ""]
    if not rows_db:
        lines.append("No activity recorded yet.")
    else:
        lines.append(f"Page {page} / {total_pages}")
        for r in rows_db:
            lines.append(f"[{fmt_time(r['created_at'])}] {r['action']}{(' — ' + r['detail']) if r['detail'] else ''}")
    rows = []
    if total_pages > 1:
        navrow = []
        if page > 1:
            navrow.append(B("⬅ Previous", "nav", screen="activity", params={"page": page - 1}))
        navrow.append(B(f"📄 {page} / {total_pages}", "noop"))
        if page < total_pages:
            navrow.append(B("➡ Next", "nav", screen="activity", params={"page": page + 1}))
        rows.append(navrow)
    rows.append([B("⬅ Back", "nav", screen="system_dash", params={})])
    return "\n".join(lines), rows


async def r_health(p):
    du = hosting_storage()
    db_ok = await db_status()
    score = 100.0
    reasons = []
    if not db_ok:
        score -= 30
        reasons.append("Database disconnected")
    if du:
        if du[3] >= STORAGE_CRIT_PCT:
            score -= 25
            reasons.append("Storage critical")
        elif du[3] >= STORAGE_WARN_PCT:
            score -= 10
            reasons.append("Storage warning")
    if await network_status() == "Offline":
        score -= 10
        reasons.append("Network offline")
    score = max(0, score)
    status = "Excellent" if score >= 90 else ("Good" if score >= 75 else ("Warning" if score >= 50 else "Critical"))
    lines = ["💡 System Health", ""]
    lines.append(f"Hosting Health: {score:.0f}%")
    lines.append(f"Status: {status}")
    if reasons:
        lines.append("")
        lines.append("Issues:")
        lines.extend(f"• {r}" for r in reasons)
    rows = [
        [B("🔄 Refresh", "nav", screen="health", params={}), B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_distribution(p):
    stats = await global_stats()
    lines = ["📦 Project Distribution", ""]
    lines.append(f"Python Bots: {stats['total']}")
    lines.append(f"Inactive: {stats['stopped'] + stats['error']}")
    lines.append(f"Total: {stats['total']}")
    rows = [
        [B("🔄 Refresh", "nav", screen="distribution", params={}), B("⬅ Back", "nav", screen="system_dash", params={})],
    ]
    return "\n".join(lines), rows


async def r_global_settings(p):
    lines = ["⚙ Settings", ""]
    lines.append("Hosting-wide configuration:")
    rows = [
        [B("👤 Users", "nav", screen="users", params={}), B("📅 Scheduler", "nav", screen="scheduler", params={})],
        [B("⬅ Back", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


async def r_scheduler(p):
    schedules = await list_schedules()
    lines = ["📅 Scheduler", ""]
    if not schedules:
        lines.append("No scheduled jobs.")
    else:
        for s in schedules:
            if s["type"] == "restart":
                proj = await get_project(s["project_id"]) if s["project_id"] else None
                target = proj["name"] if proj else "?"
                desc = f"🔄 Restart {target}"
            else:
                if s["project_id"]:
                    proj = await get_project(s["project_id"])
                    desc = f"💾 Backup {proj['name'] if proj else '?'}"
                else:
                    desc = "💾 Backup all projects"
            day = "Every Day" if s["day"] == "daily" else s["day"].title()
            lines.append(f"{desc} — {day} {s['time']}")
    rows = [[B("➕ Add Schedule", "sched_add")]]
    for s in schedules:
        rows.append([B(f"🗑 Remove: {s['type']} {s['time']}", "sched_delete_ask", sid=s["id"])])
    rows.append([B("⬅ Back", "nav", screen="global_settings", params={})])
    return "\n".join(lines), rows


async def r_sched_type(p):
    lines = ["📅 Add Schedule", "", "Select the job type:"]
    rows = [
        [B("🔄 Restart", "sched_day_ask", jtype="restart"), B("💾 Backup", "sched_day_ask", jtype="backup")],
        [B("⬅ Back", "nav", screen="scheduler", params={})],
    ]
    return "\n".join(lines), rows


async def r_sched_day(p):
    jtype = p["jtype"]
    lines = ["📅 Add Schedule", "", "Select the frequency:"]
    rows = [
        [B("🗓 Every Day", "sched_project_ask", jtype=jtype, day="daily")],
    ]
    for key in WEEKDAYS:
        rows.append([B(key.title(), "sched_project_ask", jtype=jtype, day=key)])
    rows.append([B("⬅ Back", "nav", screen="sched_type", params={})])
    return "\n".join(lines), rows


async def r_sched_project(p):
    jtype = p.get("jtype", "restart")
    day = p.get("day", "daily")
    projects = await list_projects()
    lines = ["📅 Add Schedule", "", "Select the project:"]
    rows = []
    if jtype == "backup":
        rows.append([B("📦 All Projects", "sched_all", jtype=jtype, day=day)])
    for pr in projects:
        rows.append([B(pr["name"], "sched_project_pick", jtype=jtype, day=day, pid=pr["id"])])
    if not projects:
        lines.append("No projects available.")
    rows.append([B("⬅ Back", "nav", screen="sched_day", params={"jtype": jtype})])
    return "\n".join(lines), rows


async def r_users(p):
    users = await list_users()
    lines = ["👤 Users", "", "Telegram ID — Role"]
    for u in users:
        lines.append(f"{u['telegram_id']} — {u['role'].title()}")
    rows = [[B("➕ Add User", "user_add_ask")]]
    for u in users:
        if u["role"] != "owner":
            rows.append([B(f"⚙ {u['telegram_id']}", "user_action", tid=u["telegram_id"])])
    rows.append([B("⬅ Back", "nav", screen="global_settings", params={})])
    return "\n".join(lines), rows


async def r_global_backups(p):
    projects = await list_projects()
    lines = ["💾 Backups", ""]
    if not projects:
        lines.append("No projects yet.")
    total_size = 0
    total_count = 0
    for pr in projects:
        backups, cnt = await list_backups(pr["id"], 1, 1000)
        if backups:
            size = sum(b["size"] for b in backups)
            total_size += size
            total_count += cnt
            lines.append(f"{pr['name']} — {cnt} backup(s) — {human_size(size)}")
    if total_count == 0:
        lines.append("No backups found. Open a project → Backup to create one.")
    lines.append("")
    lines.append(f"Total Backups: {total_count}")
    lines.append(f"Total Size: {human_size(total_size)}")
    rows = [[B("⬅ Back", "nav", screen="home", params={})]]
    return "\n".join(lines), rows


async def r_deploy_summary(p):
    data = p.get("data", {})
    lines = ["📦 Project Summary", ""]
    lines.append(f"Project Name: {data.get('name', '?')}")
    lines.append(f"Main File: {data.get('main_file', '?')}")
    lines.append(f"Python: {data.get('python', '?')}")
    lines.append("Status: Ready")
    rows = [
        [B("✅ Deploy", "deploy_confirm"), B("❌ Cancel", "deploy_cancel")],
    ]
    return "\n".join(lines), rows


async def r_deploy_main_select(p):
    data = p.get("data", {})
    py_files = data.get("py_files", [])
    lines = ["Main File Not Found", "", "Select Main File:"]
    rows = []
    for f in py_files[:12]:
        rows.append([B(f"🐍 {f}", "deploy_main_pick", fname=f)])
    rows.append([B("❌ Cancel", "deploy_cancel")])
    return "\n".join(lines), rows


async def r_deploy_done(p):
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return "⚠ Project not found.", [[B("⬅ Back", "nav", screen="home", params={})]]
    lines = ["🎉 Project Created Successfully", "", f"📦 {proj['name']}", f"Main File: {proj['main_file']}", f"Python: {proj['python']}"]
    rows = [
        [B("▶ Start", "start_proj", pid=pid), B("📁 Files", "nav", screen="files", params={"pid": pid, "page": 1})],
        [B("⬅ Home", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


async def r_start_pending(p):
    return "🔄 Starting Project...\n\nLoading...", []


async def r_restart_pending(p):
    return "🔄 Restarting...\n\nStopping... Waiting... Starting...", []


async def r_start_done(p):
    return "✅ Done.\n🟢 Running", [[B("▶ Dashboard", "nav", screen="project_dash", params={"pid": p["pid"]}),
                                     B("📁 Files", "nav", screen="files", params={"pid": p["pid"], "page": 1})],
                                    [B("⬅ Home", "nav", screen="home", params={})]]


async def r_start_failed(p):
    proj = await get_project(int(p["pid"]))
    error = p.get("error", "Unknown error")
    hint = p.get("hint", "")
    lines = ["❌ Failed To Start", "", f"Project: {proj['name'] if proj else '?'}", f"Error: {error}"]
    if hint:
        lines.append(f"Hint: {hint}")
    lines.append("")
    lines.append("View Logs?")
    rows = [
        [B("📜 Logs", "nav", screen="log_view", params={"pid": p["pid"], "logtype": "runtime"}),
         B("🔄 Retry", "start_proj", pid=p["pid"])],
        [B("⏹ Stop", "stop_ask", pid=p["pid"]), B("⬅ Home", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


async def r_stop_done(p):
    proj = await get_project(int(p["pid"]))
    lines = ["🔴 Project Stopped", "", f"{proj['name'] if proj else '?'} has been safely shut down."]
    rows = [
        [B("▶ Start", "start_proj", pid=p["pid"]), B("📂 Dashboard", "nav", screen="project_dash", params={"pid": p["pid"]})],
        [B("⬅ Home", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


async def r_restart_done(p):
    proj = await get_project(int(p["pid"]))
    uptime = "00:00:00"
    if proj and proj.get("started_at"):
        uptime = duration_hhmmss(time.time() - proj["started_at"])
    lines = ["🔄 Restart Successful", "", f"New Uptime: {uptime}"]
    rows = [
        [B("📂 Dashboard", "nav", screen="project_dash", params={"pid": p["pid"]}), B("⬅ Home", "nav", screen="home", params={})],
    ]
    return "\n".join(lines), rows


SCREEN_RENDERERS = {
    "home": r_home,
    "deploy_menu": r_deploy_menu,
    "my_projects": r_my_projects,
    "batch_menu": r_batch_menu,
    "batch_select": r_batch_select,
    "project_dash": r_project_dash,
    "project_info": r_project_info,
    "deploy_history": r_deploy_history,
    "recovery": r_recovery,
    "files": r_files,
    "file_info": r_file_info,
    "logs": r_logs,
    "log_view": r_log_view,
    "resources": r_resources,
    "resource_monitor": r_resource_monitor,
    "backups_menu": r_backups_menu,
    "backup_list": r_backup_list,
    "settings_menu": r_settings_menu,
    "env_vars": r_env_vars,
    "env_var_action": r_env_var_action,
    "py_versions": r_py_versions,
    "startup_file": r_startup_file,
    "requirements": r_requirements,
    "notifications": r_notifications,
    "system_dash": r_system_dash,
    "live_resources": r_live_resources,
    "storage_info": r_storage_info,
    "statistics": r_statistics,
    "system_info": r_system_info,
    "usage_history": r_usage_history,
    "system_logs": r_system_logs,
    "activity": r_activity,
    "health": r_health,
    "distribution": r_distribution,
    "global_settings": r_global_settings,
    "scheduler": r_scheduler,
    "sched_type": r_sched_type,
    "sched_day": r_sched_day,
    "sched_project": r_sched_project,
    "users": r_users,
    "global_backups": r_global_backups,
    "deploy_summary": r_deploy_summary,
    "deploy_main_select": r_deploy_main_select,
    "deploy_done": r_deploy_done,
    "start_pending": r_start_pending,
    "restart_pending": r_restart_pending,
    "start_done": r_start_done,
    "start_failed": r_start_failed,
    "stop_done": r_stop_done,
    "restart_done": r_restart_done,
}

# =====================================================================
# WIZARD STATE
# =====================================================================

WIZ = {}


def wiz_set(user_id, stage, data=None):
    WIZ[user_id] = {"stage": stage, "data": data or {}}


def wiz_get(user_id):
    return WIZ.get(user_id)


def wiz_clear(user_id):
    WIZ.pop(user_id, None)


async def send_or_edit(context, chat_id, text, markup, msg_id=None):
    if msg_id:
        try:
            return await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                                       text=text, reply_markup=markup)
        except TelegramError:
            pass
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except TelegramError:
        return None


async def perm(update, role="admin"):
    uid = update.callback_query.from_user.id
    if not await require(uid, role):
        await update.callback_query.answer("⛔ Access Denied", show_alert=True)
        return False
    return True


async def send_document(context, chat_id, path: str, caption=None):
    if not os.path.exists(path):
        await toast(context, chat_id, "❌ File not found on disk.")
        return
    size = os.path.getsize(path)
    if size > MAX_DOWNLOAD_BYTES:
        await toast(context, chat_id, f"❌ File too large to send via Telegram (max {human_size(MAX_DOWNLOAD_BYTES)}).")
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    except TelegramError:
        pass
    try:
        with open(path, "rb") as fh:
            await context.bot.send_document(chat_id=chat_id, document=fh, caption=caption)
    except TelegramError as e:
        await toast(context, chat_id, f"❌ Failed to send file: {e}")


async def download_document(context, message, dest: str):
    file = await context.bot.get_file(message.document.file_id)
    await file.download_to_drive(custom_path=dest)
    return dest


def default_python() -> str:
    avail = available_python_versions()
    return avail[0][0] if avail else "3"


# =====================================================================
# CALLBACK HANDLERS
# =====================================================================

OWNER_HANDLERS = {"user_add_ask", "user_role_set", "user_remove_yes", "user_action"}

ADMIN_HANDLERS = {
    "deploy_menu_open", "deploy_ask", "deploy_confirm", "deploy_main_pick",
    "upload_ask", "replace_ask", "replace_running_go", "replace_confirm_yes",
    "replace_restart_yes", "file_delete_yes", "rename_confirm_yes",
    "backup_create", "backup_restore_yes", "backup_delete_yes", "export_project",
    "env_add_ask", "env_edit_ask", "env_save_confirm", "env_var_delete_yes",
    "auto_restart_toggle", "py_ver_select", "startup_select", "req_toggle",
    "req_install_now", "cache_yes", "log_clear_yes", "notif_clear_yes",
    "start_proj", "stop_yes", "restart_proj", "recovery_yes", "recovery_latest_yes",
    "batch_select", "batch_toggle", "batch_page", "batch_run_ask", "batch_run_yes",
    "sched_add", "sched_day_ask", "sched_project_pick", "sched_all",
    "sched_delete_ask", "sched_delete_yes", "search_ask", "wiz_cancel",
    "op_restart_yes", "op_restart_no",
    "batch_menu", "file_open", "backup_list", "sched_project_ask",
    "delete_project_ask", "delete_project_yes",
}


async def h_nav(update, context, p):
    q = update.callback_query
    screen = p.get("screen")
    params = p.get("params") or {}
    await show_screen(context, q.from_user.id, q.message.chat_id, screen, params,
                      edit_message_id=q.message.message_id)


async def h_screen_batch_menu(update, context, p):
    q = update.callback_query
    if not await require(q.from_user.id, "admin"):
        return await q.answer("⛔ Access Denied", show_alert=True)
    await show_screen(context, q.from_user.id, q.message.chat_id, "batch_menu", {},
                      edit_message_id=q.message.message_id)


async def h_screen_file_open(update, context, p):
    q = update.callback_query
    if not await require(q.from_user.id, "admin"):
        return await q.answer("⛔ Access Denied", show_alert=True)
    await show_screen(context, q.from_user.id, q.message.chat_id, "file_info",
                      {"pid": p["pid"], "fname": p["fname"]},
                      edit_message_id=q.message.message_id)


async def h_screen_backup_list(update, context, p):
    q = update.callback_query
    if not await require(q.from_user.id, "admin"):
        return await q.answer("⛔ Access Denied", show_alert=True)
    await show_screen(context, q.from_user.id, q.message.chat_id, "backup_list",
                      {"pid": p["pid"], "action": p.get("action", "restore"), "page": int(p.get("page", 1))},
                      edit_message_id=q.message.message_id)


async def h_screen_sched_project_ask(update, context, p):
    q = update.callback_query
    if not await require(q.from_user.id, "admin"):
        return await q.answer("⛔ Access Denied", show_alert=True)
    await show_screen(context, q.from_user.id, q.message.chat_id, "sched_project",
                      {"jtype": p.get("jtype", "restart"), "day": p.get("day", "daily")},
                      edit_message_id=q.message.message_id)


async def h_noop(update, context, p):
    await update.callback_query.answer("Updated")


async def h_wiz_cancel(update, context, p):
    q = update.callback_query
    wiz_clear(q.from_user.id)
    await show_screen(context, q.from_user.id, q.message.chat_id, "home", {},
                      edit_message_id=q.message.message_id)


async def h_deploy_menu_open(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "deploy_menu", {},
                      edit_message_id=q.message.message_id)


async def h_deploy_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    dtype = p.get("dtype", "single")
    wiz_clear(uid)
    if dtype == "single":
        wiz_set(uid, "deploy_file", {"dtype": "single"})
        text = "📄 Single File Deploy\n\nPlease send the Python file to deploy.\n\n(This is a .py file upload.)"
    else:
        wiz_set(uid, "deploy_zip", {"dtype": dtype})
        label = "📥 Import" if dtype == "import" else "📦 ZIP"
        text = f"{label} Project\n\nPlease send the ZIP archive of your project."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_deploy_confirm(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    du = hosting_storage()
    if du and du[3] >= STORAGE_CRIT_PCT:
        await q.answer("❌ Storage Full — New Deploy Blocked", show_alert=True)
        return
    state = wiz_get(uid)
    if not state or state["stage"] != "deploy_name":
        await q.answer("Deploy session expired. Start again.", show_alert=True)
        return
    data = state["data"]
    name = data.get("name")
    main_file = data.get("main_file")
    python = data.get("python")
    dtype = data.get("dtype", "single")
    files = data.get("files")
    if files is None and data.get("tmp"):
        files = await load_project_files(data.get("base") or data["tmp"], main_file)
    if not files or not main_file or not name:
        await q.answer("Deploy data is incomplete. Start again.", show_alert=True)
        return
    result = await create_project(name, files, main_file, python, dtype)
    if result["ok"]:
        cleanup_tmp(data.get("tmp"))
        wiz_clear(uid)
        await show_screen(context, uid, q.message.chat_id, "deploy_done", {"pid": result["pid"]},
                          edit_message_id=q.message.message_id)
        await refresh_open_screens(uid)
    else:
        await q.answer(f"❌ Deploy failed: {result['error']}", show_alert=True)


async def h_deploy_cancel(update, context, p):
    q = update.callback_query
    wiz_clear(q.from_user.id)
    await show_screen(context, q.from_user.id, q.message.chat_id, "home", {},
                      edit_message_id=q.message.message_id)


async def h_deploy_main_pick(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    state = wiz_get(uid)
    if not state:
        return
    data = state["data"]
    data["main_file"] = p["fname"]
    wiz_set(uid, "deploy_name", data)
    text = f"✔ Main File Selected: {p['fname']}\n\nPlease enter Project Name."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_project_open(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        await q.answer("Project not found", show_alert=True)
        return
    await show_screen(context, q.from_user.id, q.message.chat_id, "project_dash", {"pid": pid},
                      edit_message_id=q.message.message_id)


async def h_start_proj(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        await q.answer("Project not found", show_alert=True)
        return
    await show_screen(context, uid, q.message.chat_id, "start_pending", {"pid": pid},
                      edit_message_id=q.message.message_id)
    result = await run_start(proj)
    if result["ok"]:
        await notify("Project Started", f"▶ {proj['name']} started")
        await show_screen(context, uid, q.message.chat_id, "start_done", {"pid": pid},
                          edit_message_id=q.message.message_id)
    else:
        await show_screen(context, uid, q.message.chat_id, "start_failed",
                          {"pid": pid, "error": result.get("error"), "hint": result.get("hint")},
                          edit_message_id=q.message.message_id)
    await refresh_open_screens(uid)


async def h_stop_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return
    text = f"⏹ Stop {proj['name']}?"
    rows = [
        [B("✅ Yes", "stop_yes", pid=pid), B("❌ No", "nav", screen="project_dash", params={"pid": pid})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_stop_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    await send_or_edit(context, q.message.chat_id, "⏹ Stopping... (Safe Shutdown)", None, q.message.message_id)
    proj = await get_project(pid)
    if proj:
        await stop_project(proj)
    await show_screen(context, uid, q.message.chat_id, "stop_done", {"pid": pid},
                      edit_message_id=q.message.message_id)
    await refresh_open_screens(uid)


async def h_delete_project_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return
    text = (f"🗑 Delete Project\n\n⚠ This will PERMANENTLY delete '{proj['name']}' "
            f"including all its files, backups and settings.\n\nThis cannot be undone. Continue?")
    rows = [
        [B("🗑 Yes, Delete", "delete_project_yes", pid=pid),
         B("❌ No", "nav", screen="project_dash", params={"pid": pid})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_delete_project_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    result = await delete_project(pid)
    if not result["ok"]:
        await q.answer(f"❌ {result['error']}", show_alert=True)
        return
    await show_screen(context, uid, q.message.chat_id, "my_projects", {"page": 1},
                      edit_message_id=q.message.message_id)
    await q.answer("🗑 Project deleted")
    await refresh_open_screens(uid)


async def h_restart_proj(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return
    await show_screen(context, uid, q.message.chat_id, "restart_pending", {"pid": pid},
                      edit_message_id=q.message.message_id)
    result = await restart_project(proj)
    if result["ok"]:
        await notify("Restart Complete", f"🔄 Project Restarted Successfully: {proj['name']}")
        await show_screen(context, uid, q.message.chat_id, "restart_done", {"pid": pid},
                          edit_message_id=q.message.message_id)
    else:
        await show_screen(context, uid, q.message.chat_id, "start_failed",
                          {"pid": pid, "error": result.get("error"), "hint": result.get("hint")},
                          edit_message_id=q.message.message_id)
    await refresh_open_screens(uid)


async def h_log_refresh(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    logtype = p.get("logtype", "runtime")
    await show_screen(context, q.from_user.id, q.message.chat_id, "log_view",
                      {"pid": pid, "logtype": logtype}, edit_message_id=q.message.message_id)


async def h_log_download(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    logtype = p.get("logtype", "runtime")
    path = await download_log(pid, logtype)
    await send_document(context, q.message.chat_id, path, caption=f"{logtype}.log")
    await q.answer("Log sent")


async def h_log_clear_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    logtype = p.get("logtype", "runtime")
    text = f"⚠ Clear the {logtype} log?\n\nThis cannot be undone."
    rows = [
        [B("🗑 Clear", "log_clear_yes", pid=pid, logtype=logtype),
         B("❌ Cancel", "nav", screen="log_view", params={"pid": pid, "logtype": logtype})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_log_clear_yes(update, context, p):
    q = update.callback_query
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    logtype = p.get("logtype", "runtime")
    await clear_log(pid, logtype)
    await show_screen(context, q.from_user.id, q.message.chat_id, "log_view",
                      {"pid": pid, "logtype": logtype}, edit_message_id=q.message.message_id)


async def h_file_download(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    fname = p["fname"]
    path = str(file_path_for(pid, fname))
    await send_document(context, q.message.chat_id, path, caption=fname)
    await q.answer("File sent")


async def h_replace_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    fname = p["fname"]
    proj = await get_project(pid)
    if proj and proj["status"] == "running":
        text = ("⚠ The project is currently running.\n\n"
                "To replace the file safely, the project must be stopped first.\n\n"
                "Continue?")
        rows = [
            [B("✅ Stop & Replace", "replace_running_go", pid=pid, fname=fname),
             B("❌ Cancel", "nav", screen="file_info", params={"pid": pid, "fname": fname})],
        ]
    else:
        wiz_set(uid, "replace_wait_file", {"pid": pid, "fname": fname, "was_running": False})
        text = "🔄 Please send the new file that will replace the current file."
        rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_replace_running_go(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    fname = p["fname"]
    proj = await get_project(pid)
    if proj and proj["status"] == "running":
        await stop_project(proj)
    wiz_set(uid, "replace_wait_file", {"pid": pid, "fname": fname, "was_running": True})
    text = "⏹ Project stopped.\n\n🔄 Please send the new file that will replace the current file."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def _replace_confirm_ui(context, chat_id, pid, fname, tmp_path, was_running):
    proj = await get_project(pid)
    pname = proj["name"] if proj else "?"
    text = (f"🔄 Replace\n\nCurrent File: {fname}\n\n↓\n\nNew File: {fname}\n\n"
            f"Replace this file in {pname}?")
    rows = [
        [B("✅ Replace", "replace_confirm_yes", pid=pid, fname=fname, tmp=tmp_path, was_running=was_running),
         B("❌ Cancel", "nav", screen="file_info", params={"pid": pid, "fname": fname})],
    ]
    await send_or_edit(context, chat_id, text, build_markup(rows))


async def h_replace_confirm_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    fname = p["fname"]
    tmp = p.get("tmp")
    was_running = p.get("was_running", False)
    try:
        content = open(tmp, "rb").read()
    except OSError:
        await q.answer("Staged file lost. Please try again.", show_alert=True)
        return
    result = await replace_file(pid, fname, content)
    cleanup_tmp(tmp)
    if not result["ok"]:
        await q.answer(f"❌ Replace failed: {result['error']}", show_alert=True)
        return
    if was_running:
        await send_or_edit(context, q.message.chat_id, "✅ File replaced.\n\n🔄 Project was stopped. Restart it now?",
                           build_markup([[B("✅ Restart Now", "op_restart_yes", pid=pid),
                                          B("❌ No", "op_restart_no", pid=pid)]]),
                           q.message.message_id)
    else:
        text = "✅ File Replaced Successfully."
        rows = [
            [B("📁 File Info", "nav", screen="file_info", params={"pid": pid, "fname": fname}),
             B("📂 Dashboard", "nav", screen="project_dash", params={"pid": pid})],
        ]
        await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    await refresh_open_screens(uid)


async def h_op_restart_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    await show_screen(context, uid, q.message.chat_id, "start_pending", {"pid": pid},
                      edit_message_id=q.message.message_id)
    proj = await get_project(pid)
    result = await run_start(proj) if proj else {"ok": False, "error": "Project not found"}
    if result["ok"]:
        await show_screen(context, uid, q.message.chat_id, "start_done", {"pid": pid},
                          edit_message_id=q.message.message_id)
    else:
        await show_screen(context, uid, q.message.chat_id, "start_failed",
                          {"pid": pid, "error": result.get("error"), "hint": result.get("hint")},
                          edit_message_id=q.message.message_id)
    await refresh_open_screens(uid)


async def h_op_restart_no(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    await show_screen(context, q.from_user.id, q.message.chat_id, "project_dash", {"pid": pid},
                      edit_message_id=q.message.message_id)


async def h_rename_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    fname = p["fname"]
    wiz_set(uid, "rename_wait_name", {"pid": pid, "old": fname})
    text = "✏ Please send the new file name."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def _rename_confirm_ui(context, chat_id, pid, old, new):
    text = f"✏ Rename\n\n{old}\n\n↓\n\n{new}"
    rows = [
        [B("✅ Rename", "rename_confirm_yes", pid=pid, old=old, new=new),
         B("❌ Cancel", "nav", screen="file_info", params={"pid": pid, "fname": old})],
    ]
    await send_or_edit(context, chat_id, text, build_markup(rows))


async def h_rename_confirm_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    result = await rename_file(pid, p["old"], p["new"])
    if not result["ok"]:
        await q.answer(f"❌ {result['error']}", show_alert=True)
        return
    await show_screen(context, uid, q.message.chat_id, "files", {"pid": pid, "page": 1},
                      edit_message_id=q.message.message_id)
    await q.answer("✅ File renamed")


async def h_file_delete_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    fname = p["fname"]
    text = "⚠ Warning\n\nThis file will be permanently deleted.\n\nDo you want to continue?"
    rows = [
        [B("🗑 Delete", "file_delete_yes", pid=pid, fname=fname),
         B("❌ Cancel", "nav", screen="file_info", params={"pid": pid, "fname": fname})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_file_delete_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    fname = p["fname"]
    await delete_file(pid, fname)
    await show_screen(context, uid, q.message.chat_id, "files", {"pid": pid, "page": 1},
                      edit_message_id=q.message.message_id)
    await q.answer("🗑 File deleted")


async def h_upload_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    wiz_set(uid, "upload_wait_file", {"pid": pid})
    text = "📤 Please send the file to upload to this project."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_backup_create(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    await send_or_edit(context, q.message.chat_id, "💾 Creating backup...\n\nPlease wait.", None,
                       q.message.message_id)
    result = await create_backup(pid)
    text = f"✅ Backup Created\n\n{result['name']}\nSize: {human_size(result['size'])}"
    rows = [
        [B("💾 Backup", "nav", screen="backups_menu", params={"pid": pid}),
         B("📂 Dashboard", "nav", screen="project_dash", params={"pid": pid})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    await refresh_open_screens(uid)


async def h_backup_restore_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    bid = int(p["bid"])
    b = await get_backup(bid)
    name = b["name"] if b else "?"
    text = f"♻ Restore from backup?\n\n{b['name'] if b else name}\n\nThis will overwrite current project files."
    rows = [
        [B("✅ Restore", "backup_restore_yes", pid=pid, bid=bid),
         B("❌ Cancel", "nav", screen="backup_list", params={"pid": pid, "action": "restore", "page": 1})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_backup_restore_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    bid = int(p["bid"])
    await send_or_edit(context, q.message.chat_id, "♻ Restoring backup...\n\nPlease wait.", None,
                       q.message.message_id)
    result = await restore_backup(pid, bid)
    proj = await get_project(pid)
    if not result["ok"]:
        await q.answer(f"❌ Restore failed: {result['error']}", show_alert=True)
        return
    if proj and proj["status"] == "running":
        await send_or_edit(context, q.message.chat_id, "✅ Backup restored.\n\n🔄 Project is running. Restart it now?",
                           build_markup([[B("✅ Restart Now", "op_restart_yes", pid=pid),
                                          B("❌ No", "op_restart_no", pid=pid)]]),
                           q.message.message_id)
    else:
        await send_or_edit(context, q.message.chat_id, "✅ Backup restored successfully.",
                           build_markup([[B("💾 Backup", "nav", screen="backups_menu", params={"pid": pid}),
                                          B("📂 Dashboard", "nav", screen="project_dash", params={"pid": pid})]]),
                           q.message.message_id)
    await refresh_open_screens(uid)


async def h_backup_download_pick(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    bid = int(p["bid"])
    b = await get_backup(bid)
    if not b:
        await q.answer("Backup not found", show_alert=True)
        return
    path = str(backup_path(pid, b["name"]))
    await send_document(context, q.message.chat_id, path, caption=b["name"])
    await q.answer("Backup sent")


async def h_backup_delete_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    bid = int(p["bid"])
    b = await get_backup(bid)
    name = b["name"] if b else "?"
    text = f"🗑 Delete backup?\n\n{name}\n\nThis backup will be permanently removed."
    rows = [
        [B("🗑 Delete", "backup_delete_yes", pid=pid, bid=bid),
         B("❌ Cancel", "nav", screen="backup_list", params={"pid": pid, "action": "delete", "page": 1})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_backup_delete_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    await delete_backup(pid, int(p["bid"]))
    await q.answer("🗑 Backup deleted")
    await show_screen(context, uid, q.message.chat_id, "backup_list",
                      {"pid": pid, "action": "delete", "page": 1}, edit_message_id=q.message.message_id)


async def h_backup_page(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "backup_list",
                      {"pid": p["pid"], "action": p["action"], "page": p["page"]},
                      edit_message_id=q.message.message_id)


async def h_export_project(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    await send_or_edit(context, q.message.chat_id, "📤 Preparing export...\n\nPlease wait.", None,
                       q.message.message_id)
    zpath = await export_project(pid)
    await send_document(context, q.message.chat_id, str(zpath), caption=f"export_p{pid}.zip")
    cleanup_tmp(str(zpath))
    await show_screen(context, uid, q.message.chat_id, "backups_menu", {"pid": pid},
                      edit_message_id=q.message.message_id)


async def h_env_add_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    wiz_set(uid, "env_wait_key", {"pid": pid})
    text = "🌐 Please send the variable KEY:"
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_env_edit_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    key = p["key"]
    wiz_set(uid, "env_wait_value", {"pid": pid, "key": key})
    text = f"🌐 Please send the new VALUE for '{key}':"
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_env_save_confirm(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    key = p["key"]
    value = p["value"]
    await db_exec(
        "INSERT INTO env_vars (project_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(project_id, key) DO UPDATE SET value=excluded.value",
        (pid, key, value),
    )
    await project_activity(pid, "Env Variable Set", key)
    await show_screen(context, uid, q.message.chat_id, "env_vars", {"pid": pid},
                      edit_message_id=q.message.message_id)
    await q.answer("✅ Saved")


async def h_env_var_open(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "env_var_action",
                      {"pid": p["pid"], "key": p["key"]}, edit_message_id=q.message.message_id)


async def h_env_var_delete_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    key = p["key"]
    text = f"🗑 Delete environment variable '{key}'?"
    rows = [
        [B("🗑 Delete", "env_var_delete_yes", pid=pid, key=key),
         B("❌ Cancel", "nav", screen="env_var_action", params={"pid": pid, "key": key})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_env_var_delete_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    key = p["key"]
    await db_exec("DELETE FROM env_vars WHERE project_id=? AND key=?", (pid, key))
    await project_activity(pid, "Env Variable Deleted", key)
    await show_screen(context, uid, q.message.chat_id, "env_vars", {"pid": pid},
                      edit_message_id=q.message.message_id)
    await q.answer("🗑 Deleted")


async def h_auto_restart_toggle(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return
    new_val = 0 if proj["auto_restart"] else 1
    await set_project_field(pid, "auto_restart", new_val)
    await project_activity(pid, "Auto Restart", "ON" if new_val else "OFF")
    await show_screen(context, uid, q.message.chat_id, "settings_menu", {"pid": pid},
                      edit_message_id=q.message.message_id)
    await q.answer("✅ Updated")


async def h_py_ver_select(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    ver = p["ver"]
    await set_project_field(pid, "python", ver)
    await project_activity(pid, "Python Version Changed", ver)
    await show_screen(context, uid, q.message.chat_id, "py_versions", {"pid": pid},
                      edit_message_id=q.message.message_id)
    proj = await get_project(pid)
    if proj and proj["status"] == "running":
        await q.answer("✅ Version saved. Restart the project to apply.", show_alert=False)
    else:
        await q.answer("✅ Version saved")


async def h_startup_select(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    fname = p["fname"]
    await set_project_field(pid, "main_file", fname)
    await project_activity(pid, "Startup File Changed", fname)
    await show_screen(context, uid, q.message.chat_id, "startup_file", {"pid": pid},
                      edit_message_id=q.message.message_id)
    await q.answer("✅ Startup file saved")


async def h_req_toggle(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return
    new_val = 0 if proj["req_install"] else 1
    await set_project_field(pid, "req_install", new_val)
    await show_screen(context, uid, q.message.chat_id, "requirements", {"pid": pid},
                      edit_message_id=q.message.message_id)
    await q.answer("✅ Updated")


async def h_req_install_now(update, context, p):
    q = update.callback_query
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    proj = await get_project(pid)
    if not proj:
        return
    interpreter = await resolve_python(proj["python"])
    req_file = project_dir(pid) / "requirements.txt"
    if not req_file.exists():
        await q.answer("No requirements.txt found in this project.", show_alert=True)
        return
    await q.answer("📥 Installing in background...", show_alert=False)
    asyncio.create_task(_install_requirements(proj, interpreter))
    await toast(context, q.message.chat_id,
                "📥 Installing requirements in background.\nCheck the Deploy Log for progress.")


async def h_cache_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    text = ("🧹 Delete Cache?\n\nThis removes __pycache__, *.pyc and temporary files.\n\n"
            "Main file, database and config will NOT be deleted.")
    rows = [
        [B("🗑 Delete", "cache_yes", pid=pid),
         B("❌ Cancel", "nav", screen="settings_menu", params={"pid": pid})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_cache_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    result = await delete_cache(pid)
    text = f"✅ Cache cleaned.\n\nRemoved: {result['removed']} file(s)\nFreed: {human_size(result['freed'])}"
    rows = [[B("⚙ Settings", "nav", screen="settings_menu", params={"pid": pid})]]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    await refresh_open_screens(uid)


async def h_search_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    wiz_set(uid, "search_wait", {})
    text = "🔍 Please send the project name to search."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_batch_select(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "batch_select",
                      {"action": p.get("action"), "page": p.get("page", 1), "selected": p.get("selected", [])},
                      edit_message_id=q.message.message_id)


async def h_batch_toggle(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    action = p["action"]
    selected = list(p.get("selected", []))
    pid = p["pid"]
    if pid in selected:
        selected.remove(pid)
    else:
        selected.append(pid)
    await show_screen(context, uid, q.message.chat_id, "batch_select",
                      {"action": action, "page": p.get("page", 1), "selected": selected},
                      edit_message_id=q.message.message_id)


async def h_batch_page(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "batch_select",
                      {"action": p["action"], "page": p["page"], "selected": p.get("selected", [])},
                      edit_message_id=q.message.message_id)


async def h_batch_run_ask(update, context, p):
    q = update.callback_query
    action = p["action"]
    selected = list(p.get("selected", []))
    label = "🔄 Restart" if action == "restart" else "💾 Backup"
    text = f"{label} will run on {len(selected)} project(s).\n\nContinue?"
    rows = [
        [B("✅ Yes", "batch_run_yes", action=action, selected=selected),
         B("❌ Cancel", "nav", screen="batch_select", params={"action": action, "page": 1, "selected": selected})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_batch_run_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    action = p["action"]
    selected = list(p.get("selected", []))
    await send_or_edit(context, q.message.chat_id, "⏳ Running batch operation...\n\nPlease wait.", None,
                       q.message.message_id)
    ok = 0
    failed = 0
    for pid in selected:
        try:
            proj = await get_project(pid)
            if not proj:
                failed += 1
                continue
            if action == "restart":
                res = await restart_project(proj)
            else:
                await create_backup(pid)
                res = {"ok": True}
            if res["ok"]:
                ok += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    text = f"✅ Batch complete\n\nAction: {'🔄 Restart' if action == 'restart' else '💾 Backup'}\nSucceeded: {ok}\nFailed: {failed}"
    rows = [[B("📂 My Projects", "nav", screen="my_projects", params={"page": 1}),
             B("⬅ Home", "nav", screen="home", params={})]]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    await refresh_open_screens(uid)


async def h_notif_clear_ask(update, context, p):
    q = update.callback_query
    text = "🗑 Clear all notifications?"
    rows = [
        [B("✅ Yes", "notif_clear_yes"), B("❌ No", "nav", screen="notifications", params={"page": 1})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_notif_clear_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    await delete_all_notifications()
    await show_screen(context, uid, q.message.chat_id, "notifications", {"page": 1},
                      edit_message_id=q.message.message_id)
    await q.answer("✅ Cleared")


async def h_sys_refresh(update, context, p):
    q = update.callback_query
    screen = p.get("screen", "system_dash")
    await show_screen(context, q.from_user.id, q.message.chat_id, screen, p.get("params") or {},
                      edit_message_id=q.message.message_id)
    await q.answer("🔄 Refreshed")


async def h_sort_by_size(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "storage_info",
                      {"order": p.get("order", "desc")}, edit_message_id=q.message.message_id)


async def h_recovery_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    fname = p["fname"]
    version = p["version"]
    text = f"♻ Restore this version of {fname}?\n\n{version}\n\nCurrent file will be overwritten."
    rows = [
        [B("✅ Restore", "recovery_yes", pid=pid, fname=fname, version=version),
         B("❌ Cancel", "nav", screen="recovery", params={"pid": pid})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_recovery_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    result = await restore_previous_version(pid, p["fname"], p["version"])
    if not result["ok"]:
        await q.answer(f"❌ {result['error']}", show_alert=True)
        return
    proj = await get_project(pid)
    if proj and proj["status"] == "running":
        await send_or_edit(context, q.message.chat_id, "♻ Version restored.\n\n🔄 Project is running. Restart it now?",
                           build_markup([[B("✅ Restart Now", "op_restart_yes", pid=pid),
                                          B("❌ No", "op_restart_no", pid=pid)]]),
                           q.message.message_id)
    else:
        await send_or_edit(context, q.message.chat_id, "♻ Version restored successfully.",
                           build_markup([[B("♻ Recovery", "nav", screen="recovery", params={"pid": pid})]]),
                           q.message.message_id)
    await refresh_open_screens(uid)


async def h_recovery_latest_ask(update, context, p):
    q = update.callback_query
    pid = int(p["pid"])
    text = "📦 Restore the whole project from the latest backup?\n\nCurrent files will be overwritten."
    rows = [
        [B("✅ Restore", "recovery_latest_yes", pid=pid),
         B("❌ Cancel", "nav", screen="recovery", params={"pid": pid})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_recovery_latest_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    pid = int(p["pid"])
    backups, _total = await list_backups(pid, 1, 1)
    if not backups:
        await q.answer("No backups exist for this project.", show_alert=True)
        return
    await send_or_edit(context, q.message.chat_id, "♻ Restoring latest backup...\n\nPlease wait.", None,
                       q.message.message_id)
    result = await restore_backup(pid, backups[0]["id"])
    if not result["ok"]:
        await q.answer(f"❌ {result['error']}", show_alert=True)
        return
    proj = await get_project(pid)
    if proj and proj["status"] == "running":
        await send_or_edit(context, q.message.chat_id, "✅ Backup restored.\n\n🔄 Project is running. Restart it now?",
                           build_markup([[B("✅ Restart Now", "op_restart_yes", pid=pid),
                                          B("❌ No", "op_restart_no", pid=pid)]]),
                           q.message.message_id)
    else:
        await send_or_edit(context, q.message.chat_id, "✅ Backup restored successfully.",
                           build_markup([[B("💾 Backup", "nav", screen="backups_menu", params={"pid": pid})]]),
                           q.message.message_id)
    await refresh_open_screens(uid)


async def h_sched_add(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "sched_type", {},
                      edit_message_id=q.message.message_id)


async def h_sched_day_ask(update, context, p):
    q = update.callback_query
    await show_screen(context, q.from_user.id, q.message.chat_id, "sched_day",
                      {"jtype": p["jtype"]}, edit_message_id=q.message.message_id)


async def h_sched_project_pick(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    wiz_set(uid, "sched_wait_time", {"jtype": p["jtype"], "day": p["day"], "pid": p.get("pid")})
    text = "📅 Please send the time in HH:MM (24-hour format).\n\nExample: 02:00"
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_sched_all(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    wiz_set(uid, "sched_wait_time", {"jtype": p["jtype"], "day": p["day"], "pid": None})
    text = "📅 Please send the time in HH:MM (24-hour format).\n\nExample: 02:00"
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_sched_delete_ask(update, context, p):
    q = update.callback_query
    sid = int(p["sid"])
    text = "🗑 Remove this scheduled job?"
    rows = [
        [B("🗑 Remove", "sched_delete_yes", sid=sid), B("❌ Cancel", "nav", screen="scheduler", params={})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_sched_delete_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "admin"):
        return
    await delete_schedule(int(p["sid"]))
    await q.answer("🗑 Removed")
    await show_screen(context, uid, q.message.chat_id, "scheduler", {}, edit_message_id=q.message.message_id)


async def h_user_add_ask(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "owner"):
        return
    wiz_set(uid, "user_wait_id", {})
    text = "👤 Please send the Telegram user ID to add."
    rows = [[B("❌ Cancel", "wiz_cancel")]]
    sent = await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)
    if sent:
        WIZ[uid]["data"]["msg_id"] = sent.message_id
        WIZ[uid]["data"]["chat_id"] = sent.chat_id


async def h_user_role_set(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "owner"):
        return
    tid = int(p["tid"])
    role = p["role"]
    if tid == OWNER_ID:
        await q.answer("Cannot change the owner's role.", show_alert=True)
        return
    await db_exec(
        "INSERT INTO users (telegram_id, name, role) VALUES (?, ?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role",
        (tid, str(tid), role),
    )
    role_cache_clear(tid)
    await q.answer("✅ Role saved")
    await show_screen(context, uid, q.message.chat_id, "users", {}, edit_message_id=q.message.message_id)


async def h_user_action(update, context, p):
    q = update.callback_query
    tid = int(p["tid"])
    row = await db_fetchone("SELECT role FROM users WHERE telegram_id=?", (tid,))
    role = row[0] if row else "viewer"
    text = f"👤 User: {tid}\nRole: {role.title()}"
    rows = [
        [B("👑 Owner", "user_role_set", tid=tid, role="owner"),
         B("🛡 Admin", "user_role_set", tid=tid, role="admin")],
        [B("👁 Viewer", "user_role_set", tid=tid, role="viewer")],
        [B("🗑 Remove", "user_remove_yes", tid=tid), B("⬅ Back", "nav", screen="users", params={})],
    ]
    await send_or_edit(context, q.message.chat_id, text, build_markup(rows), q.message.message_id)


async def h_user_remove_yes(update, context, p):
    q = update.callback_query
    uid = q.from_user.id
    if not await perm(update, "owner"):
        return
    tid = int(p["tid"])
    if tid == OWNER_ID:
        await q.answer("Cannot remove the owner.", show_alert=True)
        return
    await db_exec("DELETE FROM users WHERE telegram_id=?", (tid,))
    await q.answer("🗑 User removed")
    await show_screen(context, uid, q.message.chat_id, "users", {}, edit_message_id=q.message.message_id)


CALLBACK_HANDLERS = {
    "nav": h_nav,
    "noop": h_noop,
    "wiz_cancel": h_wiz_cancel,
    "deploy_menu_open": h_deploy_menu_open,
    "deploy_ask": h_deploy_ask,
    "deploy_confirm": h_deploy_confirm,
    "deploy_cancel": h_deploy_cancel,
    "deploy_main_pick": h_deploy_main_pick,
    "project_open": h_project_open,
    "start_proj": h_start_proj,
    "stop_ask": h_stop_ask,
    "stop_yes": h_stop_yes,
    "delete_project_ask": h_delete_project_ask,
    "delete_project_yes": h_delete_project_yes,
    "restart_proj": h_restart_proj,
    "log_refresh": h_log_refresh,
    "log_download": h_log_download,
    "log_clear_ask": h_log_clear_ask,
    "log_clear_yes": h_log_clear_yes,
    "file_download": h_file_download,
    "replace_ask": h_replace_ask,
    "replace_running_go": h_replace_running_go,
    "replace_confirm_yes": h_replace_confirm_yes,
    "op_restart_yes": h_op_restart_yes,
    "op_restart_no": h_op_restart_no,
    "rename_ask": h_rename_ask,
    "rename_confirm_yes": h_rename_confirm_yes,
    "file_delete_ask": h_file_delete_ask,
    "file_delete_yes": h_file_delete_yes,
    "upload_ask": h_upload_ask,
    "backup_create": h_backup_create,
    "backup_restore_ask": h_backup_restore_ask,
    "backup_restore_yes": h_backup_restore_yes,
    "backup_download_pick": h_backup_download_pick,
    "backup_delete_ask": h_backup_delete_ask,
    "backup_delete_yes": h_backup_delete_yes,
    "backup_page": h_backup_page,
    "export_project": h_export_project,
    "env_add_ask": h_env_add_ask,
    "env_edit_ask": h_env_edit_ask,
    "env_save_confirm": h_env_save_confirm,
    "env_var_open": h_env_var_open,
    "env_var_delete_ask": h_env_var_delete_ask,
    "env_var_delete_yes": h_env_var_delete_yes,
    "auto_restart_toggle": h_auto_restart_toggle,
    "py_ver_select": h_py_ver_select,
    "startup_select": h_startup_select,
    "req_toggle": h_req_toggle,
    "req_install_now": h_req_install_now,
    "cache_ask": h_cache_ask,
    "cache_yes": h_cache_yes,
    "search_ask": h_search_ask,
    "batch_select": h_batch_select,
    "batch_toggle": h_batch_toggle,
    "batch_page": h_batch_page,
    "batch_run_ask": h_batch_run_ask,
    "batch_run_yes": h_batch_run_yes,
    "notif_clear_ask": h_notif_clear_ask,
    "notif_clear_yes": h_notif_clear_yes,
    "sys_refresh": h_sys_refresh,
    "sort_by_size": h_sort_by_size,
    "recovery_ask": h_recovery_ask,
    "recovery_yes": h_recovery_yes,
    "recovery_latest_ask": h_recovery_latest_ask,
    "recovery_latest_yes": h_recovery_latest_yes,
    "sched_add": h_sched_add,
    "sched_day_ask": h_sched_day_ask,
    "sched_project_pick": h_sched_project_pick,
    "sched_all": h_sched_all,
    "sched_delete_ask": h_sched_delete_ask,
    "sched_delete_yes": h_sched_delete_yes,
    "user_add_ask": h_user_add_ask,
    "user_role_set": h_user_role_set,
    "user_action": h_user_action,
    "user_remove_yes": h_user_remove_yes,
    "batch_menu": h_screen_batch_menu,
    "file_open": h_screen_file_open,
    "backup_list": h_screen_backup_list,
    "sched_project_ask": h_screen_sched_project_ask,
}

# =====================================================================
# DISPATCH / ENTRIES
# =====================================================================

STORAGE_WARN_FLAG = False
STORAGE_CRIT_FLAG = False


async def refresh_open_screens(user_id):
    entry = OPEN_SCREENS.get(user_id)
    if not entry or not _application:
        return
    if time.time() - entry["last_edit"] < 1:
        return
    try:
        text, rows = await render(entry["screen"], entry["params"])
        markup = build_markup(rows)
        if text == entry["text"] and text_of_markup(markup) == entry["markup"]:
            return
        await _application.bot.edit_message_text(
            chat_id=entry["chat_id"], message_id=entry["message_id"],
            text=text, reply_markup=markup,
        )
        entry["text"] = text
        entry["markup"] = text_of_markup(markup)
        entry["last_edit"] = time.time()
    except Forbidden:
        OPEN_SCREENS.pop(user_id, None)
    except TelegramError:
        pass


async def on_callback(update, context):
    q = update.callback_query
    user = update.effective_user
    await register_user(user.id, user.first_name or (user.username or ""))
    if not await show_paid_if_not_owner(context, update):
        return
    handler_name, params = CB.get(q.data)
    if not handler_name:
        await q.answer("Session expired. Use /start.", show_alert=False)
        try:
            await show_screen(context, user.id, q.message.chat_id, "home", {},
                              edit_message_id=q.message.message_id)
        except TelegramError:
            pass
        return
    if handler_name in OWNER_HANDLERS:
        if not await require(user.id, "owner"):
            await q.answer("⛔ Access Denied", show_alert=True)
            return
    elif handler_name in ADMIN_HANDLERS:
        if not await require(user.id, "admin"):
            await q.answer("⛔ Access Denied", show_alert=True)
            return
    fn = CALLBACK_HANDLERS.get(handler_name)
    if fn is None:
        await q.answer("Unknown action", show_alert=False)
        return
    try:
        await fn(update, context, params)
    except Exception:
        logger.exception("callback handler '%s' failed", handler_name)
        try:
            await toast(context, q.message.chat_id, "❌ An error occurred while processing this action.")
        except Exception:
            pass
    finally:
        # Acknowledge silently unless the handler already answered (its answer
        # is the first/effective one; a trailing ack is ignored by Telegram).
        try:
            await q.answer()
        except Exception:
            pass


async def on_text(update, context):
    user = update.effective_user
    chat = update.effective_chat.id
    await register_user(user.id, user.first_name or (user.username or ""))
    if not await show_paid_if_not_owner(context, update):
        return
    state = wiz_get(user.id)
    if not state:
        return
    stage = state["stage"]
    data = state["data"]
    text = (update.effective_message.text or "").strip()
    if not await require(user.id, "admin"):
        await toast(context, chat, "⛔ Access Denied")
        return
    try:
        if stage == "deploy_name":
            err = await validate_project_name(text)
            if err:
                await toast(context, chat, f"❌ {err}")
                return
            data["name"] = text
            wiz_set(user.id, "deploy_name", data)
            await show_screen(context, user.id, chat, "deploy_summary", {"data": data})
            return
        if stage == "rename_wait_name":
            pid = data["pid"]
            old = data["old"]
            err = validate_new_filename(text)
            if not err and file_exists(pid, text):
                err = "A file with this name already exists."
            if err:
                await toast(context, chat, f"❌ {err}")
                return
            await _rename_confirm_ui(context, chat, pid, old, text)
            wiz_clear(user.id)
            return
        if stage == "search_wait":
            projects = await list_projects()
            matches = [pr for pr in projects if text.lower() in pr["name"].lower()]
            wiz_clear(user.id)
            if not matches:
                rows = [[B("🔍 Try Again", "search_ask"), B("⬅ Back", "nav", screen="my_projects", params={"page": 1})]]
                await send_or_edit(context, chat, "🔍 No projects found.", build_markup(rows))
                return
            if len(matches) == 1:
                await show_screen(context, user.id, chat, "project_dash", {"pid": matches[0]["id"]})
                return
            lines = ["🔍 Search Results", ""]
            rows = []
            for pr in matches:
                rows.append([B(f"{pr['name']} {STATUS_ICON.get(pr['status'], '')}", "project_open", pid=pr["id"])])
            rows.append([B("⬅ Back", "nav", screen="my_projects", params={"page": 1})])
            await send_or_edit(context, chat, "\n".join(lines), build_markup(rows))
            return
        if stage == "env_wait_key":
            pid = data["pid"]
            key = text
            if not key or len(key) > 50 or any(ch in key for ch in "=\x00\n"):
                await toast(context, chat, "❌ Invalid variable key.")
                return
            wiz_set(user.id, "env_wait_value", {"pid": pid, "key": key})
            msg = await send_or_edit(context, chat, f"🌐 Please send the VALUE for '{key}':",
                                     build_markup([[B("❌ Cancel", "wiz_cancel")]]))
            if msg:
                WIZ[user.id]["data"]["msg_id"] = msg.message_id
            return
        if stage == "env_wait_value":
            pid = data["pid"]
            key = data["key"]
            text2 = f"🌐 Set '{key}' = {truncate(text, 60)}?"
            rows = [[B("✅ Save", "env_save_confirm", pid=pid, key=key, value=text),
                     B("❌ Cancel", "nav", screen="env_vars", params={"pid": pid})]]
            await send_or_edit(context, chat, text2, build_markup(rows))
            wiz_clear(user.id)
            return
        if stage == "sched_wait_time":
            jtype = data["jtype"]
            day = data["day"]
            pid = data.get("pid")
            if not re.match(r"^\d{2}:\d{2}$", text):
                await toast(context, chat, "❌ Invalid time. Use HH:MM format (e.g. 02:00).")
                return
            h, m = text.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                await toast(context, chat, "❌ Invalid time.")
                return
            res = await add_schedule(jtype, day, text, pid)
            if not res["ok"]:
                await toast(context, chat, f"❌ {res['error']}")
                return
            wiz_clear(user.id)
            await show_screen(context, user.id, chat, "scheduler", {})
            await toast(context, chat, "✅ Schedule saved")
            return
        if stage == "user_wait_id":
            try:
                tid = int(text)
            except ValueError:
                await toast(context, chat, "❌ Please send a valid Telegram user ID (numbers only).")
                return
            wiz_clear(user.id)
            rows = [
                [B("👑 Owner", "user_role_set", tid=tid, role="owner"),
                 B("🛡 Admin", "user_role_set", tid=tid, role="admin")],
                [B("👁 Viewer", "user_role_set", tid=tid, role="viewer")],
                [B("❌ Cancel", "nav", screen="users", params={})],
            ]
            await send_or_edit(context, chat, f"👤 Choose the role for user {tid}:", build_markup(rows))
            return
    except Exception:
        logger.exception("text handler failed")
        await toast(context, chat, "❌ An error occurred.")


async def on_document(update, context):
    user = update.effective_user
    chat = update.effective_chat.id
    await register_user(user.id, user.first_name or (user.username or ""))
    if not await show_paid_if_not_owner(context, update):
        return
    state = wiz_get(user.id)
    doc = update.effective_message.document
    if not state:
        await toast(context, chat, "No upload flow is active. Use /start to open the panel.")
        return
    stage = state["stage"]
    data = state["data"]
    if not await require(user.id, "admin"):
        await toast(context, chat, "⛔ Access Denied")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await toast(context, chat, f"❌ File too large (max {human_size(MAX_UPLOAD_BYTES)}).")
        return
    try:
        if stage == "deploy_file":
            fname = sanitize_filename(doc.file_name or "file.py")
            if not fname.lower().endswith(".py"):
                await toast(context, chat, "❌ Only Python (.py) files are supported for Single File deploy.")
                return
            tmp = TMP_DIR / f"deploy_{user.id}_{int(time.time())}.py"
            try:
                await download_document(context, update.effective_message, str(tmp))
                content = tmp.read_bytes()
            except Exception as e:
                await toast(context, chat, f"❌ Failed to receive file: {e}")
                return
            if not validate_python_file(content):
                await toast(context, chat, "❌ Invalid or corrupt Python file.")
                cleanup_tmp(str(tmp))
                return
            msg = await send_or_edit(context, chat, "Processing File...\n\n✔ Python File Found\n\nPlease enter Project Name.",
                                     build_markup([[B("❌ Cancel", "wiz_cancel")]]), data.get("msg_id"))
            wiz_set(user.id, "deploy_name", {"files": {fname: content}, "main_file": fname,
                                             "python": default_python(), "dtype": "single", "tmp": None})
            if msg:
                WIZ[user.id]["data"]["msg_id"] = msg.message_id
            return
        if stage == "deploy_zip":
            tmp = TMP_DIR / f"deploy_{user.id}_{int(time.time())}.zip"
            try:
                await download_document(context, update.effective_message, str(tmp))
            except Exception as e:
                await toast(context, chat, f"❌ Failed to receive archive: {e}")
                return
            try:
                zok = zipfile.is_zipfile(str(tmp)) and zipfile.ZipFile(str(tmp)).testzip() is None
            except Exception:
                zok = False
            if not zok:
                await toast(context, chat, "❌ Invalid or corrupt ZIP archive.")
                cleanup_tmp(str(tmp))
                return
            await send_or_edit(context, chat, "Processing File...\n\n📦 Extracting archive...", None, data.get("msg_id"))
            scan = await scan_zip_archive(tmp)
            if not scan["ok"]:
                await toast(context, chat, f"❌ {scan.get('error', 'Invalid archive')}")
                cleanup_tmp(str(tmp))
                cleanup_tmp(scan.get("tmp"))
                return
            lines = ["📦 Archive Extracted", ""]
            if scan["main_file"]:
                lines.append(f"✔ {scan['main_file']} Found")
            if scan["has_requirements"]:
                lines.append("✔ requirements.txt Found")
            if scan["has_env"]:
                lines.append("✔ .env Found — will import variables")
            if scan["has_plugins"]:
                lines.append("✔ Plugins Folder Found")
            if not scan["main_file"]:
                lines.append("✖ Main File Not Found")
            text = "\n".join(lines)
            if scan["main_file"]:
                wiz_set(user.id, "deploy_name", {"files": None, "main_file": scan["main_file"],
                                                 "python": default_python(), "dtype": data.get("dtype", "zip"),
                                                 "tmp": scan["tmp"], "base": scan["base"]})
                msg = await send_or_edit(context, chat, text + "\n\nPlease enter Project Name.",
                                         build_markup([[B("❌ Cancel", "wiz_cancel")]]), data.get("msg_id"))
                if msg:
                    WIZ[user.id]["data"]["msg_id"] = msg.message_id
            else:
                wiz_set(user.id, "deploy_select_main", {"tmp": scan["tmp"], "base": scan["base"],
                                                        "py_files": scan["py_files"],
                                                        "python": default_python(), "dtype": data.get("dtype", "zip")})
                await show_screen(context, user.id, chat, "deploy_main_select",
                                  {"data": WIZ[user.id]["data"]}, edit_message_id=data.get("msg_id"))
            cleanup_tmp(str(tmp))
            return
        if stage == "replace_wait_file":
            pid = data["pid"]
            fname = data["fname"]
            was_running = data.get("was_running", False)
            tmp = TMP_DIR / f"replace_{user.id}_{int(time.time())}.tmp"
            try:
                await download_document(context, update.effective_message, str(tmp))
                content = tmp.read_bytes()
                if not content:
                    raise ValueError("empty")
            except Exception as e:
                await toast(context, chat, f"❌ The received file is empty or corrupt: {e}")
                cleanup_tmp(str(tmp))
                return
            wiz_clear(user.id)
            await _replace_confirm_ui(context, chat, pid, fname, str(tmp), was_running)
            return
        if stage == "upload_wait_file":
            pid = data["pid"]
            fname = sanitize_filename(doc.file_name or "file")
            if not fname:
                await toast(context, chat, "❌ Invalid file name.")
                return
            if file_exists(pid, fname):
                await toast(context, chat, f"❌ A file named '{fname}' already exists.")
                return
            tmp = TMP_DIR / f"upload_{user.id}_{int(time.time())}.tmp"
            try:
                await download_document(context, update.effective_message, str(tmp))
                content = tmp.read_bytes()
            except Exception as e:
                await toast(context, chat, f"❌ Failed to receive file: {e}")
                return
            res = await upload_file(pid, fname, content)
            cleanup_tmp(str(tmp))
            if not res["ok"]:
                await toast(context, chat, f"❌ {res['error']}")
                return
            wiz_clear(user.id)
            await toast(context, chat, f"✅ File uploaded: {fname}")
            await show_screen(context, user.id, chat, "files", {"pid": pid, "page": 1})
            return
        await toast(context, chat, "This upload flow is not active.")
    except Exception:
        logger.exception("document handler failed")
        await toast(context, chat, "❌ An error occurred while processing the file.")


async def cmd_start(update, context):
    user = update.effective_user
    await register_user(user.id, user.first_name or (user.username or ""))
    if not await require(user.id, "admin"):
        text, rows = paid_screen()
        await _deliver(context, update.effective_chat.id, text, build_markup(rows))
        return
    wiz_clear(user.id)
    OPEN_SCREENS.pop(user.id, None)
    await show_screen(context, user.id, update.effective_chat.id, "home", {})


async def cmd_cancel(update, context):
    user = update.effective_user
    await register_user(user.id, user.first_name or (user.username or ""))
    if not await require(user.id, "admin"):
        text, rows = paid_screen()
        await _deliver(context, update.effective_chat.id, text, build_markup(rows))
        return
    wiz_clear(user.id)
    await show_screen(context, user.id, update.effective_chat.id, "home", {})


async def cmd_help(update, context):
    user = update.effective_user
    await register_user(user.id, user.first_name or (user.username or ""))
    if not await require(user.id, "admin"):
        text, rows = paid_screen()
        await _deliver(context, update.effective_chat.id, text, build_markup(rows))
        return
    text = (
        "🚀 Telegram Hosting Panel\n\n"
        "Commands:\n"
        "/start — Open the hosting dashboard\n"
        "/cancel — Cancel the current flow\n"
        "/help — Show this help\n\n"
        "Everything else is managed with buttons inside the panel."
    )
    await update.effective_message.reply_text(text)


# =====================================================================
# BACKGROUND LOOPS
# =====================================================================

async def live_update_loop():
    while True:
        try:
            await asyncio.sleep(LIVE_UPDATE_INTERVAL)
            CB.cleanup()
            entries = list(OPEN_SCREENS.entries.items())
            for uid, entry in entries:
                try:
                    if time.time() - entry["last_edit"] < LIVE_MIN_GAP:
                        continue
                    text, rows = await render(entry["screen"], entry["params"])
                    markup = build_markup(rows)
                    if text == entry["text"] and text_of_markup(markup) == entry["markup"]:
                        continue
                    await _application.bot.edit_message_text(
                        chat_id=entry["chat_id"], message_id=entry["message_id"],
                        text=text, reply_markup=markup,
                    )
                    entry["text"] = text
                    entry["markup"] = text_of_markup(markup)
                    entry["last_edit"] = time.time()
                except Forbidden:
                    OPEN_SCREENS.pop(uid, None)
                except TelegramError:
                    pass
        except Exception as e:
            logger.error("live update loop: %s", e)


async def monitor_loop():
    global STORAGE_WARN_FLAG, STORAGE_CRIT_FLAG
    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL)
            d = day_str()
            mt = await metrics_today()
            cpu = system_cpu()
            await metrics_peak(d, "cpu_peak", cpu)
            ram_used, ram_total = system_ram()
            if ram_total:
                await metrics_peak(d, "ram_peak", ram_used)
            projects = await list_projects()
            total_proj_storage = sum(await asyncio.gather(
                *[adir_size(str(project_dir(p["id"]))) for p in projects])) if projects else 0
            if mt["storage_initial"] == 0:
                await storage_initial_set(d, total_proj_storage)
            du = hosting_storage()
            if du:
                pct = du[3]
                if pct >= STORAGE_CRIT_PCT and not STORAGE_CRIT_FLAG:
                    STORAGE_CRIT_FLAG = True
                    await notify("Storage Warning", f"❌ Storage Full\nNew Deploy Blocked\n\nUsed: {human_size(du[1])}\nRemaining: {human_size(du[2])}")
                elif pct < STORAGE_CRIT_PCT:
                    STORAGE_CRIT_FLAG = False
                if pct >= STORAGE_WARN_PCT and not STORAGE_WARN_FLAG:
                    STORAGE_WARN_FLAG = True
                    await notify("Storage Warning", f"⚠ Storage Warning\n\nUsed: {human_size(du[1])}\nRemaining: {human_size(du[2])}")
                elif pct < STORAGE_WARN_PCT:
                    STORAGE_WARN_FLAG = False
            else:
                STORAGE_WARN_FLAG = False
                STORAGE_CRIT_FLAG = False
            # Reconcile "running" rows against the OS: a process that vanished
            # (killed, Space rebuild) must not keep showing 🟢 Running.
            for proj in projects:
                if proj["status"] != "running":
                    continue
                pid = proj.get("pid")
                if pid and await is_process_alive(pid):
                    continue
                # DB says running but no live process -> mark stopped with reason.
                await set_project_status(proj["id"], "stopped")
                await set_project_field(proj["id"], "pid", None)
                await set_project_field(proj["id"], "error", "Process no longer alive (status reconciled)")
                if pid:
                    PROCS.pop(pid, None)
                logger.info("monitor: project %s marked stopped (pid %s not alive)", proj["id"], pid)
        except Exception as e:
            logger.error("monitor loop: %s", e)


async def scheduler_loop():
    while True:
        try:
            await asyncio.sleep(SCHEDULE_INTERVAL)
            await scheduler_tick()
        except Exception as e:
            logger.error("scheduler loop: %s", e)


# =====================================================================
# MAIN
# =====================================================================

async def uptime_loop():
    """Inbuilt uptime robot (bot side): every 60s opens a TCP connection to the
    public URL and localhost health endpoint. Redundant with the Flask keep-alive
    thread so the Space is pinged even if the web thread dies. Logs failures."""
    try:
        from urllib.parse import urlparse
        public = (os.environ.get("HOST_URL") or "https://sujajl-788-ghhjj.hf.space").rstrip("/")
        pub = urlparse(public + "/health")
        targets = [
            ("127.0.0.1", int(os.environ.get("PORT", "7860"))),
            (pub.hostname, pub.port or 443),
        ]
        logger.info("uptime robot (bot loop) targets: %s", targets)
        while True:
            for host, port in targets:
                try:
                    reader, writer = await asyncio.open_connection(host, port, limit=512)
                    writer.close()
                    await writer.wait_closed()
                except Exception as exc:
                    logger.error("uptime robot ping %s:%s FAILED: %s", host, port, exc)
            await asyncio.sleep(60)
    except Exception:
        logger.exception("uptime robot crashed")


async def post_init(application):
    global _application, _loop
    _application = application
    _loop = asyncio.get_running_loop()
    await db_init()
    logger.info("Database ready at %s", DB_PATH)
    _start_cpu_sampler()
    await reconcile_startup_statuses()
    asyncio.create_task(uptime_loop())
    asyncio.create_task(live_update_loop())
    asyncio.create_task(monitor_loop())
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(fs_restore_all_missing())
    await db_system_log("Hosting Started")
    await db_system_log("Database Connected")
    await db_system_log("Backup Service Started")
    logger.info("Hosting panel background services started")


def _build_application():
    global _application
    builder = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
    )
    application = builder.build()
    _application = application
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    return application


def _missing_config() -> str:
    if not TOKEN:
        return "HOSTING_BOT_TOKEN is not set"
    if not OWNER_ID:
        return "HOSTING_OWNER_ID is not set"
    return ""


def main():
    missing = _missing_config()
    if missing:
        logger.error(missing)
        print(f"ERROR: {missing}. Please set it as an environment variable and run again.")
        return
    application = _build_application()
    logger.info("Telegram Hosting Panel starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
        drop_pending_updates=True,
    )


WEBHOOK_SECRET = os.environ.get("HOSTING_WEBHOOK_SECRET", "s3cret_wbhk")


async def _run_async():
    global _application, _loop
    missing = _missing_config()
    if missing:
        logger.error(missing)
        print(f"ERROR: {missing}. Please set it as an environment variable and run again.")
        return
    application = _build_application()
    _application = application
    _loop = asyncio.get_running_loop()
    logger.info("Telegram Hosting Panel starting (webhook mode)...")
    await application.initialize()
    await application.post_init(application)
    await application.start()
    public = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("HOST_URL")
        or "https://sujajl-788-ghhjj.hf.space"
    ).rstrip("/")
    webhook_url = f"{public}/webhook/{WEBHOOK_SECRET}"
    try:
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Webhook set: %s", webhook_url)
    except Exception as exc:
        logger.error("Failed to set webhook: %s", exc)
    while True:
        await asyncio.sleep(3600)


def deliver_webhook_update(payload: dict) -> bool:
    """Called from the Flask (web) thread. Feeds a received webhook update into
    the running asyncio application loop."""
    app = _application
    loop = _loop
    if app is None or loop is None:
        return False
    try:
        from telegram import Update as _Update
        update = _Update.de_json(payload, app.bot)
        if update is None:
            return False
        future = asyncio.run_coroutine_threadsafe(app.process_update(update), loop)
        future.result(timeout=30)
        return True
    except Exception as exc:
        logger.error("deliver_webhook_update failed: %s", exc)
        return False


if __name__ == "__main__":
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        main()
    else:
        asyncio.ensure_future(_run_async())

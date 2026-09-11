# -*- coding: utf-8 -*-
"""
tidb_store — TiDB cloud backup/restore for HostingBot.

Persists uploaded user files + sqlite DB into TiDB (MySQL protocol) so the bot
can fully self-heal: if the Space storage is wiped, startup pulls everything
back from TiDB automatically.
"""
import os
import io
import sys
import time
import base64
import sqlite3
import threading
import logging

logger = logging.getLogger('tidb_store')

try:
    import pymysql
    HAVE_PYMYSQL = True
except Exception:
    HAVE_PYMYSQL = False

# ── TiDB cluster — credentials MUST come from env (secrets) ──
TIDB_HOST = os.getenv('TIDB_HOST', '')
TIDB_PORT = int(os.getenv('TIDB_PORT', '4000'))
TIDB_USER = os.getenv('TIDB_USER', '')
TIDB_PASSWORD = os.getenv('TIDB_PASSWORD', '')
TIDB_DB = os.getenv('TIDB_DB', '')

DB_PATH = os.getenv('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'bot.db'))
_state = {'enabled': False, 'thread': None}

_lock = threading.Lock()


def _connect():
    if not HAVE_PYMYSQL:
        return None
    return pymysql.connect(
        host=TIDB_HOST, port=TIDB_PORT,
        user=TIDB_USER, password=TIDB_PASSWORD,
        database=TIDB_DB, charset='utf8mb4',
        connect_timeout=15, read_timeout=30, write_timeout=30,
        autocommit=True,
    )


def check_connection():
    """Validate TiDB connectivity; sets _state['enabled']."""
    if not (TIDB_HOST and TIDB_USER and TIDB_PASSWORD):
        logger.info("TiDB credentials not configured - store disabled")
        _state['enabled'] = False
        return False
    try:
        conn = _connect()
        if conn is None:
            logger.error("pymysql not installed - TiDB store disabled")
            return False
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS bot_files ("
                " uid BIGINT, name VARCHAR(255), ftype VARCHAR(32),"
                " data MEDIUMBLOB, ts DATETIME,"
                " PRIMARY KEY (uid, name))")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS bot_state ("
                " k VARCHAR(255) PRIMARY KEY, v MEDIUMTEXT, ts DATETIME)")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS bot_sqlite ("
                " id TINYINT PRIMARY KEY, data MEDIUMBLOB, ts DATETIME)")
        conn.close()
        _state['enabled'] = True
        logger.info("TiDB connected: %s/%s (db=%s)", TIDB_HOST, TIDB_PORT, TIDB_DB)
        return True
    except Exception as e:
        _state['enabled'] = False
        logger.error("TiDB connect failed: %s", e)
        return False


def enabled():
    return _state['enabled']


# ─────────────── file backup ───────────────

def backup_file(uid, name, path, ftype='executable'):
    if not _state['enabled']:
        return False
    try:
        with open(path, 'rb') as f:
            payload = base64.b64encode(f.read()).decode('ascii')
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO bot_files (uid, name, ftype, data, ts) '
                'VALUES (%s, %s, %s, %s, UTC_TIMESTAMP()) '
                'ON DUPLICATE KEY UPDATE ftype=VALUES(ftype), data=VALUES(data), ts=UTC_TIMESTAMP()',
                (int(uid), name, ftype, payload))
        conn.close()
        logger.info("TiDB backup: %s/%s", uid, name)
        return True
    except Exception as e:
        logger.error("TiDB backup_file failed %s/%s: %s", uid, name, e)
        return False


def delete_file_backup(uid, name):
    if not _state['enabled']:
        return
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('DELETE FROM bot_files WHERE uid=%s AND name=%s', (int(uid), name))
        conn.close()
        logger.info("TiDB deleted backup: %s/%s", uid, name)
    except Exception as e:
        logger.error("TiDB delete failed: %s", e)


def list_backed_up_files():
    """Return [(uid, name, ftype)] of everything in TiDB."""
    if not _state['enabled']:
        return []
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('SELECT uid, name, ftype FROM bot_files')
            rows = cur.fetchall()
        conn.close()
        return [(int(u), n, t) for u, n, t in rows]
    except Exception as e:
        logger.error("TiDB list failed: %s", e)
        return []


def restore_all_files(upload_root):
    """Write every backed-up file back to disk under upload_root/<uid>/<name>.
    Returns number restored."""
    if not _state['enabled']:
        return 0
    count = 0
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('SELECT uid, name, data FROM bot_files')
            rows = cur.fetchall()
        conn.close()
        for uid, name, data in rows:
            folder = os.path.join(upload_root, str(uid))
            os.makedirs(folder, exist_ok=True)
            final = os.path.join(folder, name)
            try:
                raw = base64.b64decode(data)
                with open(final, 'wb') as f:
                    f.write(raw)
                count += 1
            except Exception:
                continue
        if count:
            logger.info("TiDB restored %d files", count)
        return count
    except Exception as e:
        logger.error("TiDB restore failed: %s", e)
        return count


# ─────────────── sqlite backup ───────────────

def backup_sqlite(db_path):
    if not _state['enabled'] or not os.path.exists(db_path):
        return False
    try:
        with open(db_path, 'rb') as f:
            payload = base64.b64encode(f.read()).decode('ascii')
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('INSERT INTO bot_sqlite (id, data, ts) VALUES (0, %s, UTC_TIMESTAMP()) '
                        'ON DUPLICATE KEY UPDATE data=VALUES(data), ts=UTC_TIMESTAMP()', (payload,))
        conn.close()
        logger.info("TiDB sqlite backup: %d bytes", len(payload))
        return True
    except Exception as e:
        logger.error("TiDB sqlite backup failed: %s", e)
        return False


def restore_sqlite(db_path):
    """Pull sqlite DB from TiDB and overwrite local (if local missing/corrupt)."""
    if not _state['enabled']:
        return False
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('SELECT data FROM bot_sqlite WHERE id=0')
            row = cur.fetchone()
        conn.close()
        if not row:
            return False
        raw = base64.b64decode(row[0])
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with open(db_path, 'wb') as f:
            f.write(raw)
        logger.info("TiDB sqlite restored: %d bytes", len(raw))
        return True
    except Exception as e:
        logger.error("TiDB sqlite restore failed: %s", e)
        return False


# ─────────────── bot_state (json blobs) ───────────────

def set_state(key, value):
    """Store an arbitrary JSON value under a key."""
    if not _state['enabled']:
        return False
    try:
        import json
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('INSERT INTO bot_state (k, v, ts) VALUES (%s, %s, UTC_TIMESTAMP()) '
                        'ON DUPLICATE KEY UPDATE v=VALUES(v), ts=UTC_TIMESTAMP()',
                        (key, json.dumps(value)))
        conn.close()
        return True
    except Exception as e:
        logger.error("TiDB set_state failed: %s", e)
        return False


def get_state(key, default=None):
    if not _state['enabled']:
        return default
    try:
        import json
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('SELECT v FROM bot_state WHERE k=%s', (key,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return default
        return json.loads(row[0])
    except Exception as e:
        logger.error("TiDB get_state failed: %s", e)
        return default


def start(background=True):
    """Connect at startup; optionally in a background thread."""
    def _init():
        try:
            check_connection()
        except Exception as e:
            logger.error("TiDB init error: %s", e)

    if background:
        t = threading.Thread(target=_init, daemon=True)
        t.start()
        _state['thread'] = t
    else:
        _init()


def start(background=True):
    """Connect at startup; optionally in a background thread."""
    def _init():
        try:
            check_connection()
        except Exception as e:
            logger.error("TiDB init error: %s", e)

    if background:
        t = threading.Thread(target=_init, daemon=True)
        t.start()
        _state['thread'] = t
    else:
        _init()


def init():
    start(background=True)


def init_sync():
    try:
        check_connection()
    except Exception as e:
        logger.error("TiDB init error: %s", e)
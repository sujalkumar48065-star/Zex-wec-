# -*- coding: utf-8 -*-
"""
tidb_manager.py - Automatic Multi-Database (TiDB Cloud) Failover Manager
========================================================================

HOW THE AUTOMATIC FAILOVER SYSTEM WORKS
---------------------------------------
1. The bot's existing local SQLite layer is left 100% untouched. At startup
   the hosting bot calls tidb_manager.install_patch(), which transparently
   replaces the `sqlite3.connect(DATABASE_PATH, ...)` entry point with a
   TiDB-backed proxy connection that speaks the same API
   (cursor / execute / fetchone / fetchall / rowcount / commit / close).
   No existing bot code is modified.

2. By default ALL new data is written to Account 1 (TIDB1_* credentials).

3. If Account 1 cannot accept data because its storage limit is reached
   (or another storage / connection error occurs), the manager:
       - marks Account 1 as failed,
       - automatically switches the active account to Account 2,
       - retries the SAME operation on the new connection,
       - logs the failover (passwords are NEVER logged).
   The caller / user notices no interruption.

4. Existing data in Account 1 stays accessible (it is never deleted), and
   any existing local SQLite rows are migrated into Account 1 once.

5. If Account 2 is also unavailable, the manager raises a clean
   DatabaseFailoverError. Every existing bot DB function already wraps its
   database calls in try/except, so the bot logs the error and keeps
   running - it never crashes.

6. Extra safety net: if BOTH TiDB accounts are unreachable at startup the
   patch is NOT installed and the bot simply keeps using its original
   local SQLite database, exactly as before this feature was added.

Credentials are read from the environment (.env file) so no password ever
appears in source code, logs or process listings.
"""

import os
import re
import sys
import time
import json
import logging
import threading
import subprocess

# Auto-install pymysql if it is missing (same pattern the hosting bot uses).
try:
    import pymysql
    from pymysql.err import (
        OperationalError,
        ProgrammingError,
        InterfaceError,
        MySQLError,
    )
    HAS_PYMYSQL = True
except ImportError:
    HAS_PYMYSQL = False
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pymysql"],
            check=True,
            capture_output=True,
        )
        import pymysql
        from pymysql.err import (
            OperationalError,
            ProgrammingError,
            InterfaceError,
            MySQLError,
        )
        HAS_PYMYSQL = True
    except Exception:
        HAS_PYMYSQL = False

logger = logging.getLogger("tidb_manager")

# ---------------------------------------------------------------------------
# Load credentials from the .env file placed next to this module (if present).
# os.environ values already set (e.g. by the bot's load_dotenv) take priority.
# ---------------------------------------------------------------------------
def _load_dotenv_file(path):
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    except Exception as exc:
        logger.debug("Could not read %s: %s", path, exc)


_load_dotenv_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
_load_dotenv_file(os.path.join(os.getcwd(), ".env"))

# ---------------------------------------------------------------------------
# Account configuration (two TiDB Cloud accounts).
# Database names default to 'zex_hosting' and are auto-created if missing.
# ---------------------------------------------------------------------------
DB_DEFAULT = os.getenv("TIDB_DEFAULT_DB", "zex_hosting")


def _load_accounts():
    accounts = []
    for prefix, name in (("TIDB1", "Account1"), ("TIDB2", "Account2")):
        host = os.getenv(prefix + "_HOST")
        user = os.getenv(prefix + "_USER")
        password = os.getenv(prefix + "_PASS")
        if not host or not user or not password:
            logger.warning("Incomplete %s credentials - account skipped.", name)
            continue
        accounts.append(
            {
                "name": name,
                "host": host,
                "port": int(os.getenv(prefix + "_PORT", "4000")),
                "user": user,
                "password": password,
                "database": os.getenv(prefix + "_DB", DB_DEFAULT),
            }
        )
    return accounts


# ---------------------------------------------------------------------------
# SQL translation from SQLite dialect to MySQL/TiDB dialect.
# Uses plain string replacement (robust, no regex surprises).
# ---------------------------------------------------------------------------
def sql_translate(sql):
    s = sql.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
    s = s.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
    s = s.replace("AUTOINCREMENT", "AUTO_INCREMENT")
    s = s.replace("?", "%s")
    return s


# ---------------------------------------------------------------------------
# TiDB-compatible DDL for the bot's eight tables (created idempotently).
# ---------------------------------------------------------------------------
DDL = [
    "CREATE TABLE IF NOT EXISTS subscriptions (user_id BIGINT PRIMARY KEY, expiry VARCHAR(255))",
    "CREATE TABLE IF NOT EXISTS user_files (user_id BIGINT, file_name VARCHAR(255), file_type VARCHAR(32), PRIMARY KEY (user_id, file_name))",
    "CREATE TABLE IF NOT EXISTS active_users (user_id BIGINT PRIMARY KEY, join_date VARCHAR(255), last_seen VARCHAR(255))",
    "CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY, added_by BIGINT, added_date VARCHAR(255))",
    "CREATE TABLE IF NOT EXISTS banned_users (user_id BIGINT PRIMARY KEY, reason VARCHAR(1024), banned_by BIGINT, ban_date VARCHAR(255))",
    "CREATE TABLE IF NOT EXISTS user_limits (user_id BIGINT PRIMARY KEY, file_limit BIGINT, set_by BIGINT, set_date VARCHAR(255))",
    "CREATE TABLE IF NOT EXISTS mandatory_channels (channel_id VARCHAR(255) PRIMARY KEY, channel_username VARCHAR(255), channel_name VARCHAR(255), added_by BIGINT, added_date VARCHAR(255))",
    "CREATE TABLE IF NOT EXISTS install_logs (id BIGINT PRIMARY KEY AUTO_INCREMENT, user_id BIGINT, module_name VARCHAR(255), package_name VARCHAR(255), status VARCHAR(64), log TEXT, install_date VARCHAR(255))",
]


class DatabaseFailoverError(Exception):
    """Raised when both TiDB accounts are unavailable after retries."""


# Error codes / message markers that mean "switch to the other account".
FAILOVER_ERRNOS = {
    1114,  # table is full / storage limit reached
    1206,  # total number of locks exceeded
    1290,  # server is read-only
    2003,  # can't connect to server
    2006,  # server has gone away
    2013,  # lost connection to server
    8004,  # TiDB server memory usage exceeds threshold
    8005,  # transaction too large
    8006,  # the number of transaction commit retries exceeds limit
    8028,  # table is full (TiDB)
    9001,  # TiDB server is down
    9002,  # TiDB server timeout
    9004,  # transaction is too large
    9005,  # region is unavailable
    9006,  # region is unavailable
    9007,  # region is unavailable
    9010,  # region is unavailable
    9013,  # region is unavailable
}

FAILOVER_MSG_MARKERS = [
    "full",
    "read-only",
    "unavailable",
    "storage",
    "memory",
    "region",
    "quota",
    "refused",
    "gone away",
    "lost connection",
    "too many connections",
    "can't connect",
    "connection",
    "socket",
    "locked",
    "timeout",
    "unreachable",
]


def is_failover_error(exc):
    """Return True if the error means we should try the other account."""
    code = None
    args = getattr(exc, "args", None)
    if args and isinstance(args, (list, tuple)) and args:
        try:
            code = int(args[0])
        except (TypeError, ValueError):
            code = None
    if code in FAILOVER_ERRNOS:
        return True
    try:
        msg = str(exc).lower()
    except Exception:
        msg = ""
    return any(marker in msg for marker in FAILOVER_MSG_MARKERS)


# ---------------------------------------------------------------------------
# The failover manager (single instance).
# ---------------------------------------------------------------------------
class TiDBFailoverManager:
    def __init__(self, accounts):
        self.accounts = accounts
        self.active = 0  # default: Account 1
        self.failed = [False] * len(accounts)
        self._lock = threading.Lock()

    def _mask(self, value):
        if not value:
            return "***"
        if len(value) <= 6:
            return "***"
        return value[:2] + "***" + value[-2:]

    def _connect_account(self, idx, retries=2):
        """Open a fresh real connection to account idx with small retries."""
        account = self.accounts[idx]
        last_error = None
        for attempt in range(retries):
            try:
                return pymysql.connect(
                    host=account["host"],
                    port=account["port"],
                    user=account["user"],
                    password=account["password"],
                    database=account["database"],
                    ssl={"ssl": {}},
                    connect_timeout=10,
                    read_timeout=30,
                    write_timeout=30,
                    charset="utf8mb4",
                    autocommit=False,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Connect %s (%s@%s) attempt %d/%d failed: %s",
                    account["name"],
                    account["user"],
                    account["host"],
                    attempt + 1,
                    retries,
                    exc,
                )
                time.sleep(0.4 * (attempt + 1))
        self.failed[idx] = True
        raise last_error

    def connect_for(self, idx):
        """Direct connection helper (used during migration)."""
        return self._connect_account(idx)

    def switch_account(self, reason):
        """Mark the current account failed and activate the other one."""
        with self._lock:
            old = self.active
            other = 1 - self.active
            self.failed[old] = True
            self.active = other
            self.failed[other] = False
            target = self.accounts[other]
            logger.warning(
                "FAILOVER: %s -> %s (reason: %s)",
                self.accounts[old]["name"],
                target["name"],
                reason,
            )

    def open_active(self):
        """Open a connection to the active (non-failed) account, switching if
        necessary. Raises DatabaseFailoverError when BOTH accounts fail."""
        for _ in range(len(self.accounts)):
            if self.failed[self.active]:
                self.switch_account(OperationalError("active account marked failed"))
                continue
            try:
                return self._connect_account(self.active)
            except Exception as exc:
                logger.error(
                    "Connection to %s failed: %s",
                    self.accounts[self.active]["name"],
                    exc,
                )
                self.switch_account(exc)
        raise DatabaseFailoverError("Both TiDB accounts are currently unavailable.")

    def ensure_schema(self, idx):
        """Create the database and tables on account idx (idempotent)."""
        account = self.accounts[idx]
        conn = pymysql.connect(
            host=account["host"],
            port=account["port"],
            user=account["user"],
            password=account["password"],
            ssl={"ssl": {}},
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            charset="utf8mb4",
            autocommit=False,
        )
        try:
            cur = conn.cursor()
            cur.execute("CREATE DATABASE IF NOT EXISTS `%s`" % account["database"])
            cur.execute("USE `%s`" % account["database"])
            for ddl in DDL:
                cur.execute(ddl)
            conn.commit()
            logger.info(
                "Schema ready on %s (db=%s, host=%s)",
                account["name"],
                account["database"],
                account["host"],
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Proxy connection + cursor. These mimic the sqlite3 connection API so the
# bot's existing database code works unchanged.
#
# IMPORTANT: the real connection is opened LAZILY (first execute), never in
# __init__. Some bot functions call sqlite3.connect() OUTSIDE their try/except
# block, so connecting eagerly could raise an uncaught DatabaseFailoverError.
# With lazy connect the failure surfaces at c.execute() which IS inside the
# bot's try/except (both `except sqlite3.Error` and `except Exception`).
# ---------------------------------------------------------------------------
class TiDBProxyCursor:
    def __init__(self, connection):
        self._conn = connection
        self._cur = None

    def execute(self, query, args=None):
        self._cur = self._conn._execute(query, args)
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone() if self._cur else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur else []

    @property
    def rowcount(self):
        return self._cur.rowcount if self._cur else -1

    @property
    def lastrowid(self):
        return self._cur.lastrowid if self._cur else 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TiDBProxyConnection:
    def __init__(self, manager):
        self._manager = manager
        self._real = None
        self._pending = []  # (translated_sql, args) executed since last commit

    def _ensure_real(self):
        if self._real is not None:
            return self._real
        try:
            self._real = self._manager.open_active()
        except DatabaseFailoverError as exc:
            exc_cls = _FAILOVER_EXC or DatabaseFailoverError
            raise exc_cls(str(exc))
        return self._real

    def _table_exists(self, table):
        try:
            cur = self._real.cursor()
            cur.execute("SHOW TABLES LIKE %s", (table,))
            return cur.fetchone() is not None
        except Exception:
            return True  # on error, assume it exists (safe no-op)

    def _is_benign_create(self, exc, sql):
        """TiDB rejects CREATE TABLE IF NOT EXISTS statements that use a TEXT/
        BLOB column in the PRIMARY KEY with error 1170 - even when the table
        already exists. The bot's existing CREATE TABLE statements use SQLite
        types (TEXT PRIMARY KEY). Because this manager pre-creates the tables
        with compatible types, those CREATEs are intended no-ops. Treat them as
        successful when the table already exists."""
        if not sql.lstrip().upper().startswith("CREATE TABLE IF NOT EXISTS"):
            return False
        args = getattr(exc, "args", None)
        code = None
        if args and isinstance(args, (list, tuple)) and args:
            try:
                code = int(args[0])
            except (TypeError, ValueError):
                code = None
        if code != 1170:
            return False
        m = re.match(r"CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?", sql, re.I | re.S)
        if not m:
            return False
        return self._table_exists(m.group(1))

    def _execute(self, sql, args):
        """Execute a single statement with automatic failover + retry."""
        translated = sql_translate(sql)
        self._ensure_real()
        attempts = 0
        while True:
            try:
                cur = self._real.cursor()
                if args is None:
                    cur.execute(translated)
                else:
                    cur.execute(translated, args)
                self._pending.append((translated, args))
                return cur
            except Exception as exc:
                if self._is_benign_create(exc, translated):
                    return self._real.cursor()
                if is_failover_error(exc) and attempts < 2:
                    attempts += 1
                    self._manager.switch_account(exc)
                    self._real = self._manager.open_active()
                    self._pending = []
                    continue
                raise

    def cursor(self):
        return TiDBProxyCursor(self)

    def commit(self):
        if self._real is None:
            return
        try:
            self._real.commit()
            self._pending = []
        except Exception as exc:
            if is_failover_error(exc):
                self._manager.switch_account(exc)
                try:
                    self._real = self._manager.open_active()
                except DatabaseFailoverError:
                    self._real = None
                    raise
                for sql, args in self._pending:
                    cur = self._real.cursor()
                    if args is None:
                        cur.execute(sql)
                    else:
                        cur.execute(sql, args)
                self._real.commit()
                self._pending = []
            else:
                raise

    def rollback(self):
        if self._real is None:
            return
        try:
            self._real.rollback()
        except Exception:
            pass

    def close(self):
        if self._real is None:
            return
        try:
            self._real.close()
        except Exception:
            pass
        self._real = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        try:
            self.close()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Patch installation entry point (called by the hosting bot).
# ---------------------------------------------------------------------------
_TARGET_DB_PATH = None
_ORIGINAL_CONNECT = None
manager = None
_FAILOVER_EXC = None  # sqlite3.Error-compatible error, built by install_patch


def _migrate_sqlite_to_tidb(sqlite3_module, database_path, mgr):
    """One-time, idempotent migration of existing local SQLite rows into TiDB."""
    flag_path = os.path.join(os.path.dirname(database_path), "tidb_migrated.flag")
    try:
        if os.path.exists(flag_path):
            logger.info("SQLite migration already done - skipping.")
            return
    except Exception:
        pass

    try:
        src = sqlite3_module.connect(database_path)
    except Exception as exc:
        logger.error("Could not open SQLite for migration: %s", exc)
        return

    tables = [
        "subscriptions",
        "user_files",
        "active_users",
        "admins",
        "banned_users",
        "user_limits",
        "mandatory_channels",
        "install_logs",
    ]
    migrated_total = 0
    try:
        cur = src.cursor()
        for table in tables:
            try:
                cur.execute('SELECT * FROM "%s"' % table)
                rows = cur.fetchall()
                if not rows:
                    continue
                columns = [desc[0] for desc in cur.description]
                placeholders = ", ".join(["%s"] * len(columns))
                column_sql = ", ".join(columns)
                insert_sql = "INSERT IGNORE INTO %s (%s) VALUES (%s)" % (
                    table,
                    column_sql,
                    placeholders,
                )
                conn = mgr.connect_for(mgr.active)
                try:
                    c2 = conn.cursor()
                    c2.executemany(insert_sql, rows)
                    conn.commit()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                migrated_total += len(rows)
                logger.info("Migrated %d rows into table %s", len(rows), table)
            except Exception as exc:
                logger.warning("Migration for table %s skipped: %s", table, exc)
        try:
            with open(flag_path, "w", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    finally:
        try:
            src.close()
        except Exception:
            pass
    logger.info("SQLite -> TiDB migration finished (%d rows total).", migrated_total)


def install_patch(sqlite3_module, database_path):
    """
    Validate TiDB connectivity, prepare schema, migrate local SQLite data and
    finally patch sqlite3.connect so the bot transparently uses TiDB with
    automatic Account1 -> Account2 failover.

    Returns True when TiDB failover is active, False when the bot should keep
    using its original SQLite database.
    """
    global manager, _TARGET_DB_PATH, _ORIGINAL_CONNECT, _FAILOVER_EXC

    if not HAS_PYMYSQL:
        logger.error("pymysql unavailable - TiDB failover DISABLED, keeping SQLite.")
        return False

    accounts = _load_accounts()
    if len(accounts) < 2:
        logger.error(
            "Need both accounts configured - TiDB failover DISABLED, keeping SQLite."
        )
        return False

    manager = TiDBFailoverManager(accounts)

    # 1) Connectivity check + schema creation (try Account 1 first, then 2).
    healthy = False
    for idx in range(len(accounts)):
        try:
            manager.ensure_schema(idx)
            test = manager.connect_for(idx)
            test.close()
            healthy = True
            logger.info(
                "TiDB %s reachable (user=%s, host=%s).",
                accounts[idx]["name"],
                accounts[idx]["user"],
                accounts[idx]["host"],
            )
            break
        except Exception as exc:
            logger.error(
                "TiDB %s unavailable: %s",
                accounts[idx]["name"],
                str(exc)[:200],
            )
    if not healthy:
        logger.critical(
            "BOTH TiDB accounts unreachable. Keeping local SQLite as last resort."
        )
        # Remove the migration flag so any rows written to local SQLite while
        # TiDB was down get migrated the next time a TiDB account is reachable.
        try:
            flag_path = os.path.join(
                os.path.dirname(database_path), "tidb_migrated.flag"
            )
            if os.path.exists(flag_path):
                os.remove(flag_path)
        except Exception:
            pass
        return False

    # 2) One-time migration of existing local SQLite data into the active account.
    try:
        _migrate_sqlite_to_tidb(sqlite3_module, database_path, manager)
    except Exception as exc:
        logger.error("SQLite migration error (non fatal): %s", exc)

    # 3) Patch sqlite3.connect for the bot's database path.
    #    Build a sqlite3.Error-compatible exception so the bot's existing
    #    `except sqlite3.Error` clauses also catch "TiDB is down" errors.
    _FAILOVER_EXC = type(
        "TiDBUnavailableError", (DatabaseFailoverError, sqlite3_module.Error), {}
    )
    _TARGET_DB_PATH = database_path
    _ORIGINAL_CONNECT = sqlite3_module.connect

    def patched_connect(database, *args, **kwargs):
        if database == _TARGET_DB_PATH:
            return TiDBProxyConnection(manager)
        return _ORIGINAL_CONNECT(database, *args, **kwargs)

    sqlite3_module.connect = patched_connect

    primary = accounts[manager.active]["name"]
    backup = accounts[1 - manager.active]["name"]
    logger.warning(
        "TiDB AUTO-FAILOVER ENABLED: primary=%s backup=%s. All DB writes now use TiDB.",
        primary,
        backup,
    )
    return True


def current_status():
    """Small status helper (used by tests / diagnostics)."""
    if manager is None:
        return {"active": None, "enabled": False}
    account = manager.accounts[manager.active]
    return {
        "enabled": True,
        "active": account["name"],
        "host": account["host"],
        "user": account["user"],
    }

import asyncio
import logging
import os
import re
import ssl
import sys
import threading

import aiomysql

import tidb_manager

logger = logging.getLogger("tidb_aiosqlite")

# Number of simultaneous TiDB connections kept open. This lets independent
# queries run concurrently (instead of serializing everything on one
# connection), which is the main latency fix for the slow responses.
POOL_SIZE = int(os.environ.get("TIDB_POOL_SIZE", "4"))

# ---------------------------------------------------------------------------
# TiDB-compatible DDL for the bot's twelve tables (created idempotently).
# The reserved `key` column is backticked; TEXT primary keys become VARCHAR.
# ---------------------------------------------------------------------------
DDL = [
    "CREATE TABLE IF NOT EXISTS meta (`key` VARCHAR(255) PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS projects (id BIGINT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(255) UNIQUE NOT NULL, path TEXT NOT NULL, main_file TEXT NOT NULL, python TEXT NOT NULL, status VARCHAR(64) NOT NULL DEFAULT 'stopped', auto_restart BIGINT NOT NULL DEFAULT 0, req_install BIGINT NOT NULL DEFAULT 1, req_hash TEXT, req_status TEXT, req_error TEXT, crash_count BIGINT NOT NULL DEFAULT 0, created_at DOUBLE NOT NULL, last_deploy DOUBLE, last_backup DOUBLE, last_restart DOUBLE, started_at DOUBLE, pid BIGINT, error TEXT, deploy_count BIGINT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS env_vars (project_id BIGINT NOT NULL, `key` VARCHAR(255) NOT NULL, value TEXT NOT NULL, PRIMARY KEY (project_id, `key`))",
    "CREATE TABLE IF NOT EXISTS backups (id BIGINT PRIMARY KEY AUTO_INCREMENT, project_id BIGINT NOT NULL, name TEXT NOT NULL, size BIGINT NOT NULL DEFAULT 0, created_at DOUBLE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS notifications (id BIGINT PRIMARY KEY AUTO_INCREMENT, type TEXT NOT NULL, message TEXT NOT NULL, created_at DOUBLE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS activity (id BIGINT PRIMARY KEY AUTO_INCREMENT, project_id BIGINT, action TEXT NOT NULL, detail TEXT, created_at DOUBLE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS system_logs (id BIGINT PRIMARY KEY AUTO_INCREMENT, message TEXT NOT NULL, created_at DOUBLE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS users (telegram_id BIGINT PRIMARY KEY, name TEXT, role TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS schedules (id BIGINT PRIMARY KEY AUTO_INCREMENT, type TEXT NOT NULL, day TEXT NOT NULL, time TEXT NOT NULL, project_id BIGINT, enabled BIGINT NOT NULL DEFAULT 1, last_run TEXT)",
    "CREATE TABLE IF NOT EXISTS deploy_history (id BIGINT PRIMARY KEY AUTO_INCREMENT, project_id BIGINT NOT NULL, version BIGINT NOT NULL, type TEXT NOT NULL, detail TEXT, created_at DOUBLE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS counters (`key` VARCHAR(255) PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS day_metrics (day VARCHAR(32) PRIMARY KEY, deploys BIGINT NOT NULL DEFAULT 0, restarts BIGINT NOT NULL DEFAULT 0, backups BIGINT NOT NULL DEFAULT 0, replacements BIGINT NOT NULL DEFAULT 0, cpu_peak DOUBLE NOT NULL DEFAULT 0, ram_peak BIGINT NOT NULL DEFAULT 0, storage_initial BIGINT NOT NULL DEFAULT 0)",
    # Mirrors every project's source files into TiDB so they survive HF's
    # ephemeral /data (wiped on Space restart/stop). Files are chunked into
    # small BLOBs to stay well under the packet limit.
    "CREATE TABLE IF NOT EXISTS project_files (project_id BIGINT NOT NULL, path VARCHAR(512) NOT NULL, chunk_idx INT NOT NULL, data LONGBLOB NOT NULL, file_size BIGINT NOT NULL, updated_at DOUBLE NOT NULL, PRIMARY KEY (project_id, path, chunk_idx))",
]

_DDL_TABLE_NAMES = [
    "meta", "projects", "env_vars", "backups", "notifications", "activity",
    "system_logs", "users", "schedules", "deploy_history", "counters", "day_metrics",
    "project_files",
]

# Transport/connection-level MySQL error numbers that mean the pooled
# connection is dead and must be discarded and replaced (not the whole
# account). 0 covers the aiomysql "Not connected" state of an idle-closed
# connection; 2003/2006/2013 are classic "server has gone away" family.
_CONN_ERRNOS = {0, 1042, 1043, 1156, 2003, 2006, 2013, 4031}

_CONN_MSG_MARKERS = (
    "not connected",
    "lost connection",
    "gone away",
    "connection reset",
    "server closed",
    "packet sequence",
    "broken pipe",
)


def sql_translate(sql: str) -> str:
    """Translate SQLite dialect to MySQL/TiDB dialect.

    - `?` placeholders -> `%s`
    - AUTOINCREMENT -> AUTO_INCREMENT
    - INSERT OR IGNORE/REPLACE INTO -> INSERT IGNORE/REPLACE INTO
    - ON CONFLICT(...) DO UPDATE SET -> ON DUPLICATE KEY UPDATE
    - excluded.col -> VALUES(col)
    - bare `key` column references -> backticked (reserved word)
    """
    s = sql
    s = s.replace("?", "%s")
    s = s.replace("AUTOINCREMENT", "AUTO_INCREMENT")
    s = s.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
    s = s.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
    s = re.sub(r"\s+COLLATE\s+NOCASE\b", "", s)
    s = re.sub(
        r"\s+ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\s+",
        " ON DUPLICATE KEY UPDATE ",
        s,
    )
    s = re.sub(
        r"\bMAX\s*\(\s*(\w+)\s*,\s*excluded\.\1\s*\)",
        r"GREATEST(\1, VALUES(\1))",
        s,
    )
    s = re.sub(r"\bexcluded\.(\w+)", lambda m: "VALUES(%s)" % m.group(1), s)
    s = re.sub(r"\bkey\b", "`key`", s)
    return s


class _Row:
    """dict-like row supporting row[0], row["col"], dict(row)."""

    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, values):
        self._cols = list(cols)
        self._vals = values
        self._map = dict(zip(cols, values))

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return self._map[k]

    def keys(self):
        return self._cols

    def values(self):
        return list(self._map.values())

    def items(self):
        return list(self._map.items())

    def get(self, k, default=None):
        return self._map.get(k, default)

    def __iter__(self):
        return iter(self._cols)

    def __len__(self):
        return len(self._cols)

    def __contains__(self, k):
        return k in self._map

    def __eq__(self, other):
        if isinstance(other, _Row):
            return self._map == other._map
        if isinstance(other, dict):
            return self._map == other
        return NotImplemented

    def __repr__(self):
        return repr(self._map)


class _Cursor:
    """aiosqlite-compatible cursor that releases its pooled TiDB connection
    once the results are consumed (or close_real() is called)."""

    def __init__(self, real_cur, release=None):
        self._real = real_cur
        self._release = release
        self._cols = None
        self._rowcount = real_cur.rowcount if real_cur is not None else -1
        self._lastrowid = real_cur.lastrowid if real_cur is not None else 0

    async def _finish(self):
        real = self._real
        self._real = None
        if real is not None:
            try:
                await real.close()
            except Exception:
                pass
        cb, self._release = self._release, None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    async def fetchall(self):
        if self._real is None:
            return []
        try:
            rows = await self._real.fetchall()
            desc = self._real.description or []
            self._cols = [d[0] for d in desc]
            return [_Row(self._cols, row) for row in rows]
        finally:
            await self._finish()

    async def fetchone(self):
        if self._real is None:
            return None
        try:
            row = await self._real.fetchone()
            if row is None:
                return None
            desc = self._real.description or []
            self._cols = [d[0] for d in desc]
            return _Row(self._cols, row)
        finally:
            await self._finish()

    async def close_real(self):
        await self._finish()

    @property
    def rowcount(self):
        return self._rowcount

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def description(self):
        return getattr(self._real, "description", None)


class _AsyncManager:
    def __init__(self, accounts):
        self.accounts = accounts
        self.active = 0
        self.failed = [False] * len(accounts)
        self._lock = asyncio.Lock()

    @staticmethod
    def _ssl_ctx():
        return ssl.create_default_context()

    async def _connect(self, idx):
        account = self.accounts[idx]
        return await aiomysql.connect(
            host=account["host"],
            port=account["port"],
            user=account["user"],
            password=account["password"],
            db=account["database"],
            ssl=self._ssl_ctx(),
            connect_timeout=10,
            charset="utf8mb4",
            autocommit=True,
        )

    async def switch_account(self, reason):
        async with self._lock:
            old = self.active
            other = 1 - self.active
            self.failed[old] = True
            self.active = other
            self.failed[other] = False
            logger.warning(
                "FAILOVER: %s -> %s (reason: %s)",
                self.accounts[old]["name"],
                self.accounts[other]["name"],
                reason,
            )

    async def open_active(self):
        for _ in range(len(self.accounts)):
            if self.failed[self.active]:
                await self.switch_account("active account marked failed")
                continue
            try:
                return await self._connect(self.active)
            except Exception as exc:
                logger.warning("Connect %s failed: %s", self.accounts[self.active]["name"], exc)
                await self.switch_account(exc)
        raise tidb_manager.DatabaseFailoverError("Both TiDB accounts unavailable")

    async def _ensure_schema(self, idx):
        account = self.accounts[idx]
        conn = await aiomysql.connect(
            host=account["host"],
            port=account["port"],
            user=account["user"],
            password=account["password"],
            ssl=self._ssl_ctx(),
            connect_timeout=10,
            charset="utf8mb4",
        )
        cur = await conn.cursor()
        await cur.execute("CREATE DATABASE IF NOT EXISTS `%s`" % account["database"])
        await cur.execute("USE `%s`" % account["database"])
        for ddl in DDL:
            try:
                await cur.execute(ddl)
            except Exception as exc:
                logger.warning("DDL skipped (%s): %s", ddl[:60], exc)
        await conn.commit()
        await conn.ensure_closed()

    async def table_exists(self, conn, table):
        cur = await conn.cursor()
        await cur.execute("SHOW TABLES LIKE %s", (table,))
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    def is_benign_create(self, exc, sql):
        """A CREATE TABLE IF NOT EXISTS whose only failure is that an
        equivalent (or stricter) table already exists."""
        if not sql.lstrip().lower().startswith("create"):
            return False
        code = None
        args = getattr(exc, "args", None)
        if args and isinstance(args, (list, tuple)) and args:
            try:
                code = int(args[0])
            except (TypeError, ValueError):
                code = None
        if code not in (1101, 1170, 1171, 1064):
            return False
        m = re.search(r"create\s+table\s+(?:if\s+not\s+exists\s+)?`?(\w+)`?", sql, re.I)
        if not m:
            return False
        return True

    @staticmethod
    def is_conn_error(exc):
        """True when a connection/transport-level failure means this pooled
        connection can no longer be reused (it must be discarded + replaced)."""
        code = None
        args = getattr(exc, "args", None)
        if args and isinstance(args, (list, tuple)) and args:
            try:
                code = int(args[0])
            except (TypeError, ValueError):
                code = None
        if code in _CONN_ERRNOS:
            return True
        try:
            msg = str(exc).lower()
        except Exception:
            msg = ""
        for marker in _CONN_MSG_MARKERS:
            if marker in msg:
                return True
        return False


class _TiDBConnection:
    """aiosqlite-compatible connection backed by a pool of TiDB connections.

    Multiple cursors may be open concurrently (each takes a distinct pooled
    connection), so independent queries no longer serialize behind a single
    connection. A connection is checked out for the full lifetime of its
    cursor and returned to the free-queue when the cursor is consumed or
    closed, which guarantees one connection is never shared by two tasks at
    the same time. Connections use autocommit so a commit() is a no-op.
    """

    def __init__(self, manager):
        self._manager = manager
        self._pool = []           # all open connections (for close/rebuild)
        self._available = asyncio.Queue()   # connections currently free
        self._lock = asyncio.Lock()
        self._schema_checked = False
        self.row_factory = None
        self._in_txn = False

    async def _ensure_pool(self):
        if self._pool:
            return
        async with self._lock:
            if self._pool:
                return
            conns = []
            for _ in range(POOL_SIZE):
                conns.append(await self._manager.open_active())
            for conn in conns:
                self._pool.append(conn)
                self._available.put_nowait(conn)
            if not self._schema_checked:
                await self._migrate_schema()
                self._schema_checked = True

    async def _acquire(self):
        return await self._available.get()

    def _release(self, conn):
        try:
            self._available.put_nowait(conn)
        except Exception:
            pass

    async def _discard(self, conn):
        try:
            await conn.ensure_closed()
        except Exception:
            pass
        try:
            self._pool.remove(conn)
        except ValueError:
            pass

    async def _replenish(self):
        async with self._lock:
            while len(self._pool) < POOL_SIZE:
                try:
                    conn = await self._manager.open_active()
                    self._pool.append(conn)
                    self._available.put_nowait(conn)
                except Exception as exc:
                    logger.warning("TiDB replenish failed: %s", exc)
                    break

    async def _rebuild(self):
        async with self._lock:
            old, self._pool = self._pool, []
            for conn in old:
                try:
                    await conn.ensure_closed()
                except Exception:
                    pass
            self._available = asyncio.Queue()
            for _ in range(POOL_SIZE):
                self._pool.append(await self._manager.open_active())
            for conn in self._pool:
                self._available.put_nowait(conn)
            self._schema_checked = True

    async def _migrate_schema(self):
        conn = await self._acquire()
        cur = await conn.cursor()
        try:
            await cur.execute("SHOW TABLES LIKE 'meta'")
            if await cur.fetchone() is None:
                await self._manager._ensure_schema(self._manager.active)
            else:
                # Existing deployments already have `meta`, so _ensure_schema
                # is skipped on boot; make sure newer tables (project_files)
                # are still created idempotently.
                await self._ensure_ddl(conn)
        finally:
            await cur.close()
            self._release(conn)

    async def _ensure_ddl(self, conn):
        """Run the idempotent DDL list on an existing pooled connection."""
        cur = await conn.cursor()
        try:
            for ddl in DDL:
                try:
                    await cur.execute(ddl)
                except Exception as exc:
                    logger.warning("DDL skipped (%s): %s", ddl[:60], exc)
            await conn.commit()
        finally:
            await cur.close()

    async def execute(self, sql, params=None):
        translated = sql_translate(sql)
        for attempt in range(5):
            await self._ensure_pool()
            conn = await self._acquire()
            cur = None
            try:
                cur = await conn.cursor()
                if params:
                    await cur.execute(translated, params)
                else:
                    await cur.execute(translated)
                return _Cursor(cur, _PoolRelease(self, conn))
            except Exception as exc:
                if cur is not None:
                    try:
                        await cur.close()
                    except Exception:
                        pass
                if self._manager.is_benign_create(exc, sql):
                    self._release(conn)
                    m = re.search(r"create\s+table\s+(?:if\s+not\s+exists\s+)?`?(\w+)`?", sql, re.I)
                    if m and await self._manager.table_exists(conn, m.group(1)):
                        logger.debug("benign create swallowed: %s", sql[:60])
                        return _Cursor(None)
                if self._manager.is_conn_error(exc) or tidb_manager.is_failover_error(exc):
                    logger.warning("TiDB error %s -> discarding connection", exc)
                    await self._discard(conn)
                    if not self._pool:
                        try:
                            await self._manager.switch_account(exc)
                        except Exception:
                            pass
                        await self._rebuild()
                    else:
                        await self._replenish()
                    continue
                self._release(conn)
                raise
        raise RuntimeError("TiDB execute retries exhausted: %s" % sql[:120])

    async def executescript(self, sql):
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur = await self.execute(stmt)
            if cur is not None and getattr(cur, "close_real", None):
                try:
                    await cur.close_real()
                except Exception:
                    pass
        await self.commit()

    async def commit(self):
        self._in_txn = False

    async def rollback(self):
        self._in_txn = False

    async def close(self):
        async with self._lock:
            pool, self._pool = self._pool, []
            self._available = asyncio.Queue()
        for conn in pool:
            try:
                await conn.ensure_closed()
            except Exception:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class _PoolRelease:
    """Release callback bound to a specific pooled connection; returns it to
    the free queue exactly once."""

    __slots__ = ("_owner", "_conn", "_done")

    def __init__(self, owner, conn):
        self._owner = owner
        self._conn = conn
        self._done = False

    def __call__(self):
        if not self._done:
            self._done = True
            self._owner._release(self._conn)


_manager = None
_orig_connect = None


async def _patched_connect(database, *args, **kwargs):
    """Route the bot's aiosqlite.connect() to the TiDB failover layer."""
    global _manager
    if _manager is not None:
        return _TiDBConnection(_manager)
    return await _orig_connect(database, *args, **kwargs)


async def bootstrap():
    """Enable the TiDB layer if both accounts are reachable. Returns True when
    the bot should talk to TiDB, False to keep the local SQLite database."""
    global _manager, _orig_connect
    accounts = tidb_manager._load_accounts()
    if len(accounts) < 2:
        logger.warning("TiDB disabled: need both accounts configured")
        return False

    mgr = _AsyncManager(accounts)
    healthy_idx = None
    for idx in range(len(accounts)):
        try:
            await mgr._ensure_schema(idx)
            if healthy_idx is None:
                healthy_idx = idx
        except Exception as exc:
            logger.error("Schema init failed on %s: %s", accounts[idx]["name"], exc)

    if healthy_idx is None:
        logger.error("TiDB FAILOVER: both accounts unavailable - falling back to local SQLite")
        return False

    # Ensure schema on EVERY account so failover never lands on a database
    # that is missing newer tables (e.g. project_files).
    mgr.active = healthy_idx
    mgr.failed = [False] * len(accounts)
    logger.info("TiDB schema ready on %s", accounts[healthy_idx]["name"])

    import aiosqlite

    _orig_connect = aiosqlite.connect
    aiosqlite.connect = _patched_connect
    _manager = mgr
    logger.info("TiDB async failover ENABLED (%s -> %s) pool=%d",
                accounts[0]["name"], accounts[1]["name"], POOL_SIZE)
    return True

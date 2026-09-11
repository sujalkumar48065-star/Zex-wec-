#!/usr/bin/env python3
"""render_hbi.py - HBI bot runner (subprocess).

Runs the Hbi (HOSTING2X / @HOSTING2X_ROBOT) bot in a SEPARATE process from the
VIP panel bot so its global `sqlite3 -> TiDB` shim cannot leak into the VIP
bot. It runs in POLLING mode only and does NOT start its own Flask server
(render_app.py owns the public $PORT), so there is no port conflict.

Env contract (all optional, read from the shared Render env):
  HOSTING2X_BOT_TOKEN   Hbi bot token (defaults to the baked-in one)
  OWNER_ID              owner telegram id
  TIDB1_HOST/USER/PASS  primary TiDB account (mandatory for persistence)
  TIDB2_HOST/USER/PASS  failover TiDB account (optional)
  TIDB_DEFAULT_DB       default database (default zex_hosting)
  FILE_CRYPT_KEY        key used to encrypt user-uploaded files (optional)
  TG_API_PROXY          optional outbound Telegram proxy override
"""
import os
import sys
import time
import threading
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("render_hbi")

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# sqlite3 -> TiDB shim MUST be injected before Hbi import (and only in this process)
import tidb_shim  # noqa: E402
sys.modules["sqlite3"] = tidb_shim

# Telegram API (Render reaches api.telegram.org directly; keep proxy override)
import telebot  # noqa: E402

proxy = os.environ.get("TG_API_PROXY", "").rstrip("/")
if proxy:
    if not proxy.endswith("/bot"):
        proxy = proxy + "/bot"
    telebot.apihelper.API_URL = proxy + "{0}/{1}"
    telebot.apihelper.FILE_URL = proxy.replace("/bot", "/file/bot") + "{0}/{1}"
    telebot.apihelper.TIMEOUT = 60
    log.info("Outbound API_URL via proxy: %s", telebot.apihelper.API_URL)
else:
    telebot.apihelper.TIMEOUT = 60
    log.info("Using direct api.telegram.org (no proxy)")

# import bot module (registers handlers; init_db()+load_data() at module level)
import Hbi as hx  # noqa: E402


def _background_sync():
    """Run the TiDB-heavy file/web sync in a background thread so a slow or
    stalled TiDB query can NEVER keep the bot off-line. Every step below is
    time-bounded by tidb_shim (read/write timeouts + failover reconnect) and
    runs concurrently with polling. Each step is isolated so a single stalled
    query only delays that one step and the periodic sync threads are started
    as early as possible."""
    import file_sync
    import web_sync

    # Start the periodic sync loops FIRST so new user uploads mirror to TiDB
    # even if the one-time bulk restore below stalls on a slow query.
    try:
        file_sync.start_thread(hx.BASE_DIR)
    except Exception as e:
        log.error("file_sync periodic thread failed to start: %s", e)
    _web_dir = os.path.join(hx.BASE_DIR, "web_files")
    try:
        os.makedirs(_web_dir, exist_ok=True)
        web_sync.start_thread(_web_dir)
    except Exception as e:
        log.error("web_sync periodic thread failed to start: %s", e)

    for name, fn in [
        ("file_sync.ensure_schema", file_sync.ensure_schema),
        ("file_sync.migrate_encrypt", file_sync.migrate_encrypt),
        ("web_sync.ensure_schema", web_sync.ensure_schema),
    ]:
        try:
            fn()
        except Exception as e:
            log.error("%s failed: %s", name, e)

    try:
        _restored = file_sync.restore_all(hx.BASE_DIR)
        log.info("File store restored %d files from TiDB", _restored)
    except Exception as e:
        log.error("file restore failed: %s", e)
    try:
        _restored_web = web_sync.restore_files(_web_dir)
        log.info("Web files restored %d from TiDB", _restored_web)
    except Exception as e:
        log.error("web restore failed: %s", e)

    try:
        hx._restore_after_reboot()
    except Exception as e:
        log.error("reboot restore failed: %s", e)


threading.Thread(target=_background_sync, daemon=True, name="hbi-bg-sync").start()
log.info("Background TiDB sync thread started (polling will start immediately)")


def _poll_loop():
    import requests
    while True:
        try:
            hx.bot.infinity_polling(logger_level=logging.INFO, timeout=60, long_polling_timeout=30)
        except requests.exceptions.ReadTimeout:
            log.warning("Hbi polling ReadTimeout. Restart in 5s...")
            time.sleep(5)
        except requests.exceptions.ConnectionError:
            log.error("Hbi polling ConnectionError. Retry 15s...")
            time.sleep(15)
        except Exception as e:
            log.critical("Hbi polling error: %s", e)
            time.sleep(30)
        finally:
            log.warning("Hbi polling attempt ended, restarting if in loop.")
            time.sleep(1)


if __name__ == "__main__":
    log.info("Hbi bot runner started in subprocess mode (polling first, sync in background).")
    _poll_loop()

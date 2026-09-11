#!/usr/bin/env python3
# render_app.py
# Render.com entry point for the Telegram Hosting Panel.
#
# Runs the bot in WEBHOOK mode on a Render web service:
#   - Flask serves the webhook + health endpoints on $PORT (Render sets it).
#   - Telegram sends updates INBOUND to  https://<service>.onrender.com/webhook/<secret>
#   - Outbound Telegram API calls go DIRECTLY to api.telegram.org (Render can
#     reach it, unlike HuggingFace Spaces), unless TG_API_PROXY is set.
#   - Data persists under the Render Persistent Disk (/var/data) via
#     HOSTING_DATA_DIR, or falls back to TiDB (2-account) if configured.
#   - A keep-alive thread pings /health so the service stays responsive; the
#     render.yaml cron job pings the public URL every 5 min so Render's free
#     web service never sleeps (~15 min idle timeout).

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, 'src')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# Persist bot data. On Render free there is usually NO persistent disk, so the
# requested mount (/var/data) may not exist or be writable. Try it first, then
# transparently fall back to a guaranteed-writable dir (app root or /tmp) so a
# permissions problem can never crash startup. Note: without a disk the data is
# ephemeral and wipes on redeploy; TiDB (if configured) survives regardless.
_DEFAULT_DATA = '/var/data/hosting_data'
_REQUESTED = os.environ.get('HOSTING_DATA_DIR', _DEFAULT_DATA)


def _resolve_data_dir() -> str:
    candidates = [_REQUESTED]
    if _REQUESTED != _DEFAULT_DATA:
        candidates.append(_DEFAULT_DATA)
    candidates += [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hosting_data'),
        '/tmp/hosting_data',
    ]
    for cand in candidates:
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, '.write_test')
            with open(probe, 'w') as fh:
                fh.write('ok')
            os.remove(probe)
            return cand
        except Exception:
            continue
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hosting_data')


DATA_DIR = _resolve_data_dir()
os.environ['HOSTING_DATA_DIR'] = DATA_DIR
for sub in ('projects', 'logs', 'tmp'):
    os.makedirs(os.path.join(DATA_DIR, sub), exist_ok=True)
logger.info("HOSTING_DATA_DIR resolved to: %s", DATA_DIR)

# Outbound Telegram Bot API calls go directly to api.telegram.org on Render.
# Set TG_API_PROXY to route via a proxy instead (only for HF-like setups).
TG_PROXY = (os.environ.get('TG_API_PROXY') or '').strip().rstrip('/')
if TG_PROXY:
    from telegram.ext import ApplicationBuilder
    from telegram._utils.defaultvalue import DefaultValue

    _orig_build = ApplicationBuilder.build

    def _patched_build(self):
        if isinstance(self._base_url, DefaultValue):
            self._base_url = TG_PROXY + '/bot'
        if isinstance(self._base_file_url, DefaultValue):
            self._base_file_url = TG_PROXY + '/file/bot'
        return _orig_build(self)

    ApplicationBuilder.build = _patched_build
    logger.info("Telegram API routed via proxy: %s", TG_PROXY)
else:
    logger.info("Telegram API routed directly to api.telegram.org")

import tidb_aiosqlite
import main as bot_module


def run_bot():
    async def _boot():
        ti_enabled = await tidb_aiosqlite.bootstrap()
        logger.info("TiDB async failover: %s",
                    "ENABLED" if ti_enabled else "OFF (local SQLite fallback)")
        await bot_module._run_async()

    try:
        asyncio.run(_boot())
    except Exception as exc:
        logger.error("Bot fatal error: %s", exc, exc_info=True)


def _bot_supervisor():
    """Remote self-healing: if the bot thread dies (crash, exception, webhook
    loop exit), restart it automatically with a short backoff so the panel never
    stays dead until the next manual deploy."""
    failures = 0
    while True:
        t = threading.Thread(target=run_bot, daemon=True)
        t.start()
        t.join()
        failures += 1
        delay = min(10 * failures, 120)
        logger.error("Bot thread exited (failure #%s); restarting in %ss", failures, delay)
        time.sleep(delay)
        if failures >= 12:
            failures = 0


def _hbi_supervisor():
    """Remote self-healing for the HBI bot (separate subprocess).

    The HBI bot runs `render_hbi.py` as its own OS process so its global
    `sqlite3 -> TiDB` shim cannot interfere with the VIP bot in this process.
    This supervisor (re)launches it and restarts it if it ever exits."""
    global _hbi_proc
    run_dir = os.path.dirname(os.path.abspath(__file__))
    failures = 0
    while True:
        log_line = os.path.join(DATA_DIR, 'logs', 'hbi_sub.log')
        os.makedirs(os.path.dirname(log_line), exist_ok=True)
        env = dict(os.environ)
        env['HBI_SUBPROCESS'] = '1'
        # The VIP bot in THIS process reads TIDB1_*/TIDB2_* too (its bootstrap()
        # enables TiDB when both accounts are present), so we must NOT leak the
        # Hbi credentials into the shared env. Inject them ONLY into the child:
        #   HBI_TIDB1_* / HBI_TIDB2_*  ->  TIDB1_* / TIDB2_*  (child only)
        #   HBI_BOT_TOKEN              ->  HOSTING2X_BOT_TOKEN (child only)
        for prefix in ('HBI_TIDB1', 'HBI_TIDB2'):
            dst = prefix.replace('HBI_', '')
            if os.environ.get(prefix + '_HOST'):
                env[dst + '_HOST'] = os.environ[prefix + '_HOST']
            if os.environ.get(prefix + '_PORT'):
                env[dst + '_PORT'] = os.environ[prefix + '_PORT']
            if os.environ.get(prefix + '_USER'):
                env[dst + '_USER'] = os.environ[prefix + '_USER']
            if os.environ.get(prefix + '_PASS'):
                env[dst + '_PASS'] = os.environ[prefix + '_PASS']
            if os.environ.get(prefix + '_DB'):
                env[dst + '_DB'] = os.environ[prefix + '_DB']
        if os.environ.get('HBI_TIDB_DB'):
            env['TIDB_DEFAULT_DB'] = os.environ['HBI_TIDB_DB']
        if os.environ.get('HBI_BOT_TOKEN'):
            env['HOSTING2X_BOT_TOKEN'] = os.environ['HBI_BOT_TOKEN']
        try:
            proc = subprocess.Popen(
                [sys.executable, 'render_hbi.py'],
                cwd=run_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            _hbi_proc = proc
            logger.info("Hbi subprocess started (pid=%s)", proc.pid)

            def _tee_child():
                try:
                    with open(log_line, 'ab', buffering=0) as fh:
                        for raw in iter(proc.stdout.readline, b''):
                            line = raw.decode('utf-8', 'replace').rstrip()
                            if not line:
                                continue
                            fh.write((line + '\n').encode('utf-8'))
                            logger.info("[HBI] %s", line)
                except Exception as exc:
                    logger.error("Hbi log tee error: %s", exc)

            threading.Thread(target=_tee_child, daemon=True).start()
            code = proc.wait()
            failures += 1
            delay = min(10 * failures, 60)
            logger.error("Hbi subprocess exited code=%s (failure #%s); restarting in %ss",
                         code, failures, delay)
            time.sleep(delay)
            if failures >= 12:
                failures = 0
        except Exception as exc:
            failures += 1
            logger.error("Hbi supervisor error: %s", exc)
            time.sleep(10)


if os.environ.get('PANEL_ENABLE') == '1':
    threading.Thread(target=_bot_supervisor, daemon=True).start()
    logger.info("VIP panel bot supervisor started")
else:
    logger.info("VIP panel bot DISABLED (set PANEL_ENABLE=1 to re-enable)")
_hbi_proc = None
if os.environ.get('HBI_DISABLE') != '1':
    threading.Thread(target=_hbi_supervisor, daemon=True).start()
    logger.info("Hbi subprocess supervisor started")


def _public_base_url() -> str:
    """Resolve the external base URL, preferring Render's auto-injected env vars
    (no manual PUBLIC_BASE_URL needed), then explicit overrides. Returns '' when
    no URL is resolvable."""
    rext = (os.environ.get('RENDER_EXTERNAL_URL') or os.environ.get('RENDER_EXTERNAL_HOSTNAME') or '')
    railway = os.environ.get('RAILWAY_PUBLIC_DOMAIN') or os.environ.get('RAILWAY_STATIC_URL') or ''
    manual = (os.environ.get('PUBLIC_BASE_URL') or os.environ.get('HOST_URL') or '')
    base = rext or railway or manual
    base = base.strip()
    if not base:
        return ''
    if not base.startswith('http'):
        base = 'https://' + base
    return base.rstrip('/')


# Resolve and publish the public base URL at module level so it is available both
# when run directly and under gunicorn (gunicorn imports this module).
PORT = int(os.environ.get('PORT', 10000))
BASE_URL = _public_base_url()
bot_module.WEBHOOK_SECRET = os.environ.get('HOSTING_WEBHOOK_SECRET', 's3cret_wbhk')
if BASE_URL:
    os.environ.setdefault('PUBLIC_BASE_URL', BASE_URL)
    logger.info("Public base URL: %s (port %s)", BASE_URL, PORT)


def _keep_alive():
    """Inbuilt uptime robot: periodically hit our own health endpoint so the
    Render free web service never looks idle. A localhost hit alone does NOT
    keep Render awake — the ping MUST go out through the load balancer, so we
    always hit the external URL first (using Render's RENDER_EXTERNAL_URL).
    Pings every 60s and logs failures."""
    targets = ['http://127.0.0.1:%d/health' % PORT]
    if BASE_URL:
        targets.append(BASE_URL + '/health')
    ok = 0
    while True:
        for target in targets:
            try:
                urllib.request.urlopen(target, timeout=15).read()
                ok += 1
            except Exception as exc:
                ok = 0
                logger.error("keep-alive ping %s FAILED: %s", target, exc)
        if ok % 20 == 0 and ok > 0:
            logger.info("uptime robot alive, pings ok=%s", ok)
        time.sleep(60)


threading.Thread(target=_keep_alive, daemon=True).start()
logger.info("Inbuilt uptime robot started (pings /health every 60s)")

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)


@app.route('/')
def home():
    return 'HOSTING BOT is running 24/7'


@app.route('/webhook/<secret>', methods=['POST'])
def webhook(secret):
    if secret != bot_module.WEBHOOK_SECRET:
        return jsonify(status='invalid_secret'), 403
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify(status='no_payload'), 400
    ok = bot_module.deliver_webhook_update(payload)
    return jsonify(status='ok' if ok else 'processing_failed'), (200 if ok else 503)


@app.route('/api/status')
def api_status():
    try:
        projects = bot_module.asyncio.run_coroutine_threadsafe(
            bot_module.list_projects(), bot_module._loop).result(timeout=10)
    except Exception as exc:
        return jsonify(error='unavailable', detail=str(exc)), 502
    out = []
    for p in projects:
        out.append({
            'id': p.get('id'),
            'name': p.get('name'),
            'status': p.get('status'),
            'pid': p.get('pid'),
            'error': p.get('error'),
            'crash_count': p.get('crash_count'),
            'auto_restart': p.get('auto_restart'),
        })
    return jsonify(projects=out)


@app.route('/api/logs/<int:pid>/<logtype>')
def api_logs(pid, logtype):
    if logtype not in ('runtime', 'error', 'deploy'):
        return jsonify(error='bad logtype'), 400
    try:
        text = bot_module.asyncio.run_coroutine_threadsafe(
            bot_module.tail_log(pid, logtype, 40), bot_module._loop).result(timeout=10)
    except Exception as exc:
        return jsonify(error='unavailable', detail=str(exc)), 502
    return jsonify(pid=pid, logtype=logtype, content=text)


@app.route('/health')
def health():
    try:
        db_ok = bot_module.health_db_ok()
    except Exception:
        db_ok = False
    if bot_module.bot_polling_alive():
        return jsonify(status='ok', bot='running', polling=True, db=bool(db_ok))
    if db_ok:
        return jsonify(status='ok', bot='starting', polling=False, db=True)
    return jsonify(status='degraded', bot='restarting', polling=False, db=False)


@app.route('/admin/hbi')
def admin_hbi():
    _p = _hbi_proc
    alive = bool(_p) and _p.poll() is None
    tail = ''
    try:
        inline = os.path.join(DATA_DIR, 'logs', 'hbi_sub.log')
        with open(inline, 'r', errors='replace') as fh:
            tail = ''.join(fh.readlines()[-60:])
    except Exception as exc:
        tail = 'log unavailable: %s' % exc
    return jsonify(alive=alive,
                   returncode=(_p.poll() if _p else None),
                   pid=(_p.pid if _p else None),
                   log=tail)


_PANEL_SECRET = (os.environ.get('HOSTING_WEBHOOK_SECRET') or 's3cret_wbhk')


if __name__ == '__main__':
    # Direct run (single-process dev / Render when not using gunicorn).
    logger.info("Health server + internal keep-alive on port %s", PORT)
    app.run(host='0.0.0.0', port=PORT)

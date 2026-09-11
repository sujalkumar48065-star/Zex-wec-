#!/usr/bin/env python3
"""Minimal Render runner — polling mode bot + Flask health check."""
import asyncio
import logging
import os
import sys
import threading
import time
import urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger('render_runner')

DATA_DIR = os.environ.get('HOSTING_DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hosting_data'))
os.makedirs(os.path.join(DATA_DIR, 'projects'), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, 'logs'), exist_ok=True)
os.environ['HOSTING_DATA_DIR'] = DATA_DIR

import main as bot_module

_bot_alive = False

def _run_bot():
    global _bot_alive
    tok = os.environ.get('HOSTING_BOT_TOKEN', '')
    if not tok:
        log.error('HOSTING_BOT_TOKEN not set')
        return
    def _boot():
        try:
            import tidb_aiosqlite
            asyncio.run(tidb_aiosqlite.bootstrap())
        except Exception as e:
            log.warning('TiDB skip: %s', e)
        asyncio.run(bot_module._run_polling_async())

    failures = 0
    while True:
        t = threading.Thread(target=_boot, daemon=True)
        t.start()
        t.join()
        _bot_alive = True
        failures += 1
        delay = min(10 * failures, 120)
        log.error('Bot exited (failure #%d); restart in %ds', failures, delay)
        time.sleep(delay)
        if failures >= 20:
            failures = 0

log.info('Starting bot supervisor thread...')
threading.Thread(target=_run_bot, daemon=True).start()

def _keep_alive():
    port = int(os.environ.get('PORT', 10000))
    targets = ['http://127.0.0.1:%d/health' % port]
    base = (os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    if base:
        targets.append(base + '/health')
    while True:
        for t in targets:
            try:
                urllib.request.urlopen(t, timeout=15).read()
            except Exception:
                pass
        time.sleep(60)

threading.Thread(target=_keep_alive, daemon=True).start()

from flask import Flask, jsonify, request
app = Flask(__name__)

@app.route('/')
def home():
    return 'HOSTING BOT is running 24/7'

@app.route('/health')
def health():
    return jsonify(status='ok', bot='running'), 200

@app.route('/webhook/<secret>', methods=['POST'])
def webhook(secret):
    global _bot_alive
    if not bot_module.bot_polling_alive():
        return jsonify(status='bot_stopped'), 503
    if secret != os.environ.get('HOSTING_WEBHOOK_SECRET', 's3cret_wbhk'):
        return jsonify(msg='bad secret'), 403
    payload = request.get_json(force=True, silent=True) or {}
    ok = bot_module.deliver_webhook_update(payload)
    log.info('Webhook update: update_id=%s delivered=%s', (payload.get('update_id')), ok)
    if not ok:
        return jsonify(status='queued_or_failed'), 202
    return jsonify(status='ok'), 200

PORT = int(os.environ.get('PORT', 10000))
log.info('Health server on port %d', PORT)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)

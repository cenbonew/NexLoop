"""Web layout batch 8 (2026-10-18): the workbench Action page on real intents in a real browser engine (headless Chromium).

Four synthetic intents through the actual governed paths (commitment fixture, independent effect Worker, loopback providers):
fulfilled (provider completes, Worker finalizes), unknown (the provider answers 503 to the POST), dispatching with the outbox in
query_pending (provider accepted, awaiting the query), failed (never dispatched; its origin Run expired and the executor settles
it). The page must show each as a worded badge with its tone, the technical code only in the title, the dispatch column in
words ("无需再次派发" for a finished intent) and the observation time as a <time datetime>. Opt-in lane (`-m real_browser`);
synthetic data; REAL_DISPATCH_ENABLED=false; run serially."""
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import execution_plan, governed_effect_executor  # noqa: F401
from test_agent_host import free_port
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_sse_real_browser import ROOT, build_web
from test_workbench_read_pg import WORKBENCH_APP
from test_workbench_ui_pg import ui  # noqa: F401
from workbench_stack_fixture import private

pytestmark = [pytest.mark.real_browser, pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)]


class _Unavailable(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        self.send_response(503)
        self.send_header('Content-Length', '0')
        self.end_headers()
    do_GET = do_POST = _reply


def state(admin, intent):
    return admin.execute('select i.state,o.state from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id) where intent_id=%s',
                         (intent,)).fetchone()


def test_action_page_badges_on_real_intents(ui, admin, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration, create_app
    from support.effect_provider import effect_provider
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    c = ui
    c.configure_categories()
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        done = c.submit({'service': '合成服务 A'})['intent_id']
        c.deliver(provider, done)
        # Unknown, then at once dispatching: the unknown outbox backs off ~3 s, so the next Worker claims the new intent.
        unknown = c.submit({'service': '合成服务 B'})['intent_id']
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Unavailable)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            assert c.dispatch(SimpleNamespace(origin=f'http://127.0.0.1:{server.server_address[1]}'))['status'] == 'unknown'
        finally:
            server.shutdown()
        pending = c.submit({'service': '合成服务 C'})['intent_id']
        c.dispatch(provider)
    failed = c.submit({'service': '合成服务 D'})['intent_id']
    run = admin.execute('select run_id from runtime.nexloop_effect_submissions where intent_id=%s', (failed,)).fetchone()[0]
    admin.execute("update authz.nexloop_run_credentials set issued_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 second' where run_id=%s", (run,))
    with c['fixture'].current_executor() as executor:
        assert executor.settle_expired_origin(intent_id=failed) == 'failed'
    assert [state(admin, i) for i in (done, unknown, pending, failed)] == [('fulfilled', 'fulfilled'), ('unknown', 'unknown'), ('dispatching', 'query_pending'), ('failed', 'failed')]

    web = tmp_path / 'web'
    build_web(web)
    port = free_port()
    origin = f'https://127.0.0.1:{port}'
    certificate, key = tmp_path / 'tls-cert.pem', tmp_path / 'tls-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(certificate), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    config = ApiConfiguration(tmp_path / 'business-dsn', tmp_path / 'authority-key', tmp_path / 'app-artifacts', c['signer'].key_id,
        BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, 'synthetic-browser-app', origin), web_root=web,
        execution_profile='deterministic-test', workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, origin))
    server = uvicorn.Server(uvicorn.Config(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                                           access_log=False, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), 'API did not start'
        time.sleep(0.1)
    shots = Path(os.environ.get('NEXLOOP_LAYOUT_SCREENSHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    owner = c['staff']['owner']
    session = private(tmp_path / 'session.json', json.dumps({'origin': origin, 'owner': {'username': owner['username'],
        'password_file': str(private(tmp_path / 'owner-password', owner['password']))}}))
    result = tmp_path / 'result.json'
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_ACTIONS_E2E_SESSION_FILE=str(session), NEXLOOP_ACTIONS_E2E_RESULT_FILE=str(result), NEXLOOP_ACTIONS_E2E_SCREENSHOTS=str(shots))
        run = subprocess.run([shutil.which('node'), 'test/browser/workbench-actions.browser.mjs'], cwd=ROOT / 'apps/web', env=env, capture_output=True, text=True, timeout=180)
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-3000:]
        assert owner['password'] not in run.stdout + run.stderr
        observed_page = json.loads(result.read_text())
        rows = observed_page['rows']
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    # Newest first: failed (D), dispatching (C), unknown (B), fulfilled (A).
    assert [(r['text'], r['tone'], r['code']) for r in rows] == [('失败', 'tone-fail', 'failed'), ('派发中', 'tone-pending', 'dispatching'),
                                                              ('待核对', 'tone-pending', 'unknown'), ('渠道已完成', 'tone-ok', 'fulfilled')], rows
    assert rows[0]['dispatch'].startswith('不适用') and rows[3]['dispatch'] == '无需再次派发'
    assert all('not_queued' not in (r['dispatch'] or '') for r in (rows[0], rows[3]))
    observed = [r['time'] for r in rows if r['time']]
    assert observed and all(t['datetime'] and t['title'].startswith('北京时间') and '（' in t['text'] for t in observed), observed
    # The live region announces the count for the current filter; the filter keeps focus; visually hidden.
    assert observed_page['live'] == {'all': '全部：4 条', 'failed': '失败：1 条', 'focus_kept': True, 'attrs': ['polite', 'status', True]}, observed_page['live']

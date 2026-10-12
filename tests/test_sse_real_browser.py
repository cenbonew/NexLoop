"""AT-015 in a real browser engine (headless Chromium via Playwright): the built WebChat, served by the actual API over TLS on
127.0.0.1, survives an offline period, a page reload and an API restart, showing every committed message once and in order on
the same conversation (apps/web/test/browser/sse-resume.browser.mjs).

Opt-in lane, not part of the default stages: marked `real_browser` and deselected unless selected with `-m real_browser`
(see tests/conftest.py). Needs Node 24 (`nvm use 24`), the pinned Playwright of apps/web and its Chromium headless shell
(docs/implementation/AT-015-real-browser.md). Synthetic tenant and fixture credentials only; run serially."""
import contextlib
import json
import os
import secrets
import shutil
import ssl
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from psycopg.conninfo import make_conninfo

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_agent_host import files, free_port
from test_precise_pg_admission_outage import process, wait_live
from test_trusted_configuration_pg import private

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.real_browser


def build_web(out):
    pnpm = shutil.which('pnpm')
    assert pnpm, 'pnpm (Node 24) is required for the real browser test'
    # A private output directory: the shared apps/web/dist is left alone (serial-build owns it).
    run = subprocess.run([pnpm, '--filter', '@nexloop/web', 'exec', 'vite', 'build', '--outDir', str(out), '--emptyOutDir'], cwd=ROOT,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    return pnpm


def test_real_chromium_webchat_resumes_after_offline_reload_and_api_restart(assembled_message, admin, tmp_path):  # noqa: F811
    f = assembled_message
    o = f['original']
    p = o['paths']
    port = free_port()  # the API
    origin_port = free_port()  # the page's origin: the relay of the browser script, standing for the network
    origin = f'https://127.0.0.1:{origin_port}'
    api = f'https://127.0.0.1:{port}'  # direct calls carry the origin's Host, as the relayed browser requests do
    web = tmp_path / 'web'
    pnpm = build_web(web)
    dsn = private(tmp_path, 'api-dsn', make_conninfo(o['pg'], user='nexloop_api'))
    rate = private(tmp_path, 'rate-key', secrets.token_hex(32))
    tls = tmp_path / 'tls'
    tls.mkdir(mode=0o700)
    files(tls)
    cert = tls / 'host-cert.pem'
    args = ['--database-url-file', str(dsn), '--signing-key-file', str(p['backend_signing']), '--signing-key-id', 'explicit-configuration',
            '--artifact-root', str(tmp_path / 'artifacts'), '--mode', 'test', '--execution-profile', 'deterministic-test', '--port', str(port),
            '--tls-certificate-file', str(cert), '--tls-key-file', str(tls / 'host-key.pem'), '--identity-database-url-file', str(p['identity']),
            '--browser-rate-key-file', str(rate), '--browser-tenant-id', o['tenant'], '--browser-application-id', o['application'],
            '--browser-origin', origin, '--web-root', str(web)]
    hidden = tuple(f['tokens'].values()) + (p['password'].read_text(),)
    control = tmp_path / 'control'
    control.mkdir(mode=0o700)
    result = tmp_path / 'browser-result.json'
    verify = ssl.create_default_context(cafile=str(cert))
    with contextlib.ExitStack() as stack:
        child = stack.enter_context(process('nexloop_eios.http_api', args, hidden))
        with httpx.Client(base_url=api, verify=verify, trust_env=False, timeout=5, headers={'Host': f'127.0.0.1:{origin_port}'}) as client:
            wait_live(child, client)
            login = client.post('/api/v1/auth/login', headers={'Origin': origin}, json={'username': 'explicit-user', 'password': p['password'].read_text()})
            assert login.status_code == 200
            csrf = login.json()['csrf_token']
            headers = {'Origin': origin, 'X-CSRF-Token': csrf}
            created = client.post('/api/v1/conversations', headers={**headers, 'Idempotency-Key': 'browser-resume-conversation-key'}, json={})
            assert created.status_code == 200
            cid = created.json()['id']
            for index in (1, 2):
                r = client.post(f'/api/v1/conversations/{cid}/messages', headers={**headers, 'Idempotency-Key': f'browser-resume-stable-{index}'},
                                json={'body': f'browser resume {index}'})
                assert r.status_code == 202
            cookie = '; '.join(f'{k}={v}' for k, v in client.cookies.items())
        session = private(tmp_path, 'browser-session', json.dumps({'origin': origin, 'origin_port': origin_port, 'api_port': port, 'ca_file': str(cert), 'cookie': cookie, 'csrf': csrf,
                                                                   'conversation_id': cid, 'username': 'explicit-user', 'password_file': str(p['password'])}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_BROWSER_E2E_SESSION_FILE=str(session), NEXLOOP_BROWSER_E2E_RESULT_FILE=str(result), NEXLOOP_BROWSER_E2E_CONTROL_DIR=str(control))
        node = subprocess.Popen([shutil.which('node'), 'test/browser/sse-resume.browser.mjs'], cwd=ROOT / 'apps/web', env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        end = time.monotonic() + 180
        restarted = False
        while node.poll() is None:
            assert time.monotonic() < end, 'browser run timed out'
            if not restarted and (control / 'restart-request').exists():
                stack.close()  # stops the API (terminate, then the secret check of process())
                child = stack.enter_context(process('nexloop_eios.http_api', args, hidden))
                with httpx.Client(base_url=api, verify=verify, trust_env=False, timeout=5, headers={'Host': f'127.0.0.1:{origin_port}'}) as client:
                    wait_live(child, client)
                (control / 'restarted').write_text('1')
                restarted = True
            time.sleep(.1)
        stdout, stderr = node.communicate(timeout=10)
        assert node.returncode == 0, stdout[-4000:] + stderr[-3000:]
        assert all(v not in stdout + stderr for v in (*hidden, csrf, cookie))
        observed = json.loads(result.read_text())
    want = [f'browser resume {i}' for i in range(1, 9)]
    phases = observed['phases']
    assert restarted
    assert phases['initial'] == {'shown': want[:2], 'conversation_matches': True}
    offline = phases['offline']
    assert offline['shown_while_offline'] == want[:3]  # nothing arrives while the browser is offline
    assert offline['shown_after'] == want[:5] and offline['distinct'] == 5 and offline['conversation_matches'] and offline['unavailable_cleared']
    assert phases['reload'] == {'shown': want[:5], 'conversation_matches': True}
    assert phases['restart'] == {'shown': want, 'distinct': 8, 'conversation_matches': True}
    streams = observed['stream_requests']
    # The browser's own EventSource resumes with Last-Event-ID after the offline period and after the restart.
    resumed = [s['last_event_id'] for s in streams if s['last_event_id']]
    assert '3' in resumed and '6' in resumed, streams
    assert observed['relay_connections'] >= 3  # initial, after the offline period, after the restart
    assert admin.execute('select count(*) from runtime.nexloop_message_outbox').fetchone() == (8,)
    output = os.environ.get('NEXLOOP_REAL_BROWSER_EVIDENCE_FILE')
    if output:
        assert Path(output).is_absolute()
        Path(output).write_text(json.dumps({'conversation_id': cid, **observed}, indent=2) + '\n')

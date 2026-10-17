"""Web layout batch 1 acceptance (docs/implementation/web-layout-audit-20261018.md §4.2) in a real browser engine (headless Chromium via
Playwright): the built Web served by the production app over loopback HTTPS on the synthetic workbench stack
(tests/workbench_stack_fixture.py), measured by apps/web/test/browser/web-layout.browser.mjs at 1440, 1024 and 390 px, dark at 1440
and 390.

Opt-in lane like tests/test_sse_real_browser.py: marked `real_browser`, deselected unless `-m real_browser`. Needs Node 24, the pinned
Playwright of apps/web and its Chromium headless shell. Synthetic accounts only; REAL_DISPATCH_ENABLED=false; run serially.
NEXLOOP_LAYOUT_SCREENSHOTS (an absolute directory) keeps the screenshots; otherwise they stay in the test's temp directory."""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_agent_host import free_port
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_sse_real_browser import ROOT, build_web
from workbench_stack_fixture import private, workbench_stack

pytestmark = pytest.mark.real_browser
PAGES = ('customer-login', 'customer-webchat', 'wb-login', 'wb-overview', 'wb-consumers', 'wb-consumer', 'wb-conversation', 'wb-settings')


def test_real_chromium_web_layout_batch_one(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from nexloop_eios.http_api import create_app
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    web = tmp_path / 'web'
    build_web(web)
    port = free_port()
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=web)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                                           access_log=False, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), 'API did not start'
        time.sleep(0.1)
    shots = Path(os.environ.get('NEXLOOP_LAYOUT_SCREENSHOTS') or tmp_path / 'shots')
    assert shots.is_absolute()
    shots.mkdir(parents=True, exist_ok=True)
    account = lambda who: {'username': credentials[who]['username'], 'password_file': str(private(tmp_path / f'{who}-password', credentials[who]['password']))}  # noqa: E731
    session = private(tmp_path / 'session.json', json.dumps({'origin': credentials['origin'], 'owner': account('owner'), 'customer': account('customer'),
        'consumer_id': credentials['consumer_id'], 'conversation_id': credentials['conversation_id']}))
    result = tmp_path / 'result.json'
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_LAYOUT_E2E_SESSION_FILE=str(session), NEXLOOP_LAYOUT_E2E_RESULT_FILE=str(result), NEXLOOP_LAYOUT_E2E_SCREENSHOTS=str(shots))
        run = subprocess.run([shutil.which('node'), 'test/browser/web-layout.browser.mjs'], cwd=ROOT / 'apps/web', env=env, capture_output=True, text=True, timeout=300)
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-3000:]
        assert all(credentials[w]['password'] not in run.stdout + run.stderr for w in ('owner', 'customer'))
        observed = json.loads(result.read_text())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    pages = observed['pages']
    expected = {f'{p}-{w}' for p in PAGES for w in (1440, 1024, 390)} | {f'{p}-{w}-dark' for p in PAGES for w in (1440, 390)}
    assert set(pages) == expected
    failing = {k: v for k, v in pages.items() if v['hscroll'] > 0 or v['offscreen'] or v['font_min'] < 13 or v['th_max'] > 44}
    assert failing == {}, failing
    assert observed['dark_differs'] == {f'{p}-{w}': True for p in PAGES for w in (1440, 390)}
    assert observed['menu'] == {'toggle_visible': True, 'nav_hidden_before': True, 'nav_visible_after': True, 'entries': 14,
                                'closed_after_navigation': True, 'hscroll_open': 0}

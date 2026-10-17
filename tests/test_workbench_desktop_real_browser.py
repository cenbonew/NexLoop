"""Workbench on desktop and phone (owner feedback 2026-10-17) in a real browser engine (headless Chromium via Playwright): the built
Web served by the production app over loopback HTTPS on the synthetic workbench stack (tests/workbench_stack_fixture.py), driven by
apps/web/test/browser/workbench-desktop.browser.mjs at 1440 px and 375 px.

Opt-in lane like tests/test_sse_real_browser.py: marked `real_browser`, deselected unless `-m real_browser`. Needs Node 24, the pinned
Playwright of apps/web and its Chromium headless shell. Synthetic accounts only; REAL_DISPATCH_ENABLED=false; run serially.
NEXLOOP_WORKBENCH_SCREENSHOTS (an absolute directory) keeps the screenshots; otherwise they stay in the test's temp directory."""
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


def test_real_chromium_workbench_desktop_and_phone(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
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
    shots = Path(os.environ.get('NEXLOOP_WORKBENCH_SCREENSHOTS') or tmp_path / 'shots')
    assert shots.is_absolute()
    shots.mkdir(parents=True, exist_ok=True)
    owner = credentials['owner']
    session = private(tmp_path / 'session.json', json.dumps({'origin': credentials['origin'], 'username': owner['username'],
        'password_file': str(private(tmp_path / 'owner-password', owner['password'])), 'consumer_id': credentials['consumer_id'],
        'conversation_id': credentials['conversation_id']}))
    result = tmp_path / 'result.json'
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_WORKBENCH_E2E_SESSION_FILE=str(session), NEXLOOP_WORKBENCH_E2E_RESULT_FILE=str(result), NEXLOOP_WORKBENCH_E2E_SCREENSHOTS=str(shots))
        run = subprocess.run([shutil.which('node'), 'test/browser/workbench-desktop.browser.mjs'], cwd=ROOT / 'apps/web', env=env,
                             capture_output=True, text=True, timeout=180)
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-3000:]
        assert owner['password'] not in run.stdout + run.stderr
        observed = json.loads(result.read_text())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    desktop = observed['desktop']
    assert desktop['main'] >= 1300 and 180 <= desktop['nav'] <= 240          # the width is used, sidebar + content
    assert max(desktop['header_heights']) <= 44                                 # every header on one line
    assert desktop['name'].startswith('客户 ') and '未登记名称' in desktop['name']  # the fixture's Consumer has no display_name
    detail = observed['detail']
    assert detail['lead'].startswith('客户 ') and '最新' in detail['items'][0] and '1 条消息' in detail['items'][0]
    conversation = observed['conversation']
    assert conversation['bar_above_messages'] and conversation['pill_before'] == 'Agent 处理中'
    assert conversation['pill_taken'] == '人工接管中' and conversation['pill_back'] == 'Agent 处理中'
    assert '已交还给 Agent' in conversation['notice']
    switches = observed['switches']   # the owner's real dispatch button, through the governed Action (2026-10-18)
    state = dict(switches['before'])
    assert state['部署层 REAL_DISPATCH_ENABLED'] == '未知' and state['租户层'] == '未设置（关）'
    assert '打开租户开关（数据模式 real）' in switches['plan'] and 'enterprise.deliver:1' in switches['plan']
    assert switches['opened'].count('完成，新版本') == 2 and '未完成' not in switches['opened']
    opened = dict(switches['after_open'])
    assert opened['租户层'] == '开' and opened['Action 层 · enterprise.deliver:1'] == '开'
    assert switches['closed'].count('完成，新版本') == 2
    closed = dict(switches['after_close'])
    assert closed['租户层'] == '关' and closed['Action 层 · enterprise.deliver:1'] == '关'
    assert admin.execute("select scope_kind,enabled from control.nexloop_real_dispatch_switches order by scope_kind").fetchall() == [('action_type', False), ('tenant', False)]
    assert admin.execute("select count(*) from control.nexloop_control_events where event_kind='real_dispatch'").fetchone() == (4,)
    assert observed['phone']['overflow'] == {'consumers': 0, 'consumer': 0, 'conversation': 0}
    assert observed['errors'] == []
    assert sorted(p.name for p in shots.glob('*.png')) == sorted(['consumers-1440.png', 'consumer-1440.png', 'conversation-taken-1440.png',
        'conversation-handed-back-1440.png', 'real-dispatch-opened-1440.png', 'consumers-375.png', 'consumer-375.png', 'conversation-375.png'])

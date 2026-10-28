"""Web pages batch 6 acceptance (owner walkthrough 2026-10-18: F02 facts before operations, F06 explicit scope, F07 alerts in business
words, F11 audit identifiers) in a real browser engine (headless Chromium via Playwright): the built Web served by the production app
over loopback HTTPS on the synthetic workbench stack (tests/workbench_stack_fixture.py), measured by
apps/web/test/browser/page-order.browser.mjs at 1440×900. One firing feed_backlog_age alert (value 171178 s, threshold 600 s) is seeded
as test data; nothing is submitted.

Opt-in lane like tests/test_web_layout_real_browser.py: marked `real_browser`, deselected unless `-m real_browser`. Needs Node 24, the
pinned Playwright of apps/web and its Chromium headless shell. Synthetic accounts only; REAL_DISPATCH_ENABLED=false; run serially.
NEXLOOP_ORDER_SCREENSHOTS (an absolute directory) keeps the screenshots; otherwise they stay in the test's temp directory."""
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
from workbench_stack_fixture import TENANT, private, workbench_stack

pytestmark = pytest.mark.real_browser


def seed_firing_alert(admin):  # noqa: F811
    admin.execute("""insert into control.nexloop_alert_state(tenant_id,world,dedupe_key,rule_id,severity,selector,breached_since,firing,first_fired_at,last_fired_at,
        fire_count,last_value,last_evaluated_at,rules_version) values(%s,'real','feed_backlog_age:commitment-monitor','feed_backlog_age','warning','commitment-monitor',
        clock_timestamp()-interval '2 hours',true,clock_timestamp()-interval '2 hours',clock_timestamp(),3,171178,clock_timestamp(),5)""", (TENANT,))
    admin.execute("""insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
        values(%s,'real','feed_backlog_age:commitment-monitor','feed_backlog_age','warning','firing','commitment-monitor',171178,600,5)""", (TENANT,))


def test_real_chromium_page_order_batch_six(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from nexloop_eios.http_api import create_app
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    web = tmp_path / 'web'
    build_web(web)
    port = free_port()
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=web)
    seed_firing_alert(admin)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                                           access_log=False, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), 'API did not start'
        time.sleep(0.1)
    shots = Path(os.environ.get('NEXLOOP_ORDER_SCREENSHOTS') or tmp_path / 'shots')
    assert shots.is_absolute()
    shots.mkdir(parents=True, exist_ok=True)
    session = private(tmp_path / 'session.json', json.dumps({'origin': credentials['origin'], 'consumer_id': credentials['consumer_id'],
        'owner': {'username': credentials['owner']['username'], 'password_file': str(private(tmp_path / 'owner-password', credentials['owner']['password']))}}))
    result = tmp_path / 'result.json'
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_ORDER_E2E_SESSION_FILE=str(session), NEXLOOP_ORDER_E2E_RESULT_FILE=str(result), NEXLOOP_ORDER_E2E_SCREENSHOTS=str(shots))
        run = subprocess.run([shutil.which('node'), 'test/browser/page-order.browser.mjs'], cwd=ROOT / 'apps/web', env=env, capture_output=True, text=True, timeout=300)
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-3000:]
        assert credentials['owner']['password'] not in run.stdout + run.stderr
        observed = json.loads(result.read_text())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    # F02: who and what inside the first screen, operations after the facts.
    c = observed['consumer']
    assert c['facts'] < 900 and c['facts'] < c['ops'] and c['conversations'] < c['ops'], c
    a = observed['alerts']
    assert a['table'] < a['ops'], a
    assert observed['actions']['textareas'] == 0 and '无需处理' in observed['actions']['summary'], observed['actions']
    assert observed['plans']['textareas'] == 0 and '计划在目标发布后' in observed['plans']['empty'], observed['plans']
    # F06: no default scope or operation; the impact is said once both are chosen.
    g = observed['goals']
    assert (g['scope'], g['checked'], g['submit_disabled'], g['ops_after_facts']) == ('', 0, True, True), g
    assert g['impact'].startswith('影响：将暂停 整个租户（所有客户） 的主动联系') and g['submit_enabled_after'] is True, g
    s = observed['settings']
    assert (s['open_button'], s['old_button']) == (1, 0) and s['outcome'].startswith('真实交付：不可派发'), s
    # F07: business name, raw code second, unit, threshold, a place to go.
    assert all(t in a['text'] for t in ('后台 feed 积压', 'feed_backlog_age', '48 小时（171178 秒）', '阈值 10 分钟（600 秒）', 'commitment-monitor')), a['text']
    assert a['link'] == 1
    # F11: one-line time/member/purpose cells, objects as short IDs with the full value in the title, no sideways page scroll.
    audit = observed['audit']
    assert audit['rows'] >= 1 and audit['nowrap_one_line'] and audit['full_in_title'] and audit['hscroll'] <= 0, audit
    assert audit['ids'] and all(len(i) <= 11 for i in audit['ids']), audit['ids']  # the kit's IdText: head 6, …, tail 4

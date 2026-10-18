"""Web layout batch 1 and 2 acceptance (docs/implementation/web-layout-audit-20261018.md §4.2) in a real browser engine (headless Chromium via
Playwright): the built Web served by the production app over loopback HTTPS on the synthetic workbench stack
(tests/workbench_stack_fixture.py), measured by apps/web/test/browser/web-layout.browser.mjs at 1440, 1024 and 390 px, dark at 1440
and 390. Batch 2 on the same run: the stack carries a synthetic seed (tests/smoke_seed.large_records, 12 customers) so the
consumers table has many rows; tables are cards below 640 px and tables above; WebChat's top bar, conversation names and pinned
composer.

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
from smoke_seed import large_records
from workbench_stack_fixture import private, workbench_stack

pytestmark = pytest.mark.real_browser
PAGES = ('customer-login', 'customer-webchat', 'wb-login', 'wb-overview', 'wb-consumers', 'wb-consumer', 'wb-conversation', 'wb-settings',
         'wb-actions', 'wb-commitments', 'wb-operations', 'wb-goals', 'wb-contact', 'wb-experiments', 'wb-audit')


def test_real_chromium_web_layout_batches_one_and_two(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from nexloop_eios.http_api import create_app
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    web = tmp_path / 'web'
    build_web(web)
    port = free_port()
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=web,
                                                              seed=large_records(customers=12, long_conversation_turns=20))
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
    failing = {k: v for k, v in pages.items() if v['hscroll'] > 0 or v['offscreen'] or v['font_min'] < 13 or v['th_max'] > 44
               or (k.split('-')[-1] == '390' or k.endswith('390-dark')) and v['scrollers']}
    assert failing == {}, {k: {f: v[f] for f in ('hscroll', 'offscreen', 'font_min', 'th_max', 'scrollers')} for k, v in failing.items()}
    assert observed['dark_differs'] == {f'{p}-{w}': True for p in PAGES for w in (1440, 390)}
    assert observed['menu'] == {'toggle_visible': True, 'nav_hidden_before': True, 'nav_visible_after': True, 'entries': 14,
                                'closed_after_navigation': True, 'hscroll_open': 0}
    # Batch 2: the consumers table lists the fixture's customer and the 12 seeded ones; cards below 640 px, a table above.
    consumers = {w: pages[f'wb-consumers-{w}']['tables'] for w in (1440, 1024, 390)}
    assert [t['rows'] for t in consumers[390]] == [13]
    assert consumers[390][0] | {'row_max': 0} == {'rows': 13, 'cards': True, 'head_hidden': True, 'labelled': True, 'row_max': 0}
    assert consumers[390][0]['row_max'] <= 260, consumers[390]
    assert all(not t['cards'] and not t['head_hidden'] for w in (1440, 1024) for t in consumers[w])
    narrow = [t for k, v in pages.items() if k.endswith(('-390', '-390-dark')) for t in v['tables'] if t['rows']]
    assert narrow and all(t['cards'] and t['head_hidden'] and t['labelled'] for t in narrow), narrow
    assert observed['chat'] == {'option_texts_without_ids': True, 'meta_in_bar': True, 'sent_through_composer': True, 'composer_pinned': True}
    # Batch 3: overview cards in 3+/2/1 columns and of their own height; operations likewise; bubbles within 80 % of the list (92 % on
    # phones); every empty/unavailable state in the one dashed style.
    assert [pages[f'wb-overview-{w}']['grid']['cols'] for w in (1440, 1024, 390)] == [3, 2, 1], {w: pages[f'wb-overview-{w}']['grid'] for w in (1440, 1024, 390)}
    assert all(pages[f'{p}-{w}']['grid']['heights'] > 1 for p in ('wb-overview', 'wb-operations') for w in (1440, 1024))
    assert all(0 < pages[f'wb-conversation-{w}']['bubble_ratio'] <= 0.81 for w in (1440, 1024)) and pages['wb-conversation-390']['bubble_ratio'] <= 0.93, {w: pages[f'wb-conversation-{w}']['bubble_ratio'] for w in (1440, 1024, 390)}
    assert [k for k, v in pages.items() if not v['empty_styled']] == []
    # Batch 4: radios/checkboxes at native size next to their text (pause/resume radios on goals, the checkbox on contact).
    choices = {k: v['choices'] for k, v in pages.items() if v['choices']}
    assert {k.rsplit('-', 1)[0] if not k.endswith('dark') else k.rsplit('-', 2)[0] for k in choices} >= {'wb-goals', 'wb-contact'}, sorted(choices)
    assert all(len(pages[f'wb-goals-{w}']['choices']) == 2 for w in (1440, 1024, 390))
    bad = {k: c for k, v in choices.items() for c in v if not (c['w'] <= 24 and c['h'] <= 24 and c['gap'] is not None and 0 <= c['gap'] <= 12 and c['dy'] <= 4)}
    assert bad == {}, bad
    assert all(f['w'] < f['parent'] for w in (1440, 1024) for f in pages[f'wb-goals-{w}']['fieldsets']), {w: pages[f'wb-goals-{w}']['fieldsets'] for w in (1440, 1024)}
    # Batch 5 (walkthrough F08, F12–F15): groups, next-stage pages last and marked; breadcrumbs on detail pages; the time zone note;
    # empty states with a kind and a next step (not enabled on experiments); badges in words; the page title near the top.
    assert observed['shell'] == {'groups': ['运营', '监控', '管理'], 'last': ['本体 / 演进未启用', '实验空间未启用']}, observed['shell']
    assert all(pages[f'wb-consumer-{w}']['crumbs'] == ['消费者', '客户详情'] and pages[f'wb-conversation-{w}']['crumbs'] == ['消费者', '会话'] for w in (1440, 1024, 390))
    assert all(v['tz'] == '时间均为北京时间' for k, v in pages.items() if k.startswith('wb-') and not k.startswith('wb-login'))
    assert all(e['kind'] and e['next'] for v in pages.values() for e in v['empties']), {k: v['empties'] for k, v in pages.items() if v['empties']}
    assert {e['kind'] for e in pages['wb-experiments-1440']['empties']} == {'not_enabled'}
    assert all(v['badges_worded'] for v in pages.values())
    assert all(pages[f'{p}-1440']['h1_top'] <= 160 for p in PAGES if p.startswith('wb-') and p != 'wb-login'), {p: pages[f'{p}-1440']['h1_top'] for p in PAGES if p.startswith('wb-')}
    # F14: each step moves selection and focus together; the tab list is not re-mounted by the data load.
    tabs = observed['tabs']
    sel = lambda state: [(name, chosen == 'true', index, focused) for name, chosen, index, focused in state]  # noqa: E731
    assert sel(tabs['start']) == [('未结束', True, 0, True), ('已结束', False, -1, False)]
    assert sel(tabs['right']) == [('未结束', False, -1, False), ('已结束', True, 0, True)] and tabs['same_list']
    assert sel(tabs['home']) == sel(tabs['start']) and sel(tabs['end']) == sel(tabs['right'])
    assert sel(tabs['enter']) == sel(tabs['start']) and sel(tabs['space']) == sel(tabs['right'])

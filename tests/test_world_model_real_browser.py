"""Enterprise world model W1 acceptance (docs/design/workbench-world-model-design.md; owner 2026-10-19 D1-D7) in a real browser engine
(headless Chromium via Playwright): the built Web served by the production app over loopback HTTPS on the synthetic workbench stack
(tests/workbench_stack_fixture.py), measured by apps/web/test/browser/world-model.browser.mjs. One synthetic object type with an
owner-restricted property and a relation to Consumer is seeded as test data (test_world_model_read_pg.seed_model); nothing is written
through the page.

Opt-in lane like tests/test_web_layout_real_browser.py: marked `real_browser`, deselected unless `-m real_browser`. Needs Node 24, the
pinned Playwright of apps/web and its Chromium headless shell (or NEXLOOP_CHROMIUM). Synthetic accounts only; run serially.
NEXLOOP_WORLD_SCREENSHOTS (an absolute directory) keeps the screenshots."""
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
from test_world_model_read_pg import seed_model
from workbench_stack_fixture import private, workbench_stack

pytestmark = pytest.mark.real_browser


def test_real_chromium_world_model_w1(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from nexloop_eios.http_api import create_app
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    web = tmp_path / 'web'
    build_web(web)
    port = free_port()
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=web)
    seed_model(admin)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                                           access_log=False, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), 'API did not start'
        time.sleep(0.1)
    shots = Path(os.environ.get('NEXLOOP_WORLD_SCREENSHOTS') or tmp_path / 'shots')
    assert shots.is_absolute()
    shots.mkdir(parents=True, exist_ok=True)
    session = private(tmp_path / 'session.json', json.dumps({'origin': credentials['origin'],
        'owner': {'username': credentials['owner']['username'], 'password_file': str(private(tmp_path / 'owner-password', credentials['owner']['password']))}}))
    result = tmp_path / 'result.json'
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_WORLD_E2E_SESSION_FILE=str(session), NEXLOOP_WORLD_E2E_RESULT_FILE=str(result), NEXLOOP_WORLD_E2E_SCREENSHOTS=str(shots))
        run = subprocess.run([shutil.which('node'), 'test/browser/world-model.browser.mjs'], cwd=ROOT / 'apps/web', env=env, capture_output=True, text=True, timeout=300)
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-3000:]
        assert credentials['owner']['password'] not in run.stdout + run.stderr
        observed = json.loads(result.read_text())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    assert {'title': '模型与资产', 'pages': ['企业世界模型', '数据资产', '知识工作台']} in observed['nav'], observed['nav']
    assert any(s.startswith('Object Type') for s in observed['summary']) and observed['table_rows'] >= 2
    assert observed['url_property'] == '?tab=properties'
    assert 'spending_band' in observed['withheld'] and '受限' in observed['withheld'] and 'synthetic restricted' not in observed['withheld']
    assert observed['graph_label'].startswith('对象类型关系图：') and observed['focused_node']
    assert observed['panel'].startswith(observed['focused_node']) and 'focus=' in observed['url_graph'] and 'view=graph' in observed['url_graph']
    assert observed['back_url'] == '?view=graph' and observed['panel_after_back'] == 0
    assert observed['hscroll_1440'] <= 0 and observed['hscroll_390'] <= 0, observed
    # W3 capability catalog (on L6's read): tabs, keyboard to the dispatch tab, the same outcome as settings, read only.
    assert observed['capabilities_tabs'] == ['受治理能力', '业务 Action', '渠道与交付路由', '真实派发'] and observed['capabilities_url'] == '?tab=dispatch'
    assert observed['capabilities_outcome'] == observed['settings_outcome'] and observed['capabilities_outcome'].startswith('真实交付：')
    assert observed['capabilities_forms'] == 0 and observed['capabilities_hscroll_390'] <= 0, observed
    # W2 instances (L6 0222/0223): the fixture's Consumer listed by id, its drawer with the neighbourhood, a deep link that reloads, Back.
    assert observed['instances_summary'].startswith('Consumer 共 ') and observed['instance_url'].startswith('?tab=instances&type=Consumer&object=')
    assert '邻域（深度 1）' in observed['drawer'] and '完整标识' in observed['drawer'] and observed['drawer_link'] == 1
    assert observed['drawer_after_reload'] == 1 and observed['drawer_after_back'] == 0 and observed['back_after_drawer'] == '?tab=instances&type=Consumer'
    # W4 data assets (L6 0224): the six sections; budget and Artifact list said as not available; no form; no sideways scroll.
    assert observed['assets_sections'] == ['Artifact', 'Claim 与提取', '召回索引', 'Embedding 预算', '合并配置', '备份'], observed['assets_sections']
    assert 'Artifact 列表不可用' in observed['assets_text'] and '不表示花费为 0' in observed['assets_text'] and '条 Claim' in observed['assets_text']
    assert observed['assets_forms'] == 0 and observed['assets_hscroll_390'] <= 0

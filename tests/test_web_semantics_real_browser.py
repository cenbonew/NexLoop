"""Workbench batch 7 acceptance (owner walkthrough 2026-10-18: F03 commitment semantics, F04 name states, F05 one scope, F09 list tools,
F10 customer context) in a real browser engine (headless Chromium via Playwright): the built Web served by the production app over
loopback HTTPS on the synthetic workbench stack (tests/workbench_stack_fixture.py), measured by
apps/web/test/browser/semantics.browser.mjs at 1440×900.

Test data (admin seed, synthetic): on the stack's one customer and its inbound message, a Claim and three registered commitments
(open with a due time tomorrow, open and late with a due time yesterday, fulfilled) with one exception on an open and one on the
ended commitment, plus 60 more synthetic customers so the consumer list pages. Nothing is submitted; only reads.

Opt-in lane like tests/test_web_page_order_real_browser.py: marked `real_browser`, deselected unless `-m real_browser`. Needs Node 24,
the pinned Playwright of apps/web and its Chromium headless shell. Synthetic accounts only; REAL_DISPATCH_ENABLED=false; run serially.
NEXLOOP_SEMANTICS_SCREENSHOTS (an absolute directory) keeps the screenshots; otherwise they stay in the test's temp directory."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

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
QUOTE = '我的订单一直没有发货'


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def seed_commitments(admin, credentials):  # noqa: F811
    """Three registered commitments of the stack's customer on its inbound message, through the same tables the keeper writes (0111)."""
    from commitment_fixture import publish_commitment_type
    publish_commitment_type(admin, TENANT, actions=False)
    consumer, conversation = credentials['consumer_id'], credentials['conversation_id']
    message, sequence, body = admin.execute("""select m.message_id, m.sequence, o.properties->>'body' from runtime.nexloop_conversation_messages m
        join ontology.objects o on o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id
        where m.tenant_id=%s and m.conversation_id=%s order by m.sequence limit 1""", (TENANT, conversation)).fetchone()
    start = body.index(QUOTE)
    claim = hexid('semantics-claim')
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,
        subject_text,predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,
        source_content_hash,quote,extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        values(%s,'real',%s,%s,%s,%s,'order','consumer',%s,'客户','delivery_promise',%s,'agent','affirmed','asserted','','明天',%s,%s,%s,%s,%s,%s,%s,'synthetic-1',0.9,
        'commitment','resolved','[]',%s,'[]')""",
        (TENANT, claim, conversation, consumer, 'd' * 64, consumer, Jsonb({'type': 'text', 'value': '发货'}),
         Jsonb({'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'anchor': datetime.now(UTC).isoformat(), 'timezone': 'Asia/Shanghai', 'expression': ''}),
         message, sequence, start, start + len(QUOTE), hashlib.sha256(body.encode()).hexdigest(), QUOTE, hexid('semantics-correlation')))
    now = datetime.now(UTC).replace(microsecond=0)
    rows = [('open-tomorrow', 'open', now + timedelta(days=1), False), ('late-yesterday', 'open', now - timedelta(days=1), True), ('kept', 'fulfilled', now - timedelta(days=2), False)]
    ids = {}
    for label, status, due, late in rows:
        cid = hexid('semantics-commitment-' + label)
        ids[label] = cid
        props = {'made_to': consumer, 'made_by': {'sender_kind': 'agent', 'sender_principal': 'synthetic-agent-principal', 'role_ref': 'role:synthetic', 'run_id': None, 'intent_id': None},
                 'content_ref': {'claim_id': claim, 'message_id': message, 'span': [start, start + len(QUOTE)], 'content_hash': 'e' * 64},
                 'promised_at': now.isoformat(), 'due_at': due.isoformat(), 'due_precision': 'exact', 'condition': None, 'status': status, 'late': late,
                 'fulfillment_basis': 'operator_attested' if status == 'fulfilled' else 'undetermined', 'fulfillment_evidence': [], 'related_goal_ref': None, 'supersedes': None}
        # The keeper's order (0111 guard): prepared, then the object with the intent as its source, then registered.
        admin.execute("""insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,claim_id,message_id,consumer_id,correlation_key,intent_id,properties)
            values(%s,'real',%s,%s,'claim',%s,%s,%s,%s,%s,%s)""",
            (TENANT, cid, 'semantics-' + label, claim, message, consumer, hexid('semantics-correlation'), 'semantics-intent-' + label, Jsonb(props)))
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Commitment',%s,1,%s,'nexloop-action',%s,clock_timestamp(),clock_timestamp())""", (TENANT, cid, Jsonb(props), 'semantics-intent-' + label))
        admin.execute("update runtime.nexloop_commitments set registered_at=clock_timestamp() where tenant_id=%s and world='real' and commitment_id=%s", (TENANT, cid))
    for subject, reason in (('commitment:' + ids['late-yesterday'], 'deadline_breached'), ('commitment:' + ids['kept'], 'late_fulfilment')):
        admin.execute("insert into runtime.nexloop_commitment_exceptions(tenant_id,world,subject_ref,consumer_id,reason,detail) values(%s,'real',%s,%s,%s,'{}')",
                      (TENANT, subject, consumer, reason))
    # More customers (ids and objects only, no names), so the consumer list has more than one page.
    for n in range(60):
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Consumer',%s,1,'{}','synthetic',%s,clock_timestamp(),clock_timestamp())""", (TENANT, hexid(f'semantics-consumer-{n}'), f'semantics-consumer-{n}'))
    return ids


def test_real_chromium_workbench_semantics_batch_seven(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from nexloop_eios.http_api import create_app
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    web = tmp_path / 'web'
    build_web(web)
    port = free_port()
    config, certificate, key, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=port, web=web)
    ids = seed_commitments(admin, credentials)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host='127.0.0.1', port=port, ssl_certfile=str(certificate), ssl_keyfile=str(key),
                                           access_log=False, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), 'API did not start'
        time.sleep(0.1)
    shots = Path(os.environ.get('NEXLOOP_SEMANTICS_SCREENSHOTS') or tmp_path / 'shots')
    assert shots.is_absolute()
    shots.mkdir(parents=True, exist_ok=True)
    session = private(tmp_path / 'session.json', json.dumps({'origin': credentials['origin'], 'consumer_id': credentials['consumer_id'],
        'conversation_id': credentials['conversation_id'], 'commitments': ids, 'quote': QUOTE,
        'owner': {'username': credentials['owner']['username'], 'password_file': str(private(tmp_path / 'owner-password', credentials['owner']['password']))}}))
    result = tmp_path / 'result.json'
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MODEL_', 'DATABASE_'))}
        env.update(NEXLOOP_SEMANTICS_E2E_SESSION_FILE=str(session), NEXLOOP_SEMANTICS_E2E_RESULT_FILE=str(result), NEXLOOP_SEMANTICS_E2E_SCREENSHOTS=str(shots))
        run = subprocess.run([shutil.which('node'), 'test/browser/semantics.browser.mjs'], cwd=ROOT / 'apps/web', env=env, capture_output=True, text=True, timeout=300)
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-3000:]
        assert credentials['owner']['password'] not in run.stdout + run.stderr
        observed = json.loads(result.read_text())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    o = observed
    # F03: without opening a commitment — to whom, what, when, its state, who acts next; the ID is secondary (short, copyable).
    row = o['f03']['late_row']
    assert QUOTE in row['summary'] and row['customer'] and '已逾期' in row['due'] and row['status'] and row['next'] == '负责人处理', row
    assert o['f03']['headers'] == ['承诺', '客户', '期限', '状态', '承诺方', '下一步', 'ID'] and o['f03']['id_short'] and o['f03']['copy_buttons'] >= 2, o['f03']
    # F04: the same customer, the same words on the list, the detail and the commitment list; never "restricted" for a missing name.
    names = o['f04']
    assert names['list'] == names['detail'] == names['commitments'] and names['list'].endswith('（未登记名称）'), names
    # F05: list, statistics and exceptions agree on the selected tab; tabs carry counts and a selected state.
    f05 = o['f05']
    assert f05['open']['tabs'] == ['未结束（2）', '已结束（1）', '全部'] and f05['open']['selected'] == '未结束（2）', f05
    assert f05['open']['rows'] == 2 and f05['open']['scope'] == '本范围的异常（1）' and f05['open']['exceptions'] == ['deadline_breached'], f05
    assert f05['ended']['rows'] == 1 and f05['ended']['scope'] == '本范围的异常（1）' and f05['ended']['exceptions'] == ['late_fulfilment'], f05
    assert f05['ended']['selected'].startswith('已结束') and f05['ended']['all_exceptions'] == '全部承诺异常（2）', f05
    # F09: a customer's late commitments located with filters; total shown; the filter in the URL; Back restores filter and scroll.
    f09 = o['f09']
    assert 'late=true' in f09['url'] and 'consumer=' in f09['url'] and f09['rows'] == 1 and f09['total'].startswith('共 1 条'), f09
    assert f09['back_url'] == f09['url'] and f09['back_rows'] == 1, f09
    assert f09['consumers_total'].startswith('共 61 条') and f09['consumers_page2_url'].endswith('page=50') and abs(f09['scroll_back'] - f09['scroll_before']) <= 40, f09
    # F10: from the customer to their commitments without searching; breadcrumbs name the customer; the history does not move the reading.
    f10 = o['f10']
    assert f10['commitments_url'].endswith('consumer=' + credentials['consumer_id'] + '&status=open') and f10['chip'].startswith('客户：'), f10
    assert f10['crumbs'][0] == '消费者' and f10['crumbs'][1] == names['list'] and f10['crumbs'][-1] == '会话', f10
    assert abs(f10['current_after'] - f10['current_before']) <= 2 and f10['history_snippet'], f10

"""Extraction dead letters (migration 0355, temporary number; docs/release/run-extraction-dead-letters-diagnosis.md): the data health
extraction block counts only the dead letters still needing attention (unresolved_dead_letters: no owner decision and not fully covered
by a later succeeded job of the same conversation), keeps every one as dead_lettered_total, lists the unresolved ones (ids and codes
only), and alert rules v18 move the rule to the new metric. The owner resolves one through the governed Action
claim_extraction.dead_letter_resolve from the real workbench: requeue puts its feed rows back as pending, dismiss records the reason;
one decision per job, the dead-letter row is never changed. Real PostgreSQL with the NX-030 observability fixture; admin seeds the
synthetic jobs and feed rows in replica mode."""
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from nexloop_eios.observability import load_rules
from test_browser_http import ORIGIN
from test_data_health_pg import health, replica
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

ROOT = Path(__file__).resolve().parents[1] / 'deploy/configuration'
T0 = datetime.now(UTC) - timedelta(days=3)


def job(admin, status, conversation, messages, *, at, code=None):
    job_id = str(uuid.uuid4())
    replica(admin, """insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
        request_id,normalized_input,plan_snapshot,status,result,created_at,updated_at,world,queue,attempts,max_attempts)
        values(%s,%s,'nexloop.claim.extract','1','atomic','async',%s,'synthetic','synthetic',%s,%s,'{}',%s,%s,%s,%s,'real','claim-extraction',3,3)""",
        (job_id, str(uuid.uuid4()), TENANT, str(uuid.uuid4()), Jsonb({'conversation_id': conversation, 'through_sequence': len(messages), 'message_ids': messages}),
         status, Jsonb({'code': code} if code else {}), at, at))
    return job_id


def feed(admin, conversation, messages, task_id, at):
    for i, m in enumerate(messages):
        replica(admin, """insert into runtime.nexloop_claim_extraction_feed(tenant_id,world,conversation_id,message_id,sequence,created_at,task_id,enqueued_at)
            values(%s,'real',%s,%s,%s,%s,%s,%s)""", (TENANT, conversation, m, i + 1, at, task_id, at))


def ids(n):
    return [secrets.token_hex(32) for _ in range(n)]


@pytest.fixture
def seeded(observed):  # noqa: F811
    """Three dead letters: covered (a later success holds all its Messages), partly covered, and one alone in its conversation."""
    o = observed; admin = o['admin']
    publish_registry_actions(admin, ('nexloop.claim_extraction.dead_letter_resolve',), tenant=TENANT)
    a, b, c = (secrets.token_hex(32) for _ in range(3))
    ma, mb, mc = ids(3), ids(3), ids(2)
    covered = job(admin, 'dead_lettered', a, ma, at=T0, code='provider_unavailable')
    job(admin, 'succeeded', a, ma + ids(2), at=T0 + timedelta(hours=1))
    partly = job(admin, 'dead_lettered', b, mb, at=T0, code='provider_timeout')
    job(admin, 'succeeded', b, mb[1:] + ids(1), at=T0 + timedelta(hours=1))  # does not hold mb[0]
    job(admin, 'succeeded', b, mb, at=T0 - timedelta(hours=1))                # earlier, does not count
    alone = job(admin, 'dead_lettered', c, mc, at=T0 + timedelta(minutes=5), code='runtime_lease_exhausted')
    feed(admin, c, mc, alone, T0)
    return dict(o, covered=covered, partly=partly, alone=alone, conversation=c, messages=mc)


def test_only_uncovered_unresolved_dead_letters_count_and_the_list_carries_ids_and_codes(seeded):
    s = seeded; admin = s['admin']
    x = health(admin)['extraction']
    assert x['dead_lettered_total'] == x['dead_lettered'] == 3
    assert x['unresolved_dead_letters'] == 2 and 'dead_letters' in x['reasons'] and x['status'] == 'attention'
    items = {i['task_id']: i for i in x['dead_letter_items']}
    assert set(items) == {s['partly'], s['alone']}                 # the fully covered one is not counted
    assert items[s['alone']]['code'] == 'runtime_lease_exhausted' and items[s['alone']]['message_count'] == 2
    assert set(items[s['partly']]) == {'task_id', 'code', 'attempts', 'max_attempts', 'conversation_id', 'message_count', 'created_at', 'dead_lettered_at'}
    # The alert rule reads the new metric; nothing else changes from v16.
    v16, v18 = json.loads((ROOT / 'alert-rules.v16.json').read_text()), load_rules(ROOT / 'alert-rules.v18.json')
    rule = next(r for r in v18['rules'] if r['rule_id'] == 'data_health_extraction_dead_letters')
    assert v18['version'] == 18 and rule['metric'] == 'data_health.extraction.unresolved_dead_letters'
    assert [r for r in v18['rules'] if r is not rule] == [r for r in v16['rules'] if r['rule_id'] != rule['rule_id']]


def test_the_owner_requeues_and_dismisses_from_the_workbench_and_resolved_ones_stop_counting(seeded):
    s = seeded; admin, w = s['admin'], s['w']
    with w['client']() as client:
        csrf = workbench_login(client, w['operator']).json()['csrf_token']
        assert act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['alone'], 'disposition': 'dismiss', 'reason': '无权'}).status_code == 403
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        listed = client.get('/api/v1/workbench/data-health', headers={'Origin': ORIGIN}).json()['extraction']
        assert listed['unresolved_dead_letters'] == 2 and {i['task_id'] for i in listed['dead_letter_items']} == {s['partly'], s['alone']}
        requeued = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['alone'], 'disposition': 'requeue', 'reason': '供应商已恢复'})
        assert requeued.status_code == 200, requeued.text
        bad = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['alone'], 'disposition': 'dismiss', 'reason': '改主意'})
        assert bad.status_code == 409 and bad.json()['details']['reason'] == 'already_resolved'
        not_dead = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': str(uuid.uuid4()), 'disposition': 'dismiss', 'reason': '不存在'})
        assert not_dead.status_code == 409 and not_dead.json()['details']['reason'] == 'not_found'
        dismissed = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['partly'], 'disposition': 'dismiss', 'reason': '已人工核对'})
        assert dismissed.status_code == 200, dismissed.text
    # Requeue put the job's feed rows back as pending; the dead-letter rows stay as they were.
    pending = admin.execute("select count(*) from runtime.nexloop_claim_extraction_feed where tenant_id=%s and conversation_id=%s and task_id is null and enqueued_at is null",
                            (TENANT, s['conversation'])).fetchone()[0]
    assert pending == 2
    assert admin.execute("select count(*) from runtime.jobs where tenant_id=%s and status='dead_lettered' and queue='claim-extraction'", (TENANT,)).fetchone()[0] == 3
    rows = admin.execute("select job_id,disposition,requeued_feed_rows,decided_by from runtime.nexloop_claim_extraction_dispositions where tenant_id=%s order by decided_at",
                         (TENANT,)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(s['alone'], 'requeue', 2), (s['partly'], 'dismiss', 0)] and rows[0][3] == w['owner']['principal_id']
    x = health(admin)['extraction']
    assert x['dead_lettered_total'] == 3 and x['unresolved_dead_letters'] == 0 and x['dead_letter_items'] == [] and 'dead_letters' not in x['reasons']
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute('delete from runtime.nexloop_claim_extraction_dispositions')

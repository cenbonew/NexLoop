"""Extraction dead letters (migration 0355, temporary number; docs/release/run-extraction-dead-letters-diagnosis.md): the data health
extraction block counts only the dead letters still needing attention (unresolved_dead_letters: no owner decision and not fully covered
by a later succeeded job of the same conversation), keeps every one as dead_lettered_total, lists the unresolved ones (ids and codes
only), and alert rules v18 move the rule to the new metric. The owner resolves one through the governed Action
claim_extraction.dead_letter_resolve from the real workbench: requeue enqueues a new job of the same window under its own inbox event
(the scheduler's '<conversation>:<through>' event would return the dead job again, L6 C1) and points the feed rows at it, so the
worker extracts the Messages; dismiss records the reason; one decision per job, the dead-letter row is never changed. Real PostgreSQL with the NX-030 observability fixture; admin seeds the
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
from test_real_dispatch_switch_pg import owner_sql
from support.tenants import TENANT_B
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
    assert set(items[s['partly']]) == {'task_id', 'code', 'attempts', 'max_attempts', 'conversation_id', 'message_count', 'existing_claims', 'created_at', 'dead_lettered_at'}
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
        again = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['alone'], 'disposition': 'requeue', 'reason': '再点一次'})
        assert again.status_code == 200, again.text
        bad = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['alone'], 'disposition': 'dismiss', 'reason': '改主意'})
        assert bad.status_code == 409 and bad.json()['details']['reason'] == 'already_resolved'
        not_dead = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': str(uuid.uuid4()), 'disposition': 'dismiss', 'reason': '不存在'})
        assert not_dead.status_code == 409 and not_dead.json()['details']['reason'] == 'not_found'
        dismissed = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': s['partly'], 'disposition': 'dismiss', 'reason': '已人工核对'})
        assert dismissed.status_code == 200, dismissed.text
    # Requeue enqueued one new pending job of the same window (a second requeue replays, nothing twice) and pointed the feed rows at it;
    # the dead-letter rows stay as they were.
    new = admin.execute("select job_id,status,normalized_input,max_attempts from runtime.jobs where tenant_id=%s and queue='claim-extraction' and status='pending'",
                        (TENANT,)).fetchall()
    assert len(new) == 1 and new[0][2]['message_ids'] == s['messages'] and new[0][3] == 3
    assert admin.execute("select event_id from runtime.nexloop_inbox where tenant_id=%s and job_id=%s", (TENANT, new[0][0])).fetchone()[0].endswith(':requeue:' + s['alone'])
    feed_rows = admin.execute("select task_id,count(*) from runtime.nexloop_claim_extraction_feed where tenant_id=%s and conversation_id=%s group by 1",
                              (TENANT, s['conversation'])).fetchall()
    assert feed_rows == [(new[0][0], 2)]
    assert admin.execute("select count(*) from runtime.jobs where tenant_id=%s and status='dead_lettered' and queue='claim-extraction'", (TENANT,)).fetchone()[0] == 3
    rows = admin.execute("select job_id,disposition,requeued_feed_rows,decided_by from runtime.nexloop_claim_extraction_dispositions where tenant_id=%s order by decided_at",
                         (TENANT,)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(s['alone'], 'requeue', 2), (s['partly'], 'dismiss', 0)] and rows[0][3] == w['owner']['principal_id']
    x = health(admin)['extraction']
    assert x['dead_lettered_total'] == 3 and x['unresolved_dead_letters'] == 0 and x['dead_letter_items'] == [] and 'dead_letters' not in x['reasons']
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute('delete from runtime.nexloop_claim_extraction_dispositions')


def test_another_tenant_or_world_never_reaches_the_dead_letter(seeded):
    """L6 I3: the handler's own tenant and world conditions (not only RLS): the same owner, configured as owner of tenant B too, gets
    not_found for tenant A's task id from B, and from another world of A; A's feed rows and dispositions stay untouched."""
    s = seeded; admin, w = s['admin'], s['w']
    replica(admin, """insert into control.nexloop_workbench_configurations select (jsonb_populate_record(null::control.nexloop_workbench_configurations,
        to_jsonb(c)||jsonb_build_object('tenant_id',%s::text))).* from control.nexloop_workbench_configurations c where c.tenant_id=%s order by manifest_version desc limit 1""",
            (TENANT_B, TENANT))
    body = Jsonb({'operation': 'resolve_extraction_dead_letter', 'request_id': 'synthetic-cross', 'task_id': s['alone'], 'disposition': 'requeue', 'reason': '越界'})
    for tenant, world in ((TENANT_B, 'real'), (TENANT, 'test')):
        with pytest.raises(psycopg.Error, match='not_found'):
            owner_sql(admin, "select runtime.nexloop_governed_resolve_extraction_dead_letter(%s,%s,%s,'human','synthetic-intent',%s)",
                      (tenant, world, w['owner']['principal_id'], body))
    assert admin.execute("select task_id,count(*) from runtime.nexloop_claim_extraction_feed where conversation_id=%s group by 1", (s['conversation'],)).fetchall() == [(s['alone'], 2)]
    assert admin.execute('select count(*) from runtime.nexloop_claim_extraction_dispositions').fetchone()[0] == 0
    assert admin.execute("select count(*) from runtime.jobs where queue='claim-extraction' and status='pending'").fetchone()[0] == 0


def test_requeue_end_to_end_a_new_job_the_worker_extracts_and_claims_are_recorded(observed):  # noqa: F811
    """L6 C1: real Messages, the real scheduler and worker. The window dead-letters (no provider answer); with no newer Message the
    scheduler alone would return the dead job again; the owner's requeue makes a new job, the worker extracts it and Claims exist."""
    from psycopg.conninfo import make_conninfo
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.assembly import open_core
    from nexloop_eios.authorization import authenticate_service
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.conversation_messages import ConversationMessagePort
    from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler, ClaimExtractionWorker
    from nexloop_eios.conversation_extraction import DeterministicExtractionProvider
    from test_claim_extraction_jobs_pg import EXTRACT_TARGET, QUEUE_TARGET, make_window, registered_provider
    from test_claim_store_pg import source_targets
    o = observed; admin, w = o['admin'], o['w']; f = w['f']
    publish_registry_actions(admin, ('nexloop.claim_extraction.dead_letter_resolve',), tenant=TENANT)
    # The fixture's port session predates the workbench provisioning (a newer realm epoch): the same Human authenticates again.
    human = authenticate_browser_business(f['reader'].pool, f['base']['issued'].session, world='real')
    conversation_id, message_ids = make_window({'port': ConversationMessagePort(f['reader'].pool, human, f['reader'].signer)},
                                               ['付款页面一直报错。', '解决后我再考虑续费。', '好的'], 'dead-letter-e2e')
    api, signer = f['reader'].pool, f['reader'].signer
    with open_core(make_conninfo(o['pg'], user='nexloop_domain_worker')) as worker_pool:
        _, scheduler_token = seed_multi_authority(admin, api, [EXTRACT_TARGET, QUEUE_TARGET], identity_suffix='-dl-scheduler')
        _, worker_token = seed_multi_authority(admin, worker_pool, [QUEUE_TARGET] + source_targets(conversation_id, message_ids), identity_suffix='-dl-worker')
        scheduler_session = authenticate_service(api, scheduler_token, world='real'); worker_session = authenticate_service(worker_pool, worker_token, world='real')
        scheduler = ClaimExtractionScheduler(api, scheduler_session, signer, quiet_seconds=0, max_attempts=1)
        # The workbench fixture's own conversation is due too (the worker holds no READ on it: failed, not dead-lettered).
        tasks = scheduler.run_once()
        (dead,) = [t for t in tasks if admin.execute('select normalized_input->>%s from runtime.jobs where job_id=%s', ('conversation_id', t)).fetchone()[0] == conversation_id]
        failing = ClaimExtractionWorker(worker_pool, worker_session, signer, DeterministicExtractionProvider({}), retry_base_seconds=0)
        for _ in tasks:
            failing.run_once()
        assert admin.execute('select status from runtime.jobs where job_id=%s', (dead,)).fetchone()[0] == 'dead_lettered'
        # The bug L6 reproduced: put back as pending, the scheduler lands on the dead job again (same event id, same payload).
        assert scheduler.run_once() == []
        before = health(admin)['extraction']['unresolved_dead_letters']
        assert dead in {i['task_id'] for i in health(admin)['extraction']['dead_letter_items']}
        with w['client']() as client:
            csrf = workbench_login(client, w['owner']).json()['csrf_token']
            requeued = act(client, csrf, 'resolve_extraction_dead_letter', {'task_id': dead, 'disposition': 'requeue', 'reason': '供应商已恢复'})
            assert requeued.status_code == 200, requeued.text
        new = admin.execute("select job_id from runtime.jobs where queue='claim-extraction' and status='pending'").fetchall()
        assert len(new) == 1 and new[0][0] != dead and scheduler.run_once() == []    # nothing else is enqueued
        provider = registered_provider({'worker_pool': worker_pool, 'worker': worker_session, 'signer': signer, 'conversation_id': conversation_id}, message_ids)
        worker = ClaimExtractionWorker(worker_pool, worker_session, signer, provider, timezone='Asia/Shanghai')
        assert worker.run_once() == 'succeeded' and worker.run_once() == 'idle'
    assert admin.execute("select status,result->>'claims' from runtime.jobs where job_id=%s", (new[0][0],)).fetchone() == ('succeeded', '2')
    assert admin.execute('select count(*) from ontology.nexloop_claims where source_message_id = any(%s)', (message_ids,)).fetchone()[0] == 2
    assert admin.execute('select status from runtime.jobs where job_id=%s', (dead,)).fetchone()[0] == 'dead_lettered'
    x = health(admin)['extraction']
    assert x['unresolved_dead_letters'] == before - 1 and dead not in {i['task_id'] for i in x['dead_letter_items']}

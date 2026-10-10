"""NX-030: metrics snapshot, alert rules and evaluator, workbench reads and the read-only loopback export (0151).

Real PostgreSQL, real workbench login cookies (slice 1 fixture), a real alert-evaluator service credential on the domain-worker
role, the real configurator for the rule file and the real nexloop_metrics role. State is seeded by admin as synthetic operational
rows (refusals, a dead-lettered feed item, an escalation, a breached commitment exception) plus process samples written through
the sample function; the evaluation itself is the production SQL.
"""
import hashlib
import secrets
import threading
import urllib.request
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType

from multi_authority_fixture import seed_multi_authority
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.observability import (AlertEvaluator, AlertRulesRejected, GUARD, GuardTimings, apply_rules, load_rules, record_sample,
    render_prometheus, serve_export, validate_rules)
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, private, provision, published_action, uow, workbench,
    workbench_login)

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v1.json'
RAW = '请不要再给我发短信了'


@pytest.fixture
def observed(workbench, admin, pg, tmp_path):
    w = workbench
    provision(w)
    assert apply_rules(load_rules(RULES), TENANT, database_url_file=w['configurator'])['configured'] is True
    with open_core(make_conninfo(pg, user='nexloop_domain_worker')) as pool:
        session, token = seed_multi_authority(admin, pool, [('eios:action:nexloop.alert.evaluate:1', ResourceType.ACTION, Operation.EXECUTE)],
            identity_suffix='-alert-evaluator')
        evaluator = lambda: AlertEvaluator(pool, authenticate_service(pool, token, world='real'), w['f']['reader'].signer).run_once()
        yield dict(w=w, admin=admin, pg=pg, evaluate=evaluator, domain=pool)


def seed_state(admin, f):
    consumer, conversation, message = f['consumer'], 'c' * 64, 'd' * 64
    admin.execute("insert into runtime.nexloop_dispatch_refusals(tenant_id,world,path,code,intent_id) values(%s,'real','effect','NXB01',%s)", (TENANT, uuid.uuid4()))
    admin.execute("insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,status,last_code) values(%s,'real','claim-match','synthetic-item','{}','dead_lettered','matcher_failed')", (TENANT,))
    admin.execute("insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail) values(%s,'real',%s,%s,%s,'fallback_failed','{}')",
        (TENANT, message, conversation, consumer))
    admin.execute("insert into runtime.nexloop_commitment_exceptions(tenant_id,world,subject_ref,consumer_id,reason,detail) values(%s,'real',%s,%s,'breached','{}')",
        (TENANT, 'commitment:' + 'e' * 64, consumer))


def test_snapshot_is_codes_counts_and_ages_never_raw_text(observed):
    o = observed
    w, admin = o['w'], o['admin']
    seed_state(admin, w['f'])
    record_sample(w['f']['reader'].pool, 'guard', 'runtime-guard', {'calls': 100, 'timeouts': 3, 'errors': 0, 'p50_ms': 40, 'p95_ms': 1700, 'max_ms': 2300})
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        response = client.get('/api/v1/workbench/metrics')
        assert response.status_code == 200 and RAW not in response.text
        m = response.json()
        assert m['refusals']['last_5m'] == {'NXB01': 1} and m['replies']['escalations_last_hour'] == {'fallback_failed': 1}
        assert m['commitments']['exceptions_last_hour'] == {'breached': 1} and m['feeds']['claim-match']['dead_lettered_last_hour'] == 1
        assert m['guard']['status'] == 'ok' and m['guard']['timeout_rate'] == 0.03 and m['guard']['p95_ms_max'] == 1700
        assert m['host'] == {'status': 'unavailable'} and m['backup'] == {'status': 'unavailable'}
        assert m['effects']['unknown']['count'] == 0 and m['connections']['total'] >= 1
        # The overview backlog block is now served (was "unavailable" in slice 1).
        overview = client.get('/api/v1/workbench/overview').json()
        assert overview['backlog']['status'] == 'ok' and overview['backlog']['data']['feeds']['claim-match']['dead_lettered'] == 1
        assert client.get('/api/v1/workbench/metrics?world=test').status_code == 422
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get('/api/v1/workbench/metrics').status_code == 403


def test_evaluator_fires_once_deduplicates_resolves_and_waits_for_duration(observed):
    o = observed
    w, admin = o['w'], o['admin']
    seed_state(admin, w['f'])
    record_sample(w['f']['reader'].pool, 'pool', 'synthetic-api', {'requests_waiting': 2, 'pool_size': 4, 'max_size': 4})
    first = o['evaluate']()
    assert first['evaluated'] is True and first['rules_version'] == 1
    events = lambda: admin.execute('select rule_id,coalesce(selector,\'\'),kind from control.nexloop_alert_events order by event_id').fetchall()
    fired = set(events())
    assert {('refusal_budget_exhausted', 'NXB01', 'firing'), ('reply_escalated', 'fallback_failed', 'firing'), ('commitment_breached', 'breached', 'firing'),
            ('feed_dead_letter', 'claim-match', 'firing')} <= fired
    # pool_waiting needs five minutes of breach: breached now, not yet firing.
    assert not any(r == 'pool_waiting' for r, _, _ in fired)
    assert admin.execute("select firing,breached_since is not null from control.nexloop_alert_state where rule_id='pool_waiting'").fetchone() == (False, True)
    # A second pass adds no event (one alert per rule and selector while it lasts).
    o['evaluate']()
    assert len(events()) == len(fired)
    # The dead letter ages out of the one-hour window: resolved once; pool waiting has lasted 6 minutes: fires.
    admin.execute("update runtime.nexloop_work_feed set changed_at=clock_timestamp()-interval '2 hours' where item_key='synthetic-item'")
    admin.execute("update control.nexloop_alert_state set breached_since=clock_timestamp()-interval '6 minutes' where rule_id='pool_waiting'")
    o['evaluate']()
    later = events()[len(fired):]
    assert ('feed_dead_letter', 'claim-match', 'resolved') in later and ('pool_waiting', 'synthetic-api', 'firing') in later
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        alerts = client.get('/api/v1/workbench/alerts').json()
        assert alerts['real_world_only'] is True and alerts['evaluator']['stale'] is False
        assert {a['rule_id'] for a in alerts['firing']} >= {'refusal_budget_exhausted', 'reply_escalated', 'commitment_breached', 'pool_waiting'}
        assert 'feed_dead_letter' not in {a['rule_id'] for a in alerts['firing']}
        # The human-action audit is the owner's.
        assert client.get('/api/v1/workbench/human-actions').status_code == 403
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        assert client.get('/api/v1/workbench/conversations/' + w['conversation']['id']).status_code == 200
        audit = client.get('/api/v1/workbench/human-actions').json()['items']
        assert audit[0]['category'] == 'read' and audit[0]['action'] == 'read.conversation' and audit[0]['principal_id'] == w['owner']['principal_id']
        assert RAW not in str(audit)
    # Append-only events.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('delete from control.nexloop_alert_events')


def test_rules_are_trusted_configuration_only(observed):
    o = observed
    rules = load_rules(RULES)
    # The same version again is a replay; a lower or equal different version is refused.
    assert apply_rules(rules, TENANT, database_url_file=o['w']['configurator'])['replay'] is True
    changed = {**rules, 'rules': rules['rules'][:3]}
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        apply_rules(changed, TENANT, database_url_file=o['w']['configurator'])
    for broken in ({**rules, 'rules': [{**rules['rules'][0], 'condition': 'lt'}]}, {**rules, 'rules': [{**rules['rules'][0], 'metric': 'queues.*'}]},
                   {**rules, 'rules': rules['rules'] + [rules['rules'][0]]}, {**rules, 'schema_version': 'x'}):
        with pytest.raises(AlertRulesRejected):
            validate_rules(broken)
    with psycopg.connect(make_conninfo(o['pg'], user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select control.nexloop_configure_alert_rules('synthetic-a','{}'::jsonb)")
    with psycopg.connect(make_conninfo(o['pg'], user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select authz.nexloop_alert_evaluate('x','real','{}','x','{}')")


def test_samples_accept_numbers_only(observed):
    pool = observed['w']['f']['reader'].pool
    for kind, sample in [('guard', {'calls': 'many'}), ('guard', {'note': 1}), ('disk', {'calls': 1}), ('pool', {'requests_waiting': RAW})]:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            record_sample(pool, kind, 'synthetic', sample)
    timings = GuardTimings()
    for ms in [10] * 98 + [1999, 2500]:
        timings.observe(ms)
    sample, _ = timings.drain()
    assert sample == {'calls': 100, 'timeouts': 1, 'errors': 0, 'p50_ms': 10.0, 'p95_ms': 10.0, 'max_ms': 2500.0}
    assert timings.drain()[0] is None and GUARD is not None


def test_read_only_metrics_role_and_loopback_export(observed, tmp_path):
    o = observed
    admin, pg = o['admin'], o['pg']
    seed_state(admin, o['w']['f'])
    dsn = private(tmp_path, 'metrics-dsn', make_conninfo(pg, user='nexloop_metrics'))
    with psycopg.connect(make_conninfo(pg, user='nexloop_metrics')) as db:
        for statement in ('select * from runtime.nexloop_dispatch_refusals', 'select * from ontology.objects', 'select * from runtime.nexloop_conversation_messages'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(statement)
            db.rollback()
        exported = db.execute('select authz.nexloop_metrics_export()').fetchone()[0]
    assert {(s['tenant_id'], s['world']) for s in exported} >= {(TENANT, 'real'), (TENANT, 'test'), (TENANT, 'simulation')}
    with psycopg.connect(make_conninfo(pg, user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_metrics_export()')
    text = render_prometheus(exported)
    assert f'nexloop_refusals_last_5m{{key="NXB01",tenant="{TENANT}",world="real"}} 1' in text and RAW not in text
    server = serve_export(dsn, _free_port())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/internal/metrics', timeout=5).read().decode()
        assert '# TYPE nexloop_refusals_last_5m gauge' in body and 'NXB01' in body and RAW not in body
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/metrics', timeout=5)
    finally:
        server.shutdown()
        server.server_close()


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_doctor_observability_gate(observed, tmp_path):
    import json as _json
    import subprocess
    import sys
    from nexloop_eios.doctor import observability
    o = observed
    dsn = private(tmp_path, 'metrics-dsn', make_conninfo(o['pg'], user='nexloop_metrics'))
    before = observability(dsn, RULES)
    assert before['checks']['metrics_export'] is True and before['checks']['alert_evaluator_recent'] is False
    record_sample(o['w']['f']['reader'].pool, 'pool', 'synthetic-api', {'requests_waiting': 0, 'pool_size': 2, 'max_size': 4})
    o['evaluate']()
    after = observability(dsn, RULES)
    assert all(after['checks'].values()), after
    assert after['details']['observability']['backup'] == ['unavailable']
    result = subprocess.run([sys.executable, '-m', 'nexloop_eios.doctor', '--observability', '--metrics-database-url-file', str(dsn), '--alert-rules', str(RULES)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and _json.loads(result.stdout)['foundation_checks_passed'] is True and 'password' not in result.stdout
    wrong = private(tmp_path, 'api-dsn', make_conninfo(o['pg'], user='nexloop_api'))
    assert observability(wrong, RULES)['checks']['metrics_export'] is False  # only the read-only metrics role is accepted

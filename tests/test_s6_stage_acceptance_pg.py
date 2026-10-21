"""S6 closure: the stage acceptance script scripts/acceptance/s6_stage.py run locally on PostgreSQL (the positive control of every AT).

The fixture stands in for the stage: a bootstrapped catalog; five synthetic Consumers (persona simulated_agent), two failed message
Runs each with retrieval misses (and one near-duplicate pair across two Consumers, and one wide read for R3's protected ref); the
evolution budget; the evaluator registered (as the owner's trusted configuration would) and a registered test-only evaluation
credential. The script then connects as the services do (nexloop_evo_reader / _writer / _evaluator with their own credentials) and
the inspector as a read-only connection. A dry run writes nothing.
"""
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from erasure_support import mark_erasing
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_evolution_traces_pg import hexid, run
from test_evolution_v3_pg import eval_credential
from test_evolution_v1_pg import register

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import s6_stage as S  # noqa: E402

NOW = datetime.now(UTC).replace(microsecond=0)
TERM = hexid('s6-stage-term')
REF, OTHER = 'eios:object_type:Product', 'nexloop:vocabulary:color/red'
WIDE = [f'eios:property:Consumer/p{i}' for i in range(10)]
PROTECTED = WIDE[-1]


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def access(admin, run_id, terms, refs, *, browse=True):
    admin.execute("""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,operation,query_kind,term_hashes,coverage,ambiguity,
        result_refs,result_count,unresolved_count,recorded_at) values(%s,'real',%s,'access',%s,'browse','any',%s,0,false,%s,%s,0,%s)""",
                  (TENANT_A, uuid.uuid4(), run_id, list(terms), Jsonb([{'ref': r, 'revision': '1'} for r in refs]), len(refs), NOW - timedelta(minutes=30)))


def failed_run(admin, digest, consumer, minutes):
    rid = run(admin, digest, NOW - timedelta(minutes=minutes), consumer=consumer, link='message', tenant=TENANT_A)
    job = 'task_' + rid
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
            request_id,normalized_input,plan_snapshot,status,result,created_at,updated_at) values(%s,'inv-'||%s,'NexLoop.event','operations','event','queue',%s,'a',%s,%s,
            '{}','{}','failed',%s,now(),now())""", (job, rid, TENANT_A, job, job, Jsonb({'failure_code': 'run_failed'})))
        admin.execute('update runtime.nexloop_message_routes set task_id=%s where run_id=%s', (job, rid))
    return rid


@pytest.fixture
def stage(admin, pg, tmp_path):
    bootstrap(admin)
    writer, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32)
    reader, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, identity_suffix='-reader')
    evaluator, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, identity_suffix='-evaluator')
    # Trusted configuration (the owner's, not the script's): the principals, the test-only evaluation credential, the budget.
    register(admin, hexid(writer), 'writer')
    register(admin, hexid(evaluator), 'evaluator')
    evaluation, _ = eval_credential(admin, '-s6-stage')
    admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','evolution','CNY',100,%s,%s,1)""", (TENANT_A, NOW - timedelta(days=1), NOW + timedelta(days=29)))
    consumers = [hexid('s6-consumer-' + str(i)) for i in range(5)]
    runs = {}
    for i, c in enumerate(consumers):
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,'simulated_agent',1,'synthetic')",
                      (TENANT_A, c))
        runs[c] = [failed_run(admin, hexid(writer), c, 50 - 2 * i - k) for k in range(2)]
    pair = (runs[consumers[0]][0], runs[consumers[1]][0])
    for c in consumers:
        for rid in runs[c]:
            access(admin, rid, [TERM], [])                                                      # the miss
            access(admin, rid, [hexid('dup') if rid in pair else hexid('other-' + rid)], [REF, OTHER])  # a hit (the pair asked the same)
            access(admin, rid, [TERM, hexid('wide')], WIDE)                                    # a wide read: R3's protected ref is its last
    d = tmp_path / 'private'
    d.mkdir(mode=0o700)
    dsn = {role: private(d / f'{role}.dsn', make_conninfo(pg, user=user)) for role, user in
           (('reader', 'nexloop_evo_reader'), ('writer', 'nexloop_evo_writer'), ('evaluator', 'nexloop_evo_evaluator'))}
    dsn['inspector'] = private(d / 'inspector.dsn', pg)  # locally the superuser, read only; on stage a read-only audit role
    dsn['registrar'] = private(d / 'registrar.dsn', pg)
    creds = {k: private(d / f'{k}.credential', t) for k, t in (('reader', reader), ('writer', writer), ('evaluator', evaluator), ('evaluation', evaluation))}
    config = {'schema': S.SCHEMA, 'connections': dsn, 'credentials': creds,
              'window': {'since': (NOW - timedelta(hours=1)).isoformat(), 'until': (NOW + timedelta(minutes=1)).isoformat()},
              'personas': ['simulated_agent'], 'label': 's6-local-1', 'parent_revision': 'sem-7', 'evaluator_version': 'l0-1', 'model_profile': 'none',
              'created_at': '2026-10-21T00:00:00Z', 'l0_inputs': {'latency_ms': {'parent': [10.0] * 50, 'candidate': [10.5] * 50}},
              'protected_refs': [PROTECTED], 'erasure': {'consumer_id': consumers[2]}, 'state_dir': str(d / 'state')}
    path = private(d / 's6-stage.json', json.dumps(config))
    return dict(admin=admin, config=path, consumers=consumers, tokens=(writer, reader, evaluator, evaluation), pg=pg, tmp=tmp_path)


def everything(admin):
    tables = [r[0] for r in admin.execute("""select n.nspname||'.'||c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p') and n.nspname in ('authz','control','runtime','ontology') order by 1""")]
    return {t: admin.execute(f"select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {t} x").fetchone()[0] for t in tables}


def results(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def by_name(result):
    return {a['name']: a for a in result['assertions']}


def test_a_dry_run_writes_nothing_and_plans_every_at(stage, capsys):
    before = everything(stage['admin'])
    assert S.main(['all', '--config', stage['config'], '--dry-run']) == 0
    out = results(capsys)
    assert [r['command'] for r in out] == list(S.ALL) and all(r['passed'] is None and r['dry_run'] for r in out)
    assert all(r['assertions'] == [] for r in out)
    assert S.main(['r7-prepare', '--config', stage['config'], '--dry-run']) == 0
    results(capsys)
    assert everything(stage['admin']) == before


def test_every_at_passes_on_the_local_stage(stage, capsys):
    """The positive control, in the checklist's order (§4.3)."""
    expected = {'at046': True, 'r5': True, 'at048': True, 'r4': True, 'at023': True, 'r1': True, 'r3': True}
    for command, passed in expected.items():
        S.main([command, '--config', stage['config']])
        [r] = results(capsys)
        assert r['passed'] is passed, (command, [a for a in r['assertions'] if not a['ok']], [s for s in r['steps'] if not s['ok']])
        assert r['executed_by'] is None and r['judged_by'] is None and r['evidence']['type'] == 'command' and r['assertions']
    # AT-023's protected-table digests were taken before and after and are equal.
    S.main(['at023', '--config', stage['config']])
    [r] = results(capsys)
    assert r['digests']['before'] == r['digests']['after'] and len(r['digests']['before']) >= 6


def test_at048_detects_both_leakage_routes_and_passes_regrouped(stage, capsys):
    """AT-048: a split grouped by Run with one Consumer on both sides and a near-duplicate pair across the sides both return
    leakage_detected (the opening derives the Consumers itself since the 0286 fix); regrouped by Consumer both open."""
    assert S.main(['at048', '--config', stage['config']]) == 0
    [r] = results(capsys)
    got = by_name(r)
    assert set(got) == {'by_run: opening returns leakage_detected', 'near_duplicate: opening returns leakage_detected', 'by_consumer: opening passes',
                        'regrouped: opening passes'}
    assert r['passed'] is True and all(a['ok'] for a in got.values())


def test_r7_invalidates_the_holdout_and_the_snapshot_after_the_erasure(stage, capsys):
    assert S.main(['r7-prepare', '--config', stage['config']]) == 0
    results(capsys)
    assert S.main(['r7-verify', '--config', stage['config'], '--dry-run']) == 1  # before the erasure: nothing to verify yet
    results(capsys)
    mark_erasing(stage['admin'], TENANT_A, stage['consumers'][2])  # what erase_synthetic_consumer.py starts (NX-029)
    assert S.main(['r7-verify', '--config', stage['config']]) == 0
    [r] = results(capsys)
    assert r['passed'] is True and all(a['ok'] for a in r['assertions'])


def test_the_output_names_no_secret_and_bad_input_is_refused(stage, capsys, tmp_path):
    S.main(['at046', '--config', stage['config'], '--out', str(tmp_path / 'out.json')])
    text = capsys.readouterr().out + (tmp_path / 'out.json').read_text()
    for secret in stage['tokens'] + (stage['pg'],):
        assert secret not in text
    assert oct(os.stat(tmp_path / 'out.json').st_mode & 0o777) == '0o600'
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'schema': 'other'}))
    bad.chmod(0o600)
    assert S.main(['at046', '--config', str(bad)]) == 2
    loose = tmp_path / 'loose.json'
    loose.write_text(Path(stage['config']).read_text())
    loose.chmod(0o644)
    assert S.main(['at046', '--config', str(loose)]) == 2  # not private
    assert 'refused' in capsys.readouterr().out

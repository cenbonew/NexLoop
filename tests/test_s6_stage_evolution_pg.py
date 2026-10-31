"""S6 closure: the stage acceptance script scripts/acceptance/s6_stage_evolution.py run locally on PostgreSQL (the positive control of every AT).

The fixture stands in for the stage: a bootstrapped catalog; five synthetic Consumers (persona simulated_agent), two failed message
Runs each with retrieval misses (and one near-duplicate pair across two Consumers, and one wide read for R3's protected ref); the
evolution budget; the evaluator registered (as the owner's trusted configuration would) and a registered test-only evaluation
credential. The script then connects as the services do (nexloop_evo_reader / _writer / _evaluator with their own credentials) and
the inspector as a read-only connection. A dry run writes nothing.
"""
import io
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
import s6_stage_evolution as S  # noqa: E402

TERM = hexid('s6-stage-term')
REF, OTHER = 'eios:object_type:Product', 'nexloop:vocabulary:color/red'
WIDE = [f'eios:property:Consumer/p{i}' for i in range(10)]
PROTECTED = WIDE[-1]


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def access(admin, run_id, terms, refs, now, *, browse=True):
    admin.execute("""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,operation,query_kind,term_hashes,coverage,ambiguity,
        result_refs,result_count,unresolved_count,recorded_at) values(%s,'real',%s,'access',%s,'browse','any',%s,%s,false,%s,%s,0,%s)""",
                  (TENANT_A, uuid.uuid4(), run_id, list(terms), 1 if refs else 0, Jsonb([{'ref': r, 'revision': '1'} for r in refs]), len(refs),
                   now - timedelta(minutes=30)))  # a browse covers its statement exactly when it returns something (semantic.py)


def failed_run(admin, digest, consumer, minutes, now):
    rid = run(admin, digest, now - timedelta(minutes=minutes), consumer=consumer, link='message', tenant=TENANT_A)
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
    # The data and the window are both relative to the time the test runs (PG's clock), not to collection; only the budget
    # period (+29 days) depends on the real time.
    now = admin.execute("select date_trunc('second', clock_timestamp())").fetchone()[0].astimezone(UTC)
    writer, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32)
    reader, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, identity_suffix='-reader')
    evaluator, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, identity_suffix='-evaluator')
    # Trusted configuration (the owner's, not the script's): the principals, the test-only evaluation credential, the budget.
    register(admin, hexid(writer), 'writer')
    register(admin, hexid(evaluator), 'evaluator')
    evaluation, _ = eval_credential(admin, '-s6-stage')
    admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','evolution','CNY',100,%s,%s,1)""", (TENANT_A, now - timedelta(days=1), now + timedelta(days=29)))
    consumers = [hexid('s6-consumer-' + str(i)) for i in range(5)]
    runs = {}
    for i, c in enumerate(consumers):
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,'simulated_agent',1,'synthetic')",
                      (TENANT_A, c))
        runs[c] = [failed_run(admin, hexid(writer), c, 50 - 2 * i - k, now) for k in range(2)]
    pair = (runs[consumers[0]][0], runs[consumers[1]][0])
    for c in consumers:
        for rid in runs[c]:
            access(admin, rid, [TERM], [], now)                                                    # the miss
            access(admin, rid, [hexid('dup') if rid in pair else hexid('other-' + rid)], [REF, OTHER], now)  # a hit (the pair asked the same)
            access(admin, rid, [TERM, hexid('wide')], WIDE, now)                                  # a wide read: R3's protected ref is its last
    d = tmp_path / 'private'
    d.mkdir(mode=0o700)
    dsn = {role: private(d / f'{role}.dsn', make_conninfo(pg, user=user)) for role, user in
           (('reader', 'nexloop_evo_reader'), ('writer', 'nexloop_evo_writer'), ('evaluator', 'nexloop_evo_evaluator'))}
    tok = {k: private(d / f'{k}.credential', t) for k, t in (('reader', reader), ('writer', writer), ('evaluator', evaluator), ('evaluation', evaluation))}
    config = {'schema': 'nexloop-s6-stage/1', 'executor': 'L3 local', 'release': 'local', 'tenant_id': TENANT_A, 'results_dir': str(d / 'results'),
              'roles': {**{k: {'dsn_file': dsn[k], 'token_file': tok[k]} for k in ('reader', 'writer', 'evaluator')},
                        'inspector': {'dsn_file': private(d / 'inspector.dsn', pg)},  # locally the superuser, read only; on stage the bootstrap DSN
                        'evaluation': {'token_file': tok['evaluation']}, 'registrar': {'dsn_file': private(d / 'registrar.dsn', pg)}},
              'inputs': {'candidate_template': str(d / 'candidate-template.json'),
                         'evolution': {'window': {'since': (now - timedelta(hours=1)).isoformat(), 'until': (now + timedelta(minutes=1)).isoformat()},
                                       'personas': ['simulated_agent'], 'label': 's6-local-1', 'parent_revision': 'sem-7', 'evaluator_version': 'l0-1',
                                       'model_profile': 'none', 'created_at': '2026-10-21T00:00:00Z',
                                       'l0_inputs': {'latency_ms': {'parent': [10.0] * 50, 'candidate': [10.5] * 50}},
                                       'protected_refs': [PROTECTED], 'erasure_consumer_id': consumers[2]}}}
    path = private(d / 's6-stage.json', json.dumps(config))
    return dict(admin=admin, config=path, consumers=consumers, tokens=(writer, reader, evaluator, evaluation), pg=pg, tmp=tmp_path, now=now)


def everything(admin):
    tables = [r[0] for r in admin.execute("""select n.nspname||'.'||c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p') and n.nspname in ('authz','control','runtime','ontology') order by 1""")]
    return {t: admin.execute(f"select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {t} x").fetchone()[0] for t in tables}


def call(*argv):
    out = io.StringIO()
    code = S.main(list(argv), out=out)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def checks(result):
    """The assertion steps (expected values), by name."""
    return {s['name']: s for s in result['steps'] if s['name'] not in ('note', 'protected table digests') and s['expected'] not in ('ok', None)}


def test_a_dry_run_writes_nothing_and_plans_every_at(stage):
    before = everything(stage['admin'])
    code, out = call('all', '--config', stage['config'], '--dry-run')
    assert code == 0 and [r['check'] for r in out] == list(S.ALL)
    assert all(r['passed'] is None and r['dry_run'] and r['blocked'] == [] and r['schema'] == 'nexloop-s6-stage-result/1' for r in out)
    assert all(not checks(r) for r in out)  # planned and probed only
    for command in ('r7-prepare', 'candidate-template'):
        assert call(command, '--config', stage['config'], '--dry-run')[0] == 0
    assert everything(stage['admin']) == before
    assert not os.path.exists(Path(json.loads(Path(stage['config']).read_text())['results_dir']))  # a dry run writes no result file


def test_every_at_passes_on_the_local_stage(stage):
    """The positive control, in the checklist's order (§4.3); each result is also written 0600 to results_dir."""
    for command in ('at046', 'r5', 'at048', 'r4', 'at023', 'r1', 'r3'):
        code, [r] = call(command, '--config', stage['config'])
        failed = [x for x in r['steps'] if x['passed'] is not True]
        assert code == 0 and r['passed'] is True and not failed, (command, failed)
        assert r['acceptance'] == S.ACCEPTANCE[command] and checks(r) and r['world'] == 'test' and r['executor'] == 'L3 local'
    results = Path(json.loads(Path(stage['config']).read_text())['results_dir'])
    files = sorted(results.glob('*.json'))
    assert len(files) == 7 and all(oct(f.stat().st_mode & 0o777) == '0o600' for f in files)
    # AT-023's protected-table digests (equal before and after) are in the result.
    code, [r] = call('at023', '--config', stage['config'])
    got = checks(r)['protected tables unchanged']
    assert got['passed'] is True and got['expected'] == got['observed'] and len(got['expected']) >= 6


def test_at048_detects_both_leakage_routes_and_passes_regrouped(stage):
    """AT-048: a split grouped by Run with one Consumer on both sides and a near-duplicate pair across the sides both return
    leakage_detected (the opening derives the Consumers itself since the 0259 fix); regrouped by Consumer both open."""
    code, [r] = call('at048', '--config', stage['config'])
    got = checks(r)
    assert set(got) == {'by_run: opening returns leakage_detected', 'near_duplicate: opening returns leakage_detected', 'by_consumer: opening passes',
                        'regrouped: opening passes'}
    assert code == 0 and r['passed'] is True and all(x['passed'] for x in got.values())


def test_r7_invalidates_the_holdout_and_the_snapshot_after_the_erasure(stage):
    assert call('r7-prepare', '--config', stage['config'])[0] == 0
    code, [r] = call('r7-verify', '--config', stage['config'], '--dry-run')  # before the erasure: blocked (passed null), never a pass
    assert code == 1 and r['passed'] is None and r['blocked']
    mark_erasing(stage['admin'], TENANT_A, stage['consumers'][2])  # what erase_synthetic_consumer.py starts (NX-029)
    code, [r] = call('r7-verify', '--config', stage['config'])
    assert code == 0 and r['passed'] is True


def test_the_candidate_template_for_the_schema_checks(stage):
    """L2's NX-040 checks start from inputs.candidate_template: an open candidate body of this diagnosis, written 0600."""
    code, [r] = call('candidate-template', '--config', stage['config'])
    assert code == 0 and r['passed'] is True
    path = Path(json.loads(Path(stage['config']).read_text())['inputs']['candidate_template'])
    body = json.loads(path.read_text())
    assert oct(path.stat().st_mode & 0o777) == '0o600'
    assert set(body) == {'diagnosis_id', 'candidate', 'category', 'patch', 'case_groups', 'regroups', 'source_consumer_ids'}
    assert body['candidate']['status'] == 'proposed'
    assert stage['admin'].execute('select status from runtime.nexloop_evo_diagnoses where diagnosis_id=%s', (body['diagnosis_id'],)).fetchone()[0] == 'open'


def test_the_output_names_no_secret_and_bad_input_is_refused(stage, tmp_path):
    code, out = call('at046', '--config', stage['config'])
    config = json.loads(Path(stage['config']).read_text())
    text = json.dumps(out) + ''.join(f.read_text() for f in Path(config['results_dir']).glob('*.json'))
    for secret in stage['tokens'] + (stage['pg'], config['results_dir'], config['roles']['inspector']['dsn_file']):
        assert secret not in text
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'schema': 'other'}))
    bad.chmod(0o600)
    assert call('at046', '--config', str(bad))[0] == 2
    loose = tmp_path / 'loose.json'
    loose.write_text(Path(stage['config']).read_text())
    loose.chmod(0o644)
    code, out = call('at046', '--config', str(loose))
    assert code == 2 and 'refused' in out[0]  # not private


def test_the_unified_example_configuration_is_shaped_for_this_part(tmp_path):
    """The example (one for both parts) carries this part's roles and inputs.evolution; placeholders aside, the shape loads."""
    example = json.loads((ROOT / 'deploy/examples/s6-stage.example.json').read_text())
    assert {'reader', 'writer', 'evaluator', 'evaluation', 'inspector'} <= set(example['roles'])
    assert set(example['inputs']['evolution']) == {'window', 'personas', 'label', 'parent_revision', 'evaluator_version', 'model_profile', 'created_at',
                                                    'l0_inputs', 'protected_refs', 'erasure_consumer_id'}
    filled = json.loads(json.dumps(example))
    for role in filled['roles'].values():
        for k in role:
            role[k] = '/private/' + k
    filled['inputs']['candidate_template'] = '/private/template.json'
    filled['inputs']['evolution'].update(window={'since': '2026-10-21T00:00:00+08:00', 'until': '2026-10-21T23:00:00+08:00'}, label='s6-x',
                                         parent_revision='sem-1', created_at='2026-10-21T00:00:00Z', protected_refs=['eios:property:Consumer/x'])
    path = tmp_path / 'filled.json'
    path.write_text(json.dumps(filled))
    path.chmod(0o600)
    config, ev = S.load_config(path)
    assert ev['connections']['inspector'] == '/private/dsn_file' and ev['credentials']['evaluation'] == '/private/token_file'

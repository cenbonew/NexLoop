"""scripts/acceptance/at046_stage.py: the AT-046 stage run, locally.
- On PostgreSQL (a simulation world with synthetic data): the run phase probes read only; the twin port is in the release (eighth
  batch) but no twin role or input is configured here (`blocked`); without prepare's catalog publication the Run step fails and without credentials the credential step fails, so the
  run fails (a failure is never masked by blocked); the real side's digest is unchanged.
- With the Run Action published for the world (as the D10 tests seed it) and an intake stand-in, the run phase waits for the task and
  checks the Run credential's world (fake ports: no host process here).
- The dry run calls nothing; prepare goes through the owner's two Actions; no private value in a result.
"""
import io
import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import at046_stage as A  # noqa: E402

from nexloop_eios.bootstrap import bootstrap  # noqa: E402
from support.tenants import TENANT_A  # noqa: E402


def config_file(tmp_path, **inputs):
    c = {'schema': 'nexloop-s6-stage/1', 'executor': 'L2 local', 'release': 'local', 'tenant_id': TENANT_A, 'results_dir': str(tmp_path / 'results'), 'inputs': inputs}
    p = tmp_path / 'at046.json'
    p.write_text(json.dumps(c))
    p.chmod(0o600)
    return p


def run(tmp_path, phase, ports, *extra, **inputs):
    out = io.StringIO()
    code = A.main([phase, '--config', str(config_file(tmp_path, **inputs)), *extra], ports=ports, out=out)
    return code, json.loads(out.getvalue())


def steps(doc):
    return {s['name']: s for s in doc['steps']}


def world_with_data(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    world = admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject','i-at046',%s)",
                          (TENANT_A, Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'simulation', 'purpose': 'AT-046 stage',
                                            'expires_in_days': 7}))).fetchone()[0]['world']
    admin.execute("select control.nexloop_governed_generate_sim_world_data(%s,'real','owner-principal','owner-subject','i-gen',%s)",
                  (TENANT_A, Jsonb({'operation': 'generate_sim_world_data', 'request_id': 'gen', 'world': world, 'kind': 'synthetic', 'seed': 's',
                                    'consumers': 2, 'events_per_consumer': {'min': 1, 'max': 1}, 'segments': {'trial': 1}})))
    return world


def test_the_run_phase_blocks_what_this_release_lacks_and_keeps_real_unchanged(admin, pg, tmp_path):
    world = world_with_data(admin)
    ports = A.Ports(probe=lambda: psycopg.connect(pg, autocommit=True, options='-c default_transaction_read_only=on'))
    code, doc = run(tmp_path, 'run', ports, world=world)
    assert code == 1 and doc['passed'] is False, json.dumps(doc, ensure_ascii=False)  # failed steps below: never masked by blocked (5.3)
    st = steps(doc)
    assert st['synthetic_consumers_present']['observed']['consumers'] == 2
    # From the eighth batch on the twin port is in the release (NX-042 T3); here no twin role or input is configured, so it blocks on that.
    assert st['twin_observe_act']['observed']['blocked'] == 'roles.twin_port or inputs.twin not configured'
    assert st['run_action_published_for_the_world']['passed'] is False  # prepare publishes it (publish_sim_catalog)
    assert st['real_side_unchanged']['passed'] is True and st['real_side_unchanged']['observed']['tables'] > 5
    assert st['world_credentials_only_of_the_world']['passed'] is False  # no credential issued yet: none found is not a pass (5.3)
    assert TENANT_A not in json.dumps(doc)


def REFUSED(world, consumer, effect, twin):
    """The two AT-046 refusal probes as the server answers them (tests/test_at046_negatives_pg.py runs them on PostgreSQL)."""
    return {'n1': {'real_session': {'refused': True, 'sqlstate': '42501', 'reason': 'service authentication denied'}},
            'n2': {'refused': True, 'sqlstate': '42501', 'reason': 'activation command denied'}}


class FakeProbe:
    """A read-only probe answering the run phase's queries from a dict (no database)."""

    def __init__(self, world, run_credentials=None):
        self.world, self.calls = world, []
        self.run_credentials = [world] if run_credentials is None else run_credentials
        self.status = iter(['queued', 'running', 'succeeded'])

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=()):
        self.calls.append(sql.split()[0:3])
        self.sql = sql
        return self

    def fetchall(self):
        s = self.sql
        if s.startswith('set transaction'):
            return []
        if s.startswith('select count(*) from control.nexloop_action_definitions'):
            return [(1,)]
        if ' union all ' in s:
            return [('runtime.some_table', 'd' * 32), ('authz.nexloop_service_credentials', 'e' * 32)]
        if 'information_schema' in s:
            return [('runtime.some_table', True)]

        if 'to_regprocedure' in s:
            return [(False,)]
        if 'nexloop_sim_objects' in s:
            return [('c' * 64,)]
        if 'runtime.jobs' in s:
            return [(next(self.status),)]
        if 'nexloop_run_credentials' in s:
            return [(self.world,)]
        if 'array_agg' in s:
            return [(self.run_credentials,)]
        return [(0,)]


def test_with_the_run_action_published_the_run_phase_waits_for_the_task(tmp_path, monkeypatch):
    world = 'sim:' + str(uuid.uuid4())
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    seen = {}
    ports = A.Ports(probe=FakeProbe(world), intake=lambda w, c, e: seen.update(world=w, consumer=c, effect=e) or ('task-1', 'run-1'), negatives=REFUSED)
    code, doc = run(tmp_path, 'run', ports, world=world)
    st = steps(doc)
    assert st['simulation_run_completes']['passed'] is True and st['simulation_run_completes']['observed'] == {'task_status': 'succeeded', 'run_world_is_simulation': True}
    assert seen['world'] == world and seen['effect'].startswith('eios:action:') and doc['blocked'] == ['twin_observe_act: roles.twin_port or inputs.twin not configured']
    assert code == 1 and doc['passed'] is None  # still blocked on the twin: never a pass


class FakeOwner:
    csrf = 'c'

    def __init__(self):
        self.calls = []

    def _call(self, method, path, body=None, extra=None):
        self.calls.append(path)
        if path.endswith('create_sim_world'):
            return 200, {'world': 'sim:' + str(uuid.UUID(int=5, version=4)), 'created': True}
        if path.endswith('publish_sim_catalog'):
            return 200, {'world': body['world'], 'actions': ['eios:action:nexloop.service.request:1'], 'copied': 1, 'already_present': 0}
        if path.endswith('retire_consumer_twin'):
            self.retired = (body, extra)
            return 200, {'twin_id': body['twin_id'], 'version': body['version'], 'status': 'retired', 'retired': True}
        if path.endswith('publish_consumer_twin'):
            self.twin = (body, extra)
            return 200, {'twin_id': body['definition']['twin_id'], 'version': 1, 'status': 'published', 'published': True}
        return 200, {'generation_id': 'g-1', 'counts': {'consumers': 3}}


def test_prepare_goes_through_the_owner_actions_and_the_dry_run_calls_nothing(tmp_path):
    owner = FakeOwner()
    code, doc = run(tmp_path, 'prepare', A.Ports(owner=owner))
    assert code == 0 and doc['passed'] is True and owner.calls == ['/api/v1/workbench/actions/create_sim_world', '/api/v1/workbench/actions/generate_sim_world_data',
                                                         '/api/v1/workbench/actions/publish_sim_catalog']
    assert steps(doc)['world_created']['evidence']['world'].startswith('sim:')

    class Nothing(A.Ports):
        def act(self, *a):
            raise AssertionError('called')

        def read(self, *a):
            raise AssertionError('called')
    code, doc = run(tmp_path, 'run', Nothing(), '--dry-run', world='sim:x')
    assert code == 0 and doc['dry_run'] is True and len(list((tmp_path / 'results').iterdir())) == 1  # only prepare wrote a result
    code, doc = run(tmp_path, 'run', A.Ports(), world='real')
    assert code == 2


def test_a_failed_step_fails_the_run_even_with_a_blocked_step(tmp_path, monkeypatch):
    """Review 5.3: blocked never masks a failure; and a credential step that finds no credential is not a pass."""
    world = 'sim:' + str(uuid.uuid4())
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    ports = A.Ports(probe=FakeProbe(world, run_credentials=[]), intake=lambda w, c, e: ('task-1', 'run-1'))
    code, doc = run(tmp_path, 'run', ports, world=world)
    st = steps(doc)
    assert st['world_credentials_only_of_the_world']['passed'] is False  # none found
    assert doc['blocked'] and code == 1 and doc['passed'] is False, json.dumps(doc, ensure_ascii=False)
    # The same through the result document alone: blocked plus a failed step is a failure; blocked alone stays null.
    r = A.Result('at046-run', ['AT-046'], {'release': 'local', 'executor': 'L2 local', 'tenant_id': TENANT_A}, world='simulation')
    r.block('twin', 'eighth batch')
    r.step('ok', 'fine', {}, True)
    assert r.document()['passed'] is None
    r.step('bad', 'broken', {}, False)
    assert r.document()['passed'] is False and r.document()['world'] == 'simulation'


def test_the_real_digest_sees_an_update(admin, pg, tmp_path):
    """Review 5.2 positive control: a change to a world-real row of the tenant changes the digest (not only a count)."""
    world_with_data(admin)
    # catalog-exception: a real definition changed directly, the positive control of the real-side digest
    admin.execute("""insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
        values(%s,'real','eios:action:at046.probe:1','{"stable_name":"at046.probe","version":1}','{}',true)""", (TENANT_A,))
    ports = A.Ports(probe=lambda: psycopg.connect(pg, autocommit=True, options='-c default_transaction_read_only=on'))
    before = A.real_digest(ports, TENANT_A)
    assert 'control.nexloop_action_definitions' in before and len(before) > 5
    admin.execute("update control.nexloop_action_definitions set active=false where tenant_id=%s and resource_id='eios:action:at046.probe:1'", (TENANT_A,))
    after = A.real_digest(ports, TENANT_A)
    assert [t for t in before if before[t] != after[t]] == ['control.nexloop_action_definitions']
    assert {'authz.nexloop_service_credentials', 'authz.nexloop_authority_facts'} <= set(before)
    admin.execute("""insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload)
        values(%s,'scope',array['at046-probe'],'{}')""", (TENANT_A,))
    assert [t for t in after if after[t] != A.real_digest(ports, TENANT_A)[t]] == ['authz.nexloop_authority_facts']


def test_on_stage_the_run_is_three_parts_on_two_hosts_merged_into_one_result(tmp_path, monkeypatch):
    """L6 sim-issue-install review 1.1: the read-only probe reaches PostgreSQL over DATA_HOST's socket and the intake (sim_api) over
    APP_HOST's hostssl, so no single process has both. probe-before and probe-after run with the probe only, intake with the intake only
    (each refuses to use the other's port), the parts pass their evidence through --from, and merge combines them into at046-run."""
    world = 'sim:' + str(uuid.uuid4())
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)

    def part(phase, ports, *prior, **inputs):
        out = io.StringIO()
        argv = [phase, '--config', str(config_file(tmp_path, world=world, **inputs))]
        for p in prior:
            argv += ['--from', str(p)]
        code = A.main(argv, ports=ports, out=out)
        path = tmp_path / f'{phase}.json'
        path.write_text(out.getvalue())
        return code, json.loads(out.getvalue()), path

    class NoIntake(A.Ports):
        def __init__(self, probe):
            super().__init__(probe=probe)

    class NoProbe(A.Ports):
        def read(self, *a):
            raise AssertionError('the intake part never reads through the probe')
    probe = FakeProbe(world)
    code, before, before_path = part('probe-before', NoIntake(probe))
    assert code == 0 and before['passed'] is True and steps(before)['real_digest_before']['evidence']['world'] == world
    seen = {}
    code, accepted, intake_path = part('intake', NoProbe(intake=lambda w, c, e: seen.update(world=w, consumer=c) or ('task-1', 'run-1'), negatives=REFUSED), before_path)
    assert seen == {'world': world, 'consumer': 'c' * 64} and steps(accepted)['run_accepted']['evidence'] == {'task_id': 'task-1', 'run_id': 'run-1'}
    assert accepted['blocked'] == ['twin_observe_act: roles.twin_port or inputs.twin not configured'] and code == 1
    code, after, after_path = part('probe-after', NoIntake(probe), before_path, intake_path)
    assert steps(after)['simulation_run_completes']['passed'] is True and steps(after)['real_side_unchanged']['passed'] is True
    code, merged, _ = part('merge', A.Ports(), before_path, intake_path, after_path)
    assert merged['check'] == 'at046-run' and merged['passed'] is None and code == 1  # blocked on the twin only: never a pass
    assert {s['name'] for s in merged['steps']} >= {'synthetic_consumers_present', 'run_accepted', 'simulation_run_completes', 'real_side_unchanged'}
    assert steps(merged)['parts_of_one_run']['passed'] is True and steps(merged)['required_steps_present']['passed'] is True
    assert len({d['run_nonce'] for d in (before, accepted, after, merged)}) == 1 and A.s6_nonce_ok(before['run_nonce'])
    # A failed part fails the merge.
    failed = json.loads(after_path.read_text())
    failed['steps'][-1]['passed'] = False
    bad = tmp_path / 'after-failed.json'
    bad.write_text(json.dumps(failed))
    assert part('merge', A.Ports(), before_path, intake_path, bad)[1]['passed'] is False

    def forged(name, change):
        doc = json.loads(before_path.read_text() if name == 'before' else intake_path.read_text() if name == 'intake' else after_path.read_text())
        change(doc)
        path = tmp_path / f'forged-{name}-{uuid.uuid4().hex[:6]}.json'
        path.write_text(json.dumps(doc))
        return path

    def chain_of(doc):
        return next(st for st in doc['steps'] if st['name'] == 'run_chain')['evidence']
    # L6 at046-split review 1.1: spliced chains never merge into a pass - each is a failed parts_of_one_run.
    # Another run's probe-before (its own nonce), kept apart: part() writes <phase>.json, so the first one is restored afterwards.
    first = before_path.read_text()
    second_before = tmp_path / 'second-probe-before.json'
    second_before.write_text(part('probe-before', NoIntake(FakeProbe(world)))[2].read_text())
    before_path.write_text(first)
    assert json.loads(second_before.read_text())['run_nonce'] != before['run_nonce']
    spliced = {
        'the same probe-before three times': (before_path, before_path, before_path),
        'out of order': (intake_path, before_path, after_path),
        'an older probe-before of another run': (second_before, intake_path, after_path),
        'a part of another nonce': (before_path, intake_path, forged('after', lambda d: d.update(run_nonce='f' * 24))),
        'no run nonce': (before_path, intake_path, forged('after', lambda d: d.pop('run_nonce'))),
        'equal timestamps': (before_path, forged('intake', lambda d: d.update(executed_at=json.loads(before_path.read_text())['executed_at'])), after_path),
        'another world': (before_path, forged('intake', lambda d: chain_of(d).update(world='sim:' + str(uuid.uuid4()))), after_path),
        'another tenant': (before_path, intake_path, forged('after', lambda d: chain_of(d).update(tenant_ref='0' * 16))),
        'timestamps backwards': (before_path, forged('intake', lambda d: d.update(executed_at='2000-01-01T00:00:00+00:00')), after_path),
        'a dry run': (before_path, intake_path, forged('after', lambda d: d.update(dry_run=True))),
        'no chain step': (before_path, intake_path, forged('after', lambda d: d.update(steps=[st for st in d['steps'] if st['name'] != 'run_chain']))),
    }
    for why, files in spliced.items():
        code, doc, _ = part('merge', A.Ports(), *files)
        assert code == 1 and doc['passed'] is False and steps(doc)['parts_of_one_run']['passed'] is False, why
    # The differences to s6_common.load_chain (L2 note l2-at046-nonce-vs-load_chain.md), each confirmed: load_chain alone accepts these
    # (it orders the parts itself, allows equal times, and checks neither world nor tenant) - at046 refuses them on top of it.
    order = ['at046-probe-before', 'at046-intake', 'at046-probe-after']
    for why in ('out of order', 'equal timestamps', 'another world', 'another tenant', 'no chain step'):
        assert A.load_chain(spliced[why], prefix='at046-', order=order)[2] == before['run_nonce'], why
    # and these load_chain refuses itself (exactly the parts once, one run_nonce, never a dry run, chain-order times).
    for why in ('the same probe-before three times', 'an older probe-before of another run', 'a part of another nonce', 'no run nonce',
                'a dry run', 'timestamps backwards'):
        with pytest.raises(A.Refused):
            A.load_chain(spliced[why], prefix='at046-', order=order)
    # A required step missing (its part's step removed) fails the merge too.
    gone = forged('after', lambda d: d.update(steps=[st for st in d['steps'] if st['name'] != 'real_side_unchanged']))
    code, doc, _ = part('merge', A.Ports(), before_path, intake_path, gone)
    assert doc['passed'] is False and steps(doc)['required_steps_present']['observed'] == {'missing': ['real_side_unchanged']}
    # Too few results, or a part fed the wrong one, is refused before anything runs.
    assert part('merge', A.Ports(), before_path, intake_path)[1]['passed'] is False
    assert part('probe-after', NoIntake(probe), before_path, before_path)[0] == 2
    assert part('intake', NoProbe(intake=lambda *a: ('t', 'r')), intake_path)[0] == 2
    assert part('intake', NoProbe(intake=lambda *a: ('t', 'r')), forged('before', lambda d: chain_of(d).update(world='sim:' + str(uuid.uuid4()))))[0] == 2
    assert part('intake', NoProbe(), before_path)[0] == 1  # no intake port: the Run blocks


def test_the_periodic_system_bookkeeping_is_excluded_by_name_and_reported(admin, pg, tmp_path):
    """Stage finding 2026-10-22: the first stage run reported control.nexloop_alert_state and runtime.nexloop_orphan_sweeps as changed -
    tables the alert evaluator and the orphan sweeper write on a timer whether AT-046 runs or not. They leave the comparison by name,
    each with its reason, and the result lists them; every other world-real table still counts."""
    world_with_data(admin)
    tables = {t for (t,) in admin.execute("""select table_schema||'.'||table_name from information_schema.columns where column_name='world'
        and table_schema||'.'||table_name = any(%s)""", (list(A.EXCLUDED),)).fetchall()}
    assert tables == set(A.EXCLUDED)  # both have a world column: without the exclusion they would be compared
    assert all(len(reason) > 20 for reason in A.EXCLUDED.values())
    ports = A.Ports(probe=lambda: psycopg.connect(pg, autocommit=True, options='-c default_transaction_read_only=on'))
    digests = A.real_digest(ports, TENANT_A)
    assert not set(A.EXCLUDED) & set(digests) and 'control.nexloop_action_definitions' in digests


def test_prepare_can_publish_the_synthetic_twin_for_the_run_phase(tmp_path):
    """Stage finding 2026-10-22: the twin step blocked - no published consumer twin for inputs.twin. With inputs.publish_twin, prepare
    publishes the contract example's twin (generator twin-template/1, no model) through the owner's governed Action; the definition is
    one the workbench accepts, the Idempotency-Key (the governed request id) is fixed per definition so a rerun replays, and the result
    names twin_id and version for inputs.twin. Without it nothing is published."""
    from nexloop_eios import consumer_twins
    owner = FakeOwner()
    code, doc = run(tmp_path, 'prepare', A.Ports(owner=owner), publish_twin={'twin_id': 'at046-synthetic-x'})
    assert code == 0 and owner.calls[-1] == '/api/v1/workbench/actions/publish_consumer_twin'
    body, headers = owner.twin
    assert set(body) == {'definition', 'calibration_report_ref'} and body['definition']['twin_id'] == 'at046-synthetic-x' and body['definition']['generator_version'] == 'twin-template/1'
    assert body['definition']['schema'] == 'nexloop-consumer-twin/1' and not consumer_twins.definition_problems(body['definition'])
    assert steps(doc)['twin_published']['observed'] == {'status': 200, 'twin_id': 'at046-synthetic-x', 'version': 1, 'twin_status': 'published'}
    first_key = headers['Idempotency-Key']
    again = FakeOwner()
    run(tmp_path, 'prepare', A.Ports(owner=again), publish_twin={'twin_id': 'at046-synthetic-x'})
    assert again.twin[1]['Idempotency-Key'] == first_key  # the same definition: the same request id, a replay
    plain = FakeOwner()
    code, doc = run(tmp_path, 'prepare', A.Ports(owner=plain))
    assert code == 0 and not any(c.endswith('publish_consumer_twin') for c in plain.calls) and 'twin_published' not in steps(doc)


def test_the_synthetic_twin_has_its_prefix_and_cleanup_retires_it(tmp_path):
    """L6 batch-12 review 4.1: the twin prepare publishes is the tenant's - it always carries the prefix at046-synthetic- (a name without
    it is refused), and cleanup retires it after the acceptance (owner's retire_consumer_twin; only a prefixed twin; idempotent)."""
    owner = FakeOwner()
    code, doc = run(tmp_path, 'prepare', A.Ports(owner=owner), publish_twin=True)
    assert code == 0 and owner.twin[0]['definition']['twin_id'] == 'at046-synthetic-trial'
    code, doc = run(tmp_path, 'prepare', A.Ports(owner=FakeOwner()), publish_twin={'twin_id': 'trial.service_consumer'})
    assert code == 2 and 'at046-synthetic-' in doc['refused']
    owner = FakeOwner()
    code, doc = run(tmp_path, 'cleanup', A.Ports(owner=owner), twin={'twin_id': 'at046-synthetic-trial', 'version': 1})
    assert code == 0 and owner.calls == ['/api/v1/workbench/actions/retire_consumer_twin']
    body, headers = owner.retired
    assert body['twin_id'] == 'at046-synthetic-trial' and body['version'] == 1 and steps(doc)['synthetic_twin_retired']['passed'] is True
    again = FakeOwner()
    run(tmp_path, 'cleanup', A.Ports(owner=again), twin={'twin_id': 'at046-synthetic-trial', 'version': 1})
    assert again.retired[1]['Idempotency-Key'] == headers['Idempotency-Key']
    for twin in ({'twin_id': 'trial.service_consumer', 'version': 1}, {'twin_id': 'at046-synthetic-trial'}, None):
        code, doc = run(tmp_path, 'cleanup', A.Ports(owner=FakeOwner()), **({'twin': twin} if twin else {}))
        assert code == 2, twin


def test_a_retired_synthetic_twin_binds_no_more_and_no_staged_experiment_can_use_it(admin):
    """On PostgreSQL: the synthetic twin (prefixed, contract example, no model) published and then retired as cleanup does is not
    selectable, so no screening or shadow experiment can name it (D9); an uncalibrated one already was not. Real experiments never pick a
    twin themselves - a registration names it. The online stage waives the twin check altogether (0309): there a twin decides nothing."""
    from support.tenants import TENANT_A as T
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (T,))
    definition = {k: v for k, v in json.loads(A.TWIN_EXAMPLE.read_text()).items() if k != 'frozen'}
    definition['twin_id'] = A.SYNTHETIC_TWIN + 'trial'

    def governed(fn, body):
        return admin.execute(f"select control.{fn}(%s,'real','owner-principal','owner-subject',%s,%s)", (T, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]
    out = governed('nexloop_governed_publish_consumer_twin', {'operation': 'publish_consumer_twin', 'request_id': uuid.uuid4().hex, 'definition': definition})
    assert out['status'] == 'published' and out['twin_id'].startswith(A.SYNTHETIC_TWIN)
    selectable = lambda: admin.execute('select runtime.nexloop_consumer_twin_selectable(%s,%s,%s)', (T, out['twin_id'], out['version'])).fetchone()[0]  # noqa: E731
    twin_ok = lambda stage: admin.execute('select runtime.nexloop_experiment_twin_ok(%s,%s)', (T, Jsonb({'stage': stage, 'twin': {'twin_id': out['twin_id'], 'version': out['version']}}))).fetchone()[0]  # noqa: E731
    assert selectable() is False and twin_ok('screening') is False and twin_ok('shadow') is False  # uncalibrated: never in a staged experiment
    governed('nexloop_governed_retire_consumer_twin', {'operation': 'retire_consumer_twin', 'request_id': uuid.uuid4().hex, 'twin_id': out['twin_id'],
                                                       'version': out['version'], 'reason': 'AT-046 stage acceptance finished'})
    admin.execute("select set_config('eios.tenant_id',%s,false)", (T,))
    assert admin.execute('select runtime.nexloop_consumer_twin_status(%s,%s,%s)', (T, out['twin_id'], out['version'])).fetchone()[0] == 'retired'
    assert selectable() is False and twin_ok('screening') is False and twin_ok('shadow') is False


def _workbench_validates(operation, body):
    """The workbench entry's own request validation (workbench_actions_http): the key set equals the form's, every field passes."""
    from nexloop_eios.workbench_actions_http import OPERATIONS
    spec = OPERATIONS[operation]
    if type(body) is not dict or set(body) != set(spec):
        raise ValueError('fields')
    return {name: check(body[name]) for name, check in spec.items()}


def test_the_twin_bodies_pass_the_workbench_validation_the_old_one_did_not():
    """Stage finding 2026-10-22: publish_consumer_twin answered 422 - the body named definition only while the form's fields are exactly
    definition and calibration_report_ref. The bodies at046 sends now pass the workbench's own validation; the old one is refused."""
    definition = A.twin_definition({'publish_twin': {'twin_id': 'at046-synthetic-stage'}})
    assert _workbench_validates('publish_consumer_twin', A.twin_publish_body(definition))['definition']['twin_id'] == 'at046-synthetic-stage'
    with pytest.raises(ValueError):
        _workbench_validates('publish_consumer_twin', {'definition': definition})  # the body of 0584f0a4 and before
    owner = FakeOwner()
    run(tmp_path := Path(__import__('tempfile').mkdtemp()), 'cleanup', A.Ports(owner=owner), twin={'twin_id': 'at046-synthetic-stage', 'version': 1})
    assert _workbench_validates('retire_consumer_twin', owner.retired[0])['version'] == 1


def test_publish_twin_is_on_with_the_key_and_publish_twin_alone_creates_no_world(tmp_path):
    """Dispatcher 2026-10-22: publish_twin={} was taken as off (an empty world was created and no twin). The key enables it - true, {} or
    {"twin_id"}; anything else is refused before anything is created. The publish-twin phase publishes the twin only."""
    for value in ({}, True):
        owner = FakeOwner()
        code, doc = run(tmp_path, 'prepare', A.Ports(owner=owner), publish_twin=value)
        assert code == 0 and owner.calls[-1].endswith('publish_consumer_twin') and steps(doc)['twin_published']['passed'] is True, value
    for value in (False, 'yes', {'twin_id': 'trial.service_consumer'}):
        owner = FakeOwner()
        code, doc = run(tmp_path, 'prepare', A.Ports(owner=owner), publish_twin=value)
        assert code == 2 and owner.calls == [], value  # refused before any world is created
    owner = FakeOwner()
    code, doc = run(tmp_path, 'publish-twin', A.Ports(owner=owner), publish_twin={'twin_id': 'at046-synthetic-stage'})
    assert code == 0 and owner.calls == ['/api/v1/workbench/actions/publish_consumer_twin'] and steps(doc)['twin_published']['passed'] is True

"""scripts/acceptance/at046_stage.py: the AT-046 stage run, locally.
- On PostgreSQL (a simulation world with synthetic data): the run phase probes read only, finds no twin port in this release
  (`blocked`); without prepare's catalog publication the Run step fails and without credentials the credential step fails, so the
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
    assert 'eighth batch' in st['twin_observe_act']['observed']['blocked']
    assert st['run_action_published_for_the_world']['passed'] is False  # prepare publishes it (publish_sim_catalog)
    assert st['real_side_unchanged']['passed'] is True and st['real_side_unchanged']['observed']['tables'] > 5
    assert st['world_credentials_only_of_the_world']['passed'] is False  # no credential issued yet: none found is not a pass (5.3)
    assert TENANT_A not in json.dumps(doc)


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
    ports = A.Ports(probe=FakeProbe(world), intake=lambda w, c, e: seen.update(world=w, consumer=c, effect=e) or ('task-1', 'run-1'))
    code, doc = run(tmp_path, 'run', ports, world=world)
    st = steps(doc)
    assert st['simulation_run_completes']['passed'] is True and st['simulation_run_completes']['observed'] == {'task_status': 'succeeded', 'run_world_is_simulation': True}
    assert seen['world'] == world and seen['effect'].startswith('eios:action:') and doc['blocked'] == ['twin_observe_act: the twin port is not in this release (NX-042 T3, eighth batch)']
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

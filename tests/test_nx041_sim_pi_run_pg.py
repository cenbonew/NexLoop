"""NX-041 AT-046 complete acceptance (D10 decided: three credentials per simulation world): a simulation process group runs one actual
Pi Run end to end, never touching a real credential, channel or effect table.

The wiring order of the S7b/sim-context-ref documents, every step under the simulation group's own role and the world's own credential:
1. nexloop_sim_api, with the world's api credential, issues the Run credential (authz.nexloop_issue_run_credential; app_role);
2. it fixes the Run's input by reference (authz.nexloop_sim_context_register: S7b's pack, the manifest);
3. it accepts and registers the Run with that pack text (the Run registration accepts only the fixed pack's digest);
4. nexloop_sim_worker, with the world's worker credential, claims the task and creates the activation;
5. the simulation guard (nexloop_sim_guard, the worker credential, --world-group simulation, 2 guard processes) and the simulation Agent
   Host (world_group simulation, sim context protocol, deterministic-test profile, Node 24) run the Pi Run to its end.
track_functions records the functions the Run called; the report prints them beside the simulation roles' whitelist.
Real PostgreSQL; synthetic data; the real group is not started.
"""
import json
import secrets
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.backend import open_backend
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.effect_contexts import EFFECT
from nexloop_eios.runtime_guard_worker import GuardFiles, GuardWorkerPool
from nexloop_eios.sim_context import sim_run_input
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from runtime_effect_fixture import QUEUE, seed_multi_uuid
from system_catalog_fixture import copy_catalog_to_world, publish_system_catalog
from test_agent_host import files
from test_runtime_host_admission import completed, host
from test_nx041_sim_roles_pg import WHITELIST

ROOT = Path(__file__).resolve().parents[1]
REAL_TABLES = ('runtime.nexloop_effect_intents', 'runtime.nexloop_effect_outbox', 'runtime.nexloop_outbound_messages', 'runtime.nexloop_message_outbox',
               'runtime.nexloop_incentive_issues', 'runtime.nexloop_outreach_sends', 'ontology.nexloop_claims', 'runtime.nexloop_conversation_messages')


def targets(names):
    return [('eios:action:' + name + ':1', ResourceType.ACTION, Operation.EXECUTE) for name in names]


@pytest.fixture
def sim_group(admin, pg, tmp_path):
    bootstrap(admin)
    tenant = str(uuid.uuid4())
    material = secrets.token_bytes(32)
    key = tmp_path / 'signer'
    key.write_bytes(material)
    key.chmod(0o600)
    admin.execute("insert into authz.nexloop_authority_signing_keys values('sim-run',%s,true)", (material,))
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    world = admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject','i-world',%s)",
                          (tenant, Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'simulation',
                                          'purpose': 'AT-046 complete acceptance', 'expires_in_days': 7}))).fetchone()[0]['world']
    # The Run's Action, published for the simulation world (the catalog rows of real, copied as trusted configuration writes them).
    publish_system_catalog(admin, tenant, ('Consumer', 'Goal', 'PlanStep', 'EffectControl'), (EFFECT,))
    copy_catalog_to_world(admin, tenant, world)
    # D10: the world's three credentials, one per use, each bound to it alone.
    api_token = seed_multi_uuid(admin, tenant, targets([QUEUE, EFFECT]), suffix='-sim-api', world=world, sim_use='api')
    worker_token = seed_multi_uuid(admin, tenant, targets([QUEUE]), suffix='-sim-worker', world=world, sim_use='worker')
    agent_token = seed_multi_uuid(admin, tenant, targets([EFFECT]), suffix='-sim-agent', world=world, sim_use='agent')
    admin.execute("select control.nexloop_governed_generate_sim_world_data(%s,'real','owner-principal','owner-subject','i-gen',%s)",
                  (tenant, Jsonb({'operation': 'generate_sim_world_data', 'request_id': 'gen-at046', 'world': world, 'kind': 'synthetic', 'seed': 'at046',
                                  'consumers': 1, 'events_per_consumer': {'min': 2, 'max': 2}, 'segments': {'trial': 1}})))
    consumer = admin.execute("select object_id from runtime.nexloop_sim_objects where world=%s and type_name='Consumer'", (world,)).fetchone()[0]
    return dict(tenant=tenant, world=world, key=key, api_token=api_token, worker_token=worker_token, agent_token=agent_token, consumer=consumer, pg=pg)


def step(fn, *args, **kwargs):
    """A step of the Run path; the Python ports hide the database's own refusal (raise ... from None) - it is still the context."""
    try:
        return fn(*args, **kwargs)
    except Exception as error:
        chain, e = [], error
        while e is not None and len(chain) < 8:
            chain.append(repr(e))
            e = e.__cause__ or e.__context__
        raise AssertionError(f'{getattr(fn, "__name__", fn)}: ' + ' <- '.join(chain)) from error


def database_refusal(error):
    while error is not None:
        if isinstance(error, psycopg.Error):
            return error.diag.message_primary
        error = error.__context__
    return None


def real_counts(admin):
    return {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in REAL_TABLES}


def sim_host_configuration(tmp_path, port, guard_key):
    purge = tmp_path / 'world-purge-key'
    purge.write_text(secrets.token_hex(32))
    purge.chmod(0o600)
    path = tmp_path / 'runtime-config.json'
    path.write_text(json.dumps({'guard_url': f'https://127.0.0.1:{port}/internal/v1/runtime/authorize', 'guard_key_file': str(guard_key),
                                'guard_ca_file': str(tmp_path / 'host-cert.pem'), 'runtime_profile': 'deterministic-test', 'world_group': 'simulation',
                                'host_id': 'sim-host-at046', 'world_purge_key_file': str(purge), 'context_input_protocol': 'nexloop.context-pack.sim.v1',
                                'cost_currency': 'CNY'}))
    path.chmod(0o600)
    return path


def run_sim_pi_run(f, tmp_path):
    """Steps 1-5 of the module docstring: one Pi Run of the simulation group for f's Consumer; returns what the checks need."""
    config = dict(signing_key_file=f['key'], signing_key_id='sim-run')
    with open_backend(database_url=make_conninfo(f['pg'], user='nexloop_sim_api'), artifact_root=tmp_path / 'api-artifacts', **config) as api, \
            open_backend(database_url=make_conninfo(f['pg'], user='nexloop_sim_worker'), artifact_root=tmp_path / 'worker-artifacts', **config) as backend_worker:
        source = api.authenticate(f['api_token'], world=f['world'])
        # 1. The Run credential, issued by the simulation intake in its world.
        run = step(source.issue_run_credential, action_resources=['eios:action:' + EFFECT + ':1'])
        # 2. The Run's input fixed by reference.
        pack_text = sim_run_input(source, run.run_id, f['consumer'], 8)
        assert json.loads(pack_text)['world'] == f['world']
        command = {'schema_version': '1.0', 'run_id': run.run_id, 'tenant_id': f['tenant'], 'world_id': f['world'], 'mode': 'simulation',
                   'request_id': 'sim-run-' + str(uuid.uuid4()), 'trigger_event_id': str(uuid.uuid4()), 'role_ref': 'role:sim-consumer-reply',
                   'consumer_ref': 'consumer:' + f['consumer'], 'goal_version_ref': 'goal:sim-at046', 'context_manifest_ref': 'artifact:sim-context',
                   'runtime_profile': 'deterministic-test', 'credential_ref': 'run_credential:' + run.run_id,
                   'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0', 'currency': 'CNY'},
                   'not_after': run.expires_at.isoformat(), 'runtime_owner_epoch': 1}
        # A pack the intake assembled itself is refused at the registration (positive control right after).
        forged = json.loads(pack_text)
        forged['insufficient'] = forged['insufficient'] + [{'code': 'forged_by_the_intake', 'section': 'evidence'}]  # differs whatever the pack holds
        with pytest.raises(Exception) as refused:
            source.accept_runtime_event(queue='operations', source_id='sim-intake', event_id='forged', run_token=run.token, command=command,
                                        input=json.dumps(forged, ensure_ascii=False))
        assert database_refusal(refused.value) == 'sim context: input_not_registered', repr(refused.value.__context__)
        # 3. Accepted and registered with the fixed pack.
        accepted = step(source.accept_runtime_event, queue='operations', source_id='sim-intake', event_id='event-1', run_token=run.token, command=command, input=pack_text)
        # 4. The simulation worker claims and activates.
        worker = backend_worker.authenticate(f['worker_token'], world=f['world'])
        job = step(worker.claim_task, queue='operations', lease_seconds=60)
        assert job['task_id'] == accepted['task_id']
        activation = step(worker.create_runtime_activation, queue='operations', task_id=job['task_id'], fence=job['fence'], run_id=run.run_id, command=command,
                                                      input=pack_text, owner_epoch=1)
    # 5. The simulation guard (its own role, the worker credential, two processes) and the simulation Host run the Pi Run.
    runtime, key = files(tmp_path)
    guard_key = tmp_path / 'guard-key'
    guard_key.write_text(secrets.token_hex(32))
    guard_key.chmod(0o600)
    private = tmp_path / 'guard-private'
    private.mkdir(mode=0o700)
    dsn = private / 'database-url'
    dsn.write_text(make_conninfo(f['pg'], user='nexloop_sim_guard'))
    dsn.chmod(0o600)
    credential = private / 'service-credential'
    credential.write_text(f['worker_token'])
    credential.chmod(0o600)
    pool = GuardWorkerPool(GuardFiles(dsn, f['key'], 'sim-run', credential, tmp_path / 'guard-artifacts', f['world'], guard_key, tmp_path / 'host-cert.pem',
                                      tmp_path / 'host-key.pem', world_group='simulation'), port=0, workers=2)
    port = pool.start()
    try:
        with host(runtime, key, sim_host_configuration(tmp_path, port, guard_key)) as (child, client, headers):
            started = client.post('/internal/v1/runs/start', headers=headers,
                                  json={'activation_ref': activation['activation_ref'], 'command': command, 'input': pack_text})
            assert started.status_code == 202, started.text
            done = completed(client, headers, activation['activation_ref'], command)
    finally:
        pool.stop()
    return dict(done=done, run=run, pack_text=pack_text, runtime=runtime, guard_key=guard_key)


def test_at046_a_simulation_process_group_runs_a_complete_pi_run(sim_group, admin, tmp_path):
    f = sim_group
    node = shutil.which('node')
    assert node and subprocess.run([node, '--version'], capture_output=True, text=True, check=True).stdout.startswith('v24.'), 'Node 24 required'
    assert (ROOT / 'apps/agent-host/dist/main.js').is_file(), 'build the Agent Host first'
    before = real_counts(admin)
    admin.execute("select pg_stat_reset()")
    admin.execute("do $$begin execute format('alter database %I set track_functions=''all''',current_database());end$$")
    r = run_sim_pi_run(f, tmp_path)
    done, run, runtime, guard_key = r['done'], r['run'], r['runtime'], r['guard_key']
    assert done['runtime_outcome'] == 'succeeded', done
    # The Run's state lives under its simulation world's directory; no credential or key reached it.
    world_dir = runtime / f['world'].replace(':', '-') / run.run_id
    assert (world_dir / 'runtime.sqlite').is_file()
    stored = b''.join(p.read_bytes() for p in world_dir.rglob('*') if p.is_file())
    for secret in (run.token, f['api_token'], f['worker_token'], f['agent_token'], guard_key.read_text()):
        assert secret.encode() not in stored
    # No real credential, channel or effect: every service credential the Run path used is of this world; real tables untouched.
    used = admin.execute("select array_agg(distinct w) from authz.nexloop_service_credentials, unnest(worlds) w where tenant_id=%s", (f['tenant'],)).fetchone()[0]
    assert used == [f['world']]
    assert admin.execute('select world from authz.nexloop_run_credentials where run_id=%s', (run.run_id,)).fetchone()[0] == f['world']
    assert real_counts(admin) == before
    # track_functions: what the Run path called, beside the simulation roles' whitelist (report).
    admin.execute("select pg_stat_force_next_flush()") if admin.execute("select to_regproc('pg_stat_force_next_flush')").fetchone()[0] else None
    called = [r[0] for r in admin.execute("""select s.schemaname||'.'||s.funcname from pg_stat_user_functions s
        where s.calls>0 and s.schemaname in ('authz','control','runtime','ontology') order by 1""").fetchall()]
    entries = sorted({n.split('(')[0] for role in ('nexloop_sim_api', 'nexloop_sim_worker', 'nexloop_sim_guard') for n in WHITELIST[role]})
    report = {'called': called, 'whitelisted_entries_called': [e for e in entries if e in called],
              'whitelisted_entries_not_called': [e for e in entries if e not in called]}
    (ROOT / 'build').mkdir(exist_ok=True)
    (ROOT / 'build/nx041-d10-track-functions.json').write_text(json.dumps(report, indent=1) + '\n')
    print('TRACK', json.dumps(report))
    admin.execute("do $$begin execute format('alter database %I reset track_functions',current_database());end$$")


def test_at046_the_simulation_worker_dispatches_through_its_actual_path(sim_group, admin, tmp_path):
    """Stage finding 2026-10-22 (AT-046 first stage run): the Run failed at the worker's control stage, runtime_control_unavailable /
    InsufficientPrivilege - nexloop_sim_worker could not execute the owner-control re-check (assert_task_dispatch_controls, budget
    status, budget reservation, goal binding). The test above calls claim and activation directly and so never ran that stage. Here the
    worker's own RuntimeDispatcher.run_once (claim -> owner controls -> activation -> Host -> inspect) runs as nexloop_sim_worker, the
    guard as nexloop_sim_guard, with the world's credentials only: the task succeeds."""
    from nexloop_eios.runtime_dispatch import HostControlConfiguration, RuntimeDispatcher
    f = sim_group
    node = shutil.which('node')
    assert node and subprocess.run([node, '--version'], capture_output=True, text=True, check=True).stdout.startswith('v24.'), 'Node 24 required'
    log_start = len(pg_log(f['pg']).read_text(errors='replace'))
    # A simulation world has its own model budget from its creation (S6, D6), so every simulation Run's dispatch reserves its maximum
    # cost: reserve_budget runs here as nexloop_sim_worker (asserted below).
    assert admin.execute("select count(*) from control.nexloop_budget_limits where world=%s and budget_kind='model'", (f['world'],)).fetchone()[0] == 1
    # ADR-037 (owner 2026-10-22): the deployed simulation budget - dispatcher 2, two guard children with pools of 2 (lowered from 4).
    sim = next(e for e in json.loads((ROOT / 'deploy/stage/connection-budget.v1.json').read_text())['simulation']['services']
               if e['name'] == 'sim-runtime-worker')
    assert (sim['dispatcher_pool_max'], sim['guard_workers'], sim['guard_pool_max']) == (2, 2, 2)
    config = dict(signing_key_file=f['key'], signing_key_id='sim-run')
    with open_backend(database_url=make_conninfo(f['pg'], user='nexloop_sim_api'), artifact_root=tmp_path / 'api-artifacts', **config) as api, \
            open_backend(database_url=make_conninfo(f['pg'], user='nexloop_sim_worker'), artifact_root=tmp_path / 'worker-artifacts', pool_max_size=sim['dispatcher_pool_max'],
                         **config) as backend_worker:
        assert backend_worker._pool.connection().__enter__().execute('select current_user').fetchone()[0] == 'nexloop_sim_worker'
        source = api.authenticate(f['api_token'], world=f['world'])
        run = step(source.issue_run_credential, action_resources=['eios:action:' + EFFECT + ':1'])
        pack_text = sim_run_input(source, run.run_id, f['consumer'], 8)
        command = {'schema_version': '1.0', 'run_id': run.run_id, 'tenant_id': f['tenant'], 'world_id': f['world'], 'mode': 'simulation',
                   'request_id': 'sim-run-' + str(uuid.uuid4()), 'trigger_event_id': str(uuid.uuid4()), 'role_ref': 'role:sim-consumer-reply',
                   'consumer_ref': 'consumer:' + f['consumer'], 'goal_version_ref': 'goal:sim-at046', 'context_manifest_ref': 'artifact:sim-context',
                   'runtime_profile': 'deterministic-test', 'credential_ref': 'run_credential:' + run.run_id,
                   'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0', 'currency': 'CNY'},
                   'not_after': run.expires_at.isoformat(), 'runtime_owner_epoch': 1}
        accepted = step(source.accept_runtime_event, queue='operations', source_id='sim-intake', event_id='event-dispatch', run_token=run.token,
                        command=command, input=pack_text)
        worker = backend_worker.authenticate(f['worker_token'], world=f['world'])
        runtime, key = files(tmp_path)
        guard_key = tmp_path / 'guard-key'
        guard_key.write_text(secrets.token_hex(32))
        guard_key.chmod(0o600)
        private = tmp_path / 'guard-private'
        private.mkdir(mode=0o700)
        dsn = private / 'database-url'
        dsn.write_text(make_conninfo(f['pg'], user='nexloop_sim_guard'))
        dsn.chmod(0o600)
        credential = private / 'service-credential'
        credential.write_text(f['worker_token'])
        credential.chmod(0o600)
        pool = GuardWorkerPool(GuardFiles(dsn, f['key'], 'sim-run', credential, tmp_path / 'guard-artifacts', f['world'], guard_key, tmp_path / 'host-cert.pem',
                                          tmp_path / 'host-key.pem', world_group='simulation'), port=0, workers=sim['guard_workers'],
                               pool_max=sim['guard_pool_max'])
        port = pool.start()
        try:
            with host(runtime, key, sim_host_configuration(tmp_path, port, guard_key)) as (child, client, headers):
                host_config = HostControlConfiguration(str(client.base_url).rstrip('/'), key, tmp_path / 'host-cert.pem')
                result = RuntimeDispatcher(worker, host_config, queue='operations', total_timeout=60).run_once()
        finally:
            pool.stop()
    assert result['task_id'] == accepted['task_id'] and result['status'] == 'succeeded', result
    assert admin.execute('select status from runtime.jobs where job_id=%s', (accepted['task_id'],)).fetchone()[0] == 'succeeded'
    assert admin.execute('select world from authz.nexloop_run_credentials where run_id=%s', (run.run_id,)).fetchone()[0] == f['world']
    reserved = admin.execute("select count(*) from control.nexloop_budget_consumption where tenant_id=%s and world=%s and consumption_id=%s",
                             (f['tenant'], f['world'], 'run:' + run.run_id)).fetchone()[0]
    assert reserved == 1
    # Stage 2026-10-22: the simulation worker asked the message reply settlement after its Run and was refused - a PostgreSQL ERROR per
    # Run, swallowed by the best-effort call (a refused call is not counted as a function call; the server log is where it shows). A
    # simulation Run answers no conversation message: the worker no longer asks, and nothing of its path is refused.
    logged = pg_log(f['pg']).read_text(errors='replace')[log_start:]
    assert 'permission denied for function' not in logged, [l for l in logged.splitlines() if 'permission denied' in l]


def test_a_dispatch_refused_by_the_database_is_logged_by_class_and_code_only(sim_group, admin, tmp_path, capfd):
    """Stage finding 2026-10-22: the simulation worker failed the Run and its log said only 'Runtime Worker ready'. Now one stderr line
    names the stage, the task code and the exception classes - no message, payload, Run id or token. Here the worker lacks one control
    entry (revoked for the test, restored after): the dispatch fails at its control stage, before any Host request."""
    from nexloop_eios.runtime_dispatch import HostControlConfiguration, RuntimeDispatcher
    f = sim_group
    config = dict(signing_key_file=f['key'], signing_key_id='sim-run')
    admin.execute('revoke execute on function authz.nexloop_assert_task_dispatch_controls(text,text,text) from nexloop_sim_worker')
    try:
        with open_backend(database_url=make_conninfo(f['pg'], user='nexloop_sim_api'), artifact_root=tmp_path / 'api-artifacts', **config) as api, \
                open_backend(database_url=make_conninfo(f['pg'], user='nexloop_sim_worker'), artifact_root=tmp_path / 'worker-artifacts', **config) as backend_worker:
            source = api.authenticate(f['api_token'], world=f['world'])
            run = step(source.issue_run_credential, action_resources=['eios:action:' + EFFECT + ':1'])
            pack_text = sim_run_input(source, run.run_id, f['consumer'], 8)
            command = {'schema_version': '1.0', 'run_id': run.run_id, 'tenant_id': f['tenant'], 'world_id': f['world'], 'mode': 'simulation',
                       'request_id': 'sim-run-' + str(uuid.uuid4()), 'trigger_event_id': str(uuid.uuid4()), 'role_ref': 'role:sim-consumer-reply',
                       'consumer_ref': 'consumer:' + f['consumer'], 'goal_version_ref': 'goal:sim-at046', 'context_manifest_ref': 'artifact:sim-context',
                       'runtime_profile': 'deterministic-test', 'credential_ref': 'run_credential:' + run.run_id,
                       'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0', 'currency': 'CNY'},
                       'not_after': run.expires_at.isoformat(), 'runtime_owner_epoch': 1}
            step(source.accept_runtime_event, queue='operations', source_id='sim-intake', event_id='event-log', run_token=run.token, command=command, input=pack_text)
            worker = backend_worker.authenticate(f['worker_token'], world=f['world'])
            runtime, key = files(tmp_path)
            capfd.readouterr()
            result = RuntimeDispatcher(worker, HostControlConfiguration('https://127.0.0.1:65530', key, tmp_path / 'host-cert.pem'), queue='operations',
                                       total_timeout=10).run_once()
    finally:
        admin.execute('grant execute on function authz.nexloop_assert_task_dispatch_controls(text,text,text) to nexloop_sim_worker')
    assert result['status'] == 'failed' and result['result']['code'] == 'runtime_control_unavailable'
    err = capfd.readouterr().err
    lines = [json.loads(l) for l in err.splitlines() if l.startswith('{"event":"runtime_dispatch_error"')]
    assert lines == [{'event': 'runtime_dispatch_error', 'stage': 'control', 'code': 'runtime_control_unavailable',
                      'error_class': lines[0]['error_class'], 'error_cause_class': 'InsufficientPrivilege'}], err
    assert run.run_id not in err and run.token not in err and f['tenant'] not in err


def pg_log(conninfo):
    """The test cluster's server log (tests/conftest.py: <root>/postgres.log beside <root>/socket)."""
    from psycopg.conninfo import conninfo_to_dict
    return Path(conninfo_to_dict(conninfo)['host']).parent / 'postgres.log'


def test_reply_settle_is_not_asked_for_a_simulation_or_shadow_world():
    """The reply settlement concerns conversation messages only (0200: message routes, fallback bindings); a simulation or shadow world's
    Run answers none, so the worker does not ask - no connection is taken. A real or test world is asked as before (best effort)."""
    from types import SimpleNamespace
    from nexloop_eios.runtime_dispatch import reply_settle

    class NoPool:
        def connection(self):
            raise AssertionError('no database call for a simulation world')
    for world in ('sim:' + str(uuid.uuid4()), 'shadow:' + str(uuid.uuid4())):
        worker = SimpleNamespace(_session=SimpleNamespace(world=world, token_digest='a' * 64), _backend=SimpleNamespace(_pool=NoPool()))
        assert reply_settle(worker, str(uuid.uuid4())) == {'reply_run': False}
    asked = []

    class Pool:
        def connection(self):
            asked.append(True)
            raise RuntimeError('unavailable')
    real = SimpleNamespace(_session=SimpleNamespace(world='real', token_digest='a' * 64), _backend=SimpleNamespace(_pool=Pool()))
    assert reply_settle(real, str(uuid.uuid4())) == {} and asked == [True]  # real is still asked (best effort: {} when unavailable)

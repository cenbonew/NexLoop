"""NX-041 S6 (design §6, D3, D6, §11 S6): the simulation process group.

- The runtime worker and its guard serve one world group: real and test as before, one sim:/shadow: world only with
  --world-group simulation (refused before any file is read).
- The simulation compose project is separate, off by default and holds no real credential, channel secret or provider configuration.
- Simulation model budget: each world has its own lifetime budget; the tenant's simulation account is capped at 100 CNY a month
  across worlds, kept even when a world is deleted; real budgets are untouched.
- Whole deletion (S2) also removes the world's budget rows (inside the erasure scope its purge opens) and queues the
  simulation Host's world directory for the file purger (nexloop_scheduler, no world credential).
- A stand-in intent (S4) carries the world's time (runtime.nexloop_world_now of S5 when present).
"""
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios import runtime_guard_worker, runtime_worker
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.sim_world_files import SimWorldFilePurger
from support.tenants import TENANT_A, TENANT_B

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / 'deploy/stage/compose.simulation.example.yaml'
U = lambda: str(uuid.uuid4())  # noqa: E731


def create_world(admin, kind='simulation', tenant=TENANT_A, days=30):
    return admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + uuid.uuid4().hex[:8], Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': kind,
                                                                         'purpose': 'S6 test', 'expires_in_days': days}))).fetchone()[0]['world']


def delete_world(admin, world, tenant=TENANT_A):
    return admin.execute("select control.nexloop_governed_delete_sim_world(%s,'real','owner-principal','owner-subject','i-del',%s)",
                         (tenant, Jsonb({'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': world, 'reason': '结束'}))).fetchone()[0]


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin


def consume(admin, world, amount, cid=None, unit='CNY', tenant=TENANT_A):
    admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
        values(%s,%s,'model',%s,%s,%s,'run:x','p')""", (tenant, world, cid or 'run:' + U(), amount, unit))


# 1. Processes ----------------------------------------------------------------------------------------------------------------
WORKER = ['--database-url-file', '/nonexistent/dsn', '--signing-key-file', '/nonexistent/k', '--service-credential-file', '/nonexistent/c',
          '--artifact-root', '/nonexistent/a', '--host-control-key-file', '/nonexistent/h', '--host-ca-file', '/nonexistent/ca',
          '--guard-key-file', '/nonexistent/g', '--guard-certificate-file', '/nonexistent/gc', '--guard-tls-key-file', '/nonexistent/gt',
          '--queue', 'operations', '--host-origin', 'https://127.0.0.1:20000', '--guard-port', '20001']
GUARD = ['--listen-fd', '3', '--guard-port', '20001', '--database-url-file', '/nonexistent/dsn', '--signing-key-file', '/nonexistent/k',
         '--service-credential-file', '/nonexistent/c', '--artifact-root', '/nonexistent/a', '--guard-key-file', '/nonexistent/g',
         '--guard-certificate-file', '/nonexistent/gc', '--guard-tls-key-file', '/nonexistent/gt']


@pytest.mark.parametrize('parse,base', [(runtime_worker._arguments, WORKER), (runtime_guard_worker._arguments, GUARD)])
def test_a_process_serves_one_world_group(parse, base):
    sim, shadow = 'sim:' + U(), 'shadow:' + U()
    for world, group in (('real', None), ('test', None), ('real', 'real'), (sim, 'simulation'), (shadow, 'simulation')):
        args = parse(base + ['--world', world] + (['--world-group', group] if group else []))
        assert (args.world, args.world_group) == (world, group or 'real')
    for world, group in ((sim, None), (shadow, 'real'), ('real', 'simulation'), ('test', 'simulation'), ('simulation', 'simulation'),
                         ('sim:1', 'simulation'), (sim, 'shadow')):
        with pytest.raises(SystemExit) as refused:
            parse(base + ['--world', world] + (['--world-group', group] if group else []))
        assert refused.value.code == 2, (world, group)


def test_the_simulation_compose_project_is_separate_off_by_default_and_holds_no_real_secret():
    project = yaml.safe_load(COMPOSE.read_text())
    assert project['name'] == 'nexloop-simulation'
    services = project['services']
    assert set(services) == {'sim-agent-host', 'sim-runtime-worker', 'sim-stand-in-executor', 'sim-world-files'}
    for name, service in services.items():
        assert service.get('profiles') == ['simulation'], name  # nothing starts without --profile simulation
        assert service.get('network_mode') in (None, 'service:sim-agent-host'), name  # never the host's (real) namespace
        assert all(s.startswith('sim_') for s in service.get('secrets', [])), name
    assert services['sim-agent-host']['networks'] == ['simulation'] and project['networks']['simulation']['name'] == 'nexloop-simulation'
    worker = services['sim-runtime-worker']['command']
    assert worker[worker.index('--world-group') + 1] == 'simulation' and 'NEXLOOP_SIM_WORLD' in worker[worker.index('--world') + 1]
    executor = services['sim-stand-in-executor']['command']
    assert not any(a for a in executor if 'provider' in str(a) or 'routes' in str(a) or 'channel' in str(a))
    text = COMPOSE.read_text()
    real_names = ('runtime_worker_credential', 'host_model_key', 'channel', 'provider_config', 'provider_routes', 'webchat', 'signing_key:')
    secrets = project['secrets']
    assert not any(n in name for name in secrets for n in real_names) and all(name.startswith('sim_') for name in secrets)
    assert all(v['file'].startswith('${NEXLOOP_SIM_SECRET_DIR') for v in secrets.values())
    assert 'network_mode: host' not in text
    # The real projects are unchanged: none of them runs a simulation world or the simulation group.
    for other in ('compose.app.example.yaml', 'compose.runtime-worker.example.yaml', 'compose.data.example.yaml'):
        body = (ROOT / 'deploy/stage' / other).read_text()
        assert 'world-group' not in body and 'NEXLOOP_SIM_' not in body and 'stand_in' not in body, other


# 2. Simulation model budget (D6) --------------------------------------------------------------------------------------------
def test_simulation_model_spend_is_capped_monthly_across_worlds_and_never_touches_real(db):
    a, b = create_world(db), create_world(db, kind='shadow')
    limits = db.execute("""select world,unit,limit_amount,period_start=w.created_at and period_end=w.expires_at from control.nexloop_budget_limits l
        join control.nexloop_sim_worlds w using(world) where l.budget_kind='model' order by world""").fetchall()
    assert {(w, u, int(n), ok) for w, u, n, ok in limits} == {(a, 'CNY', 100, True), (b, 'CNY', 100, True)}
    db.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','model','CNY',100000,now()-interval '1 day',now()+interval '30 days',1)""", (TENANT_A,))
    consume(db, a, 60)
    consume(db, b, 30)
    consume(db, 'real', 500)  # real spend never counts against the simulation account
    with pytest.raises(psycopg.Error, match='simulation model budget exhausted') as refused:
        consume(db, a, 20)
    assert refused.value.sqlstate == 'NXB01'
    with pytest.raises(psycopg.Error, match='simulation model budget exhausted'):
        consume(db, b, 10.01)
    consume(db, b, 10)  # exactly 100
    consume(db, a, -5, cid='run:x:settle')  # a settlement gives back
    consume(db, a, 5)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='in CNY'):
        consume(db, a, 1, unit='USD')
    # Another tenant has its own account.
    other = create_world(db, tenant=TENANT_B)
    consume(db, other, 99, tenant=TENANT_B)
    # A deleted world's spend still counts this month; real and its budget rows are untouched by the deletion.
    real_rows = db.execute("select count(*) from control.nexloop_budget_consumption where world='real'").fetchone()[0]
    assert delete_world(db, a)['status'] == 'deleted'
    assert db.execute('select count(*) from control.nexloop_budget_consumption where world=%s', (a,)).fetchone()[0] == 0
    assert db.execute('select count(*) from control.nexloop_budget_limits where world=%s', (a,)).fetchone()[0] == 0
    assert db.execute("select count(*) from control.nexloop_budget_consumption where world='real'").fetchone()[0] == real_rows
    c = create_world(db)
    with pytest.raises(psycopg.Error, match='simulation model budget exhausted'):
        consume(db, c, 1)
    spend = db.execute("select sum(amount) from control.nexloop_sim_model_spend where tenant_id=%s", (TENANT_A,)).fetchone()[0]
    assert spend == 100
    # Costs of a simulation world are in data mode simulation (S1), never real.
    assert db.execute('select runtime.nexloop_cost_data_mode(%s)', (b,)).fetchone()[0] == 'simulation'
    # The nx022 guard still refuses outside a world's purge.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), db.transaction():
        db.execute('delete from control.nexloop_budget_consumption where world=%s', (b,))


# 3. The simulation Host's world directory -----------------------------------------------------------------------------------
def test_a_deleted_world_directory_is_queued_purged_on_the_simulation_host_and_confirmed(db, pg):
    a, b = create_world(db), create_world(db)
    scheduler = lambda: psycopg.connect(make_conninfo(pg, user='nexloop_scheduler'), autocommit=True)  # noqa: E731
    with scheduler() as c:
        assert c.execute('select authz.nexloop_sim_world_files_pending(10)').fetchall() == []
    delete_world(db, a)
    calls = []

    def host(world):
        calls.append(world)
        return 'failed' if len(calls) == 1 else 'purged'
    purger = SimWorldFilePurger(scheduler, host)
    assert purger.tick() == {a: 'failed'}  # nothing confirmed that the Host did not report
    assert purger.tick() == {a: 'purged'} and purger.tick() == {}
    assert db.execute('select outcome,confirmed_at is not null from runtime.nexloop_sim_world_file_purges where sim_world=%s', (a,)).fetchone() == ('purged', True)
    assert db.execute('select count(*) from runtime.nexloop_sim_world_file_purges where sim_world=%s', (b,)).fetchone()[0] == 0
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker'):
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_sim_world_files_pending(10)')
    with scheduler() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("select authz.nexloop_sim_world_files_confirm(%s,'deleted')", (a,))


# 4. The stand-in intent's time is the world's time -----------------------------------------------------------------------------
def test_a_stand_in_intent_carries_the_world_time(db, pg):
    a = create_world(db)
    token, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + 'a' * 32, world=a, identity_suffix='-s6-time')
    digest = hashlib.sha256(token.encode()).hexdigest()

    def submit():
        body = {'intent_id': U(), 'action_name': 'nexloop.outreach.send', 'action_version': 1, 'provider_kind': 'outreach_outbox',
                'business_digest': 'b' * 64, 'request_digest': 'c' * 64, 'origin': 'twin', 'sim_clock': '2026-10-21T08:00:00+00:00'}
        with psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True) as c:
            c.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest, a, Jsonb(body)))
        return db.execute('select submitted_at,submitted_at>now()-interval \'1 minute\' from runtime.nexloop_sim_effect_intents where intent_id=%s',
                          (body['intent_id'],)).fetchone()
    assert submit()[1] is True  # before S5: the wall clock
    exists = db.execute("select to_regprocedure('runtime.nexloop_world_now(text,text)') is not null").fetchone()[0]
    if not exists:  # the S5 interface: a stand-in so the call is observable (integration brings the real one)
        db.execute("""create function runtime.nexloop_world_now(p_tenant text,p_world text) returns timestamptz language sql
            set search_path=pg_catalog,pg_temp as $$ select timestamptz '2031-01-02 03:04:05+00' $$""")
        db.execute('alter function runtime.nexloop_world_now(text,text) owner to nexloop_owner')
        assert submit()[0] == datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)

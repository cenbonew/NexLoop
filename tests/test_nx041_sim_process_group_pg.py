"""NX-041 S6 (design §6, D3, D6, §11 S6): the simulation process group.

- The runtime worker and its guard serve one world group: real and test as before, one sim:/shadow: world only with
  --world-group simulation (refused before any file is read).
- The simulation compose project is separate, off by default and holds no real credential, channel secret or provider configuration.
- Simulation model budget: each world has its own lifetime budget; the tenant's simulation account is capped at 100 CNY a month
  across worlds, kept even when a world is deleted; real budgets are untouched.
- Whole deletion (S2) also removes the world's budget rows (inside the erasure scope its purge opens) and queues the
  simulation Hosts' world directory: purged only with the database's permit, confirmed only with each Host's signed proof, by the
  purger's own role nexloop_sim_files (review 1.2, 2.3, 3.1-3.4).
- A stand-in intent (S4) keeps the wall clock as its audit time; its simulated clock is the world's time (review 5.1, 5.2).
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
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
    # S6c: the simulation runtime worker also names its guard's own DSN and serves the guard from child processes.
    extra = lambda group: ['--guard-database-url-file', '/nonexistent/guard', '--guard-workers', '2'] if parse is runtime_worker._arguments and group == 'simulation' else []  # noqa: E731
    for world, group in (('real', None), ('test', None), ('real', 'real'), (sim, 'simulation'), (shadow, 'simulation')):
        args = parse(base + ['--world', world] + (['--world-group', group] if group else []) + extra(group))
        assert (args.world, args.world_group) == (world, group or 'real')
    for world, group in ((sim, None), (shadow, 'real'), ('real', 'simulation'), ('test', 'simulation'), ('simulation', 'simulation'),
                         ('sim:1', 'simulation'), (sim, 'shadow')):
        with pytest.raises(SystemExit) as refused:
            parse(base + ['--world', world] + (['--world-group', group] if group else []) + extra(group))
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
    # Review 1.2: each simulation database role has its own DSN file; review 1.3: a fixed subnet the firewall template matches.
    assert services['sim-stand-in-executor']['secrets'] == ['sim_stand_in_worker_dsn', 'sim_world_credential']
    assert services['sim-world-files']['secrets'][0] == 'sim_files_dsn' and '--host-id' in services['sim-world-files']['command']
    assert 'sim_host_purge_key' in services['sim-agent-host']['secrets']
    assert project['networks']['simulation']['ipam']['config'][0]['subnet'] == '${NEXLOOP_SIM_SUBNET:?simulation subnet required}'
    nft = (ROOT / 'docs/implementation/NX-041-S6-simulation.nft.example').read_text()
    assert 'ip saddr __SIM_SUBNET__ ct state new drop' in nft and '__DATA_HOST_ADDR__ tcp dport __PG_PORT__ accept' in nft
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


# 3. The simulation Host's world directory (review 2.3, 3.2-3.4) ------------------------------------------------------------
def host_key(admin, host_id):
    key = secrets.token_bytes(32)
    admin.execute('insert into authz.nexloop_sim_hosts(host_id,key_material) values(%s,%s)', (host_id, key))
    return key


class FakeHost:
    """A simulation Host as the Host answers: it checks the database's permit and signs a deletion proof with its key."""

    def __init__(self, host_id, key, held=(), check_permit=True):
        self.host_id, self.key, self.held, self.calls, self.check = host_id, key, set(held), [], check_permit

    def _mac(self, text):
        return hmac.new(self.key, text.encode(), hashlib.sha256).hexdigest()

    def __call__(self, world, permit):
        self.calls.append(world)
        assert permit['host_id'] == self.host_id and permit['world'] == world
        assert not self.check or hmac.compare_digest(permit['mac'], self._mac(f"nexloop-sim-world-purge-v1:{self.host_id}:{world}:{permit['expires_at']}"))
        outcome, entries = ('purged', 3) if world in self.held else ('absent', 0)
        digest = hashlib.sha256(b'listing' if entries else b'').hexdigest()
        issued = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        seen = world in self.held
        mac = self._mac(f'nexloop-sim-world-purge-proof-v1:{self.host_id}:{world}:{outcome}:{str(seen).lower()}:{entries}:{digest}:{issued}')
        return {'world': world, 'host_id': self.host_id, 'outcome': outcome, 'seen': seen, 'entries': entries, 'listing_digest': digest,
                'after': 'absent', 'issued_at': issued, 'mac': mac}


def test_a_deleted_world_directory_is_purged_only_with_a_permit_and_confirmed_only_with_a_host_proof(db, pg):
    one, two = host_key(db, 'sim-host-one'), host_key(db, 'sim-host-two')
    a, b = create_world(db), create_world(db)
    files = lambda: psycopg.connect(make_conninfo(pg, user='nexloop_sim_files'), autocommit=True)  # noqa: E731
    with files() as c:
        assert c.execute("select * from authz.nexloop_sim_world_files_pending('sim-host-one',10)").fetchall() == []
    delete_world(db, a)
    # 3.4: the database part is done (deleted) but the summary waits for the files; the world is listed as overdue.
    assert db.execute('select status from control.nexloop_sim_worlds where world=%s', (a,)).fetchone()[0] == 'deleted'
    assert db.execute('select completed_at from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0] is None
    assert [r[0] for r in db.execute('select world from runtime.nexloop_sim_world_files_overdue(%s,0)', (TENANT_A,)).fetchall()] == [a]
    # Host two never held the world: its absent completes its own part only (3.3); host one held it and purges it.
    host_one, host_two = FakeHost('sim-host-one', one, held={a}), FakeHost('sim-host-two', two)
    assert SimWorldFilePurger(files, host_two, 'sim-host-two').tick() == {a: 'absent'}
    assert db.execute('select confirmed_at from runtime.nexloop_sim_world_file_purges where sim_world=%s', (a,)).fetchone()[0] is None
    assert SimWorldFilePurger(files, host_two, 'sim-host-two').tick() == {}  # nothing more for host two
    failing = SimWorldFilePurger(files, lambda world, permit: None, 'sim-host-one')
    assert failing.tick() == {a: 'failed'}  # a Host that answers nothing confirms nothing
    assert SimWorldFilePurger(files, host_one, 'sim-host-one').tick() == {a: 'purged'}
    assert db.execute('select outcome,confirmed_at is not null from runtime.nexloop_sim_world_file_purges where sim_world=%s', (a,)).fetchone() == ('purged', True)
    assert db.execute('select completed_at is not null from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0] is True
    assert db.execute('select count(*) from runtime.nexloop_sim_world_files_overdue(%s,0)', (TENANT_A,)).fetchone()[0] == 0
    rows = db.execute('select host_id,outcome,seen,entries from runtime.nexloop_sim_world_file_confirmations where sim_world=%s order by 1', (a,)).fetchall()
    assert rows == [('sim-host-one', 'purged', True, 3), ('sim-host-two', 'absent', False, 0)]
    assert host_one.calls == [a] and b not in host_one.calls  # an active world gets no permit
    # 3.2: a confirmation without a valid Host proof is refused - forged mac, another key, another world, stale, missing fields.
    delete_world(db, b)
    good = FakeHost('sim-host-one', one, held={b})
    with files() as c:
        permit = c.execute("select permit from authz.nexloop_sim_world_files_pending('sim-host-one',10)").fetchone()[0]
    proof = good(b, permit)
    forged = [{**proof, 'mac': '0' * 64}, FakeHost('sim-host-one', secrets.token_bytes(32), held={b}, check_permit=False)(b, permit),
              {**proof, 'world': a}, {**proof, 'outcome': 'absent'}, {k: v for k, v in proof.items() if k != 'seen'}, {**proof, 'after': 'present'}]
    for bad in forged:
        with files() as c, pytest.raises(psycopg.errors.InsufficientPrivilege, match='proof invalid'):
            c.execute("select authz.nexloop_sim_world_files_confirm('sim-host-one',%s,%s)", (b, Jsonb(bad)))
    with files() as c, pytest.raises(psycopg.errors.InsufficientPrivilege, match='proof invalid'):
        c.execute("select authz.nexloop_sim_world_files_confirm('sim-host-two',%s,%s)", (b, Jsonb(proof)))  # one Host's proof is not another's
    # 1.2/3.1: only nexloop_sim_files reaches the entries; the real scheduler (and every other application role) does not.
    for role in ('nexloop_scheduler', 'nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker'):
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from authz.nexloop_sim_world_files_pending('sim-host-one',10)")
    with files() as c:
        assert c.execute("select authz.nexloop_sim_world_files_confirm('sim-host-one',%s,%s)", (b, Jsonb(proof))).fetchone()[0] is True
        # nexloop_sim_files executes these two entries and no other security definer function.
        allowed = {r[0] for r in c.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
            where n.nspname in ('authz','control','runtime','ontology') and p.prosecdef and has_function_privilege('nexloop_sim_files',p.oid,'execute')""").fetchall()}
    assert {x.split('(')[0] for x in allowed} <= {'authz.nexloop_sim_world_files_pending', 'authz.nexloop_sim_world_files_confirm', 'runtime.nexloop_erasure_permits'}, allowed
    # An unknown Host gets nothing.
    with files() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("select * from authz.nexloop_sim_world_files_pending('sim-host-nine',10)")


def test_without_any_simulation_host_a_deleted_world_has_no_files_to_wait_for(db):
    a = create_world(db)
    delete_world(db, a)
    assert db.execute('select outcome,confirmed_at is not null from runtime.nexloop_sim_world_file_purges where sim_world=%s', (a,)).fetchone() == ('absent', True)
    assert db.execute('select completed_at is not null from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0] is True


def test_settlement_counts_the_actual_cost_and_the_month_is_utc(db):
    """Review 4.2: a reservation and its settlement (0120: the release of the unused part as a negative row) count the actual cost."""
    a = create_world(db)
    consume(db, a, 10, cid='run:settled')
    consume(db, a, -7.5, cid='run:settled:settle')
    assert db.execute('select sum(amount),min(month) from control.nexloop_sim_model_spend where sim_world=%s', (a,)).fetchone() == \
        (2.5, datetime.now(UTC).date().replace(day=1))


def test_alerts_never_evaluate_a_simulation_world():
    """Review 4.3: alert evaluation is real-world only (0131: alerts_real_world_only), so simulation spend never enters a real alert;
    the export carries the world label on every series."""
    src = (ROOT / 'packages/eios-core/src/eios/migrations/0131_nx030_observability.sql').read_text()
    assert "if p_world is distinct from 'real' then return jsonb_build_object('evaluated',false,'reason','alerts_real_world_only')" in src
    from nexloop_eios.observability import render_prometheus
    text = render_prometheus([{'tenant_id': TENANT_A, 'world': 'sim:' + U(), 'refusals': {'last_5m': {'NXB01': 1}}}])
    assert 'world="sim:' in text


# 4. The stand-in intent's time is the world's time -----------------------------------------------------------------------------
def test_a_stand_in_intent_carries_the_world_time(db, pg):
    """Integration (S4 review fixes): the server takes the world's time (S5 clock); a payload carrying sim_clock is refused."""
    a = create_world(db)
    token, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + 'a' * 32, world=a, identity_suffix='-s6-time')
    digest = hashlib.sha256(token.encode()).hexdigest()

    def submit(**extra):
        body = {'intent_id': U(), 'action_name': 'nexloop.outreach.send', 'action_version': 1, 'provider_kind': 'outreach_outbox',
                'business_digest': hashlib.sha256(U().encode()).hexdigest(), 'request_digest': 'c' * 64, 'origin': 'twin', **extra}  # one business intent each (L6)
        with psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True) as c:
            c.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest, a, Jsonb(body)))
        return db.execute("select sim_clock,submitted_at>now()-interval '1 minute' from runtime.nexloop_sim_effect_intents where intent_id=%s",
                          (body['intent_id'],)).fetchone()
    created = db.execute('select created_at from control.nexloop_sim_worlds where world=%s', (a,)).fetchone()[0]
    assert submit() == (created, True)  # the world's clock (paused at creation); the record time is the wall
    db.execute("select control.nexloop_governed_control_sim_clock(%s,'real','owner-principal','owner-subject','i-clock',%s)",
               (TENANT_A, Jsonb({'operation': 'control_sim_clock', 'request_id': U(), 'world': a, 'action': 'advance', 'seconds': 30 * 86400,
                                 'reason': '推进'})))
    assert submit() == (created + __import__('datetime').timedelta(days=30), True)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='stand-in: invalid'):
        submit(sim_clock='2031-01-02T03:04:05+00:00')

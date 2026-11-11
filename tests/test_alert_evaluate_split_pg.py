"""NX-068 defect A+B (docs/planning-drafts/alert-evaluate-split-20261111.md, L6 conditional PASS D1-D5; migration alert_evaluate_split):
the alert evaluator runs T1 maintenance and T2 snapshot without a credential and only T3 (authorized: the tenant row's FOR SHARE) over
that snapshot. Real PostgreSQL, the real evaluator service credential on the domain-worker role, the real workbench publication route.

- A slow maintenance or snapshot no longer holds a Schema publication; the short T3 still does (the control: it waits, then publishes).
- D1: a failed maintenance and a skipped evaluation are counted in the snapshot and fire their v39 rules; minutes without success fire
  alert_evaluation_stalled.
- D2: the snapshot function serves active tenants only, reuses an open snapshot, answers the snapshot id only; services cannot read the table.
- D3: the snapshot's fields are listed and are counts, codes and times only - the erasure classification ("no personal data") rests on it.
- D4: a snapshot is evaluated once; a second use is refused and changes nothing. Monotonic: an older snapshot never overwrites a newer state.
Synthetic data."""
import hashlib
import hmac
import re
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType

from multi_authority_fixture import seed_multi_authority
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios import observability as O
from nexloop_eios.context_engine.authority import ContextDenied
from nexloop_eios.observability import AlertEvaluator, apply_rules, load_rules
from nexloop_eios.observability import canonical_payload
from nx068_fixture import mark
from test_nx040_schema_publication_pg import proposal
from test_observability_pg import seed_state
from test_schema_publication_http_pg import BASE, post
from test_workbench_read_pg import TENANT, browser_business, conversations, identity, provision, published_action, uow, workbench, workbench_login  # noqa: F401

V39 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v39.json'
SLOW = 6.0


@pytest.fixture
def split(workbench, admin, pg):
    w = workbench
    provision(w)
    assert apply_rules(load_rules(V39), TENANT, database_url_file=w['configurator'])['configured'] is True
    with open_core(make_conninfo(pg, user='nexloop_domain_worker')) as pool:
        _, token = seed_multi_authority(admin, pool, [('eios:action:nexloop.alert.evaluate:1', ResourceType.ACTION, Operation.EXECUTE)],
                                        identity_suffix='-alert-evaluator')
        # Production re-authenticates every tick (background_services); fresh() is that tick's evaluator.
        fresh = lambda: AlertEvaluator(pool, authenticate_service(pool, token, world='real'), w['f']['reader'].signer)  # noqa: E731
        yield dict(w=w, admin=admin, pg=pg, ev=fresh(), fresh=fresh, pool=pool)


def worker(s):
    return psycopg.connect(make_conninfo(s['pg'], user='nexloop_domain_worker'), autocommit=True)


def take(s, failed=False, tenant=TENANT):
    with worker(s) as db:
        return db.execute('select authz.nexloop_alert_snapshot_take(%s,%s)', (tenant, failed)).fetchone()[0]


def evaluate_snapshot(s, snapshot_id, *, legacy=False):
    """T3 alone, signed as run_once signs it, over a given snapshot id (legacy: the previous release's body, no snapshot id)."""
    from nexloop_eios.context_engine.authority import action_claims
    ev = s['ev']
    body = canonical_payload({'evaluate': True} if legacy else {'evaluate': True, 'snapshot_id': str(snapshot_id)})
    claims = {**action_claims(ev.pool, ev.session, O.EVALUATE_ACTION), 'protocol': O.EVALUATE_PROTOCOL, 'key_id': ev.signer.key_id,
              'parameters_digest': hashlib.sha256(body.encode()).hexdigest()}
    text = canonical_payload(claims)
    signature = hmac.new(ev.signer.material, (O.EVALUATE_PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()
    with ev.pool.connection() as db, db.transaction():
        return db.execute('select authz.nexloop_alert_evaluate(%s,%s,%s,%s,%s)', (ev.session.token_digest, ev.session.world, text, signature, body)).fetchone()[0]


def state(admin):
    return admin.execute("""select dedupe_key,firing,last_value,last_evaluated_at from control.nexloop_alert_state where tenant_id=%s order by dedupe_key""",
                         (TENANT,)).fetchall()


def events(admin):
    return admin.execute('select count(*) from control.nexloop_alert_events where tenant_id=%s', (TENANT,)).fetchone()[0]


def firing(admin, rule):
    return admin.execute('select firing,last_value from control.nexloop_alert_state where tenant_id=%s and rule_id=%s', (TENANT, rule)).fetchall()


# ---- the lock: T1/T2 hold nothing a publication waits for; T3 does --------------------------------------------------------------
SLOWED = {
    'runtime.nexloop_run_schema_publish_smokes': ('()', '()', '()', 'integer'),
    'runtime.nexloop_metrics_snapshot': ('(text,text)', '(p_tenant text,p_world text)', '(p_tenant,p_world)', 'jsonb'),
}


def slow(admin, name, seconds):
    """Wraps the owner function `name` so it sleeps first; the evaluator code is unchanged."""
    types, params, args, returns = SLOWED[name]
    schema, fn = name.split('.')
    admin.execute(f'alter function {name}{types} rename to {fn}_fast')
    admin.execute(f"""create function {name}{params} returns {returns} language plpgsql security definer set search_path=pg_catalog,pg_temp
        as $$ begin perform pg_sleep({seconds}); return {schema}.{fn}_fast{args}; end $$""")
    admin.execute(f'alter function {name}{types} owner to nexloop_owner')


def blockers(admin, deadline=5.0):
    """While the publication waits: the holders pg_blocking_pids names (user, statement); empty if it never waited."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        rows = admin.execute("""select b.usename,b.query from pg_stat_activity w cross join lateral unnest(pg_blocking_pids(w.pid)) h(pid)
            join pg_stat_activity b on b.pid=h.pid where w.query like '%nexloop_schema_proposal_publish%'""").fetchall()
        if rows:
            return rows
        time.sleep(0.05)
    return []


def publish_during_evaluation(s, key, *, watch=5.0):
    admin, w = s['admin'], s['w']
    mark(admin, TENANT, 'Consumer.create')
    pid = proposal({'admin': admin, 'tenant': TENANT}, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    out = {}
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        def tick():
            try:
                out['result'] = s['fresh']().run_once()
            except ContextDenied as denied:  # authority changed mid-tick
                out['denied'] = type(denied).__name__
        evaluation = threading.Thread(target=tick)
        evaluation.start()
        time.sleep(0.5)
        watcher = threading.Thread(target=lambda: out.update(blockers=blockers(admin, watch)))
        watcher.start()
        started = time.monotonic()
        answer = post(client, f'{BASE}/{pid}/publish', {'rationale': 'synthetic split'}, key)
        out['waited'] = time.monotonic() - started
        watcher.join()
        evaluation.join()
    return answer, out


@pytest.mark.parametrize('name', ['runtime.nexloop_run_schema_publish_smokes', 'runtime.nexloop_metrics_snapshot'])
def test_a_slow_maintenance_or_snapshot_holds_no_publication(split, name):
    s = split
    slow(s['admin'], name, SLOW)
    answer, out = publish_during_evaluation(s, 'synthetic-split-free-' + name[-8:], watch=2.0)
    assert answer.status_code == 200 and answer.json()['published'] is True, answer.json()
    assert out['waited'] < 2.5 and out['blockers'] == [], out
    # The publication raised the authority revision during this tick: its T3 claims are refused (ContextDenied, a PermissionError) and
    # its snapshot stays open; the next tick (re-authenticated) evaluates, reusing that snapshot while it is valid (D2).
    assert out.get('denied') == 'ContextDenied' and 'result' not in out, out
    opened = s['admin'].execute('select snapshot_id from runtime.nexloop_alert_snapshots where tenant_id=%s and consumed_at is null', (TENANT,)).fetchall()
    assert len(opened) == 1
    assert s['fresh']().run_once()['evaluated'] is True
    assert s['admin'].execute('select consumed_at is not null from runtime.nexloop_alert_snapshots where snapshot_id=%s', opened[0]).fetchone()[0]


def test_the_short_t3_still_holds_the_tenant_row_and_the_publication_waits_for_it(split):
    """The control: T3 is slowed (a trigger on the alert state it updates); the publication waits for exactly that statement, then publishes."""
    s = split
    admin = s['admin']
    admin.execute("""create function public.nx068_slow_state() returns trigger language plpgsql as $$ begin if current_setting('nx068.slept',true) is distinct from '1' then
        perform set_config('nx068.slept','1',true); perform pg_sleep(1.5); end if; return new; end $$""")
    admin.execute('create trigger nx068_slow_state before update on control.nexloop_alert_state for each row execute function public.nx068_slow_state()')
    seed_state(admin, s['w']['f'])
    s['ev'].run_once()  # the states exist: the next T3 updates them (and sleeps once, on the first)
    answer, out = publish_during_evaluation(s, 'synthetic-split-t3-hold-01')
    assert answer.status_code == 200 and answer.json()['published'] is True, answer.json()
    assert out['blockers'] and all(u == 'nexloop_domain_worker' and 'authz.nexloop_alert_evaluate' in q for u, q in out['blockers']), out
    assert out['waited'] >= 0.5, out


# ---- D1 -----------------------------------------------------------------------------------------------------------------------
def test_d1_a_failed_maintenance_is_counted_and_fires(split, monkeypatch):
    s = split
    admin = s['admin']
    admin.execute('alter function runtime.nexloop_observability_maintain() rename to nexloop_observability_maintain_off')
    try:
        result = s['ev'].run_once()
    finally:
        admin.execute('alter function runtime.nexloop_observability_maintain_off() rename to nexloop_observability_maintain')
    assert result['evaluated'] is True and result['maintenance_failed'] is True  # the alerts were still evaluated
    s['ev'].run_once()  # the next snapshot counts it (within the hour)
    assert firing(s['admin'], 'alert_maintenance_failed') == [(True, 1)]


def test_d1_skipped_evaluations_and_minutes_without_success_fire(split):
    s = split
    admin = s['admin']
    assert evaluate_snapshot(s, uuid.uuid4()) == {'evaluated': False, 'reason': 'snapshot_unknown'}
    stale = take(s)
    admin.execute("update runtime.nexloop_alert_snapshots set taken_at=taken_at-interval '3 minutes' where snapshot_id=%s", (stale,))
    assert evaluate_snapshot(s, stale) == {'evaluated': False, 'reason': 'snapshot_stale'}
    s['ev'].run_once()
    assert firing(admin, 'alert_evaluation_skipped') == [(True, 2)]
    assert firing(admin, 'alert_evaluation_stalled') == [(False, 0)]  # skipped since 0 minutes; never a success before: since the first skip
    # The last success 6 minutes ago and nothing since: the next evaluation (its snapshot taken before it records success) fires stalled.
    admin.execute("update runtime.nexloop_alert_evaluation_runs set at=at-interval '6 minutes' where tenant_id=%s", (TENANT,))
    s['ev'].run_once()
    assert firing(admin, 'alert_evaluation_stalled') == [(True, 6)]
    s['ev'].run_once()
    assert firing(admin, 'alert_evaluation_stalled') == [(False, 0)]  # resolved by the success just before


# ---- D2 -----------------------------------------------------------------------------------------------------------------------
def test_d2_active_tenants_only_one_open_snapshot_id_only_and_no_service_reads(split):
    s = split
    admin = s['admin']
    first = take(s)
    assert isinstance(first, uuid.UUID) and take(s) == first  # reused while open and valid
    assert admin.execute("select prorettype::regtype::text from pg_proc where oid='authz.nexloop_alert_snapshot_take(text,boolean)'::regprocedure").fetchone()[0] == 'uuid'
    assert admin.execute('select count(*) from runtime.nexloop_alert_snapshots where tenant_id=%s', (TENANT,)).fetchone()[0] == 1
    take(s, failed=True)
    assert admin.execute('select maintenance_failed from runtime.nexloop_alert_snapshots where snapshot_id=%s', (first,)).fetchone()[0] is True
    admin.execute("update runtime.nexloop_alert_snapshots set taken_at=taken_at-interval '3 minutes' where snapshot_id=%s", (first,))
    assert take(s) != first  # an expired open snapshot is not reused
    for tenant in ('00000000-0000-4000-8000-0000000000ff', TENANT):
        if tenant == TENANT:
            admin.execute("update control.nexloop_tenants set status='suspended' where tenant_id=%s", (TENANT,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='tenant unavailable'):
            take(s, tenant=tenant)
    admin.execute("update control.nexloop_tenants set status='active' where tenant_id=%s", (TENANT,))
    for role in ('nexloop_domain_worker', 'nexloop_api', 'nexloop_metrics'):
        with psycopg.connect(make_conninfo(s['pg'], user=role)) as db:
            for table in ('runtime.nexloop_alert_snapshots', 'runtime.nexloop_alert_evaluation_runs'):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute(f'select * from {table}')
                db.rollback()
    with psycopg.connect(make_conninfo(s['pg'], user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_alert_snapshot_take(%s,false)', (TENANT,))


# ---- D3 -----------------------------------------------------------------------------------------------------------------------
TIME = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}(:?\d{2})?)?$')
CODE = re.compile(r'^[a-z][a-z0-9_.:-]{0,63}$')
IDENTITY = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-|[0-9a-f]{24,}|@|\+?\d{7,}', re.I)


def leaves(value, path=''):
    if isinstance(value, dict):
        for k, v in value.items():
            yield path + '.' + k, 'key', k
            yield from leaves(v, path + '.' + k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, f'{path}[{i}]')
    else:
        yield path, 'value', value


def test_d3_the_snapshot_holds_counts_codes_and_times_only(split):
    """The erasure classification of runtime.nexloop_alert_snapshots / _evaluation_runs is "no personal data, kept 1 day, not erased": it
    rests on this test - the snapshot of a tenant with operational state (refusals, escalations, a breached commitment, a dead letter,
    samples) has no identity in any key or value, and the tables have no identity column."""
    s = split
    admin = s['admin']
    seed_state(admin, s['w']['f'])
    O.record_sample(s['w']['f']['reader'].pool, 'pool', 'synthetic-api', {'requests_waiting': 2, 'pool_size': 4, 'max_size': 4})
    s['ev'].run_once()
    snapshot = admin.execute('select snapshot from runtime.nexloop_alert_snapshots where tenant_id=%s order by taken_at desc limit 1', (TENANT,)).fetchone()[0]
    fields = list(leaves(snapshot))
    assert len(fields) > 50 and {'.alert_evaluation.maintenance_failed_1h', '.schema_publish_smoke.problems'} <= {p for p, _, _ in fields}
    known = {TENANT, s['w']['f']['consumer']} | {r for (r,) in admin.execute(
        "select distinct entity_key[1] from authz.nexloop_authority_facts where tenant_id=%s", (TENANT,)).fetchall()}
    for path, kind, value in fields:
        if kind == 'key' or isinstance(value, str):
            assert value not in known and not IDENTITY.search(value) and (CODE.match(value) or TIME.match(value) or kind == 'key' and re.match(r'^[A-Za-z0-9_.:-]{1,64}$', value)), (path, value)
        else:
            assert value is None or isinstance(value, (int, float, bool)), (path, value)
    columns = {t: {c for (c,) in admin.execute("select column_name from information_schema.columns where table_schema='runtime' and table_name=%s", (t,)).fetchall()}
               for t in ('nexloop_alert_snapshots', 'nexloop_alert_evaluation_runs')}
    assert columns == {'nexloop_alert_snapshots': {'snapshot_id', 'tenant_id', 'world', 'taken_at', 'maintenance_failed', 'snapshot', 'consumed_at'},
                       'nexloop_alert_evaluation_runs': {'run_id', 'tenant_id', 'world', 'at', 'outcome'}}
    declared = dict(admin.execute("select table_name::text,reason from control.nexloop_real_effect_tables where table_name::text like 'runtime.nexloop_alert_%'").fetchall())
    assert set(declared) == {'runtime.nexloop_alert_snapshots', 'runtime.nexloop_alert_evaluation_runs'}
    assert 'counts, codes and times only' in declared['runtime.nexloop_alert_snapshots']


# ---- D4 and monotonic ----------------------------------------------------------------------------------------------------------
def test_d4_a_snapshot_is_evaluated_once(split):
    s = split
    admin = s['admin']
    seed_state(admin, s['w']['f'])
    snapshot = take(s)
    assert evaluate_snapshot(s, snapshot)['evaluated'] is True
    before = (state(admin), events(admin))
    assert evaluate_snapshot(s, snapshot) == {'evaluated': False, 'reason': 'snapshot_consumed'}
    assert (state(admin), events(admin)) == before
    assert admin.execute("select outcome from runtime.nexloop_alert_evaluation_runs where tenant_id=%s order by run_id", (TENANT,)).fetchall() == [('evaluated',), ('snapshot_consumed',)]


def test_an_older_snapshot_never_overwrites_a_newer_state(split):
    s = split
    admin = s['admin']
    seed_state(admin, s['w']['f'])
    older = take(s)
    # Set aside (marked used) so the next take makes a newer one; taken 30 s earlier.
    admin.execute("update runtime.nexloop_alert_snapshots set taken_at=taken_at-interval '30 seconds',consumed_at=clock_timestamp() where snapshot_id=%s", (older,))
    newer = take(s)
    assert newer != older and evaluate_snapshot(s, newer)['evaluated'] is True
    after_newer = (state(admin), events(admin))
    assert after_newer[0] and after_newer[1] > 0
    admin.execute('update runtime.nexloop_alert_snapshots set consumed_at=null where snapshot_id=%s', (older,))
    late = evaluate_snapshot(s, older)  # arrives late (both snapshots valid and unused)
    assert late['evaluated'] is True and late['fired'] == 0 and late['resolved'] == 0 and late['kept_newer'] == len(after_newer[0])
    assert (state(admin), events(admin)) == after_newer


def test_t3_refuses_a_missing_or_foreign_snapshot(split):
    from support.tenants import TENANT_OTHER
    s = split
    admin = s['admin']
    assert evaluate_snapshot(s, 'not-a-uuid') == {'evaluated': False, 'reason': 'snapshot_missing'}
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_OTHER,))
    with worker(s) as db:
        foreign = db.execute('select authz.nexloop_alert_snapshot_take(%s,false)', (TENANT_OTHER,)).fetchone()[0]
    assert evaluate_snapshot(s, foreign) == {'evaluated': False, 'reason': 'snapshot_unknown'}
    assert admin.execute('select consumed_at from runtime.nexloop_alert_snapshots where snapshot_id=%s', (foreign,)).fetchone()[0] is None
    assert evaluate_snapshot(s, uuid.uuid4()) == {'evaluated': False, 'reason': 'snapshot_unknown'}


def test_the_previous_releases_evaluator_still_evaluates(split):
    """Rolling deploy or rollback: a body without snapshot_id runs the old single transaction (maintenance and snapshot under the
    authorization) and evaluates; nothing is counted as skipped."""
    s = split
    admin = s['admin']
    seed_state(admin, s['w']['f'])
    result = evaluate_snapshot(s, None, legacy=True)
    assert result['evaluated'] is True and result['fired'] > 0, result
    assert admin.execute('select outcome from runtime.nexloop_alert_evaluation_runs where tenant_id=%s', (TENANT,)).fetchall() == [('evaluated',)]
    assert admin.execute('select count(*) from runtime.nexloop_alert_snapshots where tenant_id=%s and consumed_at is not null', (TENANT,)).fetchone()[0] == 1

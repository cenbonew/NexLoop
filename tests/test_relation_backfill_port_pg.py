"""NX-054 P3 slice 2 (the relation backfill port migration; nexloop_eios.relation_backfill): the backfill through its signed port as
its own service principal (nexloop_domain_worker, eios:action:nexloop.relation.backfill:1). The runner walks a run to completed
and verify balances; a batch replays by its intent; the port sets the 1 s bound and the engine refuses another; other roles and
malformed commands are refused. V-1 / V-4 (design §8): while a batch stretched by a slow derivation holds a Consumer's KEY SHARE,
a write that takes that Consumer FOR UPDATE under the real paths' default lock_timeout of 3000 ms (as the governed edit commit of
0015 does) is blocked by the backfill session (pg_blocking_pids), waits and succeeds, and the whole batch stays under 1 s; the
control group runs the same batch through the signed port with both bounds lifted (statement_timeout 5 s, no 750 ms early
commit; test-only redefinitions, restored after) and the write fails with 55P03. The write stands for the edit's lock, not the
whole edit Action. Also (L6 I-6): a payload naming a tenant is refused, a non-real world is refused, the port runs the batch
under lock_timeout 200 ms, and another tenant's objects are not touched."""
import io
import json
import secrets
import threading
import time

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios import relation_backfill as B
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.plan_reevaluation import PlanUnavailable
from nexloop_eios.postgres_artifacts import AuthoritySigner
from support.tenants import TENANT_A, TENANT_B
from test_relation_backfill_engine_pg import seeded, verify  # noqa: F401 - stock objects on the P1 catalog
from test_relation_types_p1_pg import catalog  # noqa: F401


@pytest.fixture
def port(seeded, admin, pg):  # noqa: F811 - imported fixture
    signer = AuthoritySigner('synthetic-backfill', secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)', (signer.key_id, signer.material))
    with open_core(make_conninfo(pg, user='nexloop_domain_worker')) as pool:
        _, token = seed_multi_authority(admin, pool, [(B.ACTION, ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-relation-backfill')
        make = lambda: B.RelationBackfillPort(pool, authenticate_service(pool, token, world='real'), signer)  # noqa: E731

        class Fresh:
            def __getattr__(self, name):
                return getattr(make(), name)

        yield dict(seeded, port=Fresh(), pool=pool, token=token, signer=signer)


def test_a_run_through_the_signed_port(port):
    p, admin = port['port'], port['admin']
    pre = p.preflight()
    assert pre['blocking'] == []
    assert p.start('run-1', pre['digest'])['state'] == 'running'
    out, slept = io.StringIO(), []
    assert B.RelationBackfill(p, out=out, sleep=slept.append).run('run-1') == 0
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[-1]['event'] == 'complete' and lines[-1]['completed'] is True
    assert {x['event'] for x in lines} >= {'batch', 'rescan', 'complete'} and all(x['elapsed_ms'] < 1000 for x in lines if x['event'] == 'batch')
    assert p.status('run-1')['state'] == 'completed'
    v = verify(admin)[('goal_of', 'real')]
    assert v['a'] == v['b'] == 6
    assert p.verify()['skipped_remaining'] == 0
    started_by = admin.execute("select started_by from control.nexloop_relation_backfill_runs where run_id='run-1'").fetchone()[0]
    assert started_by.startswith('service:') and 'relation-backfill' in started_by
    # Revert through the port: only this run's relations go.
    assert B.RelationBackfill(p, out=io.StringIO(), sleep=slept.append).revert('run-1') == 0
    assert verify(admin)[('goal_of', 'real')]['b'] == 0


def test_a_batch_replays_by_its_intent(port):
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    first = p.batch('run-1', 'Goal', 'real', '', 2)
    again = p.batch('run-1', 'Goal', 'real', '', 2)
    assert again == {**first, 'replayed': True}  # the stored answer, not the engine's "cursor moved"
    nxt = p.batch('run-1', 'Goal', 'real', first['after'], 2)
    assert nxt['after'] > first['after'] and 'replayed' not in nxt


def test_the_port_refuses_what_it_must(port, pg):
    p = port['port']
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        p._signed({'verb': 'batch', 'run_id': 'run-1', 'source_type': 'Goal', 'world': 'real', 'after': 'x', 'batch': 2})
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        p._signed({'verb': 'unknown'})
    p.start('run-1', p.preflight()['digest'])
    # The engine's bound: a port that set no 1 s statement_timeout is refused (I-1).
    loose = B.RelationBackfillPort(port['pool'], authenticate_service(port['pool'], port['token'], world='real'), port['signer'])
    loose._prepare = lambda db: db.execute("select set_config('statement_timeout','5s',true)")
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        loose.batch('run-1', 'Goal', 'real', '', 2)
    # Another role holds no execute on the port.
    with open_core(make_conninfo(pg, user='nexloop_api')) as api:
        other = B.RelationBackfillPort(api, authenticate_service(api, port['token'], world='real'), port['signer'])
        with pytest.raises((PlanUnavailable, psycopg.errors.InsufficientPrivilege)):
            other.status('run-1')


def slow_derivation(admin, seconds):
    """Each relation the batch creates takes `seconds` more; the lock_timeout the batch runs under is recorded (I-6)."""
    admin.execute('create table if not exists public.nexloop_bf_seen(lock_timeout text)')
    admin.execute('grant insert on public.nexloop_bf_seen to nexloop_owner')
    admin.execute(f"""create or replace function public.nexloop_bf_slow_row() returns trigger language plpgsql as
        $$begin insert into public.nexloop_bf_seen values(current_setting('lock_timeout'));perform pg_sleep({seconds});return null;end $$""")
    admin.execute('grant execute on function public.nexloop_bf_slow_row() to nexloop_owner')
    admin.execute("""create trigger nexloop_bf_slow_row after insert on control.nexloop_relation_backfill_created for each row
        execute function public.nexloop_bf_slow_row()""")


def no_slow_derivation(admin):
    admin.execute('drop trigger if exists nexloop_bf_slow_row on control.nexloop_relation_backfill_created')


def held_by_backfill(monitor, consumer):
    """L6 I-7: the backfill session holds the Consumer's row lock now (pgrowlocks: KEY SHARE by a nexloop_domain_worker pid)."""
    return monitor.execute("""select exists(select 1 from pgrowlocks('ontology.objects') l join ontology.objects o on o.ctid=l.locked_row
        where o.object_id=%s and l.pids && array(select pid from pg_stat_activity where usename='nexloop_domain_worker'))""", (consumer,)).fetchone()[0]


def edit_lock(pg, consumer, result):
    """The governed edit's commit takes the edited object FOR UPDATE under the real paths' lock_timeout (3000 ms default), once
    the backfill holds the Consumer (polled, not slept). A monitor records whether the backfill session blocked it
    (pg_blocking_pids), not inferred from the wait."""
    with psycopg.connect(pg) as db, psycopg.connect(pg, autocommit=True) as monitor:
        deadline = time.monotonic() + 5
        while not held_by_backfill(monitor, consumer):
            if time.monotonic() > deadline:
                result['never_held'] = True
                return
            time.sleep(0.01)
        db.execute("set lock_timeout='3000ms'")
        pid, blockers = db.info.backend_pid, set()
        watching = threading.Event()

        def watch():
            while not watching.is_set():
                blockers.update(monitor.execute('select pg_blocking_pids(%s)', (pid,)).fetchone()[0])
                time.sleep(0.02)

        watcher = threading.Thread(target=watch)
        watcher.start()
        t0 = time.monotonic()
        try:
            db.execute('select 1 from ontology.objects where object_id=%s for update', (consumer,))
            result['ok'] = True
        except psycopg.errors.LockNotAvailable:
            result['ok'] = False
        result['waited'] = time.monotonic() - t0
        watching.set()
        watcher.join()
        result['blockers'] = blockers


def backfill_pids(pg):
    with psycopg.connect(pg, autocommit=True) as db:
        return {r[0] for r in db.execute("select pid from pg_stat_activity where usename='nexloop_domain_worker'").fetchall()}


def b_first(port):
    port['admin'].execute('create extension if not exists pgrowlocks')
    """Every Goal of the seed names b, so the batch takes b's KEY SHARE at its first object (well before the edit starts)."""
    port['admin'].execute("update ontology.objects set properties=jsonb_build_object('consumer_id',%s::text) where type_name='Goal' and properties->>'consumer_id'=%s",
                          (port['b'], port['a']))
    port['admin'].execute("delete from ontology.relations")  # the trigger derived them on that update: stock again


def test_v1_a_bounded_batch_blocks_an_edit_briefly_and_v4_stays_under_one_second(port, admin, pg):
    p = port['port']
    b_first(port)
    p.start('run-1', p.preflight()['digest'])
    slow_derivation(admin, 0.2)  # each relation the batch creates: 0.2 s more, so the batch meets the 750 ms early commit
    try:
        result = {}
        worker = threading.Thread(target=edit_lock, args=(pg, port['b'], result))
        worker.start()
        got = p.batch('run-1', 'Goal', 'real', '', 200)
        worker.join(10)
        seen = {r[0] for r in admin.execute('select lock_timeout from public.nexloop_bf_seen').fetchall()}
    finally:
        no_slow_derivation(admin)
    assert got['early'] is True and got['elapsed_ms'] < 1000 and got['target_locked'] == 0  # V-4: the whole batch, from its start
    assert result['ok'] is True and result['waited'] < 1.0  # the edit went through
    assert result['blockers'] and result['blockers'] <= backfill_pids(pg)  # blocked, and only by the backfill's own session
    assert seen == {'200ms'}  # I-6: the port's lock_timeout inside the batch
    assert admin.execute('select count(*) from ontology.relations where target_object_id=%s', (port['b'],)).fetchone()[0] >= 1


def test_v1_control_group_both_bounds_lifted_fails_the_edit(port, admin, pg):
    """The same batch through the signed port with statement_timeout 5 s and no 750 ms early commit (test-only redefinitions of
    the engine's bound and early commit, restored after): it holds b's KEY SHARE over 3 s and the edit fails with 55P03."""
    p = port['port']
    b_first(port)
    p.start('run-1', p.preflight()['digest'])
    originals = {f: admin.execute(f"select pg_get_functiondef('{f}'::regprocedure)").fetchone()[0] for f in
                 ('control.nexloop_relation_backfill_assert_timeout()', 'control.nexloop_relation_backfill_batch(text,text,text,text,text,integer)')}
    lifted = {f: d.replace('v>1000', 'v>5000').replace("interval '750 milliseconds'", "interval '1 hour'") for f, d in originals.items()}
    assert all(lifted[f] != originals[f] for f in originals)
    for d in lifted.values():
        admin.execute(d)
    slow_derivation(admin, 0.6)
    loose = B.RelationBackfillPort(port['pool'], authenticate_service(port['pool'], port['token'], world='real'), port['signer'])
    loose._prepare = lambda db: db.execute("select set_config('statement_timeout','5s',true)")
    try:
        result = {}
        worker = threading.Thread(target=edit_lock, args=(pg, port['b'], result))
        worker.start()
        got = loose.batch('run-1', 'Goal', 'real', '', 200)
        worker.join(10)
    finally:
        no_slow_derivation(admin)
        for d in originals.values():
            admin.execute(d)
    restored = {f: admin.execute(f"select pg_get_functiondef('{f}'::regprocedure)").fetchone()[0] for f in originals}
    assert restored == originals  # the test-only redefinitions are gone
    assert got['early'] is False and got['elapsed_ms'] > 3000
    assert result['ok'] is False and result['waited'] >= 2.9 and result['blockers']


def test_the_port_refuses_a_tenant_in_the_payload_and_a_non_real_world(port, admin):
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    with pytest.raises(psycopg.errors.InvalidParameterValue):  # M4: the tenant is the service identity's only
        p._signed({'verb': 'batch', 'run_id': 'run-1', 'source_type': 'Goal', 'world': 'real', 'after': '', 'batch': 2, 'tenant_id': TENANT_B})
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        p._signed({'verb': 'status', 'run_id': 'run-1', 'tenant_id': TENANT_B})
    # M6: a service credential with the backfill authority in the test world is refused by the port.
    _, test_token = seed_multi_authority(admin, port['pool'], [(B.ACTION, ResourceType.ACTION, Operation.EXECUTE)],
                                         identity_suffix='-relation-backfill-test', world='test')
    other = B.RelationBackfillPort(port['pool'], authenticate_service(port['pool'], test_token, world='test'), port['signer'])
    with pytest.raises((PlanUnavailable, psycopg.errors.InsufficientPrivilege)):
        other.status('run-1')


def test_another_tenants_objects_are_not_touched(port, admin):
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_B,))
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Consumer',%s,1,'{}','synthetic','p3',clock_timestamp()-interval '1 minute',clock_timestamp()-interval '1 minute'),
                  (%s,'real','Goal',%s,1,jsonb_build_object('consumer_id',%s::text),'synthetic','p3',clock_timestamp()-interval '1 minute',clock_timestamp()-interval '1 minute')""",
                      (TENANT_B, '1' * 64, TENANT_B, '2' * 64, '1' * 64))
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    assert B.RelationBackfill(p, out=io.StringIO(), sleep=lambda s: None).run('run-1') == 0
    assert admin.execute('select count(*) from ontology.relations where tenant_id=%s', (TENANT_B,)).fetchone()[0] == 0
    counts = {x['relation_name']: x for x in p.status('run-1')['counts'] if x['world'] == 'real'}
    assert counts['goal_of']['scanned'] == 7  # A's six Goals and its dangling one; B's Goal is not A's
    assert admin.execute('select count(*) from control.nexloop_relation_backfill_intents').fetchone()[0] == 0  # S-13: gone at completion


def test_an_unknown_result_resends_the_same_intent(port):
    """S-10: the connection drops after the batch committed; the runner sends the same intent and gets the stored answer."""
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    calls = []

    class Flaky:
        def __getattr__(self, name):
            real = getattr(p, name)
            if name != 'batch':
                return real

            def batch(*args):
                calls.append(args)
                got = real(*args)
                if len(calls) == 1:
                    raise psycopg.OperationalError('connection lost')
                return got
            return batch

    out = io.StringIO()
    assert B.RelationBackfill(Flaky(), out=out, sleep=lambda s: None).run('run-1') == 0
    assert calls[0] == calls[1]
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0] == {'event': 'retry', 'source_type': calls[0][1], 'world': 'real', 'code': 'connection'}


def test_a_stop_signal_ends_the_run_between_calls(port):
    """S-12: SIGTERM / SIGINT set the stop event: one interrupted line, exit code 3, the run stays running."""
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    stop = threading.Event()
    stop.set()
    out = io.StringIO()
    assert B.RelationBackfill(p, out=out, sleep=lambda s: None, stop=stop).run('run-1') == B.INTERRUPTED
    assert json.loads(out.getvalue().splitlines()[-1]) == {'event': 'interrupted'} and p.status('run-1')['state'] == 'running'


def test_complete_and_abort_give_up_on_a_held_lock_within_the_ports_lock_timeout(port, pg):
    """L6 I-6 (M5): the port sets lock_timeout 200 ms for every verb; complete and abort, which the engine does not time
    themselves, give up with 55P03 instead of waiting into their 1 s statement_timeout."""
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    with psycopg.connect(pg) as other:
        other.execute("select 1 from control.nexloop_relation_backfill_runs where run_id='run-1' for update")
        for call in (lambda: p.complete('run-1'), lambda: p.abort('run-1')):
            t0 = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                call()
            assert time.monotonic() - t0 < 0.9
        other.rollback()
    assert p.status('run-1')['state'] == 'running'


def flaky(p, errors):
    """A port whose batch raises the given errors, one per call, before it calls the real batch; the sizes asked are kept."""
    sizes = []

    class Flaky:
        def __getattr__(self, name):
            real = getattr(p, name)
            if name != 'batch':
                return real

            def batch(*args):
                sizes.append(args[4])
                if errors:
                    raise errors.pop(0)
                return real(*args)
            return batch
    return Flaky(), sizes


def test_a_statement_timeout_halves_the_next_batch(port):
    """L6 C-2: 57014 is an OperationalError; it halves the batch (it is not taken for an unknown result)."""
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    f, sizes = flaky(p, [psycopg.errors.QueryCanceled('canceling statement due to statement timeout')])
    out = io.StringIO()
    assert B.RelationBackfill(f, out=out, sleep=lambda s: None, consumer_batch=100).run('run-1') == 0
    assert sizes[1] == sizes[0] // 2
    assert json.loads(out.getvalue().splitlines()[0])['code'] == '57014'


def test_retryable_errors_go_on_past_the_unknown_result_limit(port):
    """L6 C-2: 55P03 / 40001 / 40P01 retry the batch however often; they never count as unknown results."""
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    errors = [psycopg.errors.LockNotAvailable('x'), psycopg.errors.SerializationFailure('x'), psycopg.errors.DeadlockDetected('x')] * 3
    f, sizes = flaky(p, errors)
    out = io.StringIO()
    assert B.RelationBackfill(f, out=out, sleep=lambda s: None).run('run-1') == 0  # 9 > UNKNOWN_RESULT_RETRIES, still done
    assert len({s for s in sizes[:10]}) == 1  # same size: never halved
    assert [json.loads(x)['code'] for x in out.getvalue().splitlines()[:9]] == ['55P03', '40001', '40P01'] * 3

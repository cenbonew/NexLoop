"""NX-054 P3 slice 1 (the relation backfill migration; docs/design/relation-backfill-p3.md): the backfill engine on real PostgreSQL.
Stock objects written in replica mode (the triggers did not derive them) get their relations, marked {"derived":true,"backfill":
run}; verify balances. A second session holds locks to exercise the concurrency rules: a source locked FOR UPDATE without a
property change is persisted as skipped and the rescan fills it (C-2), also when it is the last of the batch range (N-3); a target
locked FOR UPDATE (delivery, N-1) or being deleted is target_locked and the run never aborts (C-1); waiters on the batch are counted
(R-5). Also: the 750 ms early commit (R-1, V-3), resume and a replay after the cursor moved (N-6), a changed catalog aborts (I-2),
abort from outside (S-5), a purging simulation world is skipped (N-2), erasing Consumers are skipped (S-4, I-6), preflight blocking
items (§6), revert removes only this run's relations (§7). The signed Action entry is slice 2; here admin calls the owner functions
as that entry will, with READ COMMITTED and statement_timeout 1s."""
import hashlib
import threading
import time

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog, publish  # noqa: F401 - the P1 fixture (object types, configurator)


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def stock(admin, type_name, object_id, props, *, world='real', age='1 minute'):
    """A stock object: replica mode, so no trigger derives; written `age` ago (older than the 10 s recent-write skip)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,%s,%s,%s,1,%s,'synthetic','p3',clock_timestamp()-%s::interval,clock_timestamp()-%s::interval)""",
                      (TENANT_A, world, type_name, object_id, Jsonb(props), age, age))


def call(admin, fn, *args):
    with admin.transaction():
        admin.execute("set local statement_timeout='1s'")
        return admin.execute(f'select control.nexloop_relation_backfill_{fn}(%s{",%s" * len(args)})', (TENANT_A, *args)).fetchone()[0]


def digest(admin):
    return admin.execute('select control.nexloop_relation_backfill_digest(%s)', (TENANT_A,)).fetchone()[0]


def start(admin, run='run-1'):
    return call(admin, 'start', run, digest(admin), 'test')


def cursors(admin, run='run-1'):
    return call(admin, 'status', run)['cursors']


def walk(admin, run='run-1', batch=200):
    """Every cursor of the run to done (no rescan)."""
    results = []
    for c in cursors(admin, run):
        if c['done']:
            continue
        after = c['after']
        while True:
            got = call(admin, 'batch', run, c['source_type'], c['world'], after, batch)
            results.append(got)
            if got.get('done') or 'stopped' in got:
                break
            after = got['after']
    return results


def drive(admin, run='run-1', batch=200):
    """The run loop of the coming CLI, reduced: every cursor to done, then the rescan, then complete."""
    results = walk(admin, run, batch)
    while call(admin, 'rescan', run, 100)['remaining']:
        pass
    results.append(call(admin, 'complete', run))
    return results


def verify(admin):
    return {(v['relation'], v['world']): v for v in call(admin, 'verify')}


def relation(admin, source, name='goal_of'):
    return admin.execute('select target_object_id,metadata from ontology.relations where source_object_id=%s and relation_name=%s', (source, name)).fetchall()


@pytest.fixture
def seeded(catalog):  # noqa: F811 - imported fixture
    c = catalog
    admin = c['admin']
    publish(c)
    a, b = hexid('bf-consumer-a'), hexid('bf-consumer-b')
    for x in (a, b):
        stock(admin, 'Consumer', x, {})
    goals = [hexid(f'bf-goal-{i}') for i in range(6)]
    for i, g in enumerate(goals):
        stock(admin, 'Goal', g, {'consumer_id': a if i % 2 else b})
    stock(admin, 'Goal', hexid('bf-goal-dangling'), {'consumer_id': hexid('nobody')})
    return dict(c, a=a, b=b, goals=sorted(goals))


def test_stock_objects_get_their_relations_and_verify_balances(seeded):
    admin, goals = seeded['admin'], seeded['goals']
    pre = call(admin, 'preflight')
    assert pre['blocking'] == [] and pre['digest'] == digest(admin)
    assert start(admin)['state'] == 'running'
    assert start(admin)['replayed'] is True  # the same run id: the same run
    assert cursors(admin)[0]['source_type'] in ('Goal', 'PlanStep')  # ownership source types first
    results = drive(admin)
    assert results[-1] == {'completed': True, 'open_cursors': 0, 'skipped': 0}
    for g in goals:
        [(target, metadata)] = relation(admin, g)
        assert metadata == {'derived': True, 'backfill': 'run-1'}
    v = verify(admin)[('goal_of', 'real')]
    assert v['a'] == v['b'] == 6 and v['dangling'] == 1 and v['extra'] == 0
    status = call(admin, 'status', 'run-1')
    assert status['state'] == 'completed' and status['created'] == 6
    counts = {(x['relation_name'], x['world']): x for x in status['counts']}
    assert counts[('goal_of', 'real')]['created'] == 6 and counts[('goal_of', 'real')]['dangling'] == 1
    # A second run finds them all: existing, nothing created, nothing to revert.
    start(admin, 'run-2')
    drive(admin, 'run-2')
    assert call(admin, 'status', 'run-2')['created'] == 0
    # The trigger keeps maintaining a backfilled relation (design §4.2): the property changes, the relation follows.
    admin.execute("update ontology.objects set properties=jsonb_build_object('consumer_id',%s::text) where object_id=%s", (seeded['a'], goals[0]))
    assert [t for t, _ in relation(admin, goals[0])] == [seeded['a']]


def test_a_locked_source_is_persisted_and_the_rescan_fills_it(seeded):
    """C-2 and N-3: a source held FOR UPDATE without a property change (a CAS that changes nothing) - here the last of the range."""
    admin, goals = seeded['admin'], seeded['goals']
    start(admin)
    last = goals[-1]
    with psycopg.connect(seeded['pg']) as other:
        other.execute('select 1 from ontology.objects where object_id=%s for update', (last,))
        got = call(admin, 'batch', 'run-1', 'Goal', 'real', '', 200)
        assert got['skipped'] == 1 and got['after'] == last  # the cursor reaches hi; the locked one is persisted, not passed over
        status = call(admin, 'status', 'run-1')
        assert [s['object_id'] for s in status['skipped']] == [last]
        assert call(admin, 'rescan', 'run-1', 100) == {'cleared': 0, 'kept': 1, 'remaining': 1, 'max_attempts': 1}
        walk(admin)  # every cursor done
        assert call(admin, 'complete', 'run-1') == {'completed': False, 'open_cursors': 0, 'skipped': 1}  # never with a skipped object
        other.rollback()
    assert call(admin, 'rescan', 'run-1', 100)['remaining'] == 0
    assert relation(admin, last)[0][1] == {'derived': True, 'backfill': 'run-1'}


def test_a_busy_target_is_target_locked_and_the_run_goes_on(seeded):
    """C-1 / N-1: the Consumer held FOR UPDATE (as delivery does) and another being deleted: no 23503, no abort."""
    admin = seeded['admin']
    start(admin)
    with psycopg.connect(seeded['pg']) as other:
        other.execute('select 1 from ontology.objects where object_id=%s for update', (seeded['a'],))
        got = call(admin, 'batch', 'run-1', 'Goal', 'real', '', 200)
        assert got['target_locked'] == 3 and 'stopped' not in got
        other.rollback()
        # The other Consumer is deleted while the rescan runs: its Goals lose the target; they are dangling, the run goes on.
        other.execute('delete from ontology.objects where object_id=%s', (seeded['a'],))
        assert call(admin, 'rescan', 'run-1', 100)['kept'] == 3  # still busy (the delete is not committed)
        other.commit()
    assert call(admin, 'rescan', 'run-1', 100)['remaining'] == 0
    assert call(admin, 'status', 'run-1')['state'] == 'running'


def test_waiters_on_the_batch_are_counted(seeded):
    """R-5 / V-2: a session waiting for the batch's transaction is counted in the batch's answer."""
    admin = seeded['admin']
    start(admin)
    waited = {}
    target = admin.execute("select properties->>'consumer_id' from ontology.objects where object_id=%s", (seeded['goals'][0],)).fetchone()[0]

    def wait_for_consumer():
        with psycopg.connect(seeded['pg']) as other:
            t0 = time.monotonic()
            other.execute('select 1 from ontology.objects where object_id=%s for update', (target,))
            waited['s'] = time.monotonic() - t0

    with admin.transaction():
        admin.execute("set local statement_timeout='1s'")
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("select control.nexloop_relation_backfill_object(o,'run-1','{}') from ontology.objects o where o.object_id=%s for share",
                      (seeded['goals'][0],))  # takes the Consumer's KEY SHARE
        worker = threading.Thread(target=wait_for_consumer)
        worker.start()
        time.sleep(0.5)
        n = admin.execute('select count(*) from pg_stat_activity a where pg_backend_pid()=any(pg_blocking_pids(a.pid))').fetchone()[0]
    worker.join(5)
    assert n == 1 and waited['s'] >= 0.4


def test_the_batch_commits_early_after_750_ms(seeded):
    """R-1 / V-3: 750 ms from the transaction's start; at least one object, the cursor at the last one processed."""
    admin, goals = seeded['admin'], seeded['goals']
    start(admin)
    with admin.transaction():
        admin.execute("set local statement_timeout='2s'")
        admin.execute('select pg_sleep(0.8)')
        got = admin.execute("select control.nexloop_relation_backfill_batch(%s,'run-1','Goal','real','',200)", (TENANT_A,)).fetchone()[0]
    assert got['early'] is True and got['locked'] == 1 and got['after'] == goals[0] and got['skipped'] == 0
    assert drive(admin)[-1]['completed'] is True and verify(admin)[('goal_of', 'real')]['b'] == 6


def test_resume_and_a_replay_after_the_cursor_moved(seeded):
    admin = seeded['admin']
    start(admin)
    first = call(admin, 'batch', 'run-1', 'Goal', 'real', '', 2)
    with pytest.raises(psycopg.errors.SerializationFailure):  # N-6: the same range again is refused, never a silent skip
        call(admin, 'batch', 'run-1', 'Goal', 'real', '', 2)
    assert call(admin, 'batch', 'run-1', 'Goal', 'real', first['after'], 2)['after'] > first['after']  # resumes at the cursor
    assert drive(admin)[-1]['completed'] is True


def test_a_changed_catalog_aborts_and_abort_works_from_outside(seeded):
    admin = seeded['admin']
    start(admin)
    admin.execute("update control.nexloop_relation_derivations set version=version where false")  # nothing: digest unchanged
    assert 'stopped' not in call(admin, 'batch', 'run-1', 'Goal', 'real', '', 2)
    with admin.transaction():  # stands for a newly published version (I-2)
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("update control.nexloop_relation_derivations set version=version+1 where tenant_id=%s and relation_name='goal_of'", (TENANT_A,))
    got = call(admin, 'batch', 'run-1', 'Goal', 'real', cursors(admin)[0]['after'] if cursors(admin)[0]['source_type'] == 'Goal' else '', 2)
    assert got == {'stopped': 'aborted', 'reason': 'catalog_changed'}
    assert call(admin, 'rescan', 'run-1', 10) == {'stopped': 'aborted', 'reason': 'catalog_changed'}  # N-5
    # A new run on the new map; abort from another session stops it (S-5).
    start(admin, 'run-2')
    with psycopg.connect(seeded['pg'], autocommit=True) as other:
        assert other.execute("select control.nexloop_relation_backfill_abort(%s,'run-2','operator')", (TENANT_A,)).fetchone()[0]['aborted'] is True
    assert call(admin, 'batch', 'run-2', 'Goal', 'real', '', 2) == {'stopped': 'aborted', 'reason': 'operator'}


def test_preflight_blocks_and_one_active_run(seeded):
    admin = seeded['admin']
    start(admin)
    assert {b['item'] for b in call(admin, 'preflight')['blocking']} == {'active_run'}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        start(admin, 'run-2')
    call(admin, 'abort', 'run-1', 'test')
    admin.execute('alter table ontology.objects disable trigger nexloop_relation_derive_update')
    try:
        assert {b['item'] for b in call(admin, 'preflight')['blocking']} == {'derivation_missing_or_disabled'}
    finally:
        admin.execute('alter table ontology.objects enable trigger nexloop_relation_derive_update')
    stock(admin, 'RelationshipAssessment', hexid('bf-ra'), {'relation_name': 'goal_of'})
    assert {b['item'] for b in call(admin, 'preflight')['blocking']} == {'assessment_name_collision'}


def test_erasing_consumers_and_a_purging_world_are_skipped(seeded):
    admin = seeded['admin']
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
            values(%s,'real','bf-erasure',%s,'owner','test','test','purging',0)""", (TENANT_A, seeded['a']))
        admin.execute("""insert into control.nexloop_sim_worlds(world,tenant_id,created_by,purpose,status,expires_at,request_id)
            values('sim:0000bf01-0000-4000-8000-000000000001',%s,'test','test','deleting',clock_timestamp()+interval '1 day','bf-sim-1')""", (TENANT_A,))
    sim = 'sim:0000bf01-0000-4000-8000-000000000001'
    stock(admin, 'Consumer', hexid('bf-sim-consumer'), {}, world=sim)
    stock(admin, 'Goal', hexid('bf-sim-goal'), {'consumer_id': hexid('bf-sim-consumer')}, world=sim)
    start(admin)
    drive(admin)
    counts = {(x['relation_name'], x['world']): x for x in call(admin, 'status', 'run-1')['counts']}
    assert counts[('goal_of', 'real')]['skipped_erasure'] == 3 and counts[('goal_of', 'real')]['created'] == 3
    assert counts[('goal_of', 'sim:0000bf01-0000-4000-8000-000000000001')]['skipped_world_purge'] == 1
    v = verify(admin)[('goal_of', 'real')]
    assert v['a'] == v['b'] == 3 and v['erasing'] == 3


def test_revert_removes_only_this_runs_relations(seeded):
    admin, goals = seeded['admin'], seeded['goals']
    derived = hexid('bf-goal-derived')
    with admin.transaction():
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Goal',%s,1,%s,'synthetic','p3',clock_timestamp(),clock_timestamp())""", (TENANT_A, derived, Jsonb({'consumer_id': seeded['a']})))
    start(admin)
    drive(admin)
    # goals[0]: the trigger drops the backfilled relation and derives the same relation id again (derived only): revert keeps it.
    target = admin.execute("select properties->>'consumer_id' from ontology.objects where object_id=%s", (goals[0],)).fetchone()[0]
    admin.execute("update ontology.objects set properties='{}' where object_id=%s", (goals[0],))
    admin.execute("update ontology.objects set properties=jsonb_build_object('consumer_id',%s::text) where object_id=%s", (target, goals[0]))
    start(admin, 'run-2')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # only a stopped run reverts
        call(admin, 'revert_batch', 'run-2', 10)
    call(admin, 'abort', 'run-2', 'test')
    while call(admin, 'revert_batch', 'run-1', 2)['state'] != 'reverted':
        pass
    assert all(relation(admin, g) == [] for g in goals[1:])
    assert relation(admin, goals[0]) == [(target, {'derived': True})]
    assert relation(admin, derived) == [(seeded['a'], {'derived': True})]  # derived by the trigger: untouched
    assert call(admin, 'status', 'run-1')['state'] == 'reverted'

"""NX-041 sim world limits (owner decision 2026-10-21, as recommended): at most 20 active simulation or shadow worlds per tenant
(deleting and deleted worlds do not count; concurrent creations cannot pass the limit together), an expiry of at most 30 days
(default 30), and expired worlds deleted as a whole by the retention keeper through S2's delete path (summary kept 30 days); a
world whose deletion fails or stays blocked never holds up the others; real and test are untouched.

Real PostgreSQL on a clean catalog; the helpers of the S2 tests (the SQL handlers the governed entry calls).
"""
import threading
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A, TENANT_B
from test_nx041_sim_worlds_pg import call, create, db, delete, seed_rows, totals, world_row  # noqa: F401

LIMIT = 20


def body(request=None, days=30, kind='simulation'):
    out = {'operation': 'create_sim_world', 'request_id': request or uuid.uuid4().hex, 'kind': kind, 'purpose': '上限实验'}
    if days is not None:
        out['expires_in_days'] = days
    return out


def make(admin, tenant=TENANT_A, **kw):
    return call(admin, 'nexloop_governed_create_sim_world', body(**kw), tenant)


def refused(admin, reason, tenant=TENANT_A, **kw):
    with pytest.raises(psycopg.errors.InvalidParameterValue, match=f'sim world: {reason}'), admin.transaction():
        make(admin, tenant, **kw)
    return True


def active(admin, tenant=TENANT_A):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    return admin.execute("select count(*) from control.nexloop_sim_worlds where tenant_id=%s and status='active'", (tenant,)).fetchone()[0]


def expire(admin, world):
    """The world's expiry moved into the past (trusted configuration with triggers off; the registry guard fixes expires_at)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update control.nexloop_sim_worlds set expires_at=clock_timestamp()-interval '1 minute',created_at=clock_timestamp()-interval '2 days' where world=%s", (world,))


def sweep(admin, tenant=TENANT_A):
    return admin.execute("select runtime.nexloop_retention_sweep(%s,'real','sim_world_summary',100,'sweep:limits','service:retention_keeper')",
                         (tenant,)).fetchone()[0]


def test_the_21st_active_world_is_refused_and_a_deleted_or_deleting_one_frees_a_place(db):
    worlds = [make(db, kind='shadow' if i % 2 else 'simulation', request=f'r{i}')['world'] for i in range(LIMIT)]
    assert active(db) == LIMIT
    assert refused(db, 'limit_reached') and refused(db, 'limit_reached', kind='shadow')
    # A replayed request still answers with its world at the limit; another tenant has its own count.
    assert make(db, request='r2') == {**make(db, request='r2'), 'created': False}
    assert make(db, tenant=TENANT_B)['created'] is True
    # Deleting one frees a place (the deleted world does not count) ...
    assert delete(db, worlds[0])['status'] == 'deleted'
    extra = make(db)['world']
    assert refused(db, 'limit_reached') and active(db) == LIMIT
    # ... and so does a world still deleting.
    db.execute("update control.nexloop_sim_worlds set status='deleting' where world=%s", (extra,))
    assert make(db)['created'] is True and refused(db, 'limit_reached')


def test_concurrent_creations_never_pass_the_limit(db, pg):
    for i in range(LIMIT - 1):
        make(db, request=f'pre{i}')
    # Two transactions: the second waits on the first's lock and, once it commits, sees the 20th world and is refused.
    first, second = psycopg.connect(pg), psycopg.connect(pg)
    try:
        call(first, 'nexloop_governed_create_sim_world', body(request='race-1'))  # uncommitted
        outcome = {}

        def race():
            try:
                call(second, 'nexloop_governed_create_sim_world', body(request='race-2')); second.commit(); outcome['ok'] = True
            except psycopg.Error as e:
                second.rollback(); outcome['error'] = e
        thread = threading.Thread(target=race)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive()  # waits on the first transaction's lock
        first.commit()
        thread.join(10)
        assert isinstance(outcome.get('error'), psycopg.errors.InvalidParameterValue) and 'limit_reached' in str(outcome['error'])
    finally:
        first.close(); second.close()
    assert active(db) == LIMIT
    # A burst: with 15 active, eight concurrent creations give exactly five worlds.
    for w in [r[0] for r in db.execute("select world from control.nexloop_sim_worlds where tenant_id=%s and status='active' limit 5", (TENANT_A,)).fetchall()]:
        delete(db, w)
    results, barrier = [], threading.Barrier(8)

    def burst(i):
        with psycopg.connect(pg, autocommit=True) as conn:
            barrier.wait()
            try:
                results.append(call(conn, 'nexloop_governed_create_sim_world', body(request=f'burst{i}'))['created'])
            except psycopg.errors.InvalidParameterValue as e:
                results.append(str(e).split('\n')[0])
    threads = [threading.Thread(target=burst, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(map(str, results)) == ['True'] * 5 + ['sim world: limit_reached'] * 3
    assert active(db) == LIMIT


def test_expiry_defaults_to_30_days_and_longer_is_refused(db):
    for days in (31, 90, 365):
        assert refused(db, 'expiry_over_30_days', days=days)
    for bad in (0, 366, '30'):
        assert refused(db, 'invalid', days=bad)
    span = lambda w: db.execute('select expires_at-created_at from control.nexloop_sim_worlds where world=%s', (w,)).fetchone()[0].days  # noqa: E731
    assert span(make(db, days=None)['world']) == 30
    assert span(make(db, days=30)['world']) == 30 and span(make(db, days=7)['world']) == 7
    from nexloop_eios.workbench_actions_http import OPERATIONS
    with pytest.raises(ValueError):
        OPERATIONS['create_sim_world']['expires_in_days'](31)


def test_the_keeper_deletes_expired_worlds_as_a_whole_with_a_summary_and_never_touches_real_or_test(db):
    gone, kept_world = make(db, days=1)['world'], make(db)['world']
    other = make(db, tenant=TENANT_B, days=1)['world']
    seed_rows(db, TENANT_A, gone, 3, 'gone')
    for tenant, world in ((TENANT_A, 'real'), (TENANT_A, 'test'), (TENANT_B, 'real'), (TENANT_A, kept_world), (TENANT_B, other)):
        seed_rows(db, tenant, world, 2, 'keep')
    before = totals(db, ['real', 'test', kept_world, other])
    expire(db, gone)
    expire(db, other)  # another tenant's expired world waits for that tenant's own pass
    result = sweep(db)
    assert result['counts']['worlds_expired'] == 1 and result['counts']['worlds_failed'] == 0
    assert world_row(db, gone)[1:] == ('deleted', True) and world_row(db, kept_world)[1] == 'active'
    assert sum(totals(db, [gone]).values()) == 0
    summary = db.execute('select counts,completed_at is not null from control.nexloop_sim_world_summaries where world=%s', (gone,)).fetchone()
    assert summary[1] is True and summary[0]['runtime.nexloop_work_feed'] == 3
    # Through S2's delete path: ledger entry and audit event under the keeper's principal; a second pass changes nothing.
    assert db.execute("select count(*) from control.nexloop_deletion_ledger where subject_ref=%s", (gone,)).fetchone()[0] == 1
    assert db.execute("select actor_id from runtime.audit_events where event_id=%s", ('sim-world:delete:' + gone,)).fetchone()[0] == 'service:retention_keeper'
    assert sweep(db)['counts']['worlds_expired'] == 0
    after = totals(db, ['real', 'test', kept_world, other])
    assert {t for t in before if after[t] != before[t]} <= {'runtime.nexloop_erasure_tombstones', 'control.nexloop_deletion_ledger'}
    assert all(after[t] >= before[t] for t in before) and world_row(db, other)[1] == 'active'
    assert sweep(db, TENANT_B)['counts']['worlds_expired'] == 1 and world_row(db, other)[1] == 'deleted'
    # The summary still leaves 30 days after the deletion completed (S2's retention unchanged).
    db.execute("update control.nexloop_sim_world_summaries set completed_at=completed_at-interval '31 days' where world=%s", (gone,))
    assert sweep(db)['processed'] == 1


def test_a_failing_or_blocked_world_never_holds_up_the_others(db):
    failing, blocked, fine = (make(db, days=1)['world'] for _ in range(3))
    for w in (failing, blocked, fine):
        seed_rows(db, TENANT_A, w, 2, 'gone')
        expire(db, w)
    # Test-only triggers: starting the deletion of one world raises; deleting another world's work feed rows raises (blocked table).
    db.execute(f"""create function public.zz_fail() returns trigger language plpgsql as $$begin
        if tg_table_name='nexloop_sim_worlds' and old.world='{failing}' then raise exception 'zz start fails';end if;
        if tg_table_name='nexloop_work_feed' and old.world='{blocked}' then raise exception 'zz table blocked';end if;
        return case when tg_op='DELETE' then old else new end;end $$""")
    db.execute('create trigger zz_fail before update on control.nexloop_sim_worlds for each row execute function public.zz_fail()')
    db.execute('create trigger zz_fail before delete on runtime.nexloop_work_feed for each row execute function public.zz_fail()')
    result = sweep(db)
    assert result['counts']['worlds_expired'] == 2 and result['counts']['worlds_failed'] == 1
    assert world_row(db, failing)[1] == 'active' and world_row(db, blocked)[1] == 'deleting' and world_row(db, fine)[1] == 'deleted'
    progress = db.execute('select progress from control.nexloop_sim_world_summaries where world=%s', (blocked,)).fetchone()[0]
    assert 'runtime.nexloop_work_feed' in progress['blocked']
    db.execute('drop trigger zz_fail on control.nexloop_sim_worlds')
    db.execute('drop trigger zz_fail on runtime.nexloop_work_feed')
    result = sweep(db)
    assert result['counts']['worlds_expired'] == 1 and world_row(db, failing)[1] == 'deleted' and world_row(db, blocked)[1] == 'deleted'


def test_rotation_continues_the_least_recently_continued_deleting_worlds_first(db):
    """Eleven worlds stuck deleting with ten passes per round: the one left out of a round comes first in the next."""
    worlds = [make(db)['world'] for _ in range(11)]
    for w in worlds:
        seed_rows(db, TENANT_A, w, 1, 'gone')
    db.execute("""create function public.zz_block() returns trigger language plpgsql as $$begin
        if old.world like 'sim:%' then raise exception 'zz table blocked';end if;return old;end $$""")
    db.execute('create trigger zz_block before delete on runtime.nexloop_work_feed for each row execute function public.zz_block()')
    for w in worlds:
        assert delete(db, w)['status'] == 'deleting'
    at = lambda: dict(db.execute('select world,progress->>\'at\' from control.nexloop_sim_world_summaries where world=any(%s)', (worlds,)).fetchall())  # noqa: E731
    first = at()
    sweep(db)
    second = at()
    left_out = [w for w in worlds if second[w] == first[w]]
    assert len(left_out) == 1 and sweep(db)['counts']['worlds_resumed'] == 10
    assert at()[left_out[0]] != second[left_out[0]]

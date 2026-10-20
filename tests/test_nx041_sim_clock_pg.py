"""NX-041 S5 (design §4, D5, §11 S5): every simulation or shadow world has its own clock (paused at creation; advance, pause, resume,
rate), changed only forward through the owner's governed Action with an audit row per change; the time-dependent logic reads the
world time in a simulation world (commitment due, plan reassessment, outreach approval expiry, quiet hours and frequency windows, the
7-day metrics), while real and test keep reading the wall exactly as before.

Real PostgreSQL on a clean catalog. Admin calls the SQL handlers the governed entry calls (as test_nx041_sim_worlds_pg) and seeds rows
as trusted configuration. The real and test regression of the converted functions is their own suites (commitments, plan
re-evaluation, outreach send/runs/approval/metrics), run unchanged; here real and test are checked against the wall directly.
"""
import hashlib
import time
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

DAY = 86400
CONSUMER = 'c' * 64
CONVERTED = ('authz.nexloop_commitment_command', 'runtime.nexloop_commitment_touch', 'runtime.nexloop_plan_mark_due', 'authz.nexloop_plan_feed_touch',
             'runtime.nexloop_outreach_accept', 'control.nexloop_governed_outreach_approve', 'runtime.nexloop_outreach_expire_approvals',
             'authz.nexloop_outreach_command', 'runtime.nexloop_outreach_metrics')


def call(admin, fn, body, tenant=TENANT_A, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (tenant, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def create(admin, kind='simulation', tenant=TENANT_A):
    return call(admin, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': kind,
                                                             'purpose': '时钟实验', 'expires_in_days': 30}, tenant)['world']


def clock(admin, world, action, tenant=TENANT_A, request=None, **extra):
    return call(admin, 'nexloop_governed_control_sim_clock', {'operation': 'control_sim_clock', 'request_id': request or uuid.uuid4().hex,
                                                              'world': world, 'action': action, 'reason': '推进模拟', **extra}, tenant)


def refused(admin, code, world, action, tenant=TENANT_A, **extra):
    with pytest.raises(psycopg.errors.InvalidParameterValue, match=f'sim clock: {code}'), admin.transaction():
        clock(admin, world, action, tenant, **extra)
    return True


def now(admin, world, tenant=TENANT_A):
    return admin.execute('select runtime.nexloop_world_now(%s,%s)', (tenant, world)).fetchone()[0]


def wall(admin):
    return admin.execute('select clock_timestamp()').fetchone()[0]


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin


def send(admin, world, *, state='pending_approval', admitted=False, frequency='service', tenant=TENANT_A):
    """An outreach intent as the acceptance writes it (seeded; created_at is stamped with the world time in a simulation world)."""
    intent = 'outreach:' + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    admin.execute("""insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,
        channel,origin_ref,body,body_digest,initiator,principal_id,request_intent,control_snapshot,state,admitted_at)
        values(%s,%s,%s,%s,'service.followup',1,'service.followup',%s,'native.webchat','owner:fixture','你好',%s,'human','owner-principal','fixture','{}',%s,
         case when %s then runtime.nexloop_world_now(%s,%s) end)""",
                  (tenant, world, intent, CONSUMER, frequency, 'd' * 64, state, admitted, tenant, world))
    return intent


def approval(admin, world, intent, tenant=TENANT_A):
    admin.execute("""insert into runtime.nexloop_outreach_approvals(tenant_id,world,intent_id,requested_by,expires_at)
        values(%s,%s,%s,'owner-principal',runtime.nexloop_world_now(%s,%s)+runtime.nexloop_outreach_approval_window())""", (tenant, world, intent, tenant, world))


def approval_state(admin, world, intent):
    return admin.execute('select a.decision,s.state,s.refusal_code from runtime.nexloop_outreach_approvals a join runtime.nexloop_outreach_sends s using(tenant_id,world,intent_id) '
                         'where a.world=%s and a.intent_id=%s', (world, intent)).fetchone()


def target(admin, world, due, at=None, tenant=TENANT_A):
    """The commitment decision the keeper's evaluate verb takes at the world time (authz.nexloop_commitment_command passes v_now)."""
    props = {'status': 'open', 'fulfillment_basis': 'effect', 'promised_at': '2026-01-01T00:00:00Z', 'due_at': due.isoformat()}
    return admin.execute('select runtime.nexloop_commitment_target(%s,%s,%s,%s,coalesce(%s,runtime.nexloop_world_now(%s,%s)))',
                         (tenant, world, 'e' * 64, Jsonb(props), at, tenant, world)).fetchone()[0]


def test_each_simulation_world_has_its_own_clock_paused_at_creation_while_real_and_test_read_the_wall(db):
    a, b, shadow = create(db), create(db), create(db, kind='shadow')
    created = db.execute('select created_at from control.nexloop_sim_worlds where world=%s', (a,)).fetchone()[0]
    assert now(db, a) == created  # D5: never the wall by default
    time.sleep(0.2)
    assert now(db, a) == created and now(db, shadow) == db.execute('select created_at from control.nexloop_sim_worlds where world=%s', (shadow,)).fetchone()[0]
    assert db.execute('select operation,revision,paused,principal_id from control.nexloop_sim_clock_events where world=%s', (a,)).fetchall() == [
        ('created', 1, True, 'owner-principal')]
    # One world's clock moves; the other's does not.
    moved = clock(db, a, 'advance', seconds=3600)
    assert moved['changed'] is True and moved['revision'] == 2 and now(db, a) == created + timedelta(hours=1)
    assert now(db, b) == db.execute('select created_at from control.nexloop_sim_worlds where world=%s', (b,)).fetchone()[0]
    # Real and test: exactly the wall, and the wall moment of a time is the time itself.
    for world in ('real', 'test'):
        before = wall(db)
        assert before <= now(db, world) <= wall(db)
        assert db.execute('select runtime.nexloop_world_wall_at(%s,%s,%s)', (TENANT_A, world, created)).fetchone()[0] == created
    # A world of another tenant has no clock here (no wall fallback either).
    other = create(db, tenant=TENANT_B)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match='world clock unavailable'), db.transaction():
        now(db, other, tenant=TENANT_A)


def test_the_clock_moves_forward_only_through_the_owner_action_and_every_change_is_audited(db):
    world = create(db)
    start = now(db, world)
    request = uuid.uuid4().hex
    first = clock(db, world, 'advance', request=request, seconds=30 * DAY)
    again = clock(db, world, 'advance', request=request, seconds=30 * DAY)
    assert (again['changed'], again['revision'], again['world_time']) == (False, first['revision'], first['world_time'])  # a retry
    assert now(db, world) == start + timedelta(days=30)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='request_reused'), db.transaction():
        clock(db, world, 'advance', request=request, seconds=60)
    # Real and test have no clock; the data mode, malformed and unregistered worlds are not simulation worlds here.
    for target_world in ('real', 'test', 'simulation', 'sim:1'):
        assert refused(db, 'not_a_simulation_world', target_world, 'pause')
    assert refused(db, 'world_unavailable', 'sim:' + str(uuid.uuid4()), 'pause')
    assert refused(db, 'world_unavailable', create(db, tenant=TENANT_B), 'pause')  # another tenant's world
    # Only forward: no zero, negative or fractional step, no zero rate; pausing twice or resuming a running clock is refused.
    for extra in ({'seconds': 0}, {'seconds': -60}, {'seconds': 1.5}, {'seconds': 315360001}):
        assert refused(db, 'invalid', world, 'advance', **extra)
    for rate in (0, -1, 100001):
        assert refused(db, 'invalid', world, 'set_rate', rate=rate)
    assert refused(db, 'already_paused', world, 'pause')
    assert refused(db, 'invalid', world, 'pause', seconds=60)  # parameters belong to their action
    # The Action acts from the owner's real world only.
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='owner_world_only'), db.transaction():
        call(db, 'nexloop_governed_control_sim_clock', {'operation': 'control_sim_clock', 'request_id': 'x', 'world': world, 'action': 'resume',
                                                         'reason': 'x'}, world='test')
    # No other writer: a direct change, even with a later anchor, is refused; so is going back with the Action's flag.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='forward only'), db.transaction():
        db.execute("update control.nexloop_sim_clocks set sim_at=sim_at+interval '1 day',revision=revision+1 where world=%s", (world,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='forward only'), db.transaction():
        db.execute("select set_config('nexloop.sim_clock_change','on',true)")
        db.execute("update control.nexloop_sim_clocks set sim_at=sim_at-interval '1 second',anchor_at=clock_timestamp(),revision=revision+1 where world=%s", (world,))
    for sql in ("update control.nexloop_sim_clock_events set reason='x' where world=%s", 'delete from control.nexloop_sim_clock_events where world=%s',
                'delete from control.nexloop_sim_clocks where world=%s'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(sql, (world,))
    # Every change: an event per revision and an audit event; nothing for refusals.
    clock(db, world, 'set_rate', rate=60)
    clock(db, world, 'resume')
    clock(db, world, 'pause')
    events = db.execute('select revision,operation,seconds,rate,paused,principal_id,reason from control.nexloop_sim_clock_events where world=%s order by revision',
                        (world,)).fetchall()
    assert [(e[0], e[1]) for e in events] == [(1, 'created'), (2, 'advance'), (3, 'set_rate'), (4, 'resume'), (5, 'pause')]
    assert events[1][2] == 30 * DAY and float(events[2][3]) == 60 and events[3][4] is False and events[4][4] is True
    assert all(e[5] == 'owner-principal' and (e[1] == 'created' or e[6] == '推进模拟') for e in events)
    assert db.execute("select array_agg(action order by (payload->>'revision')::integer) from runtime.audit_events where trace_id='sim_clock' and payload->>'world'=%s",
                      (world,)).fetchone()[0] == ['nexloop.sim_clock.advance', 'nexloop.sim_clock.set_rate', 'nexloop.sim_clock.resume', 'nexloop.sim_clock.pause']


def test_a_running_clock_goes_at_its_rate_and_a_paused_one_stands(db):
    world = create(db)
    clock(db, world, 'set_rate', rate=1000)
    clock(db, world, 'resume')
    first, w1 = now(db, world), wall(db)
    time.sleep(0.3)
    second, w2 = now(db, world), wall(db)
    assert (second - first).total_seconds() >= 0.9 * 1000 * (w2 - w1).total_seconds() - 1
    clock(db, world, 'pause')
    frozen = now(db, world)
    time.sleep(0.2)
    assert now(db, world) == frozen


def test_advancing_thirty_days_expires_the_windows_of_the_simulation_world_but_not_of_real_at_the_same_moment(db):
    world = create(db)
    sim_start, real_start = now(db, world), wall(db)
    # Commitments: due in ten days in each world.
    sim_due, real_due = sim_start + timedelta(days=10), real_start + timedelta(days=10)
    assert target(db, world, sim_due) == {} and target(db, 'real', real_due) == {}
    # Outreach in real: an approval pending (24 h window), an admitted marketing send (frequency and the 7-day windows). A simulation
    # world holds no outreach send at all (S4: the real effect path refuses a simulation world; only the stand-in executes).
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real effect path refused'), db.transaction():
        send(db, world)
    real_pending = send(db, 'real')
    approval(db, 'real', real_pending)
    send(db, 'real', state='dispatching', admitted=True, frequency='marketing')
    metrics = lambda w: db.execute('select runtime.nexloop_outreach_metrics(%s,%s)', (TENANT_A, w)).fetchone()[0]
    contacts = lambda w, hours: db.execute('select runtime.nexloop_outreach_contacts_since(%s,%s,%s,runtime.nexloop_world_now(%s,%s)-make_interval(hours=>%s))',
                                           (TENANT_A, w, CONSUMER, TENANT_A, w, hours)).fetchone()[0]
    assert metrics('real')['accepted_7d'] == 2 and contacts('real', 24) == 1

    clock(db, world, 'advance', seconds=30 * DAY)
    assert now(db, world) == sim_start + timedelta(days=30)
    # The simulation world: the commitment is breached; its 7-day windows end at the world time.
    assert target(db, world, sim_due) == {'status': 'breached'}
    assert metrics(world)['accepted_7d'] == 0
    # Real at the same moment: nothing moved.
    assert target(db, 'real', real_due) == {}
    assert db.execute('select runtime.nexloop_outreach_expire_approvals(%s,%s)', (TENANT_A, 'real')).fetchone()[0] == 0
    assert approval_state(db, 'real', real_pending) == (None, 'pending_approval', None)
    assert metrics('real')['accepted_7d'] == 2 and contacts('real', 24) == 1


def test_while_the_clock_is_paused_nothing_falls_due_in_the_simulation_world(db):
    world = create(db)
    start = now(db, world)
    soon = start + timedelta(seconds=1)
    time.sleep(1.2)  # the wall passes the due time
    assert target(db, world, soon) == {}
    clock(db, world, 'advance', seconds=2)
    assert target(db, world, soon) == {'status': 'breached'}


def test_world_due_times_are_scheduled_on_the_wall_and_rescheduled_by_every_clock_change(db):
    world = create(db)
    start = now(db, world)
    wall_at = lambda at: db.execute('select runtime.nexloop_world_wall_at(%s,%s,%s)', (TENANT_A, world, at)).fetchone()[0]
    never = lambda at: db.execute("select runtime.nexloop_world_wall_at(%s,%s,%s)='infinity'", (TENANT_A, world, at)).fetchone()[0]
    # Paused: a later world time is never reached on the wall; a past one is due now.
    assert never(start + timedelta(hours=1))
    before = wall(db)
    assert before <= wall_at(start) <= wall(db)
    clock(db, world, 'set_rate', rate=3600)
    clock(db, world, 'resume')
    w = wall(db)
    assert abs((wall_at(now(db, world) + timedelta(hours=1)) - w).total_seconds() - 1) < 0.5  # one world hour is one wall second
    clock(db, world, 'pause')
    # The commitment monitor item is put at the wall moment of its due time (infinity while paused) and made due by the next change.
    commitment = 'f' * 64
    db.execute('select runtime.nexloop_commitment_touch(%s,%s,%s,%s)', (TENANT_A, world, commitment, now(db, world) + timedelta(days=3)))
    db.execute('select runtime.nexloop_commitment_touch(%s,%s,%s,%s)', (TENANT_A, 'real', commitment, wall(db) + timedelta(days=3)))
    item = lambda w: db.execute("select available_at from runtime.nexloop_work_feed where world=%s and feed='commitment-monitor' and item_key=%s",
                                (w, 'commitment:' + commitment)).fetchone()[0]
    assert db.execute("select available_at='infinity' from runtime.nexloop_work_feed where world=%s and feed='commitment-monitor'", (world,)).fetchone()[0]
    real_at = item('real')
    assert (real_at - wall(db)).days in (2, 3)  # real: the wall due time itself
    result = clock(db, world, 'advance', seconds=4 * DAY)
    assert result['requeued'] == 1 and item(world) <= wall(db) and item('real') == real_at


def test_only_the_time_expressions_changed_and_a_deleted_world_takes_its_clock(db):
    for name in CONVERTED:
        source = db.execute('select prosrc from pg_proc where oid=%s::regprocedure', (db.execute('select %s::regproc::regprocedure::text', (name,)).fetchone()[0],)).fetchone()[0]
        assert 'nexloop_world_now' in source or 'nexloop_world_wall_at' in source, name
    # The leases and the audit time stay the wall in the dispatch.
    source = db.execute("select prosrc from pg_proc where oid='authz.nexloop_outreach_command'::regproc").fetchone()[0]
    assert "lease_until=clock_timestamp()+interval '30 seconds'" in source and "lease_until<=clock_timestamp()" in source
    world = create(db)
    clock(db, world, 'advance', seconds=60)
    deleted = call(db, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': world, 'reason': '实验结束'})
    assert deleted['status'] == 'deleted' and deleted['blocked'] == {}
    assert db.execute('select count(*) from control.nexloop_sim_clocks where world=%s', (world,)).fetchone()[0] == 0
    assert db.execute('select count(*) from control.nexloop_sim_clock_events where world=%s', (world,)).fetchone()[0] == 0
    counts = db.execute('select counts from control.nexloop_sim_world_summaries where world=%s', (world,)).fetchone()[0]
    assert counts == {'control.nexloop_sim_clock_events': 2, 'control.nexloop_sim_clocks': 1}
    assert refused(db, 'world_unavailable', world, 'resume')

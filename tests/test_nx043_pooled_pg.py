"""NX-043 pooled results (owner 2026-10-21 on the D2 / D6 conflict): rounds of one strategy comparison pooled through
nexloop.experiment.report kind pooled. Two rounds of 20 per arm report together; another strategy version or maturity definition is
refused; a Consumer in two rounds counts once (earliest allocation); a pool change below the minimum increase gives no new result.
Allocations are written directly (X4 caps a round at 20 candidates); synthetic ids."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx043_experiments_pg import db, publish, ready, register, registration, strategy  # noqa: F401
from test_nx043_reports_pg import pay, replica, sha

TODAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
PAYMENT = {'key': 'payment', 'direction': 'increase'}


def day(n):
    return TODAY - timedelta(days=n)


def online(admin, experiment, **changes):
    return register(admin, registration('real', None, experiment_id=experiment, stage='online',
                                        **{'primary_metric': PAYMENT, 'sample_size': {'candidate': 20, 'control': 20}, **changes}))


def allocate(admin, experiment, arm, ids, days_ago=16):
    for c in ids:
        replica(admin, """insert into control.nexloop_experiment_allocations(tenant_id,experiment_id,consumer_id,cluster_key,arm,allocated_at)
            values(%s,%s,%s,%s,%s,%s)""", (TENANT_A, experiment, c, c, arm, day(days_ago)))


def people(label, n):
    return [sha(f'pool-{label}-{i}') for i in range(n)]


def pooled(admin, experiments, as_of=None):
    return admin.execute("select control.nexloop_governed_read_experiment_report(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (TENANT_A, 'intent-' + uuid.uuid4().hex[:12], Jsonb({'operation': 'read_experiment_report', 'request_id': uuid.uuid4().hex,
                                                                              'kind': 'pooled', 'experiment_ids': experiments,
                                                                              'as_of': (as_of or TODAY).isoformat()}))).fetchone()[0]


@pytest.fixture
def rounds(ready, db):  # noqa: F811
    first = online(db, 'pool.r1')
    online(db, 'pool.r2')
    for exp in ('pool.r1', 'pool.r2'):
        allocate(db, exp, 'candidate', people(exp + '-c', 20))
        allocate(db, exp, 'control', people(exp + '-k', 20))
    for c in people('pool.r1-c', 8) + people('pool.r2-c', 8) + people('pool.r1-k', 4) + people('pool.r2-k', 4):
        pay(db, c, allocated_days_ago=16)
    return {'admin': db, 'notice': first.get('notice')}


def test_two_rounds_of_twenty_report_together(rounds):
    admin = rounds['admin']
    assert rounds['notice'].startswith('single_round_below_minimum_sample')  # the registration said so (and was not refused)
    single = admin.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)', (TENANT_A, 'pool.r1', TODAY, 'owner-principal')).fetchone()[0]
    assert single['reason'] == 'insufficient_sample'
    r = pooled(admin, ['pool.r1', 'pool.r2'])
    assert (r['conclusion'], r['level'], r['experiment_ids']) == ('reported', 'pooled', ['pool.r1', 'pool.r2'])
    assert (r['scores']['candidate']['matured'], r['scores']['candidate']['successes']) == (40, 16)
    assert (r['scores']['control']['matured'], r['scores']['control']['successes']) == (40, 8)
    assert r['difference']['estimate'] == 0.2 and 'Pooled over these experiments' in r['statement']
    assert pooled(admin, ['pool.r2', 'pool.r1'])['replayed'] is True  # the same pool and as_of: the frozen report


def test_another_strategy_version_or_maturity_definition_is_refused(rounds):
    admin = rounds['admin']
    publish(admin, strategy('followup.default', follow_up_delay_hours=36), request='control-v2')  # version 2 of the control
    online(admin, 'pool.other', control={'strategy_id': 'followup.default', 'version': 2})
    online(admin, 'pool.window', maturity={'window_days': 21})
    for other, code in (('pool.other', 'strategy_mismatch'), ('pool.window', 'maturity_mismatch')):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match=code), admin.transaction():
            pooled(admin, ['pool.r1', other])


def test_a_consumer_in_both_arms_is_left_out_and_one_arm_twice_counts_once(rounds):
    admin = rounds['admin']
    online(admin, 'pool.r3')
    twice = people('pool.r1-k', 20)  # round 1 controls, allocated again as round 3 candidates later
    allocate(admin, 'pool.r3', 'candidate', twice, days_ago=15)
    allocate(admin, 'pool.r3', 'control', people('pool.r3-k', 20), days_ago=15)
    r = pooled(admin, ['pool.r1', 'pool.r2', 'pool.r3'])
    # L6 pooled review 2.2: the 20 exposed to both strategies (round 1 control, round 3 candidate) are in neither arm.
    assert r['scores']['candidate']['matured'] == 40 and r['scores']['control']['matured'] == 40
    assert r['left_out'] == {'erased': 'none', 'cross_allocated': 'some'}
    single = admin.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)', (TENANT_A, 'pool.r3', TODAY, 'owner-principal')).fetchone()[0]
    assert single['left_out'] == {'erased': 'none', 'cross_allocated': 'some'}  # round 3 alone leaves them out too


def test_a_pool_change_below_the_minimum_increase_gives_no_new_result(rounds):
    admin = rounds['admin']
    pooled(admin, ['pool.r1', 'pool.r2'], day(1))
    online(admin, 'pool.r4')
    allocate(admin, 'pool.r4', 'candidate', people('pool.r4-c', 5), days_ago=16)
    allocate(admin, 'pool.r4', 'control', people('pool.r4-k', 5), days_ago=16)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='too_few_new_matured'), admin.transaction():
        pooled(admin, ['pool.r1', 'pool.r2', 'pool.r4'])  # an added experiment of 5 per arm: no new result
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='as_of_not_after_last'), admin.transaction():
        pooled(admin, ['pool.r1', 'pool.r2'], day(2))
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='window_not_on_grid'), admin.transaction():
        pooled(admin, ['pool.r1', 'pool.r2'], TODAY - timedelta(hours=3))
    allocate(admin, 'pool.r4', 'candidate', people('pool.r4-c2', 10), days_ago=16)
    allocate(admin, 'pool.r4', 'control', people('pool.r4-k2', 10), days_ago=16)
    assert pooled(admin, ['pool.r1', 'pool.r2', 'pool.r4'])['scores']['candidate']['matured'] == 55  # 15 more per arm



def tombstone(admin, consumer):
    """The NX-029 wiring of L5 (aeb5f43a): an erased Consumer's allocation rows carry one pseudonym (here written as that trigger does;
    the relaxed CHECK is L5's 0295 in the real catalog)."""
    pseudonym = 'pseudonym:' + uuid.uuid4().hex
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update control.nexloop_experiment_allocations set consumer_id=%s,cluster_key=%s where consumer_id=%s", (pseudonym, pseudonym, consumer))


@pytest.fixture
def relaxed(rounds):
    """Integration: L5's 0295 relaxes the CHECK in the real catalog (an erased Consumer's rows carry a pseudonym); no replay here."""
    admin = rounds['admin']
    definition = admin.execute("""select pg_get_constraintdef(oid) from pg_constraint where conrelid='control.nexloop_experiment_allocations'::regclass
        and conname='nexloop_experiment_allocations_consumer_id_check'""").fetchone()[0]
    assert 'pseudonym' in definition, definition
    return rounds


def test_an_erased_consumer_leaves_numerator_and_denominator(relaxed):
    """L6 pooled review 4.1: after the erasure the person is gone from the denominator, the tombstone is no unpaid Consumer, and the
    report only says some were left out."""
    admin = relaxed['admin']
    before = pooled(admin, ['pool.r1', 'pool.r2'], day(2))
    assert (before['scores']['candidate']['matured'], before['scores']['candidate']['successes']) == (40, 16)
    erased = people('pool.r1-c', 1)[0]  # a paying candidate of round 1
    tombstone(admin, erased)
    online(admin, 'pool.r5')
    allocate(admin, 'pool.r5', 'candidate', people('pool.r5-c', 11), days_ago=15)
    allocate(admin, 'pool.r5', 'control', people('pool.r5-k', 11), days_ago=15)
    after = pooled(admin, ['pool.r1', 'pool.r2', 'pool.r5'], day(1))
    # 40 - 1 erased + 11 new: the tombstone counts neither as matured nor as unpaid.
    assert (after['scores']['candidate']['matured'], after['scores']['candidate']['successes']) == (50, 15)
    assert after['left_out'] == {'erased': 'some', 'cross_allocated': 'none'}
    assert erased not in json.dumps(after) and 'pseudonym' not in json.dumps(after)
    # The single-experiment report leaves the tombstone out too.
    single = admin.execute('select runtime.nexloop_experiment_report_freeze(%s,%s,%s,%s)', (TENANT_A, 'pool.r1', TODAY, 'owner-principal')).fetchone()[0]
    assert single['left_out'] == {'erased': 'some', 'cross_allocated': 'none'}


def test_a_different_metric_direction_is_refused(rounds):
    """L6 pooled review 1.2: the whole primary metric (key and direction) must match."""
    admin = rounds['admin']
    online(admin, 'pool.down', primary_metric={'key': 'payment', 'direction': 'decrease'})
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='metric_mismatch'), admin.transaction():
        pooled(admin, ['pool.r1', 'pool.down'])


def test_the_same_consumer_id_in_another_world_is_no_cross_allocation(ready, db):  # noqa: F811
    """L6 review of bb876cb5: "the same comparison" is also the same world (as X4's in_same_comparison). A Consumer id met in an
    experiment of another world with the same strategy pair is not left out; in the same world it is (positive control)."""
    admin = db
    worlds = [admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject',%s,%s)",
                            (TENANT_A, 'i-' + uuid.uuid4().hex[:8], Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'shadow',
                                                                            'purpose': 'leave-out world', 'expires_in_days': 7}))).fetchone()[0]['world']
              for _ in range(2)]
    for exp, world in (('lw.a', worlds[0]), ('lw.b', worlds[1]), ('lw.c', worlds[0])):
        register(admin, registration(world, None, experiment_id=exp, stage='shadow', primary_metric=PAYMENT, sample_size={'candidate': 20, 'control': 20}))
    c = sha('lw-consumer')
    allocate(admin, 'lw.a', 'candidate', [c])
    allocate(admin, 'lw.b', 'control', [c])  # another world: no cross-allocation
    leave = lambda exp, arm: admin.execute('select runtime.nexloop_experiment_leave_out(%s,%s,%s,%s)', (TENANT_A, exp, c, arm)).fetchone()[0]  # noqa: E731
    assert leave('lw.a', 'candidate') is None and leave('lw.b', 'control') is None
    allocate(admin, 'lw.c', 'control', [c])  # the same world, the other arm: left out
    assert leave('lw.a', 'candidate') == 'cross_allocated'

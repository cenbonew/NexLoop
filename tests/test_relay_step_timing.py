"""Relay step timings (dispatcher 2026-10-13): per claimed message, the time of each MessageRelay step, numbers only, drained as
one histogram sample per step (kind relay_step); an idle tick records nothing; the SQL accepts known steps and numbers only."""
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.observability import RELAY_STEPS, StepClock, StepTimings, histogram_bucket


def test_step_clock_records_only_on_commit_and_total_only_for_a_queued_message():
    timings = StepTimings(RELAY_STEPS)
    now = {'t': 0.0}
    clock = StepClock(timings, clock=lambda: now['t'])
    for step, seconds in (('backlog', 0.01), ('claim', 0.02), ('goal_create', 0.3), ('not-a-step', 0.1), ('release', 0.05)):
        now['t'] += seconds
        clock.mark(step)
    assert timings.drain()[0] == {}  # nothing before commit
    clock.commit(total=True)
    samples, _ = timings.drain()
    assert set(samples) == {'backlog', 'claim', 'goal_create', 'release', 'total'}  # an unknown label is never recorded
    assert samples['goal_create']['calls'] == 1 and samples['goal_create']['max_ms'] == 300.0
    assert samples['goal_create'][f'h_{histogram_bucket(300.0)}'] == 1
    assert samples['total']['max_ms'] == 480.0  # claim -> release of the queued message, the unknown step included
    assert all(type(v) in (int, float) for sample in samples.values() for v in sample.values())
    idle = StepClock(timings, clock=lambda: now['t'])
    idle.mark('backlog')  # an idle tick never commits
    assert timings.drain()[0] == {}


def test_sql_accepts_relay_step_numbers_for_known_steps_only(admin, pg):
    bootstrap(admin)
    sample = {'calls': 2, 'timeouts': 0, 'errors': 0, 'p50_ms': 10.0, 'p95_ms': 20.0, 'max_ms': 20.0, 'h_25': 1, 'h_32': 1}
    with psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True) as db:
        record = lambda service, value: db.execute("select authz.nexloop_record_process_sample('relay_step',%s,'synthetic:1',now(),60,%s)", (service, Jsonb(value)))
        for step in RELAY_STEPS:
            record(step, sample)
        for service, value in (('drop_table', sample), ('claim', {**sample, 'message_id': 1}), ('claim', {'calls': 'one'}), ('claim', {'h_200': 1})):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                record(service, value)
    assert admin.execute("select count(*) from runtime.nexloop_process_samples where kind='relay_step'").fetchone() == (len(RELAY_STEPS),)

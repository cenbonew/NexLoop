"""NX-054 P3 slice 2 (the relation backfill port migration; nexloop_eios.relation_backfill): the backfill through its signed port as
its own service principal (nexloop_domain_worker, eios:action:nexloop.relation.backfill:1). The runner walks a run to completed
and verify balances; a batch replays by its intent; the port sets the 1 s bound and the engine refuses another; other roles and
malformed commands are refused. V-1 / V-4 (design §8): while a batch stretched by a slow derivation holds a Consumer's KEY SHARE,
a write that takes that Consumer FOR UPDATE under the real paths' default lock_timeout of 3000 ms (as the governed edit commit of
0015 does) waits and succeeds, and the whole batch stays under 1 s; the control group - the same lock held 3.5 s, as a batch
without the 750 ms early commit and the 1 s statement_timeout would - makes that write fail with 55P03. The write stands for the
edit's lock, not the whole edit Action."""
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
from support.tenants import TENANT_A
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
    loose._prepare = lambda db, payload: db.execute("select set_config('statement_timeout','5s',true)")
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        loose.batch('run-1', 'Goal', 'real', '', 2)
    # Another role holds no execute on the port.
    with open_core(make_conninfo(pg, user='nexloop_api')) as api:
        other = B.RelationBackfillPort(api, authenticate_service(api, port['token'], world='real'), port['signer'])
        with pytest.raises((PlanUnavailable, psycopg.errors.InsufficientPrivilege)):
            other.status('run-1')


def slow_derivation(admin, seconds):
    admin.execute(f"""create or replace function pg_temp.nexloop_bf_slow_row() returns trigger language plpgsql as
        $$begin perform pg_sleep({seconds});return null;end $$""")
    admin.execute("""create trigger nexloop_bf_slow_row after insert on control.nexloop_relation_backfill_created for each row
        execute function pg_temp.nexloop_bf_slow_row()""")


def edit_lock(pg, consumer, result, delay=0.15):
    """The governed edit's commit takes the edited object FOR UPDATE under the real paths' lock_timeout (3000 ms default)."""
    time.sleep(delay)
    with psycopg.connect(pg) as db:
        db.execute("set lock_timeout='3000ms'")
        t0 = time.monotonic()
        try:
            db.execute('select 1 from ontology.objects where object_id=%s for update', (consumer,))
            result['ok'] = True
        except psycopg.errors.LockNotAvailable:
            result['ok'] = False
        result['waited'] = time.monotonic() - t0


def test_v1_a_bounded_batch_never_fails_an_edit_and_v4_stays_under_one_second(port, admin, pg):
    p = port['port']
    p.start('run-1', p.preflight()['digest'])
    slow_derivation(admin, 0.2)  # each relation the batch creates: 0.2 s more, so the batch meets the 750 ms early commit
    try:
        result = {}
        worker = threading.Thread(target=edit_lock, args=(pg, port['b'], result))
        worker.start()
        got = p.batch('run-1', 'Goal', 'real', '', 200)
        worker.join(10)
    finally:
        admin.execute('drop trigger nexloop_bf_slow_row on control.nexloop_relation_backfill_created')
    assert got['early'] is True and got['elapsed_ms'] < 1000  # V-4: the whole batch, from its transaction's start
    assert result['ok'] is True and result['waited'] < 1.0  # the edit waited for the batch, then went through


def test_v1_control_group_an_unbounded_hold_fails_the_edit(port, admin, pg):
    """Without the early commit and with a statement_timeout above 3 s, a batch could hold the Consumer's KEY SHARE 3.5 s: the
    edit then fails with 55P03. So the experiment above passes because the bound holds, not because nothing conflicts."""
    result = {}
    worker = threading.Thread(target=edit_lock, args=(pg, port['b'], result))
    with psycopg.connect(pg) as holder:
        holder.execute('select 1 from ontology.objects where object_id=%s for key share', (port['b'],))
        worker.start()
        time.sleep(3.5)
        holder.rollback()
    worker.join(10)
    assert result['ok'] is False and result['waited'] >= 2.9

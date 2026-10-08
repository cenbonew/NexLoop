"""Real SIGKILL recovery of governed technical queues on disposable PostgreSQL.

Synthetic authority travels only through a private multiprocessing pipe/process
argument, never command-line arguments, files or logging. No formal objects are
written by privileged fixtures; the fixture owns only schema and authority setup.
"""
import multiprocessing
import os
import signal
import time
from contextlib import contextmanager

from psycopg.conninfo import make_conninfo
from nexloop_eios.assembly import open_core
from nexloop_eios.durable_queue import PostgresDurableQueue, StaleQueueLease
import pytest
from test_durable_queue import queues, event


def _worker(conninfo, session, signer, action, channel):
    with open_core(conninfo) as pool:
        queue = PostgresDurableQueue(pool, session, signer, queue='operations')
        channel.send(('ready', os.getpid()))
        if action == 'accept':
            result = event(queue, 'process-event')
        elif action == 'outbox':
            result = queue.claim_outbox(lease_seconds=1)
        else:
            raise ValueError('unknown synthetic process operation')
        channel.send(('committed', result))
        # The parent kills the still-live process after observing the committed
        # response. The connection is deliberately not gracefully closed.
        channel.recv()


@contextmanager
def worker(pg, port, action):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_worker, args=(
        make_conninfo(pg, user='nexloop_api' if action == 'accept' else 'nexloop_scheduler'),
        port.session, port.signer, action, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), 'restricted process did not initialize'
        assert parent.recv() == ('ready', process.pid)
        yield process, parent
    finally:
        if process.is_alive():
            os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        parent.close()
        assert not process.is_alive(), 'owned child process failed to exit'


def kill(process):
    assert process.is_alive()
    os.kill(process.pid, signal.SIGKILL)
    process.join(10)
    assert process.exitcode == -signal.SIGKILL


def test_committed_event_survives_sigkill_and_scheduler_reopen(queues, pg, admin):
    api, scheduler, _ = queues
    with worker(pg, api, 'accept') as (process, pipe):
        assert pipe.poll(20)
        state, accepted = pipe.recv()
        assert state == 'committed' and accepted['accepted']
        kill(process)
    assert event(api, 'process-event') == {**accepted, 'created': False}
    with open_core(make_conninfo(pg, user='nexloop_scheduler')) as pool:
        reopened = PostgresDurableQueue(pool, scheduler.session, scheduler.signer, queue='operations')
        job = reopened.claim()
        assert job['task_id'] == accepted['task_id']
        assert reopened.finish(task_id=job['task_id'], fence=job['fence'], status='succeeded')['status'] == 'succeeded'
        notification = reopened.claim_outbox()
        assert reopened.acknowledge_outbox(outbox_id=notification['outbox_id'], fence=notification['fence'])['delivered']
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0] == 0
    assert admin.execute('select status from runtime.invocations').fetchone()[0] == 'succeeded'


def test_outbox_leased_process_sigkill_reclaims_with_new_fence(queues, pg):
    api, scheduler, _ = queues
    event(api)
    with worker(pg, scheduler, 'outbox') as (process, pipe):
        assert pipe.poll(20)
        state, abandoned = pipe.recv()
        assert state == 'committed' and abandoned['fence'] == 1
        kill(process)
    time.sleep(1.1)
    recovered = scheduler.claim_outbox()
    assert recovered['outbox_id'] == abandoned['outbox_id']
    assert recovered['fence'] == abandoned['fence'] + 1
    with pytest.raises(StaleQueueLease):
        scheduler.acknowledge_outbox(outbox_id=abandoned['outbox_id'], fence=abandoned['fence'])
    assert scheduler.acknowledge_outbox(outbox_id=recovered['outbox_id'], fence=recovered['fence'])['delivered']


def test_sigkill_during_accept_transaction_has_no_ack_or_partial_ledger(queues, pg, admin):
    api, _, _ = queues
    # A fixture-owned fault suspends the transaction after ledger inserts but
    # before the Outbox insert and commit. It does not mutate business state.
    admin.execute("create function public.synthetic_queue_pause() returns trigger language plpgsql as $$begin perform pg_sleep(3);return new;end$$")
    admin.execute('grant execute on function public.synthetic_queue_pause() to nexloop_owner')
    admin.execute('create trigger synthetic_queue_pause before insert on runtime.nexloop_outbox for each row execute function public.synthetic_queue_pause()')
    with worker(pg, api, 'accept') as (process, pipe):
        deadline = time.monotonic() + 15
        sleeping = False
        while time.monotonic() < deadline:
            sleeping = admin.execute("select exists(select 1 from pg_stat_activity where usename='nexloop_api' and wait_event='PgSleep')").fetchone()[0]
            if sleeping:
                break
            time.sleep(.05)
        assert sleeping, 'restricted acceptance never reached the uncommitted fault'
        assert not pipe.poll(), 'acceptance was acknowledged before commit'
        kill(process)
    # DDL waits for the killed client's transaction to finish/roll back, so
    # the following checks inspect terminal database state, not just MVCC
    # invisibility while its transaction is still running.
    admin.execute('drop trigger synthetic_queue_pause on runtime.nexloop_outbox')
    for table in ['invocations', 'jobs', 'nexloop_inbox', 'nexloop_outbox', 'job_events']:
        assert admin.execute('select count(*) from runtime.' + table).fetchone()[0] == 0
    accepted = event(api, 'process-event')
    assert accepted['accepted'] and accepted['created']

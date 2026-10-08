"""A lease must remain valid through the final technical transaction writes."""
import pytest

from nexloop_eios.durable_queue import StaleQueueLease
from test_durable_queue import event, queues


def ledger_snapshot(admin):
    return tuple(
        admin.execute(
            'select coalesce(jsonb_agg(row_data order by row_data::text),\'[]\'::jsonb) '
            'from (select to_jsonb(t) row_data from runtime.' + table + ' t) snapshot'
        ).fetchone()[0]
        for table in ('invocations', 'jobs', 'job_events', 'nexloop_inbox', 'nexloop_outbox')
    )


@pytest.mark.parametrize('verb', ['claim', 'finish', 'renew', 'outbox_claim', 'outbox_ack'])
def test_expiry_during_last_writes_rolls_back_lease_transaction(queues, admin, verb):
    api, scheduler, _ = queues
    event(api)
    if verb in ('finish', 'renew'):
        lease = scheduler.claim(lease_seconds=1)
        if verb == 'finish':
            call = lambda: scheduler.finish(
                task_id=lease['task_id'], fence=lease['fence'],
                status='succeeded', result={'synthetic': 'must roll back'},
            )
        else:
            call = lambda: scheduler.renew(
                task_id=lease['task_id'], fence=lease['fence'], lease_seconds=30,
            )
    elif verb == 'outbox_ack':
        lease = scheduler.claim_outbox(lease_seconds=1)
        call = lambda: scheduler.acknowledge_outbox(
            outbox_id=lease['outbox_id'], fence=lease['fence'],
        )
    elif verb == 'claim':
        call = lambda: scheduler.claim(lease_seconds=1)
    else:
        call = lambda: scheduler.claim_outbox(lease_seconds=1)

    before = ledger_snapshot(admin)
    # Synthetic test-only DDL delays the already admitted DML. It does not
    # mutate queue/business rows and never touches a persistent environment.
    table = 'nexloop_outbox' if verb.startswith('outbox') else 'jobs'
    admin.execute(
        "create function public.synthetic_queue_commit_delay() returns trigger "
        "language plpgsql as $$begin perform pg_sleep(2);return NEW;end$$"
    )
    admin.execute(
        'create trigger synthetic_queue_delay after update on runtime.' + table +
        ' for each row execute function public.synthetic_queue_commit_delay()'
    )
    try:
        with pytest.raises(StaleQueueLease):
            call()
        # Includes all previous lease/fence, result/completion_digest, statuses,
        # timestamps, and events; no partial update or appended event survived.
        assert ledger_snapshot(admin) == before
    finally:
        admin.execute('drop trigger synthetic_queue_delay on runtime.' + table)

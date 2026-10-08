"""Real PG checks of trusted-server queue permit expiry; no business SQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
import time

import psycopg
import pytest

import nexloop_eios.durable_queue as module
from test_durable_queue import event, queues


def patch_expiry(monkeypatch, transform):
    # Modify the synthetic trusted-server claim before the normal HMAC signs it.
    # A denied result therefore proves claim validation, not a bad signature.
    original = module.canonical_payload

    def canonical(value):
        if isinstance(value, dict) and value.get('protocol') == 'nexloop-queue-command-v1':
            value = dict(value)
            transform(value)
        return original(value)

    monkeypatch.setattr(module, 'canonical_payload', canonical)


@pytest.mark.parametrize('expiry', ['missing', 'future'])
def test_validly_signed_unbounded_queue_permit_denied(queues, admin, monkeypatch, expiry):
    api, _, _ = queues

    def transform(claim):
        if expiry == 'missing':
            claim.pop('expires_at')
        else:
            claim['expires_at'] = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()

    patch_expiry(monkeypatch, transform)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='queue signature denied'):
        event(api)
    for table in ('invocations', 'jobs', 'nexloop_inbox', 'nexloop_outbox', 'job_events'):
        assert admin.execute('select count(*) from runtime.' + table).fetchone()[0] == 0


def test_queue_permit_expiring_while_waiting_for_inbox_lock_denied(queues, admin, pg, monkeypatch):
    api, _, _ = queues
    signed = threading.Event()
    deadlines = []

    def transform(claim):
        deadline = datetime.now(UTC) + timedelta(seconds=2)
        claim['expires_at'] = deadline.isoformat()
        deadlines.append(deadline)
        signed.set()

    patch_expiry(monkeypatch, transform)
    # The same exact synthetic tenant/world/source/event key used by acceptance.
    # The privileged fixture only holds a technical advisory lock; it writes no
    # ontology or queue state and uses the fixture's disposable PGDATA.
    with psycopg.connect(pg) as blocker, ThreadPoolExecutor(max_workers=1) as executor:
        blocker.execute(
            'select pg_advisory_xact_lock(hashtextextended(jsonb_build_array(%s::text,%s::text,%s::text,%s::text)::text,0))',
            (api.session.authentication.tenant_id, api.session.world, 'synthetic-channel', 'event-1'),
        )
        future = executor.submit(event, api)
        try:
            assert signed.wait(timeout=5), 'queue permit was never signed'
            poll_until = time.monotonic() + 5
            waiting = False
            while time.monotonic() < poll_until:
                waiting = admin.execute(
                    "select exists(select 1 from pg_stat_activity where usename='nexloop_api' and wait_event_type='Lock' and wait_event='advisory')"
                ).fetchone()[0]
                if waiting:
                    break
                if future.done():
                    future.result()
                    pytest.fail('acceptance completed without waiting for the held Inbox lock')
                time.sleep(0.01)
            assert waiting, 'actual PostgreSQL advisory-lock wait was not observed'
            while datetime.now(UTC) <= deadlines[0] + timedelta(milliseconds=100):
                time.sleep(0.01)
        finally:
            blocker.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='authority binding stale or invalid'):
            future.result(timeout=5)
    for table in ('invocations', 'jobs', 'nexloop_inbox', 'nexloop_outbox', 'job_events'):
        assert admin.execute('select count(*) from runtime.' + table).fetchone()[0] == 0

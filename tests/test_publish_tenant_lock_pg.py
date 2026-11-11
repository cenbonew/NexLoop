"""Stage 2026-11-11 14:29Z (R77, P5 r8-publish): the workbench Schema publication answered 503 after `canceling statement due to lock
timeout ... while locking tuple ... in relation "nexloop_tenants"`.

The mechanism, pinned here on the real workbench route:
- A publication writes ontology.object_type_versions and control.nexloop_action_definitions, whose authz.nexloop_authority_epoch_guard
  trigger raises the tenant's authority_revision: the publish transaction needs FOR NO KEY UPDATE on the tenant row.
- Every governed service authorization (0192) takes FOR SHARE on the same row until its transaction ends. Held longer than the API's
  lock_timeout (3 s), it cancels the publication: 503, everything rolled back, and the route logs the request id, method, exception type
  and SQLSTATE (55P03) - never a value. Held shorter, the publication waits and goes through.

The holder is simulated by a transaction holding FOR SHARE on the tenant row, as any governed authorization does. Which holder it was on
stage - the alert evaluation holding it through maintenance and snapshot - is fixed by alert_evaluate_split and pinned in
tests/test_alert_evaluate_split_pg.py: test_a_slow_maintenance_or_snapshot_holds_no_publication (no longer a holder) and
test_the_short_t3_still_holds_the_tenant_row_and_the_publication_waits_for_it (only T3 holds, the publication waits for exactly it).
Synthetic data."""
import logging
import threading
import time

import psycopg
from psycopg.conninfo import make_conninfo

from nx068_fixture import mark
from test_nx040_schema_publication_pg import proposal
from test_schema_publication_http_pg import BASE, post, setup
from test_workbench_read_pg import TENANT, browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

HOLD_LONG = 6.0  # well over the API lock_timeout (3 s) under CI load
HOLD_SHORT = 1.5


def publishable(w):
    setup(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    mark(w['admin'], TENANT, 'Consumer.create')
    return proposal({'admin': w['admin'], 'tenant': TENANT}, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})


def hold_share(pg, user, seconds, ready):
    """A governed authorization's lock: FOR SHARE on the tenant row, held for `seconds` in its own transaction."""
    with psycopg.connect(make_conninfo(pg, user=user)) as db:
        db.execute('select 1 from control.nexloop_tenants where tenant_id=%s for share', (TENANT,))
        ready.set()
        time.sleep(seconds)
        db.rollback()


def blockers(admin, deadline):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        rows = admin.execute("""select b.query from pg_stat_activity w cross join lateral unnest(pg_blocking_pids(w.pid)) h(pid)
            join pg_stat_activity b on b.pid=h.pid where w.query like '%nexloop_schema_proposal_publish%'""").fetchall()
        if rows:
            return [q for (q,) in rows]
        time.sleep(0.05)
    return []


def publish_while_held(w, pg, seconds, key, rationale):
    admin = w['admin']
    pid = publishable(w)
    ready, out = threading.Event(), {}
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        holder = threading.Thread(target=hold_share, args=(pg, admin.info.user, seconds, ready))
        holder.start()
        assert ready.wait(5)
        watcher = threading.Thread(target=lambda: out.update(blockers=blockers(admin, 2.5)))
        watcher.start()
        started = time.monotonic()
        answer = post(client, f'{BASE}/{pid}/publish', {'rationale': rationale}, key)
        out['waited'] = time.monotonic() - started
        watcher.join()
        holder.join()
    return pid, answer, out


def test_a_share_lock_longer_than_the_lock_timeout_cancels_the_publication_with_a_logged_503(workbench, pg, caplog):  # noqa: F811
    w = workbench
    admin = w['admin']
    caplog.set_level(logging.WARNING, logger='nexloop.workbench')
    pid, answer, out = publish_while_held(w, pg, HOLD_LONG, 'synthetic-lock-repro-0001', 'synthetic lock reproduction')
    assert answer.status_code == 503 and answer.json()['code'] == 'unavailable' and answer.json()['retryable'] is True
    assert 2.5 < out['waited'] < HOLD_LONG, out
    assert out['blockers'] and all('for share' in q for q in out['blockers']), out
    # Rolled back: no version, the proposal still frozen.
    assert admin.execute("select max(version) from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'", (TENANT,)).fetchone()[0] == 1
    assert admin.execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'frozen'
    # The log: the request (= the answer's trace_id), the route method, the exception type and 55P03; no value.
    logged = [r for r in caplog.records if r.getMessage().startswith('schema publication unavailable')]
    assert len(logged) == 1, [r.getMessage() for r in caplog.records]
    message = logged[0].getMessage()
    assert answer.json()['trace_id'] in message and "'sqlstate': '55P03'" in message and "'method': 'publish'" in message
    assert 'LockNotAvailable' in message or 'QueryCanceled' in message
    for value in ('synthetic lock reproduction', 'synthetic-lock-repro-0001', pid, 'preferred_store', TENANT):
        assert value not in message


def test_a_share_lock_shorter_than_the_lock_timeout_only_delays_the_publication(workbench, pg):  # noqa: F811
    """The control (L6 K1): the publication really waits for the holder (pg_blocking_pids names it, >= 0.5 s), then is published."""
    w = workbench
    _, answer, out = publish_while_held(w, pg, HOLD_SHORT, 'synthetic-lock-ctrl-00001', 'synthetic lock control')
    assert answer.status_code == 200 and answer.json()['published'] is True, answer.json()
    assert out['blockers'] and all('for share' in q for q in out['blockers']), out
    assert out['waited'] >= 0.5, out

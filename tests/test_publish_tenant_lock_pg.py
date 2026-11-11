"""Stage 2026-11-11 14:29Z (R77, P5 r8-publish): the workbench Schema publication answered 503 after `canceling statement due to lock
timeout ... while locking tuple ... in relation "nexloop_tenants"`. Reproduced here with the real alert evaluator and the real workbench route:

- A publication writes ontology.object_type_versions and control.nexloop_action_definitions, whose authz.nexloop_authority_epoch_guard
  trigger raises the tenant's authority_revision: the publish transaction needs FOR NO KEY UPDATE on the tenant row.
- Every governed service authorization (0192: "tenant changes ... must not resurrect an old permit") takes FOR SHARE on the same row
  and keeps it until its transaction ends. The alert evaluator's authz.nexloop_alert_evaluate is one: its transaction holds the share
  lock through the snapshot, the maintenance and the publish smoke rounds (0449).
- An evaluation longer than the API's lock_timeout (3 s) therefore cancels a concurrent publication: 503, everything rolled back, and the
  route now logs the request id, exception type and SQLSTATE (55P03) without any value.

Synthetic data; the long evaluation is simulated by a smoke runner that sleeps first (the evaluator code itself is unchanged)."""
import logging
import threading
import time

from nx068_fixture import mark
from test_nx040_schema_publication_pg import proposal
from test_observability_pg import TENANT, observed  # noqa: F401
from test_schema_publication_http_pg import BASE, post
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

SLOW = 4.5  # seconds: longer than the API lock_timeout (3 s), shorter than the evaluator's statement_timeout (10 s)


def publishable(o):
    admin = o['admin']
    mark(admin, TENANT, 'Consumer.create')
    return proposal({'admin': admin, 'tenant': TENANT}, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})


def tenant_row_modes(admin):
    return [m for (m,) in admin.execute("""select l.mode from pg_locks l join pg_stat_activity a on a.pid=l.pid
        where l.locktype='tuple' and l.relation='control.nexloop_tenants'::regclass""").fetchall()]


def slow_smokes(admin, seconds):
    admin.execute('alter function runtime.nexloop_run_schema_publish_smokes() rename to nexloop_run_schema_publish_smokes_fast')
    admin.execute(f"""create function runtime.nexloop_run_schema_publish_smokes() returns integer language plpgsql security definer
        set search_path=pg_catalog,pg_temp as $$ begin perform pg_sleep({seconds}); return runtime.nexloop_run_schema_publish_smokes_fast(); end $$""")
    admin.execute('alter function runtime.nexloop_run_schema_publish_smokes() owner to nexloop_owner')


def evaluate_in_background(o):
    out = {}

    def run():
        started = time.monotonic()
        out['result'] = o['evaluate']()
        out['seconds'] = time.monotonic() - started
    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def blocker_of_publication(admin):
    """While the publication waits: who holds what it waits for (pg_blocking_pids), and that holder's statement."""
    for _ in range(100):
        rows = admin.execute("""select w.query,b.usename,b.query from pg_stat_activity w cross join lateral unnest(pg_blocking_pids(w.pid)) h(pid)
            join pg_stat_activity b on b.pid=h.pid where w.query like '%nexloop_schema_proposal_publish%'""").fetchall()
        if rows:
            return rows
        time.sleep(0.05)
    return []


def test_a_long_alert_evaluation_holds_the_tenant_row_and_cancels_a_publication(observed, caplog):  # noqa: F811
    o = observed
    admin = o['admin']
    pid = publishable(o)
    slow_smokes(admin, SLOW)
    caplog.set_level(logging.WARNING, logger='nexloop.workbench')
    with o['w']['client']() as client:
        assert workbench_login(client, o['w']['owner']).status_code == 200
        thread, evaluation = evaluate_in_background(o)
        time.sleep(0.5)  # the evaluation is inside authz.nexloop_alert_evaluate, sleeping in its maintenance
        assert 'ShareLock' not in tenant_row_modes(admin)  # nobody waits yet
        holders = {}

        def watch():
            holders['rows'] = blocker_of_publication(admin)
        watcher = threading.Thread(target=watch)
        watcher.start()
        started = time.monotonic()
        answer = post(client, f'{BASE}/{pid}/publish', {'rationale': 'synthetic lock reproduction'}, 'synthetic-lock-repro-0001')
        waited = time.monotonic() - started
        watcher.join()
        thread.join()
    assert answer.status_code == 503 and answer.json()['code'] == 'unavailable' and answer.json()['retryable'] is True
    assert 2.5 < waited < SLOW, waited
    # The holder: the alert evaluator's governed evaluation (service role nexloop_domain_worker), not the smoke runner's own statement.
    assert holders['rows'] and all(user == 'nexloop_domain_worker' and 'authz.nexloop_alert_evaluate' in query for _, user, query in holders['rows']), holders
    assert evaluation['result'] and evaluation['seconds'] >= SLOW
    # Rolled back: no version, no publication, the proposal still frozen; the log names the request, the type and 55P03, no value.
    assert admin.execute("select max(version) from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'", (TENANT,)).fetchone()[0] == 1
    assert admin.execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'frozen'
    logged = [r for r in caplog.records if r.getMessage().startswith('schema publication unavailable')]
    assert len(logged) == 1, [r.getMessage() for r in caplog.records]
    message = logged[0].getMessage()
    assert answer.json()['trace_id'] in message and "'sqlstate': '55P03'" in message and "'method': 'publish'" in message
    assert 'LockNotAvailable' in message or 'QueryCanceled' in message
    for value in ('synthetic lock reproduction', 'synthetic-lock-repro-0001', pid, 'preferred_store', TENANT):
        assert value not in message


def test_a_short_evaluation_and_a_publication_both_go_through(observed):  # noqa: F811
    """The control: with an evaluation shorter than the lock_timeout the publication waits for it and is published."""
    o = observed
    admin = o['admin']
    pid = publishable(o)
    slow_smokes(admin, 1)
    with o['w']['client']() as client:
        assert workbench_login(client, o['w']['owner']).status_code == 200
        thread, evaluation = evaluate_in_background(o)
        time.sleep(0.3)
        answer = post(client, f'{BASE}/{pid}/publish', {'rationale': 'synthetic lock control'}, 'synthetic-lock-ctrl-00001')
        thread.join()
    assert answer.status_code == 200 and answer.json()['published'] is True, answer.json()
    assert evaluation['result']

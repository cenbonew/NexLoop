"""0205 (owner 2026-10-18, trial e80e360c follow-up): a reply whose result stayed unknown after its origin Run expired is
stopped for good by 0174/0199. The owner may order one more query (an order file and one command, run as the effect worker):
the effect worker queries the provider on its existing query-first path. Provider unavailable: still unknown, stopped again,
nothing sent. Provider has it: reconciled, the outbound reply becomes delivered. Never a second POST. Actual PG authority,
actual CLI, actual local JSON delivery provider."""
import json
import subprocess
import sys
import time

import psycopg
from psycopg.conninfo import make_conninfo

from nexloop_eios.backend import open_backend
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_outbound_messages_pg import chain, delivery_material, delivery_service, effect_worker, outbound
from test_trusted_configuration_pg import private


def requery(c, tmp_path, intents, reference):
    order = private(tmp_path, 'requery-order-' + reference.replace(':', '-'), json.dumps({'schema': 'nexloop-effect-requery-order/1', 'confirmation_ref': reference, 'intents': intents}))
    args = ['--database-url-file', str(c['effect_dsn']), *c['common'], '--service-credential-file', str(c['credentials']['assembly-executor']),
            '--artifact-root', str(tmp_path / 'requery-artifacts'), '--world', 'real', '--order-file', str(order)]
    run = subprocess.run([sys.executable, '-m', 'nexloop_eios.effect_requery', *args], capture_output=True, text=True, timeout=60)
    assert not any(h in run.stdout + run.stderr for h in c['hidden'])
    return run.returncode, [json.loads(line) for line in run.stdout.splitlines()]


def stopped(admin, intent):
    return admin.execute("select state,available_at='infinity'::timestamptz from runtime.nexloop_effect_outbox where intent_id=%s", (intent,)).fetchone()


def test_an_owner_ordered_requery_reconciles_a_stale_unknown_reply_and_never_resends(assembled_message, admin, tmp_path):  # noqa: F811
    import threading
    f = assembled_message
    with chain(f, admin, tmp_path) as c:
        tenant = c['tenant'];intent = c['receipts'][0]['intent_id']
        material = delivery_material(tmp_path, timeout=1);config, secret = material['provider_config'], material['secret']
        barrier = {'fsynced': threading.Event(), 'release': threading.Event()}

        def lease_expired():
            deadline = time.monotonic() + 10
            while not admin.execute('select lease_until is null or lease_until<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s', (intent,)).fetchone()[0]:
                assert time.monotonic() < deadline;time.sleep(.05)
        # As on stage (10/16 smoke): the reply reached the provider but its answer timed out; queries then found the provider down.
        with delivery_service(c, tmp_path, material, barrier=barrier):
            assert '"status":"unknown"' in effect_worker(c, tmp_path, config, secret, lease=3) and barrier['fsynced'].is_set()
        lease_expired()
        assert '"status":"unknown"' in effect_worker(c, tmp_path, config, secret, lease=3)
        assert outbound(admin, tenant)[0][1] == 'unknown'
        # Two days later: the origin Run credential expired and its unknown history is older than the 6 h query window (0199).
        admin.execute("update authz.nexloop_run_credentials set issued_at=clock_timestamp()-interval '2 days',expires_at=clock_timestamp()-interval '2 days'+interval '2 minutes' "
                      "where run_id=(select origin_run_id from runtime.nexloop_effect_intents where intent_id=%s)", (intent,))
        admin.execute("update runtime.nexloop_effect_events set created_at=created_at-interval '2 days' where intent_id=%s", (intent,))
        # Not stopped yet: nothing to arm (the worker's own path still owns it).
        code, lines = requery(c, tmp_path, [intent], 'owner-requery:0')
        assert code == 1 and lines == [{'intent_id': intent, 'armed': False, 'reason': 'not_stopped'}]
        with open_backend(database_url=make_conninfo(c['o']['pg'], user='nexloop_action_worker'), artifact_root=tmp_path / 'settle-artifacts',
                          signing_key_file=c['p']['backend_signing'], signing_key_id='explicit-configuration') as backend:
            executor = backend.authenticate(c['credentials']['assembly-executor'].read_text().strip(), world='real')
            assert executor.settle_expired_origin(intent_id=intent) == 'stopped'
        assert stopped(admin, intent) == ('unknown', True)
        # Order 1 while the provider is still down: armed, queried once, unknown again, stopped again. Nothing sent.
        code, lines = requery(c, tmp_path, [intent], 'owner-requery:1')
        assert code == 0 and lines == [{'intent_id': intent, 'armed': True, 'orders': 1}]
        assert stopped(admin, intent) == ('unknown', False)
        assert '"status":"unknown"' in effect_worker(c, tmp_path, config, secret, lease=3)
        assert stopped(admin, intent) == ('unknown', True) and outbound(admin, tenant)[0][1] == 'unknown'
        code, lines = requery(c, tmp_path, [intent], 'owner-requery:1')
        assert code == 1 and lines[0]['reason'] == 'reference_used'  # one query per owner reference
        # Order 2 with the provider back: the query finds the original export → reconciled, the reply delivered. One export only.
        code, lines = requery(c, tmp_path, [intent], 'owner-requery:2')
        assert code == 0 and lines[0]['armed'] is True and lines[0]['orders'] == 2
        with delivery_service(c, tmp_path, material):
            assert 'fulfilled' in effect_worker(c, tmp_path, config, secret, lease=3)
        assert outbound(admin, tenant)[0][1] == 'delivered' and len(list(material['root'].glob('*.export.json'))) == 1
        assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone() == (1,)
        # The provider's fulfilled answer is recorded (observed_fulfilled). Its governed finalization is the receipt reconciliation
        # (0065), which needs the executor's nexloop.service.receipt_reconcile grant: stage holds it (stage-principals grants[2]);
        # this local fixture does not, so the intent stays observed_fulfilled here while the reply itself is delivered.
        assert admin.execute("select state from runtime.nexloop_effect_intents where intent_id=%s", (intent,)).fetchone()[0] in ('observed_fulfilled', 'fulfilled', 'confirmed')
        assert admin.execute("select count(*) from runtime.nexloop_effect_observations where intent_id=%s and provider_state='fulfilled'", (intent,)).fetchone()[0] >= 1
        # The audit: both orders, the executor that placed them and the owner's references.
        assert admin.execute('select confirmation_ref from runtime.nexloop_effect_requery_orders where intent_id=%s order by ordered_at', (intent,)).fetchall() == [
            ('owner-requery:1',), ('owner-requery:2',)]
        code, lines = requery(c, tmp_path, [intent], 'owner-requery:3')
        assert code == 1 and lines[0]['reason'] == 'not_unknown' and lines[0]['state'] in ('observed_fulfilled', 'fulfilled', 'confirmed')


def test_only_the_effect_worker_role_may_order_and_the_order_file_is_strict(assembled_message, admin, tmp_path, pg):  # noqa: F811
    from nexloop_eios.effect_requery import load_order
    import pytest
    for role in ('nexloop_api', 'nexloop_domain_worker'):
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute('select authz.nexloop_effect_requery_order(%s,%s,%s,%s)', ('x' * 64, 'real', '00000000-0000-4000-8000-000000000000', 'owner:1'))
    good = {'schema': 'nexloop-effect-requery-order/1', 'confirmation_ref': 'owner-requery:1', 'intents': ['00000000-0000-4000-8000-000000000001']}
    assert load_order(private(tmp_path, 'good-order', json.dumps(good)))['intents'] == good['intents']
    for bad in (dict(good, schema='x'), dict(good, confirmation_ref='has space'), dict(good, intents=[]), dict(good, intents=['not-a-uuid']),
                dict(good, intents=good['intents'] * 2), dict(good, extra=1), dict(good, intents=[str(i) for i in range(17)])):
        with pytest.raises(ValueError):
            load_order(private(tmp_path, 'bad-order-' + str(abs(hash(json.dumps(bad, sort_keys=True)))), json.dumps(bad)))


from support.effect_provider import effect_provider  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401


def test_the_order_needs_a_stopped_unknown_intent_an_expired_origin_and_at_most_three_orders(governed_effect_executor, admin):  # noqa: F811
    from test_effect_dispatch import accepted
    from test_effect_origin_expired_pg import expire
    fixture = governed_effect_executor
    intent = accepted(fixture)['intent_id']
    admin.execute("update runtime.nexloop_effect_intents set state='unknown' where intent_id=%s", (intent,))
    admin.execute("update runtime.nexloop_effect_outbox set state='unknown',available_at='infinity' where intent_id=%s", (intent,))
    with fixture.current_executor() as executor:
        assert executor.requery_stale_unknown(intent_id=intent, confirmation_ref='owner:1')['reason'] == 'origin_current'
        expire(admin, fixture)
        for n in (1, 2, 3):
            assert executor.requery_stale_unknown(intent_id=intent, confirmation_ref=f'owner:{n}') == {'intent_id': intent, 'armed': True, 'orders': n}
            admin.execute("update runtime.nexloop_effect_outbox set available_at='infinity' where intent_id=%s", (intent,))  # the query left it stopped
        assert executor.requery_stale_unknown(intent_id=intent, confirmation_ref='owner:4')['reason'] == 'orders_exhausted'
    assert admin.execute('select count(*) from runtime.nexloop_effect_requery_orders where intent_id=%s', (intent,)).fetchone() == (3,)


def test_dispatch_controls_never_refuse_an_order_a_query_sends_nothing(governed_effect_executor, admin):  # noqa: F811
    """0206 (10/18 release 13): both owner orders were refused controls_refuse. A later control change (here a paused channel,
    which also supersedes the intent's control snapshot) and the real-dispatch deployment layer, unset outside the worker's
    admitting transaction, are dispatch checks; an order arms a query only, so it is armed."""
    from test_effect_dispatch import accepted
    from test_effect_origin_expired_pg import expire
    from test_nx022_dispatch_e2e import pause
    fixture = governed_effect_executor
    intent = accepted(fixture)['intent_id']
    admin.execute("update runtime.nexloop_effect_intents set state='unknown' where intent_id=%s", (intent,))
    admin.execute("update runtime.nexloop_effect_outbox set state='unknown',available_at='infinity' where intent_id=%s", (intent,))
    expire(admin, fixture)
    tenant, action = admin.execute('select tenant_id,action_name from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()
    pause(admin, tenant, 'action_type', action, paused=True)
    with fixture.current_executor() as executor:
        assert executor.requery_stale_unknown(intent_id=intent, confirmation_ref='owner:1') == {'intent_id': intent, 'armed': True, 'orders': 1}
    assert admin.execute("select available_at<='now'::timestamptz+interval '1 second' from runtime.nexloop_effect_outbox where intent_id=%s", (intent,)).fetchone() == (True,)


def test_a_requery_the_provider_answers_not_found_is_recorded_as_evidence_and_stops_again(assembled_message, admin, tmp_path):  # noqa: F811
    """0214 (10/18 stage, release 16): two owner orders armed and queried, and left no observation, because the worker turned
    the provider's not_found into a plain unknown. The provider has no record of the reply (its records start later): the
    query is recorded as a not_found observation of the current attempt, the intent stays unknown and is stopped again by the
    0199 window, nothing is sent; the owner's confirm-not-delivered check (0210) then has its evidence."""
    import threading
    f = assembled_message
    with chain(f, admin, tmp_path) as c:
        tenant = c['tenant'];intent = c['receipts'][0]['intent_id']
        material = delivery_material(tmp_path, timeout=1);config, secret = material['provider_config'], material['secret']
        barrier = {'fsynced': threading.Event(), 'release': threading.Event()}
        with delivery_service(c, tmp_path, material, barrier=barrier):
            assert '"status":"unknown"' in effect_worker(c, tmp_path, config, secret, lease=3) and barrier['fsynced'].is_set()
        # The provider keeps records from a later date only: this reply's export and manifest are gone.
        for path in list(material['root'].glob(intent + '*')):
            path.unlink()
        deadline = time.monotonic() + 10
        while not admin.execute('select lease_until is null or lease_until<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s', (intent,)).fetchone()[0]:
            assert time.monotonic() < deadline;time.sleep(.05)
        assert '"status":"unknown"' in effect_worker(c, tmp_path, config, secret, lease=3)  # the provider unreachable then: unknown
        assert outbound(admin, tenant)[0][1] == 'unknown'
        admin.execute("update authz.nexloop_run_credentials set issued_at=clock_timestamp()-interval '2 days',expires_at=clock_timestamp()-interval '2 days'+interval '2 minutes' "
                      "where run_id=(select origin_run_id from runtime.nexloop_effect_intents where intent_id=%s)", (intent,))
        admin.execute("update runtime.nexloop_effect_events set created_at=created_at-interval '2 days' where intent_id=%s", (intent,))
        admin.execute("update runtime.nexloop_effect_intents set created_at=created_at-interval '2 days' where intent_id=%s", (intent,))
        with open_backend(database_url=make_conninfo(c['o']['pg'], user='nexloop_action_worker'), artifact_root=tmp_path / 'settle-artifacts',
                          signing_key_file=c['p']['backend_signing'], signing_key_id='explicit-configuration') as backend:
            executor = backend.authenticate(c['credentials']['assembly-executor'].read_text().strip(), world='real')
            assert executor.settle_expired_origin(intent_id=intent) == 'stopped'
        posts = admin.execute('select count(*),max(attempt_revision) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone()
        events = admin.execute("select count(*) from runtime.nexloop_effect_events where intent_id=%s and event_kind='unknown'", (intent,)).fetchone()[0]
        code, lines = requery(c, tmp_path, [intent], 'owner-requery:nf')
        assert code == 0 and lines[0]['armed'] is True, lines
        with delivery_service(c, tmp_path, material):
            assert '"status":"unknown"' in effect_worker(c, tmp_path, config, secret, lease=3)
        observations = admin.execute("select provider_state,attempt_revision from runtime.nexloop_effect_observations where intent_id=%s", (intent,)).fetchall()
        assert observations == [('not_found', posts[1])]  # the current attempt's not_found, recorded
        assert stopped(admin, intent) == ('unknown', True)  # unknown again and stopped by the 0199 window
        assert admin.execute("select count(*) from runtime.nexloop_effect_events where intent_id=%s and event_kind='unknown'", (intent,)).fetchone()[0] == events + 1
        assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone()[0] == posts[0] == 1  # nothing sent
        assert outbound(admin, tenant)[0][1] == 'unknown' and list(material['root'].glob(intent + '*.export.json')) == []
        # The owner's check: the evidence is there; only the 10 minute wait remains.
        assert admin.execute("select runtime.nexloop_effect_determination_check(%s,'real',%s)->>'code'", (tenant, intent)).fetchone()[0] == 'wait_period'
        admin.execute("update runtime.nexloop_effect_observations set observed_at=observed_at-interval '11 minutes' where intent_id=%s", (intent,))
        admin.execute("update runtime.nexloop_effect_attempts set dispatched_at=dispatched_at-interval '2 days' where intent_id=%s", (intent,))  # as on stage
        assert admin.execute("select runtime.nexloop_effect_determination_check(%s,'real',%s)->>'code'", (tenant, intent)).fetchone()[0] == 'eligible'

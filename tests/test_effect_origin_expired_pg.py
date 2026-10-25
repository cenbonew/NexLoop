"""0174 (500-customer ladder): after its origin Run credential expired (a revocation alone is not enough: it may be undone, as
a restore does), and while no dispatch control refuses it (a restore fence or paused channel keeps it waiting), an intent is never re-tried with it. Never dispatched,
it fails with its outbox item (nothing was sent); dispatched and still unknown, automatic re-query stops (the reply
guarantee's reconciliation may still ask once). Unknown results are offered again only after min(60, 2^n) s. The executor
may settle only its own intents, and only when the origin is really expired (checked in SQL). Actual PG authority."""
import time

import psycopg
from support.effect_provider import effect_provider  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_effect_dispatch import accepted


def expire(admin, fixture):
    for run in fixture.runs:
        admin.execute("update authz.nexloop_run_credentials set issued_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 second' where run_id=%s", (run.run_id,))


def outbox(admin, intent):
    return admin.execute("select state,available_at='infinity'::timestamptz,extract(epoch from available_at-clock_timestamp()) from runtime.nexloop_effect_outbox where intent_id=%s",
                         (intent,)).fetchone()


def test_settle_needs_an_expired_origin_and_fails_only_a_never_dispatched_intent(governed_effect_executor, admin):
    fixture = governed_effect_executor
    intent = accepted(fixture)['intent_id']
    with fixture.current_executor() as executor:
        assert executor.settle_expired_origin(intent_id=intent) == 'none'  # origin still current: nothing changes
        assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone() == ('accepted',)
        for run in fixture.runs:
            admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s", (run.run_id,))
        assert executor.settle_expired_origin(intent_id=intent) == 'none'  # revoked, not expired: may be re-issued, keeps waiting
        for run in fixture.runs:
            admin.execute("update authz.nexloop_run_credentials set status='active' where run_id=%s", (run.run_id,))
        expire(admin, fixture)
        # Never claimed: the origin is the earliest submission's Run (as the claim would choose it).
        assert executor.settle_expired_origin(intent_id=intent) == 'failed'
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone() == ('failed',)
    assert outbox(admin, intent)[0] == 'failed'
    # NX-057 R0: one failure row, reason origin_expired, the expired origin Run as evidence.
    rows = admin.execute("select reason_code,evidence->>'origin_run_id' from runtime.nexloop_effect_failures where intent_id=%s", (intent,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == 'origin_expired' and rows[0][1] in {str(run.run_id) for run in fixture.runs}, rows


def test_a_dispatched_unknown_intent_stops_requery_and_unknown_backs_off(governed_effect_executor, admin):
    fixture = governed_effect_executor
    intent = accepted(fixture)['intent_id']
    # Synthetic dispatched state (the ledger owner's view): an attempt exists and the result is unknown.
    admin.execute("update runtime.nexloop_effect_intents set state='unknown' where intent_id=%s", (intent,))
    admin.execute("update runtime.nexloop_effect_outbox set state='unknown',available_at=clock_timestamp() where intent_id=%s", (intent,))
    for _ in range(3):
        admin.execute("insert into runtime.nexloop_effect_events values(gen_random_uuid(),%s,(select tenant_id from runtime.nexloop_effect_intents where intent_id=%s),'real','unknown',1,clock_timestamp(),'{}')",
                      (intent, intent))
    with fixture.current_executor() as executor:
        assert executor.back_off_unknown(intent_id=intent) == 4  # 3 unknown events: 2^(3-1) s
        state, infinite, seconds = outbox(admin, intent)
        assert state == 'unknown' and not infinite and 3 <= seconds <= 4.5
        expire(admin, fixture)
        assert executor.settle_expired_origin(intent_id=intent) == 'stopped'
        assert outbox(admin, intent)[:2] == ('unknown', True)
        assert admin.execute('select 1 from runtime.nexloop_effect_failures where intent_id=%s', (intent,)).fetchone() is None  # stopped, not failed
        assert executor.back_off_unknown(intent_id=intent) == 4 and outbox(admin, intent)[1] is True  # never brought back from infinity


def test_only_the_executor_role_may_settle(governed_effect_executor, admin, pg):
    from psycopg.conninfo import make_conninfo
    intent = accepted(governed_effect_executor)['intent_id']
    for role in ('nexloop_api', 'nexloop_domain_worker'):
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as db:
            for fn in ('nexloop_effect_origin_expired', 'nexloop_effect_query_backoff'):
                try:
                    db.execute(f'select authz.{fn}(%s,%s,%s)', ('x' * 64, 'real', intent))
                    raise AssertionError(fn + ' allowed for ' + role)
                except psycopg.errors.InsufficientPrivilege:
                    pass


def test_a_dispatch_control_keeps_an_expired_origin_intent_waiting(governed_effect_executor, admin):
    fixture = governed_effect_executor
    intent = accepted(fixture)['intent_id']
    expire(admin, fixture)
    from test_nx022_dispatch_e2e import pause
    tenant, action = admin.execute('select tenant_id,action_name from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()
    pause(admin, tenant, 'action_type', action, paused=True)  # the owner paused this channel: the intent waits
    with fixture.current_executor() as executor:
        assert executor.settle_expired_origin(intent_id=intent) == 'none'
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone() == ('accepted',)

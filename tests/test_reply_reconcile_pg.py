"""Reply reconciliation (0210, dispatcher 2026-10-11). AGENTS: an unknown external result is not a failure to blindly retry;
query and reconcile first. When a message's original reply is unknown at its due time, the reply worker triggers one
reconciliation query on the existing effect query path and keeps the reply due for a bounded window: delivered settles it, an
explicit failure starts the fallback as before, still unknown at the end escalates (reply_unknown) without a fallback.

Actual chain: HTTPS consumer message → relay → Pi Run → governed reply intent; a real transport timeout after the provider
fsynced the export makes the reply unknown (as tests/test_outbound_messages_pg.py does). Admin only seeds authority, moves due
times forward (time injection) and probes."""
import threading
import time

import pytest
from test_reply_fallback_pg import TEST_POLICY, guarantor, make_due, pending, provision
from test_outbound_messages_pg import chain, delivery_material, delivery_service, effect_worker, outbound
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401

pytestmark = pytest.mark.reply_fallback
POLICY = {**TEST_POLICY, 'fallback': {**TEST_POLICY['fallback'], 'reconcile_window_seconds': 30}}


def lease_expired(admin, intent):
    deadline = time.monotonic() + 10
    while not admin.execute('select lease_until is null or lease_until<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s', (intent,)).fetchone()[0]:
        assert time.monotonic() < deadline
        time.sleep(.05)


def unknown_reply(c, admin, tmp_path):
    """The provider fsyncs the export but the reply times out, then the query cannot reach it: the reply is unknown."""
    intent = c['receipts'][0]['intent_id']
    barrier = {'fsynced': threading.Event(), 'release': threading.Event()}
    material = delivery_material(tmp_path, timeout=1)
    with delivery_service(c, tmp_path, material, barrier=barrier):
        assert '"status":"unknown"' in effect_worker(c, tmp_path, material['provider_config'], material['secret'], lease=3) and barrier['fsynced'].is_set()
    lease_expired(admin, intent)
    assert '"status":"unknown"' in effect_worker(c, tmp_path, material['provider_config'], material['secret'], lease=3)
    assert outbound(admin, c['tenant'])[0][1] == 'unknown'
    lease_expired(admin, intent)
    return intent, material


def issuances(admin):
    return admin.execute('select count(*) from authz.nexloop_message_fallback_issuances').fetchone()[0]


def test_unknown_reply_is_reconciled_by_query_then_settles_without_fallback(assembled_message, admin, tmp_path):
    f = assembled_message
    token = provision(f, admin)
    with chain(f, admin, tmp_path, body='发票什么时候能开？') as c:
        c['recipe'] = f['recipe']
        intent, material = unknown_reply(c, admin, tmp_path)
        admin.execute("update runtime.nexloop_effect_outbox set available_at=clock_timestamp()+interval '1 hour' where intent_id=%s", (intent,))
        with guarantor(c, admin, tmp_path, token, policy=POLICY) as g:
            make_due(admin)
            summary = g['worker']().run_once()
            assert summary['reconciling'] == 1 and summary['fallback_started'] == 0 and summary['escalated'] == 0, summary
            started, = admin.execute('select started_at from control.nexloop_reply_reconciles where intent_id=%s', (intent,)).fetchone()
            # The query is brought forward on the existing effect query path; nothing is resent.
            assert admin.execute('select state,available_at<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s', (intent,)).fetchone() == ('unknown', True)
            # Idempotent: a second tick inside the window keeps the one reconciliation and starts nothing.
            make_due(admin)
            assert g['worker']().run_once()['reconciling'] == 1
            assert admin.execute('select count(*),min(started_at) from control.nexloop_reply_reconciles').fetchone() == (1, started)
            # The provider answers the query: delivered, the reply-due item settles; no fallback, no escalation.
            with delivery_service(c, tmp_path, material):
                assert 'fulfilled' in effect_worker(c, tmp_path, material['provider_config'], material['secret'], lease=3)
            assert outbound(admin, c['tenant'])[0][1] == 'delivered' and pending(admin, c['tenant']) == []
            make_due(admin)
            assert not any(g['worker']().run_once().values())
            assert issuances(admin) == 0 and g['read']().escalations() == []
            assert len(list(material['root'].glob('*.export.json'))) == 1


def test_reply_still_unknown_after_the_window_escalates_without_fallback(assembled_message, admin, tmp_path):
    f = assembled_message
    token = provision(f, admin)
    with chain(f, admin, tmp_path, body='退款进度怎样了？') as c:
        c['recipe'] = f['recipe']
        intent, _ = unknown_reply(c, admin, tmp_path)
        with guarantor(c, admin, tmp_path, token, policy=POLICY) as g:
            make_due(admin)
            assert g['worker']().run_once()['reconciling'] == 1
            # Time injection: the 30 s window has passed and the result is still unknown.
            admin.execute("update control.nexloop_reply_reconciles set started_at=started_at-interval '31 seconds'")
            make_due(admin)
            summary = g['worker']().run_once()
            assert summary['escalated'] == 1 and summary['fallback_started'] == 0, summary
            (row,) = g['read']().escalations()
            assert row['reason'] == 'reply_unknown' and row['detail']['intent_id'] == str(intent) and row['detail']['window_seconds'] == 30
            assert issuances(admin) == 0 and pending(admin, c['tenant']) == []
            # Escalation is once per message: a replayed tick changes nothing.
            make_due(admin)
            assert not any(g['worker']().run_once().values()) and len(g['read']().escalations()) == 1


def test_explicitly_failed_reply_gets_the_fallback(assembled_message, admin, tmp_path):
    """An original reply that failed for certain (no reconciliation needed) gets the fallback at its due time, as before."""
    f = assembled_message
    token = provision(f, admin)
    with chain(f, admin, tmp_path, body='订单发货了吗？') as c:
        c['recipe'] = f['recipe']
        agent_intent = c['receipts'][0]['intent_id']
        with admin.transaction():  # ledger state injection: the provider reported a definite failure for the reply
            admin.execute("update runtime.nexloop_effect_outbox set state='failed' where intent_id=%s", (agent_intent,))
            admin.execute("update runtime.nexloop_effect_intents set state='failed' where intent_id=%s", (agent_intent,))
        assert outbound(admin, c['tenant'])[0][1] == 'failed'
        with guarantor(c, admin, tmp_path, token, policy=POLICY) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started'] == 1
            assert admin.execute('select count(*) from authz.nexloop_message_fallback_issuances where message_id=%s', (c['consumer']['id'],)).fetchone() == (1,)
            assert admin.execute('select count(*) from control.nexloop_reply_reconciles').fetchone() == (0,)


def test_policy_v2_is_published_and_the_window_is_bounded(admin, tmp_path):
    import json
    from pathlib import Path
    from nexloop_eios.contact_restrictions import RECONCILE_WINDOW_DEFAULT, RECONCILE_WINDOW_MAX, load_reply_policy, reconcile_window
    from nexloop_eios.bootstrap import bootstrap
    root = Path(__file__).resolve().parents[1] / 'deploy/configuration'
    bootstrap(admin)
    v2 = load_reply_policy(root / 'reply-guarantee.v2.json')
    assert admin.execute('select definition from control.nexloop_reply_policies where version=2').fetchone()[0] == v2
    assert reconcile_window(v2) == 600 == RECONCILE_WINDOW_DEFAULT and RECONCILE_WINDOW_MAX == 3600
    assert reconcile_window(load_reply_policy(root / 'reply-guarantee.v1.json')) == RECONCILE_WINDOW_DEFAULT  # v1: the default
    def written(window, reply_window=1800):
        body = json.loads((root / 'reply-guarantee.v2.json').read_text())
        body['fallback']['reconcile_window_seconds'] = window
        body['reply_window_seconds'] = reply_window
        path = tmp_path / f'policy-{window}-{reply_window}.json'
        path.write_text(json.dumps(body))
        return path
    assert reconcile_window(load_reply_policy(written(30))) == 30
    for window, reply_window in ((29, 1800), (3601, 7200), ('600', 1800), (1400, 1800)):  # the last one: 300+1400+120 >= 1800
        with pytest.raises(ValueError):
            load_reply_policy(written(window, reply_window))

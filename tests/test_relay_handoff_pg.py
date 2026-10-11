"""Relay replan fix (0150): a message the relay cannot complete is handed to the reply guarantee (ADR-023) and stops blocking its
consumer (D2); a failed attempt gives the message back with a bounded count. Real PostgreSQL, the production MessageRelay in
run-profile mode (test_relay_multi_consumer_pg fixture). Failures are injected where a test says so; synthetic data."""
import pytest

from nexloop_eios.message_relay import MessageRelayPort, MessageRelayUnavailable
from nexloop_eios.run_credential_vault import RunCredentialVault
from test_relay_multi_consumer_pg import claims, multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401


def failing_once(relay, attribute='bind_effect_context'):
    """The first attempt fails after its Run credential was issued (the observed C4-low start)."""
    original = getattr(relay.planner, attribute)
    state = {'failed': False}

    def fail(*args, **kw):
        if not state['failed']:
            state['failed'] = True
            raise RuntimeError('injected failure after issuance')
        return original(*args, **kw)
    setattr(relay.planner, attribute, fail)
    return relay


def expire_lease(m, message):
    with m['admin'].transaction():
        m['admin'].execute('update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp() where message_id=%s', (message,))


def bump_revision(m, object_id):
    """Test injection: the consumer's current revision changes between two claims (as a contact refusal or an owner change does)."""
    with m['admin'].transaction():
        m['admin'].execute('set local session_replication_role=replica')
        m['admin'].execute('update ontology.objects set nexloop_revision=nexloop_revision+1 where object_id=%s', (object_id,))


def handoffs(m):
    return m['admin'].execute('select message_id,reason,run_id is not null,attempts from runtime.nexloop_relay_handoffs order by handed_at').fetchall()


def test_a_changed_command_after_a_failed_attempt_hands_the_message_to_the_fallback_and_unblocks_the_consumer(multi):
    m = multi
    admin = m['admin']
    for label in 'AB':
        m['enroll'](label)
    a1, a2 = m['consumers']['A']['messages'][:2]
    with RunCredentialVault(m['vault_dir']) as vault:
        with pytest.raises(MessageRelayUnavailable):
            failing_once(m['relay'](vault)).run_once()
        # Root cause fixed: the failed attempt gave the message back (counted) instead of holding the claim until its lease ended.
        assert admin.execute('select attempts from runtime.nexloop_relay_attempts where message_id=%s', (a1,)).fetchone() == (1,)
        run_id = admin.execute('select run_id from authz.nexloop_message_run_issuances where message_id=%s', (a1,)).fetchone()[0]
        assert admin.execute("select status from runtime.nexloop_message_outbox where message_id=%s", (a1,)).fetchone() == ('pending',)
        expire_lease(m, a1)
        bump_revision(m, m['consumers']['A']['consumer'])
        statuses = [m['relay'](vault).run_once() for _ in range(4)]
    # B (never served) first; then A1 ends on the relay path (its pinned command no longer matches); then A2 is served.
    assert statuses == ['queued', 'handed_to_fallback', 'queued', 'queued'], statuses
    # A2 was routed (a Run task exists for it): the consumer is no longer blocked behind A1.
    assert admin.execute('select task_id is not null from runtime.nexloop_message_routes where message_id=%s', (a2,)).fetchone() == (True,)
    assert admin.execute('select task_id from runtime.nexloop_message_routes where message_id=%s', (a1,)).fetchone() in (None, (None,))
    assert handoffs(m) == [(a1, 'binding_conflict', True, 1)]
    assert admin.execute('select status from authz.nexloop_run_credentials where run_id=%s', (run_id,)).fetchone() == ('revoked',)
    assert admin.execute("select status from runtime.nexloop_message_outbox where message_id=%s", (a1,)).fetchone() == ('delivered',)
    # ADR-023: its pending reply is due now for the reply guarantee (never silently dropped); A2's is not touched.
    due = dict(admin.execute("select item_key,available_at<=clock_timestamp() from runtime.nexloop_work_feed where feed='reply-due' and item_key in (%s,%s)",
                             ('reply:' + a1, 'reply:' + a2)).fetchall())
    assert due['reply:' + a1] is True
    assert admin.execute("select action,payload->>'reason' from runtime.audit_events where event_id=%s", ('relay-handoff:' + a1,)).fetchone() == (
        'nexloop.relay.handoff', 'binding_conflict')
    relay_metrics = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'relay'", (m['tenant'],)).fetchone()[0]
    assert relay_metrics['handoffs_total'] == 1 and relay_metrics['handoffs_last_hour'] == 1


def test_an_expired_credential_ends_the_message_and_a_repeated_handoff_is_idempotent(multi):
    m = multi
    admin = m['admin']
    m['enroll']('A')
    a1 = m['consumers']['A']['messages'][0]
    with RunCredentialVault(m['vault_dir']) as vault:
        with pytest.raises(MessageRelayUnavailable):
            failing_once(m['relay'](vault)).run_once()
        # The issued credential expires (test injection of its time) before the next claim.
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute("update authz.nexloop_message_run_issuances set issued_at=clock_timestamp()-interval '10 minutes',"
                          "expires_at=clock_timestamp()-interval '6 minutes' where message_id=%s", (a1,))
        expire_lease(m, a1)
        relay = m['relay'](vault)
        assert relay.run_once() == 'handed_to_fallback' and relay.last_handoff == 'credential_expired'
    assert handoffs(m) == [(a1, 'credential_expired', True, 1)]
    fence = admin.execute('select fence from runtime.nexloop_message_relay_leases where message_id=%s', (a1,)).fetchone()[0]
    port = MessageRelayPort(m['api'].authenticate(m['f']['owner_token'], world='real'))
    again = port.call('handoff', message_id=a1, fence=fence, reason='requires_governed_replan')
    assert again == {'status': 'handed_to_fallback', 'reason': 'credential_expired', 'replay': True}
    assert admin.execute('select count(*) from runtime.audit_events where action=%s', ('nexloop.relay.handoff',)).fetchone() == (1,)


def test_transient_failures_are_bounded_then_handed_off_and_other_consumers_are_unaffected(multi):
    m = multi
    admin = m['admin']
    for label in 'AB':
        m['enroll'](label)
    a1, b1 = m['consumers']['A']['messages'][0], m['consumers']['B']['messages'][0]
    port = MessageRelayPort(m['api'].authenticate(m['f']['owner_token'], world='real'))
    item = port.call('claim_next', lease_seconds=30)
    assert item['message_id'] == a1
    # A handoff for 'attempts_exhausted' before the bound is refused.
    with pytest.raises(MessageRelayUnavailable):
        port.call('handoff', message_id=a1, fence=item['fence'], reason='attempts_exhausted')
    results = [port.call('yield', message_id=a1, fence=item['fence']) for _ in range(5)]
    assert [r['attempts'] for r in results] == [1, 2, 3, 4, 5] and [r['handoff_due'] for r in results] == [False] * 4 + [True]
    assert port.call('handoff', message_id=a1, fence=item['fence'], reason='attempts_exhausted')['status'] == 'handed_to_fallback'
    # B was never touched by A's trouble: it is claimed and served as usual.
    with RunCredentialVault(m['vault_dir']) as vault:
        assert m['relay'](vault).run_once() == 'queued'
    assert claims(m)[-1] == ('B', b1)
    assert handoffs(m) == [(a1, 'attempts_exhausted', False, 5)]
    # Only the relay holding the claim can end it; the Source or a foreign fence cannot.
    with pytest.raises(MessageRelayUnavailable):
        port.call('handoff', message_id=m['consumers']['A']['messages'][1], fence=1, reason='binding_conflict')

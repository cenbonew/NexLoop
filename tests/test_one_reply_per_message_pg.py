"""NX-031 duplicate replies (0177): one inbound message gets at most one reply the customer can see, for every consumer.

The load lane found inbound messages with two delivered replies: under backlog the reply guarantee's fallback reply was
delivered first and the original Run's reply, still queued, was dispatched afterwards (the dispatch-time one-reply rule applied
only to fallback replies and restricted consumers). On the actual chain (HTTPS message, relay, Pi Run, reply guarantor, local
JSON delivery, effect worker), three orders: the fallback first and the original later (the lane's case); both admitted at
the same time; the original first and the fallback later. Plus a retry of the delivered reply and the database backstop."""
import threading

import psycopg
import pytest

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_outbound_messages_pg import chain, delivery_material, delivery_service, effect_worker, outbound
from test_reply_fallback_pg import activated_fallback, guarantor, make_due, provision

pytestmark = pytest.mark.reply_fallback


def exports(material):
    return len([p for p in material['root'].iterdir() if p.name.endswith('.export.json')])


def hold(admin, intent, held):
    """Test harness: the backlog. A queued reply is not yet due for the effect worker (or due again)."""
    admin.execute("update runtime.nexloop_effect_outbox set available_at=clock_timestamp()+%s where intent_id=%s",
                  ('1 hour' if held else '-1 second', intent))


def fallback_reply(c, g, admin, tenant, agent_intent, text):
    worker, ref, command = activated_fallback(c, g)
    worker.runtime_effect_tool(activation_ref=ref, command=command, tool_operation='submit', parameters={'message': text})
    return next(r[0] for r in outbound(admin, tenant) if r[0] != agent_intent)


def settled(admin, tenant, delivered, refused):
    states = {r[0]: r[1] for r in outbound(admin, tenant)}
    assert states == {delivered: 'delivered', refused: 'failed'}, states
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (refused,)).fetchone() == ('failed',)
    assert admin.execute('select code from runtime.nexloop_dispatch_refusals where intent_id=%s', (refused,)).fetchall() == [('NXC05',)]
    # NX-057 R0: one failure row, reason refused_one_reply:<reason>, the refusal code as evidence.
    rows = admin.execute("select reason_code,evidence->>'refusal_code' from runtime.nexloop_effect_failures where intent_id=%s", (refused,)).fetchall()
    assert len(rows) == 1 and rows[0][0].startswith('refused_one_reply:') and rows[0][1] == 'NXC05', rows


def test_the_original_reply_is_refused_once_the_fallback_reply_was_delivered(assembled_message, admin, tmp_path):
    """The lane's case: the original Run's reply is still queued when the fallback is issued and delivered; the original is
    then refused at dispatch (governed, failed, never sent)."""
    f = assembled_message
    token = provision(f, admin)
    with chain(f, admin, tmp_path, body='物流到哪了？') as c:
        c['recipe'] = f['recipe']
        tenant = c['tenant']
        ((agent_intent, state, *_),) = outbound(admin, tenant)
        assert state == 'persisted'
        hold(admin, agent_intent, True)
        with guarantor(c, admin, tmp_path, token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started'] == 1
            fallback_intent = fallback_reply(c, g, admin, tenant, agent_intent, '您好，包裹已发出。')
            material = delivery_material(tmp_path)
            with delivery_service(c, tmp_path, material):
                assert 'fulfilled' in effect_worker(c, tmp_path, material['provider_config'], material['secret'])  # the fallback
                hold(admin, agent_intent, False)
                assert 'fulfilled' not in effect_worker(c, tmp_path, material['provider_config'], material['secret'])  # the original
                assert 'fulfilled' not in effect_worker(c, tmp_path, material['provider_config'], material['secret'])  # retry: nothing new
        settled(admin, tenant, delivered=fallback_intent, refused=agent_intent)
        assert exports(material) == 1
        # A retry of the delivered reply itself is not refused by the rule; the refused one never comes back.
        admin.execute("select control.nexloop_contact_assert_intent(%s,'real',%s::uuid)", (tenant, fallback_intent))
        with pytest.raises(psycopg.Error, match='already answered') as refused:
            admin.execute("select control.nexloop_contact_assert_intent(%s,'real',%s::uuid)", (tenant, agent_intent))
        assert refused.value.sqlstate == 'NXC05'


def test_the_original_and_the_fallback_admitted_at_the_same_time_deliver_once(assembled_message, admin, tmp_path):
    """Both replies due together and two effect workers admit them concurrently: the per-message lock orders the two
    admissions; exactly one is delivered, the other refused."""
    f = assembled_message
    token = provision(f, admin)
    with chain(f, admin, tmp_path, body='发票开好了吗？') as c:
        c['recipe'] = f['recipe']
        tenant = c['tenant']
        ((agent_intent, *_),) = outbound(admin, tenant)
        hold(admin, agent_intent, True)
        with guarantor(c, admin, tmp_path, token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started'] == 1
            fallback_intent = fallback_reply(c, g, admin, tenant, agent_intent, '您好，发票已开出。')
            hold(admin, agent_intent, False)
            material = delivery_material(tmp_path)
            with delivery_service(c, tmp_path, material):
                results = []
                workers = [threading.Thread(target=lambda: results.append(effect_worker(c, tmp_path, material['provider_config'], material['secret'])))
                           for _ in range(2)]
                for w in workers:
                    w.start()
                for w in workers:
                    w.join(180)
                assert len(results) == 2
                effect_worker(c, tmp_path, material['provider_config'], material['secret'])  # whatever is left
        states = {r[0]: r[1] for r in outbound(admin, tenant)}
        assert sorted(states.values()) == ['delivered', 'failed'], states
        delivered = next(i for i, s in states.items() if s == 'delivered')
        settled(admin, tenant, delivered=delivered, refused=({agent_intent, fallback_intent} - {delivered}).pop())
        assert exports(material) == 1


def test_the_fallback_is_refused_once_the_original_reply_was_delivered_and_the_index_backstops(assembled_message, admin, tmp_path):
    """The converse order (the original first) as before 0177, and the partial unique index: no path may put a second reply
    to the same message into a state that reaches the customer."""
    f = assembled_message
    token = provision(f, admin)
    with chain(f, admin, tmp_path, body='还有库存吗？') as c:
        c['recipe'] = f['recipe']
        tenant = c['tenant']
        ((agent_intent, *_),) = outbound(admin, tenant)
        with guarantor(c, admin, tmp_path, token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started'] == 1
            material = delivery_material(tmp_path)
            with delivery_service(c, tmp_path, material):
                assert 'fulfilled' in effect_worker(c, tmp_path, material['provider_config'], material['secret'])  # the original
                fallback_intent = fallback_reply(c, g, admin, tenant, agent_intent, '您好，有库存。')
                assert 'fulfilled' not in effect_worker(c, tmp_path, material['provider_config'], material['secret'])
        settled(admin, tenant, delivered=agent_intent, refused=fallback_intent)
        assert exports(material) == 1
        with pytest.raises(psycopg.errors.UniqueViolation, match='nexloop_outbound_messages_one_reply'), admin.transaction():
            admin.execute('set local session_replication_role=replica')  # test harness: bypass the forward-only guard
            admin.execute("update runtime.nexloop_outbound_messages set delivery_state='dispatching' where intent_id=%s::uuid", (fallback_intent,))

"""ADR-030 §3 (NX-037 gap B slice 2): the two slots of one Message Run are rechecked independently at dispatch.

One relay-issued Message Run holds the reply and the enterprise delivery (test_enterprise_slot_relay_pg). Both intents are
accepted at submission; what changes afterwards is seen only by the independent effect Worker at dispatch:
- with everything open both go out (the baseline the refusals below are measured against);
- the customer's message refuses contact (the actual 0109 rule restricts the consumer in the message's own transaction): the
  enterprise delivery (customer_contact) is accepted at submission and refused at dispatch (NXC05), the reply bound to that
  inbound message still goes out (ADR-023 §2.6);
- the enterprise offering stops being valid after submission: the delivery is refused at admission, the reply is unaffected;
- the real dispatch switch is opened for the reply Action only: the delivery is refused at the Action layer (NXC07).
In every case the loopback provider never receives the enterprise intent. Synthetic data.
"""
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios import effect_intents
from nexloop_eios.effect_intents import EffectIntentUnavailable
from nexloop_eios.run_credential_vault import RunCredentialVault
from runtime_effect_fixture import PrivatePlan
from support.effect_provider import effect_provider
from test_effect_dispatch import independent_worker
from test_enterprise_slot_relay_pg import DELIVER, REPLY, issued_run, slots  # noqa: F401
from test_real_dispatch_switch_pg import set_switch
from test_relay_multi_consumer_pg import multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401

REFUSAL = '以后别再给我发消息了'


class Executor:
    def __init__(self, s, tmp_path): self.s, self.tmp_path = s, tmp_path
    def __repr__(self): return '<private slot executor configuration>'

    def executor_configuration(self):
        f = self.s['f']
        return PrivatePlan(database_url=make_conninfo(f['pg'], user='nexloop_action_worker'), signing_key_file=f['signing_key'], signing_key_id='runtime-effect',
                    artifact_root=self.tmp_path / 'slot-worker-artifacts', service_token=self.s['executor_token'], world='real')


@pytest.fixture
def issued(slots, monkeypatch, tmp_path, request):  # noqa: F811
    """D's message issued with both slots, one accepted intent per slot. The switches are opened before the intents are submitted
    (a later switch change supersedes the snapshot, NXC02): deployment, tenant, the reply Action, and the enterprise Action unless
    the test is parametrized with False."""
    s = slots
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    set_switch(s['admin'], s['tenant'], 'tenant', '*', True, 'real')
    set_switch(s['admin'], s['tenant'], 'action_type', REPLY, True)
    if getattr(request, 'param', True):
        set_switch(s['admin'], s['tenant'], 'action_type', DELIVER, True)
    s['enroll']('D')
    with RunCredentialVault(s['vault_dir']) as vault:
        assert s['slot_relay'](vault).run_once() == 'queued'
        record = vault.read(vault.message_key(s['tenant'], 'real', s['people']['D']['message']))
    run_id, resources, run_slots, _ = issued_run(s, 'D')
    assert resources == [REPLY, DELIVER] and len(run_slots) == 2
    run = s['api'].authenticate_run(record.token, world='real', run_id=str(run_id))
    reply = run.submit_effect_intent(parameters={'message': '合成回复'})
    deliver = run.submit_effect_intent(parameters={'summary': '合成企业交付', 'consumer_ref': str(run_id)}, action='nexloop.enterprise.deliver')
    assert reply['state'] == deliver['state'] == 'accepted' and reply['intent_id'] != deliver['intent_id']
    return dict(s, reply=reply['intent_id'], deliver=deliver['intent_id'], executor=Executor(s, tmp_path))


def dispatch(i, provider, rounds=4):
    results = []
    for _ in range(rounds):
        with independent_worker(i['executor'], provider.origin) as (process, pipe):
            assert pipe.poll(20)
            result = pipe.recv()
            process.join(10)
        assert process.exitcode == 0 and result['event'] == 'settled', result
        if not result['claimed']:
            break
        results.append(result)
    return results


def sent(i, tmp_path, name):
    with effect_provider(tmp_path / name) as provider:
        dispatch(i, provider)
        return {intent for (method, intent, _) in provider.control('snapshot')['requests'] if method == 'POST'}


def state(i, intent):
    return i['admin'].execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()[0]


def refusal_codes(i, intent):
    return {c for (c,) in i['admin'].execute('select code from runtime.nexloop_dispatch_refusals where intent_id=%s', (intent,)).fetchall()}


def refused(i):
    """The enterprise intent never reached the provider and is not settled; the reply did and is."""
    assert state(i, i['deliver']) == 'accepted' and state(i, i['reply']) == 'dispatching'
    assert refusal_codes(i, i['reply']) == set()


def test_with_every_check_passing_both_slots_reach_the_provider(issued, tmp_path):
    i = issued
    assert sent(i, tmp_path, 'slot-both-provider.sqlite') == {i['reply'], i['deliver']}
    assert state(i, i['deliver']) == state(i, i['reply']) == 'dispatching'


@pytest.mark.parametrize('slots', [{'D': REFUSAL}], indirect=True)
def test_a_contact_refusal_refuses_the_enterprise_delivery_at_dispatch_but_not_the_bound_reply(issued, tmp_path):
    i = issued
    d = i['people']['D']
    assert i['admin'].execute('select active from control.nexloop_contact_restrictions where consumer_id=%s', (d['consumer'],)).fetchone() == (True,)
    assert sent(i, tmp_path, 'slot-contact-provider.sqlite') == {i['reply']}
    refused(i)
    assert refusal_codes(i, i['deliver']) == {'NXC05'}


def test_an_enterprise_offering_invalid_after_submission_refuses_the_delivery_and_not_the_reply(issued, tmp_path):
    i = issued
    with i['admin'].transaction():
        i['admin'].execute('set local role nexloop_owner')
        i['admin'].execute("select set_config('eios.tenant_id',%s,true)", (i['tenant'],))
        i['admin'].execute("update ontology.objects set properties=properties||'{\"active\":false}'::jsonb where object_id=%s",
                           (i['people']['D']['offerings']['enterprise'][0],))
    assert sent(i, tmp_path, 'slot-offering-provider.sqlite') == {i['reply']}
    refused(i)  # refused by the catalog assertion inside admission: no switch or contact refusal code
    assert refusal_codes(i, i['deliver']) == set()


@pytest.mark.parametrize('issued', [False], indirect=True)
def test_the_real_dispatch_switch_of_the_reply_action_does_not_open_the_enterprise_delivery(issued, tmp_path):
    i = issued
    assert sent(i, tmp_path, 'slot-switch-provider.sqlite') == {i['reply']}
    refused(i)
    assert refusal_codes(i, i['deliver']) == {'NXC07'}


def test_a_run_holding_both_actions_with_only_the_enterprise_context_cannot_submit_a_reply(slots, monkeypatch):  # noqa: F811
    """L4 review (not covered before slice 2): the Run credential grants both Actions but the Run's only context is on the
    enterprise slot (the reply slot's row removed by the test after issuance; a relay never issues that shape). A reply intent
    finds no context on its own slot and is refused in SQL; it never borrows the enterprise context."""
    s = slots
    s['enroll']('D')
    with RunCredentialVault(s['vault_dir']) as vault:
        assert s['slot_relay'](vault).run_once() == 'queued'
        record = vault.read(vault.message_key(s['tenant'], 'real', s['people']['D']['message']))
    run_id, resources, _, _ = issued_run(s, 'D')
    assert resources == [REPLY, DELIVER]
    s['admin'].execute("delete from control.nexloop_effect_run_contexts where run_id=%s and slot_identity='service.request:primary'", (run_id,))
    assert s['admin'].execute('select slot_identity from control.nexloop_effect_run_contexts where run_id=%s', (run_id,)).fetchall() == [('enterprise.deliver:primary',)]
    run = s['api'].authenticate_run(record.token, world='real', run_id=str(run_id))
    seen, original = [], effect_intents.unavailable

    def spy(error, **arguments):
        if hasattr(error, 'diag'):
            seen.append(error.diag.message_primary)
        return original(error, **arguments)
    monkeypatch.setattr(effect_intents, 'unavailable', spy)
    with pytest.raises(EffectIntentUnavailable):
        run.submit_effect_intent(parameters={'message': '借企业上下文的回复'})
    assert seen == ['effect catalog Plan unavailable']  # no context on the reply slot: the enterprise context is never borrowed
    assert s['admin'].execute('select count(*) from runtime.nexloop_effect_intents').fetchone() == (0,)

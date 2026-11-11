"""Option B M2.1 (ADR-046 §3; design draft §2.2-§2.4): with the effect Action successor :2 admitted for Consumer v2, the whole chain
runs at :2 - the relay issues the message Run with :2 (allowed_actions, source proof, run credential), the bind proves :2 and stores
it in the context, the runtime submits the intent at :2 and the effect worker delivers it at :2 - and no A' proof is written. With
only :1 admitted nothing changes (test_effect_action_admissions_pg.test_a_prime_stays_through_m1 and the A' tests are the control).
Synthetic data, actual PostgreSQL."""
import json

import pytest

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_context_artifacts import active_worker, context_message  # noqa: F401
from test_effect_scope_successor_proof_pg import proofs, reauthenticate, reply
from test_local_json_delivery_pg import delivery_plan, live_delivery  # noqa: F401
from nexloop_eios.effect_dispatch import EffectDispatcher
from task_b_fixture import EFFECT, admit, bump_consumer_v2


def chain(admin):
    return {
        'contexts': sorted(r[0] for r in admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall()),
        'runs': sorted(json.dumps(r[0]) for r in admin.execute("""select allowed_resources from authz.nexloop_run_credentials
            where source_digest is not null and exists(select 1 from unnest(allowed_resources) x where x like %s)""", (f'eios:action:{EFFECT}:%',)).fetchall()),
        'intents': sorted((r[0], r[1]) for r in admin.execute('select action_name,(action_definition->>%s)::integer from runtime.nexloop_effect_intents', ('version',)).fetchall()),
    }


def test_the_admitted_successor_carries_the_relay_reply_at_2(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    bump_consumer_v2(admin, f['original']['tenant'])
    reauthenticate(f)
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict) and receipt['intent_id'], receipt
    found = chain(admin)
    assert found['contexts'] == [f'eios:action:{EFFECT}:2']
    assert found['runs'] == [json.dumps([f'eios:action:{EFFECT}:2'])]
    assert found['intents'] == [(EFFECT, 2)]
    assert proofs(admin) == []  # exact: no A' proof


def test_without_the_admission_the_reply_stays_at_1_through_a_prime(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    publication = bump_consumer_v2(admin, f['original']['tenant'], admitted=False)
    reauthenticate(f)
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict), receipt
    found = chain(admin)
    assert found['contexts'] == [f'eios:action:{EFFECT}:1'] and found['intents'] == [(EFFECT, 1)]
    assert proofs(admin) == [(f'eios:action:{EFFECT}:1', f'eios:action:{EFFECT}:2', publication, 2)]


def test_a_bind_naming_another_version_than_the_selection_is_refused(context_message, admin, tmp_path, monkeypatch):  # noqa: F811
    """M2-a: the registrar command recomputes the selection; proofs naming :1 for an admitted :2 are refused, nothing is bound."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    from nexloop_eios.message_relay import MessageRelayPort, MessageRelayUnavailable
    f = context_message
    bump_consumer_v2(admin, f['original']['tenant'])
    reauthenticate(f)
    monkeypatch.setattr(EffectContextRegistrar, '_effect_version', lambda self, db, verb, payload: 1)
    original = MessageRelayPort.effect_resources
    monkeypatch.setattr(MessageRelayPort, 'effect_resources', lambda self, source, consumer: {k: v.rsplit(':', 1)[0] + ':1' for k, v in original(self, source, consumer).items()})
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0] == 0


def test_a_run_naming_another_version_than_the_selection_is_not_issued(context_message, admin, tmp_path, monkeypatch):  # noqa: F811
    """The issuance command recomputes the selection for the Consumer: allowed_actions naming :1 for an admitted :2 issue no Run."""
    from nexloop_eios.message_relay import MessageRelayPort, MessageRelayUnavailable
    f = context_message
    bump_consumer_v2(admin, f['original']['tenant'])
    reauthenticate(f)
    original = MessageRelayPort.effect_resources
    monkeypatch.setattr(MessageRelayPort, 'effect_resources', lambda self, source, consumer: {k: v.rsplit(':', 1)[0] + ':1' for k, v in original(self, source, consumer).items()})
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert chain(admin)['runs'] == []


def test_retiring_a_version_an_unexpired_context_names_is_refused(context_message, admin, tmp_path):  # noqa: F811
    """M2-b: refused while a context names it (contexts are immutable and signed for their resource); nothing changes."""
    f = context_message
    tenant = f['original']['tenant']
    bump_consumer_v2(admin, tenant)
    reauthenticate(f)
    assert isinstance(reply(f, tmp_path), dict)
    admin.execute('set session authorization nexloop_configurator')
    try:
        out = admin.execute("select control.nexloop_retire_effect_action_admission(%s,%s,2,'human:owner','合成退役')", (tenant, EFFECT)).fetchone()[0]
    finally:
        admin.execute('reset session authorization')
    assert out == {'retired': False, 'admission_id': None, 'code': 'referenced_by_unexpired_context'}
    assert admin.execute('select count(*) from control.nexloop_effect_action_admissions where retired_at is null').fetchone()[0] == 1
    admin.execute("update control.nexloop_effect_contexts set valid_until=clock_timestamp()-interval '1 second'")
    admin.execute('set session authorization nexloop_configurator')
    try:
        out = admin.execute("select control.nexloop_retire_effect_action_admission(%s,%s,2,'human:owner','合成退役')", (tenant, EFFECT)).fetchone()[0]
    finally:
        admin.execute('reset session authorization')
    assert out['retired'] is True


@pytest.mark.parametrize('delivery_plan', ['consumer-v2'], indirect=True)
def test_the_effect_worker_delivers_the_admitted_successor_at_2(delivery_plan, admin):  # noqa: F811
    f = delivery_plan
    assert f['effect_version'] == 2
    accepted = f['submitter'].submit_effect_intent(parameters={'message': 'v2 合成本地投递'}, action_version=2)
    intent = accepted['intent_id']
    with live_delivery(f):
        result = EffectDispatcher(f['executor'], f['provider'], lease_seconds=30).run_once()
        assert result['status'] == 'fulfilled' and result['business_action_success'] is True and result['governed_claim_finalized'] is True
    assert admin.execute("select action_definition->>'version' from runtime.nexloop_effect_intents where intent_id=%s", (intent,)).fetchone() == ('2',)
    assert admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall() == [(f'eios:action:{EFFECT}:2',)]


@pytest.mark.parametrize('delivery_plan', ['consumer-v2-undeclared'], indirect=True)
def test_an_undeclared_successor_category_keeps_the_dispatch_closed(delivery_plan, admin):  # noqa: F811
    """The effect category (and externality) is declared per definition digest; :2 without a declaration is external and
    customer_contact (fail closed): the worker does not deliver it until trusted configuration declares :2 (run sheet, M3)."""
    f = delivery_plan
    accepted = f['submitter'].submit_effect_intent(parameters={'message': 'v2 未声明分类'}, action_version=2)
    with live_delivery(f):
        result = EffectDispatcher(f['executor'], f['provider'], lease_seconds=30).run_once()
    assert result['status'] == 'admission_unavailable' and result['business_action_success'] is False
    assert not list(f['root'].glob(accepted['intent_id'] + '*'))


@pytest.mark.parametrize('delivery_plan', ['consumer-v2'], indirect=True)
def test_configure_proving_another_version_than_the_selection_is_refused(delivery_plan, monkeypatch):  # noqa: F811
    """M2-a for configure (executor proof only, so no Run check in front of it): the executor holds :1 and :2; proving :1 for a
    control whose Consumer selects :2 is refused by the recomputation, proving :2 is accepted."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    f = delivery_plan
    owner = f['api'].authenticate(f['owner_token'], world='real')
    executor_token = f['credential_file'].read_text()
    with monkeypatch.context() as m:
        m.setattr(EffectContextRegistrar, '_effect_version', lambda self, db, verb, payload: 1)
        with pytest.raises(Exception, match='unavailable'):
            owner.configure_effect_control(control_id=f['control'], control_revision=1, executor_token=executor_token)
    assert owner.configure_effect_control(control_id=f['control'], control_revision=1, executor_token=executor_token)['control_id'] == f['control']


@pytest.mark.parametrize('delivery_plan', ['consumer-v2-wide-run'], indirect=True)
def test_an_intent_naming_another_version_than_its_context_is_refused(delivery_plan, admin):  # noqa: F811
    """The Run may hold :1 and :2; the context is :2, so an intent at :1 is refused (it is not the context's resource), at :2 accepted."""
    f = delivery_plan
    with pytest.raises(Exception, match='unavailable'):
        f['submitter'].submit_effect_intent(parameters={'message': 'v1 越界'}, action_version=1)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone() == (0,)
    accepted = f['submitter'].submit_effect_intent(parameters={'message': 'v2 正常'}, action_version=2)
    assert admin.execute("select action_definition->>'version' from runtime.nexloop_effect_intents where intent_id=%s", (accepted['intent_id'],)).fetchone() == ('2',)


@pytest.mark.parametrize('delivery_plan', ['consumer-v2-wide-unbound'], indirect=True)
def test_a_bind_proving_another_version_than_the_selection_is_refused_by_the_recomputation(delivery_plan, admin, monkeypatch):  # noqa: F811
    """M2-a for bind, isolated: the Run holds :1 and :2 and the executor both, so only the registrar's recomputation refuses proofs
    naming :1 for a Consumer whose admitted selection is :2; nothing is bound. The same bind at the selection succeeds."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    f = delivery_plan
    with monkeypatch.context() as m:
        m.setattr(EffectContextRegistrar, '_effect_version', lambda self, db, verb, payload: 1)
        with pytest.raises(Exception, match='unavailable'):
            f['bind']()
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone() == (0,)
    f['bind']()
    assert admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall() == [(f'eios:action:{EFFECT}:2',)]

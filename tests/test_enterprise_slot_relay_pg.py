"""ADR-030 (NX-037 gap B slice 2, 0180): the enterprise slot of a Message Run through the relay, on clean catalog PostgreSQL.

The owner enrolls a consumer's enterprise slot (its own EffectControl, the enterprise offering, the consumer's binding to it)
with the governed relay enrollment. A relay-claimed message of that consumer is then issued one Run credential for the
reply and the enterprise Action, with one effect context per slot bound in one transaction (reply first). A consumer without
an enterprise slot is unaffected byte for byte (profile, assignment, Run credential). An enrolled slot that is no longer
valid gives the reply slot only and a structured log, never a failed Run. Runtime roles cannot write the enrollment.
Synthetic data; the multi-consumer relay fixture of test_relay_multi_consumer_pg.
"""
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios import relay_enrollment
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.conversation_messages import ConversationMessagePort, ConversationOwnershipRegistrar
from nexloop_eios.message_relay import RUN_PROFILE_KEYS, MessageRelay, MessageRelayPort  # noqa: F401
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.property_access import RelaySourceReadRule
from nexloop_eios.run_credential_vault import RunCredentialVault
from nexloop_eios.service_offerings import enterprise_delivery_example, json_export_example
from context_source_declarations import source_declarations
from message_offering_fixture import message_read_rule_row
from runtime_effect_fixture import seed_multi_uuid
from test_postgres_action_claims import governance_inputs
from test_relay_multi_consumer_pg import action_targets, add_human, multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401

REPLY = 'eios:action:nexloop.service.request:1'
DELIVER = 'eios:action:nexloop.enterprise.deliver:1'
# The enterprise Action's parameters (dispatcher 2026-10-13): summary required (<=200), details optional (<=2000), consumer_ref
# (NexLoop's opaque consumer reference); no tenant_id; unknown keys refused.
DELIVER_SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                  'details': {'type': 'string', 'maxLength': 2000}, 'consumer_ref': {'type': 'string', 'minLength': 1, 'maxLength': 80}},
                  'required': ['summary'], 'additionalProperties': False}


def publish_enterprise_action(admin, tenant):
    definition, capability = admin.execute('select definition,capability from control.nexloop_action_definitions where tenant_id=%s and resource_id=%s',
                                           (tenant, REPLY)).fetchone()
    base = governance_inputs()
    body = dict(definition)
    body.pop('contract_digest', None)
    body.update(stable_name='nexloop.enterprise.deliver', input_schema=DELIVER_SCHEMA)
    body['capability_binding'] = {**body['capability_binding'], 'capability_name': 'nexloop.enterprise.deliver'}
    model = type(base['action_definition']).model_validate_json(json.dumps(body))
    cap = type(base['capability_snapshot']).model_validate_json(json.dumps({**capability, 'capability_name': 'nexloop.enterprise.deliver'}))
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                  (tenant, 'real', DELIVER, Jsonb(model.model_dump(mode='json')), Jsonb(cap.model_dump(mode='json'))))


@pytest.fixture
def slots(multi, request):  # noqa: F811
    """Parametrized indirectly with {label: body} to give a consumer's message another body (default: a service request)."""
    m = multi
    bodies = getattr(request, 'param', {})
    f, api, admin, tenant = m['f'], m['api'], m['admin'], m['tenant']
    publish_enterprise_action(admin, tenant)
    # One executor and one Source holding both effect Actions (a Message Run binds both slots with the same executor).
    executor_token = seed_multi_uuid(admin, tenant, action_targets(['nexloop.service.request', 'nexloop.enterprise.deliver', 'nexloop.service.query']),
                                     suffix='-slot-executor')
    executor = api.authenticate(executor_token, world='real')._session.authentication.subject_principal_id
    declared, rows = source_declarations(tenant, identity_suffix='-slot-source', extra_actions=[DELIVER])
    source_principal = declared.subject_principal_id
    rows.append(message_read_rule_row(tenant, source_principal, m['expiry']))
    rule = RelaySourceReadRule(tenant_id=tenant, principal_id=source_principal, relay_principal=m['route_principal'], active=True,
                               valid_until=datetime.now(UTC) + timedelta(hours=1))
    rows.append({'kind': 'relay_source_read_rule', 'key': [source_principal], 'payload': rule.model_dump(mode='json')})
    source_token = secrets.token_urlsafe(48)
    end = next(r['payload']['expires_at'] for r in rows if r['kind'] == 'authentication')
    admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                  (hashlib.sha256(source_token.encode()).hexdigest(), tenant, declared.credential_id, Jsonb(declared.model_dump(mode='json')), ['real'], 'nexloop-core', 'active', end))
    for row in rows:
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) '
                      'on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload', (tenant, row['kind'], row['key'], Jsonb(row['payload'])))
    expiry = m['expiry']
    people = {}
    for label in ('D', 'E'):  # D: enterprise slot enrolled; E: reply only (the zero-impact pin)
        identity, issued = add_human(f, admin, label)
        owner = api.authenticate(m['owner_token'], world='real')
        keeper = api.authenticate(m['keeper_token'], world='real')
        consumer = owner.create_object(action_name='Consumer.create', action_version=1, intent_id=f'slot-consumer-{label}', type_name='Consumer', properties={})['object_id']
        controls = {}
        for slot, action in (('reply', 'nexloop.service.request'), ('enterprise', 'nexloop.enterprise.deliver')):
            owner = api.authenticate(m['owner_token'], world='real')
            controls[slot] = owner.create_object(action_name='EffectControl.create', action_version=1, intent_id=f'slot-control-{label}-{slot}', type_name='EffectControl',
                properties={'consumer_id': consumer, 'owner_principal': owner._session.authentication.subject_principal_id, 'executor_principal': executor,
                            'budget_units': 3, 'allow_effect': True, 'valid_until': expiry})['object_id']
            owner.configure_effect_control(control_id=controls[slot], control_revision=1, executor_token=executor_token, action=action)
        offerings = {}
        for slot, properties in (('reply', json_export_example(valid_until=expiry)),
                                 ('enterprise', enterprise_delivery_example(service_code='enterprise.synthetic-delivery', title='合成企业交付', price_amount='12.50', valid_until=expiry))):
            keeper = api.authenticate(m['keeper_token'], world='real')
            offered = keeper.create_object(action_name='ServiceOffering.create', action_version=1, intent_id=f'slot-offering-{label}-{slot}', type_name='ServiceOffering',
                                           properties=properties)['object_id']
            binding = keeper.create_object(action_name='ConsumerServiceOffering.create', action_version=1, intent_id=f'slot-binding-{label}-{slot}',
                type_name='ConsumerServiceOffering', properties={'consumer_id': consumer, 'offering_id': offered, 'offering_revision': 1,
                                                                 'source_principal': source_principal, 'active': True})['object_id']
            offerings[slot] = (offered, binding)
        owner = api.authenticate(m['owner_token'], world='real')
        with api._lock:
            ConversationOwnershipRegistrar(api._pool, owner._session, api._signer).register_consumer_owner(
                consumer_id=consumer, principal_id=identity[2].principal_id, idempotency_key=f'slot-ownership-key-{label}')
        human = authenticate_browser_business(api._pool, issued.session, world='real')
        port = ConversationMessagePort(api._pool, human, api._signer)
        conversation = port.create_conversation(idempotency_key=f'slot-conversation-key-{label}')
        message = port.accept_message(conversation_id=conversation['id'], idempotency_key=f'slot-message-key-{label}-0001', body=bodies.get(label, f'{label} 请交付一次服务'))['message']['id']
        people[label] = dict(consumer=consumer, controls=controls, offerings=offerings, message=message)

    def enroll(label, *, enterprise=True, intent=None):
        p = people[label]
        extra = {'enterprise': {'control_id': p['controls']['enterprise'], 'offering_id': p['offerings']['enterprise'][0],
                                'binding_id': p['offerings']['enterprise'][1]}} if enterprise else {}
        return relay_enrollment.enroll(api.authenticate(m['owner_token'], world='real'), consumer_id=p['consumer'], control_id=p['controls']['reply'],
                                       offering_id=p['offerings']['reply'][0], binding_id=p['offerings']['reply'][1], relay_principal=m['route_principal'],
                                       intent_id=intent or f'slot-enroll-{label}-0001', **extra)

    def relay(vault):
        return MessageRelay(route=api.authenticate(f['owner_token'], world='real'), source=api.authenticate(source_token, world='real'),
                            planner=api.authenticate(m['relay_plan']['planner_token'], world='real'), executor_token=executor_token, vault=vault,
                            run_profile=m['run_profile'])
    return dict(m, people=people, enroll=enroll, slot_relay=relay, executor_token=executor_token)


def profile(s, label):
    port = MessageRelayPort(s['api'].authenticate(s['f']['owner_token'], world='real'))
    return port.call('profile', consumer_id=s['people'][label]['consumer'])


def issued_run(s, label):
    admin = s['admin']
    message = s['people'][label]['message']
    run_id = admin.execute('select run_id from authz.nexloop_message_run_issuances where message_id=%s', (message,)).fetchone()[0]
    resources = admin.execute('select allowed_resources from authz.nexloop_run_credentials where run_id=%s', (run_id,)).fetchone()[0]
    slots = admin.execute('select slot_identity from control.nexloop_effect_run_contexts where run_id=%s order by slot_identity', (run_id,)).fetchall()
    assignment = admin.execute("select properties from ontology.objects where type_name='MessageAssignment' and properties->>'message_id'=%s", (message,)).fetchone()[0]
    return run_id, resources, [s for (s,) in slots], assignment


def test_without_an_enterprise_slot_profile_assignment_and_credential_are_unchanged(slots):
    s = slots
    assert s['enroll']('E', enterprise=False)['state'] == 'active'
    p = profile(s, 'E')
    e = s['people']['E']
    # The profile keys and values are exactly the pre-ADR-030 ones (no enterprise key at all): byte pin of the canonical form.
    expected_keys = {'consumer_id', 'control_id', 'control_revision', 'consumer_revision', 'offering_id', 'binding_id', 'valid_until',
                     'enrollment_revision', 'role_id', 'link_id', 'step_id'}
    assert set(p) == expected_keys and p['control_id'] == e['controls']['reply'] and p['offering_id'] == e['offerings']['reply'][0]
    assert canonical_payload(p) == canonical_payload({k: p[k] for k in sorted(expected_keys)})
    with RunCredentialVault(s['vault_dir']) as vault:
        assert s['slot_relay'](vault).run_once() == 'queued'
    _, resources, run_slots, assignment = issued_run(s, 'E')
    assert resources == [REPLY] and run_slots == ['service.request:primary'] and assignment['allowed_actions'] == [REPLY]
    pack = s['admin'].execute('select pack_text from runtime.nexloop_context_artifact_bindings b join authz.nexloop_message_run_issuances i on i.run_id=b.run_id '
                              'where i.message_id=%s', (e['message'],)).fetchone()
    assert pack is None or 'effect_supplies' not in json.loads(pack[0])


def test_an_enrolled_enterprise_slot_gives_one_run_both_actions_and_one_context_per_slot(slots):
    s = slots
    assert s['enroll']('D')['state'] == 'active'
    d = s['people']['D']
    p = profile(s, 'D')
    assert p['enterprise'] == {'control_id': d['controls']['enterprise'], 'control_revision': 1, 'offering_id': d['offerings']['enterprise'][0],
                               'binding_id': d['offerings']['enterprise'][1]}
    with RunCredentialVault(s['vault_dir']) as vault:
        assert s['slot_relay'](vault).run_once() == 'queued'
    _, resources, run_slots, assignment = issued_run(s, 'D')
    assert resources == [REPLY, DELIVER] and assignment['allowed_actions'] == [REPLY, DELIVER]
    assert run_slots == ['enterprise.deliver:primary', 'service.request:primary']
    pack = json.loads(s['admin'].execute('select b.pack_text from runtime.nexloop_context_artifact_bindings b join authz.nexloop_message_run_issuances i '
                                          'on i.run_id=b.run_id where i.message_id=%s', (d['message'],)).fetchone()[0])
    assert [(x['action'], x['offering_id'], x['binding_id']) for x in pack['effect_supplies']] == [('nexloop.enterprise.deliver:1', *d['offerings']['enterprise'])]
    assert pack['supply']['offering_id'] == d['offerings']['reply'][0]  # supply stays the reply export offering
    steps = s['admin'].execute("select properties->>'action_name',properties->>'control_id' from ontology.objects where type_name='PlanStep' "
                               "and properties->>'goal_id'=%s order by 1", (assignment['goal_id'],)).fetchall()
    assert steps == [('nexloop.enterprise.deliver', d['controls']['enterprise']), ('nexloop.service.request', d['controls']['reply'])]


def test_an_enrolled_slot_no_longer_valid_gives_the_reply_slot_only_and_a_structured_log(slots, caplog):
    s = slots
    s['enroll']('D')
    d = s['people']['D']
    with s['admin'].transaction():
        s['admin'].execute('set local role nexloop_owner')
        s['admin'].execute("select set_config('eios.tenant_id',%s,true)", (s['tenant'],))
        s['admin'].execute("update ontology.objects set properties=properties||'{\"active\":false}'::jsonb where object_id=%s", (d['offerings']['enterprise'][0],))
    assert profile(s, 'D')['enterprise_skipped'] == 'offering_invalid'
    with caplog.at_level(logging.INFO, logger='nexloop.message_relay'), RunCredentialVault(s['vault_dir']) as vault:
        assert s['slot_relay'](vault).run_once() == 'queued'
    _, resources, run_slots, assignment = issued_run(s, 'D')
    assert resources == [REPLY] and run_slots == ['service.request:primary'] and assignment['allowed_actions'] == [REPLY]
    logged = [r for r in caplog.records if getattr(r, 'event', None) == 'enterprise_slot_skipped']
    assert logged and logged[0].reason == 'offering_invalid' and logged[0].consumer_id == d['consumer']


def test_enterprise_slot_enrollment_rules(slots):
    s = slots
    d, e = s['people']['D'], s['people']['E']
    owner = s['api'].authenticate(s['owner_token'], world='real')
    base = dict(consumer_id=d['consumer'], control_id=d['controls']['reply'], offering_id=d['offerings']['reply'][0], binding_id=d['offerings']['reply'][1],
                relay_principal=s['route_principal'])
    bad = {
        'export_offering': {'control_id': d['controls']['enterprise'], 'offering_id': d['offerings']['reply'][0], 'binding_id': d['offerings']['reply'][1]},
        'other_consumer_control': {'control_id': e['controls']['enterprise'], 'offering_id': d['offerings']['enterprise'][0], 'binding_id': d['offerings']['enterprise'][1]},
        'other_consumer_binding': {'control_id': d['controls']['enterprise'], 'offering_id': e['offerings']['enterprise'][0], 'binding_id': e['offerings']['enterprise'][1]},
        'reply_control_reused': {'control_id': d['controls']['reply'], 'offering_id': d['offerings']['enterprise'][0], 'binding_id': d['offerings']['enterprise'][1]},
    }
    for index, (case, enterprise) in enumerate(bad.items()):
        with pytest.raises(relay_enrollment.RelayEnrollmentUnavailable):
            relay_enrollment.enroll(s['api'].authenticate(s['owner_token'], world='real'), intent_id=f'slot-bad-{index}-0001', enterprise=enterprise, **base)
        assert s['admin'].execute('select count(*) from control.nexloop_relay_enrollments where consumer_id=%s', (d['consumer'],)).fetchone() == (0,), case
    with pytest.raises(relay_enrollment.RelayEnrollmentUnavailable):  # all three or none, client side
        relay_enrollment.enroll(owner, intent_id='slot-partial-0001', enterprise={'control_id': d['controls']['enterprise']}, **base)
    # Enroll, then clear the slot by enrolling again without it (a new revision; the rows are the audit).
    assert s['enroll']('D')['enrollment_revision'] == 1
    cleared = relay_enrollment.enroll(s['api'].authenticate(s['owner_token'], world='real'), intent_id='slot-clear-D-0001', **base)
    assert cleared['enrollment_revision'] == 2 and 'enterprise' not in profile(s, 'D') and 'enterprise_skipped' not in profile(s, 'D')
    rows = s['admin'].execute('select enterprise_control_id is not null,enrolled_by is not null,intent_id from control.nexloop_relay_enrollments '
                              'where consumer_id=%s order by enrollment_revision', (d['consumer'],)).fetchall()
    assert rows == [(True, True, 'slot-enroll-D-0001'), (False, True, 'slot-clear-D-0001')]
    # The table is append-only and enforces all-or-none on every row.
    with s['admin'].transaction(force_rollback=True), pytest.raises(psycopg.errors.CheckViolation):
        s['admin'].execute('''insert into control.nexloop_relay_enrollments(tenant_id,world,consumer_id,enrollment_revision,state,relay_principal,control_id,offering_id,
            binding_id,enrolled_by,intent_id,enterprise_control_id) values(%s,'real',%s,3,'active',%s,%s,%s,%s,'synthetic','slot-partial-row-0001',%s)''',
                           (s['tenant'], d['consumer'], s['route_principal'], d['controls']['reply'], d['offerings']['reply'][0], d['offerings']['reply'][1],
                            d['controls']['enterprise']))


@pytest.mark.parametrize('role', ['nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_identity'])
def test_runtime_roles_cannot_write_the_enterprise_slot(slots, role):
    s = slots
    s['enroll']('D')
    with s['admin'].transaction(force_rollback=True):
        s['admin'].execute(f'set local role {role}')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            s['admin'].execute('update control.nexloop_relay_enrollments set enterprise_control_id=null,enterprise_offering_id=null,enterprise_binding_id=null')

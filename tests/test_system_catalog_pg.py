"""NX-032 A1 T1: nexloop_eios.system_catalog (production builders) against the definitions the test fixtures publish today.

Until the fixtures read system_catalog (T4), every system Action and Function is defined twice. These tests build each fixture on
clean catalog PostgreSQL, read the definitions it published, and compare the fields readers and 0050 depend on (name, version,
scopes, object types, target systems, idempotency keys, risk, approval, policy references, preconditions, parameters, input,
output and receipt schemas, capability name, kind, side effects, idempotency). Not compared: timestamps, authors, digests and
capability schema hashes (placeholders in the fixtures), and the Consumer schema (the catalog publishes the fresh-tenant
profile declaration; design §1.1)."""
import pytest

from nexloop_eios import system_catalog as C
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from effect_execution_fixture import execution_plan  # noqa: F401
from test_scope_denials import context_message, active_worker, denial_plan, declare_record_authority  # noqa: F401
from test_enterprise_slot_relay_pg import publish_enterprise_action
from datetime import UTC, datetime

from support.tenants import TENANT_A

# Which fixture is the reference for which Action (one reference each; together they cover every built Action).
COVERAGE = {
    'assembled_message': {'nexloop.conversation.route', 'nexloop.service.request', 'Goal.create', 'PlanStep.create', 'MessageAssignment.create',
                          'nexloop.plan.bind_effect_context', 'nexloop.context.bind', 'NexLoop.queue.operations', 'Conversation.create', 'Message.create',
                          'ConsumerOwnership.create', 'nexloop.conversation.read', 'EffectControl.create', 'nexloop.effect.configure_control',
                          'nexloop.service.query', 'Consumer.create', 'nexloop.enterprise.deliver'},
    'runtime_effect_plan': {'nexloop.service.receipt_reconcile'},
    'execution_plan': {'Goal.edit', 'EffectControl.edit'},
    'role_runtime_plan': {'ServiceOffering.create', 'ConsumerServiceOffering.create', 'RoleDefinition.create', 'ConsumerRoleLink.create',
                          'RoleExecutionCeiling.create', 'RoleAssignmentScope.create', 'ConsumerRoleLink.edit', 'RoleExecutionCeiling.edit',
                          'RoleAssignmentScope.edit', 'nexloop.context.bind_role'},
    'denial_plan': {'nexloop.service.scope_denial.record'},
}
FUNCTION_COVERAGE = {'assembled_message': {'nexloop.conversation.service_receipt'}, 'denial_plan': {'nexloop.conversation.scope_denial'}}


def action_key(definition, capability):
    g = definition['governance']
    return {'stable_name': definition['stable_name'], 'version': definition['version'], 'required_scopes': sorted(definition['required_scopes']),
            'object_types': sorted(r['stable_name'] for r in definition['object_types']),  # ordered by digest (Consumer differs)
            'change_scope_types': sorted(r['stable_name'] for r in g['change_scope']['object_types']),
            'target_systems': g['change_scope']['target_systems'], 'key_fields': g['idempotency']['key_fields'], 'risk': g['risk_level'],
            'approval': g['approval_mode'], 'policy_refs': g['policy_refs'], 'preconditions': definition.get('preconditions'),
            'parameters': definition.get('parameters'), 'input_schema': definition.get('input_schema'), 'receipt_schema': definition['receipt_schema'],
            'capability': capability['capability_name'], 'binding_name': definition['capability_binding']['capability_name'], 'kind': capability['kind'],
            'side_effects': capability['has_side_effects'], 'idempotent': capability['idempotent'], 'capability_scopes': sorted(capability['required_scopes'])}


def function_key(definition, capability):
    return {'stable_name': definition['stable_name'], 'version': definition['version'], 'required_scopes': sorted(definition['required_scopes']),
            'applies_to': sorted(a['object_type']['stable_name'] for a in definition['applies_to']), 'input_schema': definition['input_schema'],
            'output_schema': definition['output_schema'], 'capability': capability['capability_name'], 'side_effects': capability['has_side_effects'],
            'idempotent': capability['idempotent'], 'capability_scopes': sorted(capability['required_scopes'])}


def built(tenant):
    args = dict(tenant=tenant, created_by='catalog-check', created_at=datetime(2026, 10, 15, tzinfo=UTC))
    return ({r['definition']['stable_name']: action_key(r['definition'], r['capability']) for r in C.compile_system_actions(**args)},
            {r['definition']['stable_name']: function_key(r['definition'], r['capability']) for r in C.compile_system_functions(**args)})


def published(admin, tenant):
    actions = {d['stable_name']: action_key(d, c) for d, c in admin.execute(
        "select definition,capability from control.nexloop_action_definitions where tenant_id=%s and world='real'", (tenant,))}
    functions = {d['stable_name']: function_key(d, c) for d, c in admin.execute(
        "select definition,capability from control.nexloop_function_definitions where tenant_id=%s and world='real'", (tenant,))}
    types = {name: body for name, body in admin.execute('select type_name,definition from ontology.object_type_versions where tenant_id=%s and version=1', (tenant,))}
    return actions, functions, types


def compare(admin, tenant, label):
    actions, functions, types = published(admin, tenant)
    ours, our_functions = built(tenant)
    for name in COVERAGE[label]:
        assert name in actions, (label, name, 'not published by the fixture')
        assert ours[name] == actions[name], (label, name, {k: (ours[name][k], actions[name][k]) for k in ours[name] if ours[name][k] != actions[name][k]})
    for name in FUNCTION_COVERAGE.get(label, ()):
        assert our_functions[name] == functions[name], (label, name)
    # Object types the fixture published from the same production schema functions are identical (Consumer: design §1.1).
    catalog = C.object_types()
    for name, body in types.items():
        if name in catalog and name != 'Consumer':
            assert catalog[name].model_dump(mode='json') == body, (label, name)


def test_the_coverage_lists_every_built_action_and_function_once():
    names = [n for group in COVERAGE.values() for n in group]
    # The workbench goal Actions have no fixture definition to compare with (tests/goal_fixture.py clones a load-chain Action).
    assert len(names) == len(set(names)) and set(names) | set(C.GOAL_ACTIONS) == {s.name for s in C.ACTIONS}
    assert {n for group in FUNCTION_COVERAGE.values() for n in group} == {s.name for s in C.FUNCTIONS}
    assert not set(C.GOAL_ACTIONS) & set(names)


def test_builders_are_deterministic_and_carry_their_source():
    a, b = built('00000000-0000-4000-8000-000000000001'), built('00000000-0000-4000-8000-000000000001')
    assert a == b and all(s.source for s in C.ACTIONS + C.FUNCTIONS)


@pytest.mark.parametrize('label', ['assembled_message'])
def test_assembled_message_definitions_match(assembled_message, admin, label):  # noqa: F811
    tenant = assembled_message['original']['tenant']
    publish_enterprise_action(admin, tenant)
    compare(admin, tenant, label)


def test_runtime_effect_definitions_match(runtime_effect_plan, admin):  # noqa: F811
    compare(admin, runtime_effect_plan['tenant'], 'runtime_effect_plan')


def test_execution_definitions_match(execution_plan, admin):  # noqa: F811
    compare(admin, TENANT_A, 'execution_plan')  # the fixture's tenant


def test_role_runtime_definitions_match(role_runtime_plan, admin):  # noqa: F811
    compare(admin, role_runtime_plan['tenant'], 'role_runtime_plan')


def test_scope_denial_definitions_match(denial_plan, admin):  # noqa: F811
    compare(admin, denial_plan['original']['tenant'], 'denial_plan')


def test_the_name_list_for_t2_is_the_builders_list():
    import json
    from pathlib import Path
    names = json.loads((Path(__file__).resolve().parents[1] / 'deploy/configuration/system-catalog-names.v1.json').read_text())
    listed = {a['stable_name']: a for a in names['system_actions']}
    assert set(listed) == {s.name for s in C.ACTIONS}
    assert all(listed[s.name]['capability'] == s.capability and sorted(listed[s.name]['object_types']) == sorted(s.types) for s in C.ACTIONS)
    assert {f['stable_name']: f['capability'] for f in names['functions']} == {s.name: s.capability for s in C.FUNCTIONS}
    assert {t['type_name'] for t in names['object_types']} >= set(C.object_types())


def test_builder_keys_named_by_t2_exist_and_build():
    keys = {'consumer_schema', 'effect_plan_schemas', 'conversation_schemas', 'message_assignment_schema', 'service_offering_schemas',
            'incentive_offering_schemas', 'role_mapping_schemas', 'role_policy_schemas', 'registrar_schema:configure', 'registrar_schema:bind',
            'context_binding_schema', 'role_context_binding_schema', 'enterprise_deliver_schema', 'message_assignment_input',
            'service_receipt_function', 'scope_denial_function'}
    assert keys <= set(C.builders()) and all(C.builders()[k]() is not None for k in keys)

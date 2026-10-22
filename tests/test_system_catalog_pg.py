"""NX-032 A1 T1: nexloop_eios.system_catalog (production builders) against the definitions the test fixtures publish today.

Until the fixtures read system_catalog (T4), every system Action and Function is defined twice. These tests build each fixture on
clean catalog PostgreSQL, read the definitions it published, and compare the fields readers and 0050 depend on (name, version,
scopes, object types, target systems, idempotency keys, risk, approval, policy references, preconditions, parameters, input,
output and receipt schemas, capability name, kind, side effects, idempotency). Not compared: timestamps, authors, digests and
capability schema hashes (placeholders in the fixtures), and the Consumer schema (the catalog publishes the fresh-tenant
profile declaration; design §1.1)."""
import json

import pytest

from nexloop_eios import system_catalog as C
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from effect_execution_fixture import execution_plan  # noqa: F401
from test_scope_denials import context_message, active_worker, denial_plan, declare_record_authority  # noqa: F401
from test_enterprise_slot_relay_pg import publish_enterprise_action
from datetime import UTC, datetime
from pathlib import Path

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


ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    from nexloop_eios import catalog_manifests
    return catalog_manifests.snapshots(catalog_manifests.load(ROOT / 'deploy/configuration/capabilities.v1.json'))


def built(tenant):
    args = dict(tenant=tenant, created_by='catalog-check', created_at=datetime(2026, 10, 15, tzinfo=UTC), snapshots=snapshots())
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


def test_the_publication_equals_the_builders_and_every_snapshot_is_the_capabilities_manifest_entry():
    """T3 (dispatcher rulings 1-3): the catalog compiled from the manifests (catalog_publish) is the builders' catalog item by item,
    every snapshot is the capabilities.v1.json entry it names, role Actions carry ontology.roles.manage in both (0057/0081), and
    nexloop.enterprise.deliver targets the service system."""
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import stage_secrets as S
    from nexloop_eios import catalog_publish as P
    tenant = '00000000-0000-4000-8000-000000000001'
    compiled = P.compile_catalog(tenant, **S.CATALOG)
    snaps = snapshots()
    for item in compiled['actions'] + compiled['functions']:
        binding = item['definition']['capability_binding']
        assert item['capability'] == snaps[(binding['capability_name'], binding['capability_version'])].model_dump(mode='json'), binding
    created_at = datetime.fromisoformat(json.loads((ROOT / 'deploy/configuration/system-actions.v1.json').read_text())['valid_from'])
    args = dict(tenant=tenant, created_by='nexloop-system-catalog', created_at=created_at, snapshots=snaps)
    ours = {r['definition']['stable_name']: r for r in C.compile_system_actions(**args) + C.compile_system_functions(**args)}
    published = {r['definition']['stable_name']: r for r in compiled['actions'] + compiled['functions']}
    assert set(ours) <= set(published)
    for name, row in ours.items():
        assert row == published[name], name
    roles = [s for s in C.ACTIONS if s.group == 'role_run' and s.name != 'nexloop.context.bind_role']
    for spec in roles:
        row = published[spec.name]
        assert 'ontology.roles.manage' in row['definition']['required_scopes'] and 'ontology.roles.manage' in row['capability']['required_scopes'], spec.name
    assert published['nexloop.enterprise.deliver']['definition']['governance']['change_scope']['target_systems'] == ['service']
    # 0101 nexloop_review_type_action_capabilities takes, per capability name, the snapshot of the first Action by resource_id. That
    # Action must bind 1.0.0 (not a role Action's 1.1.0): checked here on the catalog, on PostgreSQL in test_stage_register_pg.
    for name in ('ontology.object.create', 'ontology.object.edit'):
        first = min((f"eios:action:{n}:1", r) for n, r in published.items() if r['capability']['capability_name'] == name)[1]
        assert first['capability']['capability_version'] == '1.0.0', name


def test_the_seven_role_actions_as_published_execute_through_0057_and_0081(runtime_effect_plan, admin):  # noqa: F811
    """Dispatcher ruling (2026-10-15): role Actions bind the 1.1.0 snapshots. On clean PostgreSQL the role types and the seven role
    Actions exactly as catalog_publish compiles them are installed (as the fixtures install definitions), and a maintainer holding
    ontology.roles.manage creates the ceiling, role, scope and link, edits the ceiling and scope, and ends the link through the
    real SQL paths (0057 create/edit, 0081 role policy management)."""
    import sys
    from datetime import timedelta
    from psycopg.types.json import Jsonb
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from nexloop_eios import catalog_publish as P
    from nexloop_eios.role_mapping import RoleMappingPort
    from nexloop_eios.role_policies import RolePolicyPort
    import runtime_effect_fixture as fixture
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import stage_secrets as S
    plan = runtime_effect_plan
    tenant, api, consumer, goal, step = plan['tenant'], plan['api'], plan['consumer'], plan['goal'], plan['step']
    compiled = P.compile_catalog(tenant, **S.CATALOG)
    role_names = {s.name for s in C.ACTIONS if s.group == 'role_run' and s.name != 'nexloop.context.bind_role'}
    role_types = {'RoleDefinition', 'ConsumerRoleLink', 'RoleExecutionCeiling', 'RoleAssignmentScope'}
    for row in compiled['object_types']:
        if row['type_name'] in role_types:
            admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)', (tenant, row['type_name'], Jsonb(row)))
    for item in compiled['actions']:
        d = item['definition']
        if d['stable_name'] in role_names:
            assert item['capability']['capability_version'] == '1.1.0' and item['capability']['required_scopes'] == ['ontology.roles.manage']
            admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                          (tenant, 'real', f"eios:action:{d['stable_name']}:1", Jsonb(d), Jsonb(item['capability'])))
        else:
            assert item['capability']['capability_version'] == '1.0.0', d['stable_name']
    targets = [('eios:action:' + n + ':1', ResourceType.ACTION, Operation.EXECUTE) for n in sorted(role_names)]
    token = fixture.seed_multi_uuid(admin, tenant, targets, suffix='-catalog-role-maintainer', extra_scopes=('ontology.roles.manage',))
    from datetime import UTC, datetime as dt
    now = dt.now(UTC)
    valid = dict(valid_from=(now - timedelta(seconds=1)).isoformat(), valid_until=(now + timedelta(minutes=5)).isoformat())
    budget = {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 60, 'maximum_cost': '1.0', 'currency': 'USD'}

    def session():
        return api.authenticate(token, world='real')
    ceiling = RolePolicyPort(session()).create(kind='RoleExecutionCeiling', intent_id='catalog-ceiling', properties=dict(active=True,
        action_resources=['eios:action:' + fixture.EFFECT + ':1'], consumer_ids=[consumer], goal_ids=[goal], step_ids=[step], budget=budget, effect_units=1, **valid))
    role = RoleMappingPort(session()).create_role(intent_id='catalog-role', name='catalog-role', responsibility='governed service responsibility',
                                                 ceiling_ref=ceiling['object_id'], **valid)
    scope = RolePolicyPort(session()).create(kind='RoleAssignmentScope', intent_id='catalog-scope', properties=dict(active=True, role_id=role['object_id'],
        consumer_id=consumer, goal_ids=[goal], step_ids=[step], **valid))
    link = RoleMappingPort(session()).assign(intent_id='catalog-link', consumer_id=consumer, role_id=role['object_id'], scope=scope['object_id'], **valid)
    # Edits also need the object/property EDIT grants on the objects just created (as tests/role_run_fixture.py grants them).
    from nexloop_eios.role_policies import CEILING_FIELDS, SCOPE_FIELDS
    edit_targets = [('eios:action:' + n + ':1', ResourceType.ACTION, Operation.EXECUTE) for n in ('RoleExecutionCeiling.edit', 'RoleAssignmentScope.edit',
                                                                                                 'ConsumerRoleLink.edit')]
    for kind, obj, fields in (('RoleExecutionCeiling', ceiling['object_id'], CEILING_FIELDS), ('RoleAssignmentScope', scope['object_id'], SCOPE_FIELDS),
                              ('ConsumerRoleLink', link['object_id'], ('active',))):
        edit_targets.append(('eios:object:' + kind + '/' + obj, ResourceType.OBJECT, Operation.EDIT))
        edit_targets.extend(('eios:property:' + kind + '/' + obj + '/' + f, ResourceType.PROPERTY, Operation.EDIT) for f in fields)
    editor = fixture.seed_multi_uuid(admin, tenant, edit_targets, suffix='-catalog-role-editor', extra_scopes=('ontology.roles.manage',))
    for kind, created in (('RoleExecutionCeiling', ceiling), ('RoleAssignmentScope', scope)):
        properties, revision = admin.execute('select properties,nexloop_revision from ontology.objects where tenant_id=%s and object_id=%s',
                                             (tenant, created['object_id'])).fetchone()
        RolePolicyPort(api.authenticate(editor, world='real')).edit(kind=kind, intent_id='catalog-edit-' + kind, object_id=created['object_id'], expected_revision=revision,
                                       properties={**properties, 'active': False})
    RoleMappingPort(api.authenticate(editor, world='real')).end(intent_id='catalog-end', link_id=link['object_id'], expected_revision=1)
    rows = admin.execute("select type_name,nexloop_revision from ontology.objects where tenant_id=%s and type_name = any(%s) order by type_name",
                         (tenant, sorted(role_types))).fetchall()
    assert rows == [('ConsumerRoleLink', 2), ('RoleAssignmentScope', 2), ('RoleDefinition', 1), ('RoleExecutionCeiling', 2)]


# NX-032 A1 T4b: test files allowed to write definition rows directly. "catalog": the rows come from the production catalog
# (system_catalog / capabilities.v1.json) and the insert is only the test publication; "exception": a deliberate negative case,
# variant or test-only type, explained by a `# catalog-exception:` comment at each insert. Only shrinks: a new file that inserts
# definitions fails; a file that stops inserting must be removed from the list.
DIRECT_DEFINITION_WRITERS = {
    'tests/system_catalog_fixture.py': 'catalog', 'tests/commitment_fixture.py': 'catalog', 'tests/commercial_fixture.py': 'catalog',
    'tests/load_chain.py': 'catalog', 'tests/test_alert_silence_pg.py': 'catalog',
    'tests/test_context_audit_pg.py': 'catalog', 'tests/test_context_engine_pg.py': 'catalog', 
    'tests/test_system_catalog_pg.py': 'catalog',
    'tests/generic_offering_fixture.py': 'exception', 'tests/message_offering_fixture.py': 'exception', 'tests/test_action_definitions.py': 'exception',
    'tests/test_assessment_candidate.py': 'exception', 'tests/test_business_actions.py': 'exception', 'tests/test_claim_matching_pg.py': 'exception',
    'tests/test_goal_controls.py': 'exception',
    'tests/test_instance_approval_pg.py': 'exception', 'tests/test_ladder_deadlock_pg.py': 'exception',
    'tests/test_message_purpose_reads_pg.py': 'exception', 'tests/test_object_edits.py': 'exception', 'tests/test_receipt_tail.py': 'exception',
    'tests/test_relation_unlink_pg.py': 'exception',  # NX-054 P1: the synthetic Peer link/unlink Actions, as test_relation_actions
    'tests/test_relation_actions.py': 'exception', 'tests/test_role_mapping.py': 'exception', 'tests/test_role_policy_binding.py': 'exception',
    'tests/test_role_policy_governance.py': 'exception', 'tests/test_role_runs_first.py': 'exception', 'tests/test_runtime_authority.py': 'exception',
    'tests/test_tenant_config_pg.py': 'exception',
    'tests/test_nx041_sim_catalog_publish_pg.py': 'exception', 'tests/test_at046_stage_pg.py': 'exception',
}


def test_direct_definition_writers_only_shrink():
    import re
    pattern = re.compile(r'insert\s+into\s+control\.nexloop_(action|function)_definitions', re.I)
    writers = {}
    for path in sorted((ROOT / 'tests').rglob('*.py')):
        lines = path.read_text().split('\n')
        hits = [i for i, line in enumerate(lines) if pattern.search(line)]
        if hits:
            writers[str(path.relative_to(ROOT))] = (lines, hits)
    assert set(writers) == set(DIRECT_DEFINITION_WRITERS), {'new (use system_catalog_fixture)': sorted(set(writers) - set(DIRECT_DEFINITION_WRITERS)),
                                                           'gone (remove from the list)': sorted(set(DIRECT_DEFINITION_WRITERS) - set(writers))}
    for path, kind in DIRECT_DEFINITION_WRITERS.items():
        if kind == 'exception':
            lines, hits = writers[path]
            unexplained = [i + 1 for i in hits if not any('catalog-exception:' in lines[j] for j in range(max(0, i - 3), i))]
            assert not unexplained, (path, unexplained)

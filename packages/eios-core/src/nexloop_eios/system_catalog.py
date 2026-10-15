"""NX-032 A1 T1 (docs/implementation/NX-032-stage-publish-design.md §1): the system object types, Actions and Functions every
new tenant needs before its workers run, built by production code instead of test fixtures.

Pure and deterministic: no database, no clock (created_at and created_by are arguments, so a publication can be replayed
byte for byte through 0050, which accepts an existing definition only when it is identical). Nothing here publishes; the
trusted configuration (configurator) does that (T3). Every Action and Function names the fixture it was taken from (`source`),
and tests/test_system_catalog.py keeps both in step until the fixtures read this module (T4).

The Action template is the fixtures' common base (tests/test_postgres_action_claims.py:governance_inputs): low risk, approval
none, no policy references, no preconditions or parameters, idempotency key request_id, target system postgres, scope
action.execute, receipt schema {'type': 'object'}. The readers check exactly that (message_relay.py, effect_execution.py,
effect_contexts.py, context_artifacts.py, conversation_messages.py). Every Action's capability snapshot declares side effects:
0050 refuses an Action whose snapshot does not (mig/0162), the fixtures do the same.
"""
from dataclasses import dataclass
import hashlib

from eios.ontology.definitions import (ActionApprovalMode, ActionChangeScope, ActionDefinition, ActionGovernanceContract, ActionIdempotencyPolicy,
    ActionRiskLevel, DefinitionStatus, FunctionDefinition, OntologySchemaReference, OntologySchemaType)
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractKind, CapabilityContractSnapshot, validate_capability_binding
from nexloop_eios.postgres_artifacts import canonical_payload

OBJECT_CREATE = 'ontology.object.create'
OBJECT_EDIT = 'ontology.object.edit'
ACTION_CAPABILITY_VERSION = '1.0.0'  # the fixtures' binding (tests/test_postgres_action_claims.py:request)
FUNCTION_CAPABILITY_VERSION = '1'    # tests/local_message_assembly_fixture.py:assembled_message
# The service effect input (tests/runtime_effect_fixture.py:runtime_effect_plan, tests/message_runtime_fixture.py:message_plan).
MESSAGE_INPUT = {'type': 'object', 'properties': {'message': {'type': 'string', 'minLength': 1}}, 'required': ['message'], 'additionalProperties': False}
RECONCILE_INPUT = {'type': 'object', 'properties': {'intent_id': {'type': 'string'}, 'effect_fence': {'type': 'integer'}, 'query_id': {'type': 'string'}},
                   'required': ['intent_id', 'effect_fence', 'query_id'], 'additionalProperties': False}
# The enterprise delivery input (tests/test_enterprise_slot_relay_pg.py:DELIVER_SCHEMA).
DELIVER_INPUT = {'type': 'object', 'properties': {'summary': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                 'details': {'type': 'string', 'maxLength': 2000}, 'consumer_ref': {'type': 'string', 'minLength': 1, 'maxLength': 80}},
                 'required': ['summary'], 'additionalProperties': False}
BIND_TYPES = ('Consumer', 'Goal', 'PlanStep', 'EffectControl')


def object_types():
    """{type name: ObjectTypeDefinition v1} built by the production schema functions (design §1.1).

    Consumer: consumer_profile.consumer_profile_schema, the fresh-tenant declaration with the profile properties (the fixtures use
    a bare Consumer v1; consumer_resolution.consumer_profile_schema is a one-property subset used by its own tests). Decision
    recorded in the design §1.1: v1 is immutable once published, so the richer declaration is the one published."""
    from nexloop_eios.consumer_profile import consumer_profile_schema
    from nexloop_eios.conversation_messages import conversation_schemas
    from nexloop_eios.effect_contexts import effect_plan_schemas
    from nexloop_eios.incentives import offering_schemas as incentive_schemas
    from nexloop_eios.message_relay import message_assignment_schema
    from nexloop_eios.role_mapping import role_mapping_schemas
    from nexloop_eios.role_policies import role_policy_schemas
    from nexloop_eios.service_offerings import offering_schemas
    schemas = [consumer_profile_schema(), *effect_plan_schemas(), *conversation_schemas(), message_assignment_schema(), *offering_schemas(),
               *incentive_schemas(), *role_mapping_schemas(), *role_policy_schemas()]
    out = {}
    for schema in schemas:
        if schema.type_name in out or schema.version != 1:
            raise ValueError('system object type duplicated or not v1: ' + schema.type_name)
        out[schema.type_name] = schema
    return out


@dataclass(frozen=True)
class ActionSpec:
    name: str
    types: tuple
    capability: str
    group: str
    source: str
    target_systems: tuple = ('postgres',)
    key_fields: tuple = ('request_id',)
    scopes: tuple = ('action.execute',)
    input: str | None = None  # key of INPUTS


def _inputs():
    from nexloop_eios.context_artifacts import context_binding_schema
    from nexloop_eios.effect_contexts import registrar_schema
    from nexloop_eios.role_context_artifacts import role_context_binding_schema
    from nexloop_eios.scope_denials import record_schema
    return {'configure': registrar_schema('configure'), 'bind': registrar_schema('bind'), 'context_bind': context_binding_schema(),
            'role_context_bind': role_context_binding_schema(), 'message': MESSAGE_INPUT, 'reconcile': RECONCILE_INPUT, 'deliver': DELIVER_INPUT,
            'scope_denial_record': record_schema()}


def _create(name, group, source, scopes=('action.execute',)):
    return ActionSpec(name + '.create', (name,), OBJECT_CREATE, group, source, scopes=scopes)


ROLES = ('ontology.roles.manage',)
# Design §1.2, in the order of the name list (deploy/configuration/system-catalog-names.v1.json).
ACTIONS = (
    # message-relay (and reply-guarantor fallback)
    ActionSpec('nexloop.conversation.route', ('Conversation',), 'nexloop.conversation.route', 'relay', 'tests/message_runtime_fixture.py:message_plan'),
    ActionSpec('nexloop.service.request', ('Consumer',), 'nexloop.service.request', 'relay', 'tests/message_runtime_fixture.py:message_plan',
               target_systems=('service',), input='message'),
    _create('Goal', 'relay', 'tests/message_runtime_fixture.py:message_plan'),
    _create('PlanStep', 'relay', 'tests/message_runtime_fixture.py:message_plan'),
    _create('MessageAssignment', 'relay', 'nexloop_eios.message_assignment_contract:message_assignment_action'),
    ActionSpec('nexloop.plan.bind_effect_context', BIND_TYPES, 'nexloop.plan.bind_effect_context', 'relay',
               'tests/local_message_assembly_fixture.py:assembled_message', input='bind'),
    ActionSpec('nexloop.context.bind', BIND_TYPES, 'nexloop.context.bind', 'relay', 'tests/local_message_assembly_fixture.py:assembled_message',
               input='context_bind'),
    ActionSpec('NexLoop.queue.operations', ('Consumer',), 'NexLoop.queue.operations', 'relay', 'tests/local_message_assembly_fixture.py:assembled_message'),
    ActionSpec('nexloop.service.scope_denial.record', BIND_TYPES, 'nexloop.service.scope_denial.record', 'relay', 'tests/test_scope_denials.py:denial_plan',
               input='scope_denial_record'),
    # conversation / webchat
    _create('Conversation', 'conversation', 'tests/message_runtime_fixture.py:message_plan'),
    _create('Message', 'conversation', 'tests/message_runtime_fixture.py:message_plan'),
    _create('ConsumerOwnership', 'conversation', 'tests/message_runtime_fixture.py:message_plan'),
    ActionSpec('nexloop.conversation.read', ('Conversation',), 'nexloop.conversation.read', 'conversation', 'tests/message_runtime_fixture.py:message_plan'),
    # effect / action-worker / webchat-delivery
    _create('EffectControl', 'effect', 'tests/runtime_effect_fixture.py:runtime_effect_plan'),
    ActionSpec('nexloop.effect.configure_control', ('EffectControl',), 'nexloop.effect.configure_control', 'effect',
               'tests/runtime_effect_fixture.py:runtime_effect_plan', input='configure'),
    ActionSpec('nexloop.service.query', ('Consumer',), 'nexloop.service.query', 'effect', 'tests/runtime_effect_fixture.py:runtime_effect_plan'),
    ActionSpec('nexloop.service.receipt_reconcile', ('Consumer',), 'nexloop.service.receipt_reconcile', 'effect',
               'tests/runtime_effect_fixture.py:runtime_effect_plan', target_systems=('service',), key_fields=('intent_id',), input='reconcile'),
    ActionSpec('Goal.edit', ('Goal',), OBJECT_EDIT, 'effect', 'tests/effect_execution_fixture.py:execution_plan'),
    ActionSpec('EffectControl.edit', ('EffectControl',), OBJECT_EDIT, 'effect', 'tests/effect_execution_fixture.py:execution_plan'),
    ActionSpec('nexloop.enterprise.deliver', ('Consumer',), 'nexloop.enterprise.deliver', 'effect', 'tests/test_enterprise_slot_relay_pg.py:publish_enterprise_action',
               target_systems=('service',), input='deliver'),
    # business / catalog / enterprise setup
    _create('Consumer', 'setup', 'tests/test_business_setup_pg.py:business_plan'),
    _create('ServiceOffering', 'setup', 'tests/runtime_effect_fixture.py:install_runtime_catalog'),
    _create('ConsumerServiceOffering', 'setup', 'tests/runtime_effect_fixture.py:install_runtime_catalog'),
    # role runtime (plan-reevaluator)
    _create('RoleDefinition', 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    _create('ConsumerRoleLink', 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    _create('RoleExecutionCeiling', 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    _create('RoleAssignmentScope', 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    ActionSpec('ConsumerRoleLink.edit', ('ConsumerRoleLink',), 'consumer.edit', 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    ActionSpec('RoleExecutionCeiling.edit', ('RoleExecutionCeiling',), OBJECT_EDIT, 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    ActionSpec('RoleAssignmentScope.edit', ('RoleAssignmentScope',), OBJECT_EDIT, 'role_run', 'tests/role_run_fixture.py:role_runtime_plan', scopes=ROLES),
    # Typed on the service effect Action's types (the fixture copies nexloop.service.request's object types).
    ActionSpec('nexloop.context.bind_role', ('Consumer',), 'nexloop.context.bind_role', 'role_run', 'tests/role_run_fixture.py:role_runtime_plan',
               input='role_context_bind'),
)
# Workbench goal Actions (tests/goal_fixture.py:ACTIONS): their object type is an open design decision (design §2.1, the fixtures
# clone a Consumer-typed definition), so they are listed, not built, until it is decided.
PENDING_GOAL_ACTIONS = {'Metric.approve': 'goals.metric.approve', 'Goal.publish': 'goals.version.publish', 'Goal.propose': 'goals.agent.propose',
                        'Control.set': 'goals.control.set', 'Budget.set': 'goals.budget.set', 'Contact.release': 'goals.contact.release'}


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    capability: str
    types: tuple
    schemas: str
    source: str


FUNCTIONS = (
    FunctionSpec('nexloop.conversation.service_receipt', 'nexloop.conversation.receipts.read', ('Consumer', 'Conversation', 'Message'), 'receipt',
                 'tests/local_message_assembly_fixture.py:assembled_message'),
    FunctionSpec('nexloop.conversation.scope_denial', 'nexloop.conversation.scope_denials.read', ('Consumer', 'Conversation', 'Message'), 'scope_denial',
                 'tests/test_scope_denials.py:denial_plan'),
)


def _function_schemas(key):
    if key == 'receipt':
        from nexloop_eios.conversation_effect_receipts import receipt_query_schemas
        return receipt_query_schemas()
    from nexloop_eios.conversation_scope_denials import query_schemas
    return query_schemas()


def _ref(tenant, schema):
    return OntologySchemaReference(tenant_id=tenant, schema_type=OntologySchemaType.OBJECT_TYPE, stable_name=schema.type_name, version=schema.version,
                                   schema_digest=schema_contract_digest(schema))


def action_capability(spec, input_schema):
    """The frozen snapshot: atomic, idempotent, with side effects (0050); schema_hash over the capability name, version and input
    contract (the fixtures used a placeholder; T2's capabilities.v1.json replaces this derivation by its versioned list)."""
    digest = hashlib.sha256(canonical_payload({'capability': spec.capability, 'version': ACTION_CAPABILITY_VERSION, 'input': input_schema}).encode()).hexdigest()
    return CapabilityContractSnapshot(capability_name=spec.capability, capability_version=ACTION_CAPABILITY_VERSION, schema_hash=digest,
                                      kind=CapabilityContractKind.ATOMIC, has_side_effects=True, idempotent=True, required_scopes=spec.scopes)


def compile_system_actions(*, tenant, created_by, created_at, select=None):
    """[{'definition', 'capability'}] for ACTIONS (or the `select` names), in the 0050 manifest shape. Deterministic."""
    types = object_types()
    inputs = _inputs()
    rows = []
    for spec in ACTIONS:
        if select is not None and spec.name not in select:
            continue
        refs = tuple(_ref(tenant, types[t]) for t in spec.types)
        input_schema = inputs[spec.input] if spec.input else None
        capability = action_capability(spec, input_schema)
        definition = ActionDefinition(tenant_id=tenant, stable_name=spec.name, version=1, status=DefinitionStatus.PUBLISHED, created_by=created_by,
            created_at=created_at, required_scopes=spec.scopes, capability_binding=capability.binding(), object_types=refs,
            governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=refs, target_systems=spec.target_systems),
                risk_level=ActionRiskLevel.LOW, approval_mode=ActionApprovalMode.NONE, policy_refs=(),
                idempotency=ActionIdempotencyPolicy(key_fields=spec.key_fields)),
            receipt_schema={'type': 'object'}, **({'input_schema': input_schema} if input_schema is not None else {}))
        validate_capability_binding(definition, capability)
        rows.append({'definition': definition.model_dump(mode='json'), 'capability': capability.model_dump(mode='json')})
    if select is not None and {r['definition']['stable_name'] for r in rows} != set(select):
        raise ValueError('selected system Action not declared')
    return rows


def compile_system_functions(*, tenant, created_by, created_at):
    """[{'definition', 'capability'}] for FUNCTIONS: read-only (no side effects), scope function.execute; schema_hash over the
    input and output schemas as the fixtures compute it."""
    types = object_types()
    rows = []
    for spec in FUNCTIONS:
        input_schema, output_schema = _function_schemas(spec.schemas)
        digest = hashlib.sha256(canonical_payload({'input': input_schema, 'output': output_schema}).encode()).hexdigest()
        capability = CapabilityContractSnapshot(capability_name=spec.capability, capability_version=FUNCTION_CAPABILITY_VERSION, schema_hash=digest,
            kind=CapabilityContractKind.ATOMIC, has_side_effects=False, idempotent=True, required_scopes=('function.execute',))
        definition = FunctionDefinition(tenant_id=tenant, stable_name=spec.name, version=1, status=DefinitionStatus.PUBLISHED, created_by=created_by,
            created_at=created_at, required_scopes=('function.execute',), capability_binding=capability.binding(),
            applies_to=tuple({'object_type': _ref(tenant, types[t])} for t in spec.types), input_schema=input_schema, output_schema=output_schema)
        validate_capability_binding(definition, capability)
        rows.append({'definition': definition.model_dump(mode='json'), 'capability': capability.model_dump(mode='json')})
    return rows

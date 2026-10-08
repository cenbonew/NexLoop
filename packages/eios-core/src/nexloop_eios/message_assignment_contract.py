"""Pure published contract builder; authority comes from trusted provisioning."""
from eios.ontology.definitions import (ActionDefinition, DefinitionStatus, ActionGovernanceContract,
    ActionChangeScope, ActionRiskLevel, ActionApprovalMode, ActionIdempotencyPolicy,
    OntologySchemaReference, OntologySchemaType)
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractKind, validate_capability_binding
from nexloop_eios.message_relay import message_assignment_schema


def message_assignment_action(*, tenant, created_by, created_at, capability):
    """Requires explicit actual ontology.object.create frozen Capability snapshot.

    Does not manufacture implementation hashes, identities, grants or scopes.
    Publication itself must use the trusted technical configurator.
    """
    if capability.capability_name!='ontology.object.create' or capability.kind is not CapabilityContractKind.ATOMIC or not capability.has_side_effects or not capability.idempotent:
        raise ValueError('message_assignment_capability_unavailable')
    schema=message_assignment_schema()
    ref=OntologySchemaReference(tenant_id=tenant,schema_type=OntologySchemaType.OBJECT_TYPE,
        stable_name=schema.type_name,version=schema.version,schema_digest=schema_contract_digest(schema))
    definition=ActionDefinition(tenant_id=tenant,stable_name='MessageAssignment.create',version=1,
        status=DefinitionStatus.PUBLISHED,created_by=created_by,created_at=created_at,
        required_scopes=capability.required_scopes,capability_binding=capability.binding(),object_types=(ref,),
        governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=(ref,),target_systems=('postgres',)),
            risk_level=ActionRiskLevel.LOW,approval_mode=ActionApprovalMode.NONE,policy_refs=(),
            idempotency=ActionIdempotencyPolicy(key_fields=('request_id',))),receipt_schema={'type':'object'})
    validate_capability_binding(definition,capability)
    return definition,capability

"""Versioned business Action declarations compiled into trusted-configuration rows.

Pure builder: it reads a declaration (deploy/configuration/business-actions.v*.json),
the deployment's published object type schemas and an explicit frozen Capability
snapshot, and returns ``{'definition','capability'}`` rows for the trusted
configurator (0050). It grants nothing and never touches a database.
"""
import json
from pathlib import Path
import re

from eios.ontology.definitions import (ActionDefinition,DefinitionStatus,ActionGovernanceContract,ActionChangeScope,
    ActionRiskLevel,ActionApprovalMode,ActionIdempotencyPolicy,OntologySchemaReference,OntologySchemaType)
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractKind,validate_capability_binding

SCHEMA='nexloop-business-actions/1'
TOP={'schema_version','manifest_version','decision','actions'}
ACTION={'stable_name','version','object_type','capability_name','risk_level','approval_mode','policy_refs',
    'idempotency_key_fields','target_systems','executor_role','purpose','source_task'}


class BusinessActionsRejected(ValueError):
    pass


def validate(value):
    if type(value) is not dict or set(value)!=TOP or value['schema_version']!=SCHEMA or type(value['manifest_version']) is not int or value['manifest_version']<1:
        raise BusinessActionsRejected('manifest shape')
    if type(value['actions']) is not list or not value['actions']:raise BusinessActionsRejected('actions')
    seen=set()
    for item in value['actions']:
        if type(item) is not dict or set(item)!=ACTION:raise BusinessActionsRejected('action shape')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,159}',str(item['stable_name'])) or type(item['version']) is not int or item['version']<1:
            raise BusinessActionsRejected('action identity')
        if (item['stable_name'],item['version']) in seen:raise BusinessActionsRejected('duplicate action')
        seen.add((item['stable_name'],item['version']))
        ref=item['object_type']
        if type(ref) is not dict or set(ref)!={'stable_name','version'} or type(ref['version']) is not int:raise BusinessActionsRejected('object type')
        # This path only publishes low-risk, no-approval, policy-free creates; anything
        # stronger needs the owner's own governance, not a deployment manifest.
        if (item['capability_name']!='ontology.object.create' or item['risk_level']!='low' or item['approval_mode']!='none'
                or item['policy_refs']!=[] or item['idempotency_key_fields']!=['request_id'] or item['target_systems']!=['postgres']):
            raise BusinessActionsRejected('governance outside deployment scope')
        for name in ('executor_role','purpose','source_task'):
            if type(item[name]) is not str or not item[name].strip():raise BusinessActionsRejected(name)
    return value


def load(path):
    return validate(json.loads(Path(path).read_text()))


def compile_actions(manifest,*,tenant,created_by,created_at,capability,object_types):
    """object_types: the trusted manifest's published object type schema dicts."""
    validate(manifest)
    if capability.capability_name!='ontology.object.create' or capability.kind is not CapabilityContractKind.ATOMIC or not capability.has_side_effects or not capability.idempotent:
        raise BusinessActionsRejected('capability')
    schemas={(row['type_name'],row['version']):ObjectTypeDefinition.model_validate(row) for row in object_types}
    rows=[]
    for item in manifest['actions']:
        schema=schemas.get((item['object_type']['stable_name'],item['object_type']['version']))
        if schema is None:raise BusinessActionsRejected('object type not published')
        ref=OntologySchemaReference(tenant_id=tenant,schema_type=OntologySchemaType.OBJECT_TYPE,stable_name=schema.type_name,
            version=schema.version,schema_digest=schema_contract_digest(schema))
        definition=ActionDefinition(tenant_id=tenant,stable_name=item['stable_name'],version=item['version'],status=DefinitionStatus.PUBLISHED,
            created_by=created_by,created_at=created_at,required_scopes=capability.required_scopes,capability_binding=capability.binding(),
            object_types=(ref,),governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=(ref,),target_systems=tuple(item['target_systems'])),
                risk_level=ActionRiskLevel.LOW,approval_mode=ActionApprovalMode.NONE,policy_refs=(),
                idempotency=ActionIdempotencyPolicy(key_fields=tuple(item['idempotency_key_fields']))),receipt_schema={'type':'object'})
        validate_capability_binding(definition,capability)
        rows.append({'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')})
    return rows

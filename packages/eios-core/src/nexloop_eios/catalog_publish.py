"""NX-032 A1 T3 (docs/implementation/NX-032-stage-publish-design.md §3): compile the tenant catalog (object types, Actions,
Functions) from the versioned manifests and publish it through 0050 as the configurator.

Inputs (paths given by the caller): deploy/configuration/capabilities.v1.json (A2 snapshots), system-actions.v1.json (system
types/Actions/Functions; builders from nexloop_eios.system_catalog), business-actions.v1.json, deploy/ontology/
system-object-types.v1.json and business-object-types.v1.json. Compilation is deterministic: created_at is the system manifest's
valid_from and created_by its created_by, so a replay is byte-identical (0162 accepts an existing definition only when equal and
raises 40001 otherwise).

Before writing, the published Actions (service_grants.action_lineage, configurator) are compared with the compiled ones: an
Action published with the same name and version but other content is refused here (never left to the database), and an Action
already published identically is not sent again. Object types and Functions have no configurator inventory: they travel in the
same 0050 manifest as the Actions, so when every Action is already published identically the previous publication (atomic) has
published them too and nothing is sent (changed=False); otherwise all of them are sent and 0162 accepts the identical ones.
"""
import json
from datetime import datetime
from pathlib import Path

from eios.ontology.definitions import (ActionApprovalMode, ActionChangeScope, ActionDefinition, ActionGovernanceContract, ActionIdempotencyPolicy,
    ActionRiskLevel, DefinitionStatus, FunctionDefinition, OntologySchemaReference, OntologySchemaType)
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import validate_capability_binding
from nexloop_eios import business_actions, business_object_types, catalog_manifests, system_catalog, system_object_types


BUSINESS_CAPABILITY_VERSION = '1.0.0'


class CatalogPublishRefused(ValueError):
    pass


def _schemas_of(value):
    return list(value) if isinstance(value, (list, tuple)) else [value]


def compile_catalog(tenant, *, capabilities_path, system_actions_path, business_actions_path, system_types_path, business_types_path):
    """{'object_types': [...], 'actions': [...], 'functions': [...]} in the 0050 manifest shape, plus the counts."""
    builders = system_catalog.builders()
    capabilities = catalog_manifests.load(capabilities_path)
    snapshots = catalog_manifests.snapshots(capabilities)
    ontology_rows = system_object_types.trusted_object_types(system_types_path) + business_object_types.trusted_object_types(business_types_path)
    business = business_actions.load(business_actions_path)
    system = catalog_manifests.load_system_actions(system_actions_path, capabilities=capabilities,
        other_object_types=[(r['type_name'], r['version']) for r in ontology_rows], other_actions=[a['stable_name'] for a in business['actions']],
        builders=builders)
    created_at, created_by = datetime.fromisoformat(system['valid_from']), system['created_by']
    types = {}
    for item in system['object_types']:
        match = [s for s in _schemas_of(builders[item['builder']]()) if s.type_name == item['type_name'] and s.version == item['version']]
        if len(match) != 1:
            raise CatalogPublishRefused('builder does not produce ' + item['type_name'])
        types[item['type_name']] = match[0].model_dump(mode='json')
    for row in ontology_rows:
        if row['type_name'] in types:
            raise CatalogPublishRefused('object type declared twice: ' + row['type_name'])
        types[row['type_name']] = row
    definitions = {name: ObjectTypeDefinition.model_validate_json(json.dumps(row)) for name, row in types.items()}

    def ref(name, version):
        schema = definitions[name]
        if schema.version != version:
            raise CatalogPublishRefused(f'{name} v{version} not declared')
        return OntologySchemaReference(tenant_id=tenant, schema_type=OntologySchemaType.OBJECT_TYPE, stable_name=name, version=version,
                                       schema_digest=schema_contract_digest(schema))
    actions = []
    for item in system['actions']:
        capability = snapshots[tuple(item['capability'])]
        refs = tuple(ref(n, v) for n, v in item['object_types'])
        source = item['input']
        input_schema = None if source is None else source['schema'] if 'schema' in source else builders[source['builder']]()
        definition = ActionDefinition(tenant_id=tenant, stable_name=item['stable_name'], version=item['version'], status=DefinitionStatus.PUBLISHED,
            created_by=created_by, created_at=created_at, required_scopes=capability.required_scopes, capability_binding=capability.binding(),
            object_types=refs, governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=refs, target_systems=tuple(item['target_systems'])),
                risk_level=ActionRiskLevel.LOW, approval_mode=ActionApprovalMode.NONE, policy_refs=(),
                idempotency=ActionIdempotencyPolicy(key_fields=tuple(item['idempotency_key_fields']))),
            receipt_schema={'type': 'object'}, **({'input_schema': input_schema} if input_schema is not None else {}))
        validate_capability_binding(definition, capability)
        actions.append({'definition': definition.model_dump(mode='json'), 'capability': capability.model_dump(mode='json')})
    # Business Actions bind the 1.0.0 snapshot of their capability (business-actions.v1.json names no version).
    used = {a['capability_name'] for a in business['actions']}
    actions += business_actions.compile_actions(business, tenant=tenant, created_by=created_by, created_at=created_at, object_types=list(types.values()),
                                                capabilities={n: snapshots[(n, BUSINESS_CAPABILITY_VERSION)] for n in used})
    functions = []
    for item in system['functions']:
        capability = snapshots[tuple(item['capability'])]
        input_schema, output_schema = builders[item['builder']]()
        definition = FunctionDefinition(tenant_id=tenant, stable_name=item['stable_name'], version=item['version'], status=DefinitionStatus.PUBLISHED,
            created_by=created_by, created_at=created_at, required_scopes=capability.required_scopes, capability_binding=capability.binding(),
            applies_to=tuple({'object_type': ref(n, v)} for n, v in item['object_types']), input_schema=input_schema, output_schema=output_schema)
        validate_capability_binding(definition, capability)
        functions.append({'definition': definition.model_dump(mode='json'), 'capability': capability.model_dump(mode='json')})
    names = [a['definition']['stable_name'] for a in actions]
    if len(names) != len(set(names)):
        raise CatalogPublishRefused('Action declared twice')
    return {'object_types': [types[n] for n in sorted(types)], 'actions': sorted(actions, key=lambda a: a['definition']['stable_name']),
            'functions': functions}


def diff(compiled, lineage):
    """(Actions to send, names published with other content) against service_grants.action_lineage rows (world real)."""
    published = {(r['stable_name'], r['version']): r for r in lineage if r['world'] == 'real'}
    pending, conflicts = [], []
    for item in compiled['actions']:
        d = item['definition']
        row = published.get((d['stable_name'], d['version']))
        if row is None:
            pending.append(item)
        elif row['definition'] != d or row['capability'] != item['capability'] or not row['active']:
            conflicts.append(f"{d['stable_name']}:{d['version']}")
    return pending, conflicts


def publish(compiled, tenant, *, database_url_file, signing_key_file, signing_key_id, dry_run=False):
    """Compare, then publish the missing part through 0050 (configurator). Refuses on any same-version conflict."""
    import uuid
    from nexloop_eios import service_grants as G
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    current = G.inventory(database_url_file, tenant)
    if not current['tenant_present']:
        raise CatalogPublishRefused('tenant not configured yet: run register first')
    pending, conflicts = diff(compiled, G.action_lineage(database_url_file, tenant))
    summary = {'object_types': len(compiled['object_types']), 'actions': len(compiled['actions']), 'functions': len(compiled['functions']),
               'actions_to_publish': sorted(a['definition']['stable_name'] for a in pending), 'conflicts': conflicts}
    if conflicts:
        raise CatalogPublishRefused('published with other content (same name and version): ' + ', '.join(conflicts))
    if dry_run:
        return {'dry_run': True, **summary}
    if not pending:
        return {'changed': False, **summary}
    body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
            'object_types': compiled['object_types'], 'actions': pending, 'functions': compiled['functions'], 'authority_facts': [],
            'service_credentials': [], 'browser_applications': [], 'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
    body['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-catalog:' + str(current['authority_revision']) + ':' + canonical_payload(body)))
    result = apply_manifest_with_secrets(body, database_url_file=database_url_file, signing_key_file=Path(signing_key_file), signing_key_id=signing_key_id,
                                         service_secrets={})
    return {'changed': True, 'authority_revision': result['authority_revision'], **summary}

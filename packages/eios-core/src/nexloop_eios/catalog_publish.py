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
import hashlib
import json
import re
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


STAGE_SCHEMA = 'nexloop-stage-capabilities/1'
STAGE_DIRECTORY = Path(__file__).resolve().parents[4] / 'deploy' / 'stage'
STAGE_NAME = re.compile(r'capabilities\.stage\.v[1-9][0-9]{0,3}\.json')


def load_stage_manifest(path):
    """ADR-045: a stage-only capability manifest (deploy/stage/capabilities.stage.v*.json): {schema_version, manifest_version, decision,
    capabilities, business_actions}. Any other file, place or shape is refused; returns (manifest, {path, sha256})."""
    path = Path(path).resolve()
    if path.parent != STAGE_DIRECTORY.resolve() or not STAGE_NAME.fullmatch(path.name):
        raise CatalogPublishRefused('stage capabilities: only deploy/stage/capabilities.stage.v*.json')
    raw = path.read_bytes()
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'schema_version', 'manifest_version', 'decision', 'capabilities', 'business_actions', 'owner_actions'}
            or type(value['owner_actions']) is not list or any(type(a) is not str or not a.startswith('eios:action:') for a in value['owner_actions'])
            or value['schema_version'] != STAGE_SCHEMA or type(value['manifest_version']) is not int or value['manifest_version'] < 1
            or type(value['decision']) is not str or not value['decision'].strip() or type(value['capabilities']) is not list
            or not value['capabilities'] or type(value['business_actions']) is not list or not value['business_actions']):
        raise CatalogPublishRefused('stage capabilities: manifest shape')
    return value, {'path': 'deploy/stage/' + path.name, 'sha256': hashlib.sha256(raw).hexdigest()}


DEPLOYMENT_MARKER = 'nexloop-deployment-marker/1'


def deployment_environment(path):
    """ADR-045 (L6 C1): the environment of the target deployment from its private marker file (0600, the caller's):
    {"schema": "nexloop-deployment-marker/1", "environment": "stage" | "production"}. No environment variable is read."""
    from nexloop_eios.trusted_configuration import read_private_text
    try:
        value = json.loads(read_private_text(path, maximum=4096))
    except Exception:
        raise CatalogPublishRefused('--deployment-marker: a private (0600) JSON file {schema, environment}') from None
    if (type(value) is not dict or set(value) != {'schema', 'environment'} or value['schema'] != DEPLOYMENT_MARKER
            or value['environment'] not in ('stage', 'production')):
        raise CatalogPublishRefused('--deployment-marker: {"schema": "nexloop-deployment-marker/1", "environment": "stage" | "production"}')
    return value['environment']


def stage_only(stage_capabilities, deployment_marker):
    """The stage manifest path when both are given and the marker says stage; None when neither is given; refused otherwise."""
    if stage_capabilities is None and deployment_marker is None:
        return None
    if stage_capabilities is None or deployment_marker is None:
        raise CatalogPublishRefused('--stage-capabilities needs --deployment-marker (and the marker alone changes nothing)')
    if deployment_environment(deployment_marker) != 'stage':
        raise CatalogPublishRefused('--stage-capabilities: the target deployment is not marked stage')
    return stage_capabilities


def compile_catalog(tenant, *, capabilities_path, system_actions_path, business_actions_path, system_types_path, business_types_path,
                    stage_capabilities_path=None):
    """{'object_types': [...], 'actions': [...], 'functions': [...]} in the 0050 manifest shape, plus the counts. stage_capabilities_path
    (ADR-045; only scripts/deploy/stage_secrets.py publish passes it, for a deployment marked stage) merges a stage-only manifest:
    its capabilities and business Actions join the system ones (a name twice is refused) and the result names it (stage_manifest)."""
    builders = system_catalog.builders()
    capabilities = catalog_manifests.load(capabilities_path)
    stage = None
    if stage_capabilities_path is not None:
        stage, stage_record = load_stage_manifest(stage_capabilities_path)
        capabilities = catalog_manifests.validate({**capabilities, 'capabilities': capabilities['capabilities'] + stage['capabilities']})
    snapshots = catalog_manifests.snapshots(capabilities)
    ontology_rows = system_object_types.trusted_object_types(system_types_path) + business_object_types.trusted_object_types(business_types_path)
    business = business_actions.load(business_actions_path)
    if stage is not None:
        business = business_actions.validate({**business, 'actions': business['actions'] + stage['business_actions']})
    system = catalog_manifests.load_system_actions(system_actions_path, capabilities=capabilities,
        other_object_types=[(r['type_name'], r['version']) for r in ontology_rows], other_actions=[a['stable_name'] for a in business['actions']],
        builders=builders)
    created_at, created_by = datetime.fromisoformat(system['valid_from']), system['created_by']
    types = {}
    for item in system['object_types']:
        match = [s for s in _schemas_of(builders[item['builder']]()) if s.type_name == item['type_name'] and s.version == item['version']]
        if len(match) != 1:
            raise CatalogPublishRefused('builder does not produce ' + item['type_name'])
        types[(item['type_name'], item['version'])] = match[0].model_dump(mode='json')
    for row in ontology_rows:
        if any(name == row['type_name'] for name, _ in types):
            raise CatalogPublishRefused('object type declared twice: ' + row['type_name'])
        types[(row['type_name'], row['version'])] = row
    # Keyed by (name, version): a type may be declared at v1 and at its group-only v2 (ADR-042); v1 is sent first.
    definitions = {key: ObjectTypeDefinition.model_validate_json(json.dumps(row)) for key, row in types.items()}
    latest = {name: types[(name, version)] for name, version in sorted(types)}

    def ref(name, version):
        schema = definitions.get((name, version))
        if schema is None:
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
    actions += business_actions.compile_actions(business, tenant=tenant, created_by=created_by, created_at=created_at, object_types=list(latest.values()),
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
    compiled = {'object_types': [types[k] for k in sorted(types)], 'actions': sorted(actions, key=lambda a: a['definition']['stable_name']),
                'functions': functions}
    return compiled if stage is None else {**compiled, 'stage_manifest': stage_record}


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
               'actions_to_publish': sorted(a['definition']['stable_name'] for a in pending), 'conflicts': conflicts,
               **({'stage_manifest': compiled['stage_manifest']} if 'stage_manifest' in compiled else {})}  # ADR-045: named in every result
    if conflicts:
        raise CatalogPublishRefused('published with other content (same name and version): ' + ', '.join(conflicts))
    if dry_run:
        return {'dry_run': True, **summary}
    if not pending:
        return {'changed': False, **summary}
    increments = [t for t in compiled['object_types'] if t['version'] >= 2]
    if increments:
        # ADR-042: the v1 types first (a manifest of types only), then the group-only increments as their own step; the full
        # manifest below then finds every version present and equal.
        from nexloop_eios.trusted_configuration import apply_group_increments
        first = [t for t in compiled['object_types'] if t['version'] == 1]
        types_only = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
                      'object_types': first, 'actions': [], 'functions': [], 'authority_facts': [], 'service_credentials': [], 'browser_applications': [],
                      'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
        types_only['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-catalog-types:' + str(current['authority_revision']) + ':' + canonical_payload(types_only)))
        revision = apply_manifest_with_secrets(types_only, database_url_file=database_url_file, signing_key_file=Path(signing_key_file),
                                               signing_key_id=signing_key_id, service_secrets={})['authority_revision']
        apply_group_increments(tenant, increments, database_url_file=database_url_file, signing_key_file=Path(signing_key_file),
                               signing_key_id=signing_key_id, expected_revision=revision,
                               manifest_id=str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-catalog-increment:' + str(revision) + ':' + canonical_payload(increments))))
        current = G.inventory(database_url_file, tenant)
    body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
            'object_types': compiled['object_types'], 'actions': pending, 'functions': compiled['functions'], 'authority_facts': [],
            'service_credentials': [], 'browser_applications': [], 'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
    body['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-catalog:' + str(current['authority_revision']) + ':' + canonical_payload(body)))
    result = apply_manifest_with_secrets(body, database_url_file=database_url_file, signing_key_file=Path(signing_key_file), signing_key_id=signing_key_id,
                                         service_secrets={})
    return {'changed': True, 'authority_revision': result['authority_revision'], **summary}

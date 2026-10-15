"""NX-032 A1/A2 (docs/implementation/NX-032-stage-publish-design.md §2): the versioned capability snapshot manifest
(deploy/configuration/capabilities.v1.json, schema nexloop-capabilities/1), the system Action manifest
(deploy/configuration/system-actions.v1.json, schema nexloop-system-actions/1) and their validators.

Pure: it reads the manifest and the migration catalog, never a database. Each entry is the frozen
CapabilityContractSnapshot of one capability version plus the contract it hashes:

  schema_hash = sha256(canonical_payload({"capability": name, "version": version, "contract": contract}))

computed here, never written by hand (`write_hashes` fills it; `validate` refuses a stale one). A governed capability
(`governed: true`) must be registered in control.nexloop_governed_capabilities (0124 and later, owner-only, append-only, maintained
by migrations): that table is read from the migration files, and its contract must name the registered operation. 0050 publishes
an Action only with a side-effecting snapshot, so every capability an Action binds declares has_side_effects true (a read-only
Action says so in `side_effects_reason`); only a Function capability may be side-effect free.
"""
import hashlib
import json
import re
from pathlib import Path

from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios.postgres_artifacts import canonical_payload

CAPABILITIES_SCHEMA = 'nexloop-capabilities/1'
TOP = {'schema_version', 'manifest_version', 'decision', 'capabilities'}
ENTRY = {'capability_name', 'capability_version', 'kind', 'has_side_effects', 'idempotent', 'required_scopes', 'risk_level',
         'binds', 'governed', 'contract', 'schema_hash', 'side_effects_reason'}
BINDS = {'action', 'function'}
SNAPSHOT_FIELDS = ('capability_name', 'capability_version', 'schema_hash', 'kind', 'has_side_effects', 'idempotent', 'required_scopes', 'risk_level')
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+')
_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+')
MIGRATIONS = Path(__file__).resolve().parents[1] / 'eios' / 'migrations'
_GOVERNED_INSERT = re.compile(r'insert\s+into\s+control\.nexloop_governed_capabilities\s*\(([^)]*)\)\s*values(.*?);', re.S | re.I)
_ROW = re.compile(r"\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")


class CatalogManifestRejected(ValueError):
    pass


def schema_hash(name, version, contract):
    return hashlib.sha256(canonical_payload({'capability': name, 'version': version, 'contract': contract}).encode()).hexdigest()


def governed_capabilities(migrations=MIGRATIONS):
    """{capability: operation} of control.nexloop_governed_capabilities as the migration catalog inserts it (append-only table:
    every row comes from one INSERT of a migration). tests/test_catalog_manifests_pg.py checks it against a bootstrapped database."""
    out = {}
    for path in sorted(Path(migrations).glob('[0-9][0-9][0-9][0-9]_*.sql')):
        for columns, values in _GOVERNED_INSERT.findall(path.read_text()):
            if [c.strip() for c in columns.split(',')] != ['capability', 'operation', 'subject_rule', 'handler', 'source_task']:
                raise CatalogManifestRejected(f'governed capability insert columns in {path.name}')
            for capability, operation, *_ in _ROW.findall(values):
                if capability in out:
                    raise CatalogManifestRejected(f'governed capability registered twice: {capability}')
                out[capability] = operation
    return out


def _entry_problem(item, governed):
    if type(item) is not dict or set(item) != ENTRY:
        return 'entry shape'
    name, version = item['capability_name'], item['capability_version']
    if type(name) is not str or not _NAME.fullmatch(name) or type(version) is not str or not _VERSION.fullmatch(version):
        return 'capability identity'
    if item['kind'] != 'atomic' or item['idempotent'] is not True or item['risk_level'] != 'low':
        return 'only atomic, idempotent, low-risk snapshots are published by deployment'
    if item['binds'] not in BINDS or type(item['has_side_effects']) is not bool or type(item['governed']) is not bool:
        return 'binds / has_side_effects / governed'
    if item['binds'] == 'action' and not item['has_side_effects']:
        return '0050 publishes an Action only with a side-effecting snapshot'
    reason = item['side_effects_reason']
    if reason is not None and (type(reason) is not str or not reason.strip()):
        return 'side_effects_reason'
    scopes = item['required_scopes']
    if type(scopes) is not list or not scopes or any(type(s) is not str or not s for s in scopes) or scopes != sorted(set(scopes)):
        return 'required_scopes (non-empty, sorted, unique)'
    contract = item['contract']
    if type(contract) is not dict or not contract:
        return 'contract'
    if item['governed']:
        if name not in governed:
            return 'governed capability is not registered in control.nexloop_governed_capabilities'
        if contract.get('governed_operation') != governed[name]:
            return 'contract governed_operation differs from the registered operation'
    elif 'governed_operation' in contract:
        return 'governed_operation on a capability that is not governed'
    if item['schema_hash'] != schema_hash(name, version, contract):
        return 'schema_hash is not the computed hash of the contract'
    return None


def validate(value, *, migrations=MIGRATIONS):
    if (type(value) is not dict or set(value) != TOP or value['schema_version'] != CAPABILITIES_SCHEMA
            or type(value['manifest_version']) is not int or value['manifest_version'] < 1):
        raise CatalogManifestRejected('manifest shape')
    if type(value['decision']) is not str or not value['decision'].strip():
        raise CatalogManifestRejected('decision')
    if type(value['capabilities']) is not list or not value['capabilities']:
        raise CatalogManifestRejected('capabilities')
    governed = governed_capabilities(migrations)
    seen = set()
    for item in value['capabilities']:
        problem = _entry_problem(item, governed)
        if problem:
            raise CatalogManifestRejected(f"{problem}: {item.get('capability_name') if type(item) is dict else item!r}")
        key = (item['capability_name'], item['capability_version'])
        if key in seen:
            raise CatalogManifestRejected(f'duplicate capability version: {key[0]} {key[1]}')
        seen.add(key)
    return value


def load(path, *, migrations=MIGRATIONS):
    return validate(json.loads(Path(path).read_text()), migrations=migrations)


def snapshots(manifest):
    """{(capability_name, capability_version): CapabilityContractSnapshot} of a validated manifest."""
    return {(item['capability_name'], item['capability_version']): CapabilityContractSnapshot.model_validate_json(json.dumps({k: item[k] for k in SNAPSHOT_FIELDS}))
            for item in manifest['capabilities']}


def write_hashes(path):
    """Fills every entry's schema_hash from its contract (the hashes are never written by hand); returns the names changed."""
    path = Path(path)
    value = json.loads(path.read_text())
    changed = []
    for item in value.get('capabilities', []):
        computed = schema_hash(item['capability_name'], item['capability_version'], item['contract'])
        if item.get('schema_hash') != computed:
            item['schema_hash'] = computed
            changed.append(item['capability_name'])
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    return changed


# ---- system Actions (deploy/configuration/system-actions.v1.json, schema nexloop-system-actions/1) ------------------------------
SYSTEM_ACTIONS_SCHEMA = 'nexloop-system-actions/1'
SYSTEM_TOP = {'schema_version', 'manifest_version', 'decision', 'valid_from', 'created_by', 'object_types', 'actions', 'functions'}
SYSTEM_TYPE = {'type_name', 'version', 'builder'}
SYSTEM_ACTION = {'stable_name', 'version', 'object_types', 'capability', 'target_systems', 'idempotency_key_fields', 'input', 'group'}
SYSTEM_FUNCTION = {'stable_name', 'version', 'object_types', 'capability', 'builder', 'group'}
# The governance a deployment manifest may declare for a system Action (design §1.2: no governance; the reader checks it).
TARGETS = {('postgres',): ('request_id',), ('service',): ('request_id', 'intent_id')}
_BUILDER = re.compile(r'[a-z][a-z0-9_]*(:[a-z][a-z0-9_]*)?')
_STABLE = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,159}')


def _builder_problem(key, builders):
    if type(key) is not str or not _BUILDER.fullmatch(key):
        return 'builder key'
    if builders is not None and key not in builders:
        return f'builder {key} is not in system_catalog.BUILDERS'
    return None


def validate_system_actions(value, *, capabilities, other_object_types=(), other_actions=(), builders=None):
    """value: the system Action manifest. capabilities: a validated capabilities manifest. other_object_types: (type_name,
    version) published by the ontology manifests (deploy/ontology/*.json), which this manifest references but never redeclares.
    other_actions: (stable_name, version) of business-actions.v1.json, which must not overlap. builders: the builder keys of
    nexloop_eios.system_catalog.BUILDERS (T1); None checks the key format only."""
    if (type(value) is not dict or set(value) != SYSTEM_TOP or value['schema_version'] != SYSTEM_ACTIONS_SCHEMA
            or type(value['manifest_version']) is not int or value['manifest_version'] < 1):
        raise CatalogManifestRejected('system manifest shape')
    from datetime import datetime
    try:
        valid_from = datetime.fromisoformat(value['valid_from'])
    except (TypeError, ValueError):
        raise CatalogManifestRejected('valid_from') from None
    if valid_from.tzinfo is None:
        raise CatalogManifestRejected('valid_from needs a time zone (it is every definition\'s created_at)')
    for name in ('decision', 'created_by'):
        if type(value[name]) is not str or not value[name].strip():
            raise CatalogManifestRejected(name)
    caps = {(c['capability_name'], c['capability_version']): c for c in capabilities['capabilities']}
    types = set(other_object_types)
    for item in value['object_types']:
        if type(item) is not dict or set(item) != SYSTEM_TYPE or type(item['type_name']) is not str or item['version'] != 1:
            raise CatalogManifestRejected(f'object type entry: {item!r}')
        problem = _builder_problem(item['builder'], builders)
        if problem:
            raise CatalogManifestRejected(f"{problem}: {item['type_name']}")
        key = (item['type_name'], item['version'])
        if key in types:
            raise CatalogManifestRejected(f'object type declared twice: {key[0]}')
        types.add(key)
    seen = set(other_actions)

    def common(item, fields, binds):
        if type(item) is not dict or set(item) != fields or type(item['stable_name']) is not str or not _STABLE.fullmatch(item['stable_name']):
            raise CatalogManifestRejected(f'entry shape: {item!r}')
        name = item['stable_name']
        if type(item['version']) is not int or item['version'] < 1:
            raise CatalogManifestRejected(f'version: {name}')
        if (name, item['version']) in seen:
            raise CatalogManifestRejected(f'declared twice (system or business): {name}')
        seen.add((name, item['version']))
        refs = item['object_types']
        if (type(refs) is not list or not refs or any(type(r) is not list or len(r) != 2 for r in refs)
                or len({tuple(r) for r in refs}) != len(refs) or not all(tuple(r) in types for r in refs)):
            raise CatalogManifestRejected(f'object types not declared: {name}')
        capability = item['capability']
        entry = caps.get(tuple(capability)) if type(capability) is list and len(capability) == 2 else None
        if entry is None:
            raise CatalogManifestRejected(f'capability snapshot missing: {name}')
        if entry['binds'] != binds:
            raise CatalogManifestRejected(f'capability {entry["capability_name"]} binds {entry["binds"]}, not {binds}: {name}')
        if type(item['group']) is not str or not item['group'].strip():
            raise CatalogManifestRejected(f'group: {name}')
        return entry
    for item in value['actions']:
        entry = common(item, SYSTEM_ACTION, 'action')
        name = item['stable_name']
        targets, keys = item['target_systems'], item['idempotency_key_fields']
        if type(targets) is not list or tuple(targets) not in TARGETS or type(keys) is not list or len(keys) != 1 or keys[0] not in TARGETS[tuple(targets)]:
            raise CatalogManifestRejected(f'target systems / idempotency key: {name}')
        if targets != entry['contract'].get('target_systems'):
            raise CatalogManifestRejected(f'target systems differ from the capability contract: {name}')
        spec = item['input']
        if spec is not None:
            if type(spec) is not dict or len(spec) != 1 or not ({'builder', 'schema'} >= set(spec)):
                raise CatalogManifestRejected(f'input: {name}')
            if 'builder' in spec:
                problem = _builder_problem(spec['builder'], builders)
                if problem:
                    raise CatalogManifestRejected(f'{problem}: {name}')
            elif type(spec['schema']) is not dict or spec['schema'].get('type') != 'object':
                raise CatalogManifestRejected(f'input schema: {name}')
    for item in value['functions']:
        common(item, SYSTEM_FUNCTION, 'function')
        problem = _builder_problem(item['builder'], builders)
        if problem:
            raise CatalogManifestRejected(f"{problem}: {item['stable_name']}")
    return value


def load_system_actions(path, *, capabilities, other_object_types=(), other_actions=(), builders=None):
    return validate_system_actions(json.loads(Path(path).read_text()), capabilities=capabilities, other_object_types=other_object_types,
                                   other_actions=other_actions, builders=builders)

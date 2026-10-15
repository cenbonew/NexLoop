"""NX-032 A1/A2 (docs/implementation/NX-032-stage-publish-design.md §2): the versioned capability snapshot manifest
(deploy/configuration/capabilities.v1.json, schema nexloop-capabilities/1) and its validator.

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

"""Test publication of the system catalog (nexloop_eios.system_catalog, design NX-032-stage-publish-design.md §2.1, T4): the
fixtures insert the object types and Action / Function definitions the production catalog builds, instead of hand-built JSON.
The rows are what stage publication sends through 0050; the fixtures still insert them directly into a disposable tenant."""
import json
from datetime import UTC, datetime, timedelta

from eios.ontology.definitions import ActionDefinition
from psycopg.types.json import Jsonb

from nexloop_eios import system_catalog as SC

CREATED_BY = 'synthetic-owner'
ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
_SNAPSHOTS = None


def snapshots():
    """{(capability name, version): snapshot} from deploy/configuration/capabilities.v1.json, the one source stage publication
    binds (catalog_manifests.snapshots(), as catalog_publish)."""
    global _SNAPSHOTS
    if _SNAPSHOTS is None:
        from nexloop_eios import catalog_manifests
        _SNAPSHOTS = catalog_manifests.snapshots(catalog_manifests.load(ROOT / 'deploy/configuration/capabilities.v1.json'))
    return _SNAPSHOTS


def publish_system_catalog(admin, tenant, types, actions=(), functions=(), *, created_at=None, variants=None):
    """Inserts the named system object types, Actions and Functions (all v1) for `tenant`; `types` lists only the types not yet
    inserted (a Function's applied-to types must already be the catalog's).

    variants: {Action name: function(definition dict) -> None}, for a test that deliberately publishes a changed definition (a
    negative case or a schema variant); the catalog definition is the base and the change is visible at the call site."""
    created_at = created_at or datetime.now(UTC) - timedelta(days=1)
    schemas = SC.object_types()
    for name in types:
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',
                      (tenant, name, Jsonb(schemas[name].model_dump(mode='json'))))
    rows = SC.compile_system_actions(tenant=tenant, created_by=CREATED_BY, created_at=created_at, snapshots=snapshots(), select=tuple(actions)) if actions else []
    for row in rows:
        d = row['definition']
        if variants and d['stable_name'] in variants:
            variants[d['stable_name']](d)
            d.pop('contract_digest', None)  # recomputed for the changed definition
            d = ActionDefinition.model_validate_json(json.dumps(d)).model_dump(mode='json')
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                      (tenant, 'real', f"eios:action:{d['stable_name']}:{d['version']}", Jsonb(d), Jsonb(row['capability'])))
    for row in SC.compile_system_functions(tenant=tenant, created_by=CREATED_BY, created_at=created_at, snapshots=snapshots()):
        d = row['definition']
        if d['stable_name'] not in functions:
            continue
        # The schemas column holds every applied-to type as {reference, definition} (0048).
        used = [{'reference': ref['object_type'], 'definition': schemas[ref['object_type']['stable_name']].model_dump(mode='json')} for ref in d['applies_to']]
        admin.execute('insert into control.nexloop_function_definitions(tenant_id,world,resource_id,definition,capability,schemas) values(%s,%s,%s,%s,%s,%s)',
                      (tenant, 'real', f"eios:function:{d['stable_name']}:{d['version']}", Jsonb(d), Jsonb(row['capability']), Jsonb(used)))
    return {row['definition']['stable_name']: row for row in rows}


def business_capabilities(names):
    """{capability name: snapshot} for business Actions: their 1.0.0 snapshots (the role Actions' 1.1.0 versions of the shared
    names are not for business Actions; catalog_publish.BUSINESS_CAPABILITY_VERSION)."""
    return {name: snapshots()[(name, '1.0.0')] for name in names}


MANIFEST_CREATED_AT = datetime(2026, 10, 15, tzinfo=UTC)


def catalog_manifest_rows(tenant, types, actions=(), functions=()):
    """(object type rows, Action rows, Function rows) of the catalog for a 0050 manifest (the fixtures that publish through the
    trusted configuration). created_at is fixed, so two manifests carrying the same Action send identical rows (0162 accepts an
    existing definition only when equal)."""
    schemas = SC.object_types()
    args = dict(tenant=tenant, created_by=CREATED_BY, created_at=MANIFEST_CREATED_AT, snapshots=snapshots())
    action_rows = SC.compile_system_actions(**args, select=tuple(actions)) if actions else []
    function_rows = [r for r in SC.compile_system_functions(**args) if r['definition']['stable_name'] in functions]
    return [schemas[name].model_dump(mode='json') for name in types], action_rows, function_rows

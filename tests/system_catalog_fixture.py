"""Test publication of the system catalog (nexloop_eios.system_catalog, design NX-032-stage-publish-design.md §2.1, T4): the
fixtures insert the object types and Action / Function definitions the production catalog builds, instead of hand-built JSON.
The rows are what stage publication sends through 0050; the fixtures still insert them directly into a disposable tenant."""
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from nexloop_eios import system_catalog as SC

CREATED_BY = 'synthetic-owner'


def publish_system_catalog(admin, tenant, types, actions=(), functions=(), *, created_at=None):
    """Inserts the named system object types, Actions and Functions (all v1) for `tenant`; `types` lists only the types not yet
    inserted (a Function's applied-to types must already be the catalog's)."""
    created_at = created_at or datetime.now(UTC) - timedelta(days=1)
    schemas = SC.object_types()
    for name in types:
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',
                      (tenant, name, Jsonb(schemas[name].model_dump(mode='json'))))
    rows = SC.compile_system_actions(tenant=tenant, created_by=CREATED_BY, created_at=created_at, select=tuple(actions)) if actions else []
    for row in rows:
        d = row['definition']
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                      (tenant, 'real', f"eios:action:{d['stable_name']}:{d['version']}", Jsonb(d), Jsonb(row['capability'])))
    for row in SC.compile_system_functions(tenant=tenant, created_by=CREATED_BY, created_at=created_at):
        d = row['definition']
        if d['stable_name'] not in functions:
            continue
        # The schemas column holds every applied-to type as {reference, definition} (0048).
        used = [{'reference': ref['object_type'], 'definition': schemas[ref['object_type']['stable_name']].model_dump(mode='json')} for ref in d['applies_to']]
        admin.execute('insert into control.nexloop_function_definitions(tenant_id,world,resource_id,definition,capability,schemas) values(%s,%s,%s,%s,%s,%s)',
                      (tenant, 'real', f"eios:function:{d['stable_name']}:{d['version']}", Jsonb(d), Jsonb(row['capability']), Jsonb(used)))
    return {row['definition']['stable_name']: row for row in rows}

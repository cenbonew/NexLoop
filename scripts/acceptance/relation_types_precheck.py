#!/usr/bin/env python3
"""NX-054 publication precheck on stage (NX-054-P1.md §3; design docs/design/relation-types-design.md §2), read only.

    python3 scripts/acceptance/relation_types_precheck.py --dsn-file <inspector DSN, 0600> --tenant <TENANT_ID>

Connects once (s6_common.connect_read_only: read only set and verified before the first query) and reports, counts only - no id,
property value or business content:
  fields    per first-batch relation type (nexloop_eios.relation_catalog.FIRST_BATCH) and world: objects of the source type, those
            with the property set, 64-hex values, erasure pseudonyms, targets that exist in the same world, targets in another
            world, dangling (64 hex, no target), distinct targets and values shared by several sources - the shape P2 derives from
            and P3 backfills
  supersedes  Commitment supersedes chains: commitments superseded more than once (many_to_one keeps them; N-3)
  assessments RelationshipAssessment relation names that are also first-batch names (a collision to rename before publishing),
            and the count of assessments with version 0
  catalog   what is published already: catalog versions, relation type versions per name (latest), derivation map rows
Exit 0 when nothing blocks publication (no 64-hex shortfall, no cross-world target, no name collision), 1 otherwise, 2 refused.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import s6_common as C  # noqa: E402
from nexloop_eios.relation_catalog import FIRST_BATCH  # noqa: E402

FIELDS_SQL = """select %(name)s::text, o.world,
  count(*), count(*) filter (where o.properties->>%(prop)s is not null and o.properties->>%(prop)s<>''),
  count(*) filter (where o.properties->>%(prop)s ~ '^[a-f0-9]{64}$'),
  count(*) filter (where o.properties->>%(prop)s like 'pseudonym:%%'),
  count(*) filter (where exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=%(target)s
     and t.object_id=o.properties->>%(prop)s)),
  count(*) filter (where exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world<>o.world and t.type_name=%(target)s
     and t.object_id=o.properties->>%(prop)s)),
  count(distinct o.properties->>%(prop)s),
  count(o.properties->>%(prop)s) - count(distinct o.properties->>%(prop)s)
 from ontology.objects o where o.tenant_id=%(tenant)s and o.type_name=%(source)s group by o.world"""


def probe(db, tenant, specs=FIRST_BATCH):
    db.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    fields, problems = [], []
    for s in specs:
        for name, world, objects, with_value, hex64, pseudonyms, same_world, other_world, distinct, shared in db.execute(
                FIELDS_SQL, {'name': s.name, 'prop': s.source_property, 'target': s.target_type, 'tenant': tenant, 'source': s.source_type}).fetchall():
            fields.append({'relation': name, 'source_type': s.source_type, 'property': s.source_property, 'target_type': s.target_type, 'world': world,
                           'objects': objects, 'with_value': with_value, 'hex64': hex64, 'pseudonyms': pseudonyms, 'target_same_world': same_world,
                           'target_other_world': other_world, 'distinct_targets': distinct, 'sharing_a_target': shared,
                           'dangling': hex64 - same_world - other_world})
            if other_world:
                problems.append(f'{name}: {other_world} target(s) in another world ({world})')
            if hex64 + pseudonyms < with_value:
                problems.append(f'{name}: {with_value - hex64 - pseudonyms} value(s) neither 64 hex nor an erasure pseudonym ({world}) - not derived; check the field')
    superseded_twice = db.execute("""select count(*) from (select supersedes from runtime.nexloop_commitments where tenant_id=%s and supersedes is not null
        group by world,supersedes having count(*)>1) x""", (tenant,)).fetchone()[0]
    names = {s.name for s in specs}
    assessment_names = dict(db.execute("""select properties->>'relation_name',count(*) from ontology.objects where tenant_id=%s
        and type_name='RelationshipAssessment' group by 1""", (tenant,)).fetchall())
    collisions = sorted(n for n in assessment_names if n in names)
    if collisions:
        problems.append('RelationshipAssessment relation_name equal to a first-batch name: ' + ', '.join(collisions))
    version_zero = db.execute("""select count(*) from ontology.objects where tenant_id=%s and type_name='RelationshipAssessment'
        and properties->>'relation_version'='0'""", (tenant,)).fetchone()[0]
    catalog = {'versions': [dict(zip(('version', 'longest_ownership_chain', 'relation_types'), r)) for r in db.execute(
                   "select version,longest_ownership_chain,cardinality(relation_names) from control.nexloop_relation_catalogs where tenant_id=%s order by version",
                   (tenant,)).fetchall()],
               'latest_type_versions': dict(db.execute("""select relation_name,max(version) from ontology.relation_type_versions where tenant_id=%s
                   group by 1 order by 1""", (tenant,)).fetchall()),
               'derivation_map_rows': db.execute('select count(*) from control.nexloop_relation_derivations where tenant_id=%s', (tenant,)).fetchone()[0],
               'relations': dict(db.execute('select world,count(*) from ontology.relations where tenant_id=%s group by 1', (tenant,)).fetchall())}
    return {'fields': fields, 'supersedes': {'superseded_more_than_once': superseded_twice},
            'assessments': {'first_batch_name_collisions': collisions, 'relation_names': len(assessment_names), 'version_zero': version_zero},
            'catalog': catalog, 'blocking': problems}


def main(argv=None, out=sys.stdout, connect=None):
    p = argparse.ArgumentParser(description='NX-054 publication precheck (read only, counts only)')
    p.add_argument('--dsn-file', required=True)
    p.add_argument('--tenant', required=True)
    a = p.parse_args(argv)
    try:
        with (connect or (lambda: C.connect_read_only(a.dsn_file, who='inspector')))() as db:
            doc = probe(db, a.tenant)
    except C.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    print(json.dumps(doc, ensure_ascii=False, indent=1, default=str), file=out)
    return 1 if doc['blocking'] else 0


if __name__ == '__main__':
    sys.exit(main())

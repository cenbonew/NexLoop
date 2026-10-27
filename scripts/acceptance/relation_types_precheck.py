#!/usr/bin/env python3
"""NX-054 publication precheck on stage (NX-054-P1.md §3; design docs/design/relation-types-design.md §2), read only.

    python3 scripts/acceptance/relation_types_precheck.py --dsn-file <nexloop_metrics DSN, 0600> --tenant <TENANT_ID>

As the read-only metrics login (relation_reads.connect_metrics: role checked, statement timeout 60 s), one call of
authz.nexloop_relation_precheck (0336) with the first batch (nexloop_eios.relation_catalog.FIRST_BATCH). Counts only - no id,
property value or business content:
  fields      per first-batch relation type and world: objects, with the property, 64-hex values, erasure pseudonyms, targets in
              the same world, in another world, dangling, distinct targets, values shared by several sources
  supersedes  commitments superseded more than once (many_to_one keeps them; N-3)
  assessments RelationshipAssessment relation names equal to a first-batch name, and the count of version 0
  catalog     published catalog versions, latest version per type, derivation map rows (and how many derive), relations, derived ones
Exit 0 when nothing blocks publication (no cross-world target, no value that is neither 64 hex nor a pseudonym, no name
collision), 1 otherwise, 2 refused.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import relation_reads as RR  # noqa: E402
from nexloop_eios.relation_catalog import FIRST_BATCH  # noqa: E402


def specs(batch=FIRST_BATCH):
    return [{'name': s.name, 'source_type': s.source_type, 'property': s.source_property, 'target_type': s.target_type} for s in batch]


def blocking(doc):
    out = []
    for f in doc['fields']:
        if f['target_other_world']:
            out.append(f"{f['relation']}: {f['target_other_world']} target(s) in another world ({f['world']})")
        if f['hex64'] + f['pseudonyms'] < f['with_value']:
            out.append(f"{f['relation']}: {f['with_value'] - f['hex64'] - f['pseudonyms']} value(s) neither 64 hex nor an erasure pseudonym or marker ({f['world']}) - not derived; check the field")
    if doc['assessments']['first_batch_name_collisions']:
        out.append('RelationshipAssessment relation_name equal to a first-batch name: ' + ', '.join(doc['assessments']['first_batch_name_collisions']))
    return out


def main(argv=None, out=sys.stdout, connect=None):
    p = argparse.ArgumentParser(description='NX-054 publication precheck (read only, counts only)')
    p.add_argument('--dsn-file', required=True)
    p.add_argument('--tenant', required=True)
    a = p.parse_args(argv)
    try:
        with (connect or (lambda: RR.connect_metrics(a.dsn_file)))() as db:
            doc = db.execute('select authz.nexloop_relation_precheck(%s,%s::jsonb)', (a.tenant, json.dumps(specs()))).fetchone()[0]
    except RR.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    doc['blocking'] = blocking(doc)
    print(json.dumps(doc, ensure_ascii=False, indent=1, default=str), file=out)
    return 1 if doc['blocking'] else 0


if __name__ == '__main__':
    sys.exit(main())

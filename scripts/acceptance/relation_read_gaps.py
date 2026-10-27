#!/usr/bin/env python3
"""NX-054 P4a daily gap check on stage (docs/design/relation-read-p4.md §2.3, §2.5), read only.

    python3 scripts/acceptance/relation_read_gaps.py --dsn-file <nexloop_metrics DSN, 0600> --tenant <TENANT_ID> --evidence-dir <dir>

As the metrics login (relation_reads.checked: role checked, statement timeout 60 s), in a transaction set read only, one call of
authz.nexloop_relation_read_gaps (0357): per world and type, the objects whose property-rule Consumers the relations miss (gaps),
the same among erasing Consumers (gaps_erasing, apart: P5 looks at gaps only) and the reverse (relation_only). Counts only.
The answer is written to <evidence-dir>/<UTC date>.json (0640, replaced atomically) and printed.

Evidence older than 30 days (by its file name) is removed after each write.

Exit 2 also when the check does not cover what the published catalog should (ownership_types, 0360; 0: the tenant's relation
catalog is not published). The expected number is computed, never written down (L6 F1 on --defer, 2026-10-27): in the same
read-only transaction authz.nexloop_relation_precheck (0336, the metrics login's) gives the published relation type names and the
objects of each first-batch ownership source type; expected = the first-batch ownership source types of the published types. An ownership
type left unpublished (relation_types_publish.py --defer) is accepted only while its source type has no object in any world -
otherwise the check would miss those objects: exit 2 (deferred_with_objects). Zero expected is exit 2 as before. The evidence file
carries ownership_types, expected_ownership_types and the unpublished names with the counts.
Exit 0: gaps is 0 (dangling, gaps_erasing and relation_only are reported, never failing). Exit 1: gaps > 0 (one <3> line on stderr for journalctl -p err; the alarm itself is the failed scheduled run:
host-health records it and timer_scheduled_failure fires). Exit 2: refused, or the evidence could not be written - a check without
its evidence is a failure too.
"""
import argparse
from datetime import UTC, datetime, timedelta
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import relation_reads as RR  # noqa: E402

KEEP_DAYS = 30
# The first batch's ownership relation types (name, source type, property, target type): a copy, because the release on
# DATA_HOST carries no nexloop_eios.relation_catalog (and none of its dependencies); tests/test_relation_read_gaps_script.py
# checks it equals relation_catalog.FIRST_BATCH. Only these matter for coverage: a non-ownership type covers no object.
OWNERSHIP = (
    ('goal_of', 'Goal', 'consumer_id', 'Consumer'),
    ('plan_step_of', 'PlanStep', 'consumer_id', 'Consumer'),
    ('control_of', 'EffectControl', 'consumer_id', 'Consumer'),
    ('conversation_of', 'Conversation', 'consumer_id', 'Consumer'),
    ('message_in', 'Message', 'conversation_id', 'Conversation'),
    ('commitment_to', 'Commitment', 'made_to', 'Consumer'),
    ('commercial_record_of', 'CommercialRecord', 'consumer_ref', 'Consumer'),
    ('assignment_of', 'MessageAssignment', 'consumer_id', 'Consumer'),
    ('owned_by', 'ConsumerOwnership', 'consumer_id', 'Consumer'),
    ('role_link_of', 'ConsumerRoleLink', 'consumer_id', 'Consumer'),
    ('service_offering_of', 'ConsumerServiceOffering', 'consumer_id', 'Consumer'),
    ('incentive_offering_of', 'ConsumerIncentiveOffering', 'consumer_id', 'Consumer'),
    ('scope_for_consumer', 'RoleAssignmentScope', 'consumer_id', 'Consumer'),
    ('assessment_evidence', 'RelationshipAssessment', 'evidence_message_id', 'Message'),
)


def specs():
    return [{'name': n, 'source_type': s, 'property': p, 'target_type': t} for n, s, p, t in OWNERSHIP]


def check(conn, tenant):
    with conn.transaction():
        conn.execute('set transaction read only')
        doc = conn.execute('select authz.nexloop_relation_read_gaps(%s)', (tenant,)).fetchone()[0]
        pre = conn.execute('select authz.nexloop_relation_precheck(%s,%s::jsonb)', (tenant, json.dumps(specs()))).fetchone()[0]
    return doc, pre


def coverage(pre):
    """(expected ownership source types, unpublished ownership names, those of them whose source type has objects)."""
    published = set(pre['catalog']['latest_type_versions'])
    unpublished = sorted(n for n, *_ in OWNERSHIP if n not in published)
    with_objects = sorted({f['relation'] for f in pre['fields'] if f['relation'] in unpublished and f['objects']})
    expected = len({s for n, s, *_ in OWNERSHIP if n in published})
    return expected, unpublished, with_objects


def write_evidence(directory, doc, now):
    directory = Path(directory)
    target = directory / f"{now.strftime('%Y-%m-%d')}.json"
    fd, temp = tempfile.mkstemp(dir=directory, prefix='.relation-read-gaps-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump({**doc, 'checked_at': now.isoformat()}, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o640)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return target


def prune_evidence(directory, now, err):
    """Evidence older than 30 days goes; a failure here is a warning only (L6 S-3): today's check and its evidence stand."""
    keep_after = (now.date() - timedelta(days=KEEP_DAYS)).isoformat()
    try:
        for old in Path(directory).glob('????-??-??.json'):
            if old.stem < keep_after:
                old.unlink(missing_ok=True)
    except OSError as error:
        print('<4>nexloop-relation-read-gaps evidence_prune_failed error=' + type(error).__name__, file=err, flush=True)


def main(argv=None, *, connect=None, clock=None, out=sys.stdout, err=sys.stderr):
    p = argparse.ArgumentParser(description='NX-054 P4a relation read gap check (read only)')
    p.add_argument('--dsn-file', type=Path, required=True)
    p.add_argument('--tenant', required=True)
    p.add_argument('--evidence-dir', type=Path, required=True)
    a = p.parse_args(argv)
    try:
        conn = (connect or RR.connect_metrics)(a.dsn_file)
    except RR.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    try:
        doc, pre = check(conn, a.tenant)
    finally:
        conn.close()
    expected, unpublished, with_objects = coverage(pre)
    doc = {**doc, 'expected_ownership_types': expected, 'unpublished_ownership': unpublished}
    now = (clock or (lambda: datetime.now(UTC)))()
    try:
        path = write_evidence(a.evidence_dir, doc, now)
    except OSError as error:
        print('<3>nexloop-relation-read-gaps evidence_unwritten error=' + type(error).__name__, file=err, flush=True)
        print(json.dumps({**doc, 'evidence': None}, sort_keys=True), file=out)
        return 2
    prune_evidence(a.evidence_dir, now, err)
    print(json.dumps({**doc, 'evidence': str(path)}, sort_keys=True), file=out)
    covered = doc.get('ownership_types')
    if type(covered) is not int or expected == 0 or covered != expected:
        # Stage drill 2026-10-26: no published ownership relation type, so the check covered nothing - a failure, never "no gap".
        print(f'<3>nexloop-relation-read-gaps catalog_unpublished ownership_types={covered} expected={expected}', file=err, flush=True)
        return 2
    if with_objects:
        print(f"<3>nexloop-relation-read-gaps deferred_with_objects types={','.join(with_objects)}", file=err, flush=True)
        return 2
    if doc['gaps']:
        worlds = len({r['world'] for r in doc['rows'] if r['gaps']})
        print(f"<3>nexloop-relation-read-gaps gaps={doc['gaps']} worlds={worlds}", file=err, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

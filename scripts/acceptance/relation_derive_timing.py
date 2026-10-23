#!/usr/bin/env python3
"""NX-054 stage baseline (NX-054-P1.md §4, stage form): read-only timings to compare before ("idle": P2 trigger on, no relation type
published) and after publication ("derived"). Counts and durations only.

    relation_derive_timing.py --dsn-file <inspector DSN> --tenant T erasure --consumer <64 hex>
    relation_derive_timing.py --dsn-file <inspector DSN> --tenant T relay --since <ISO> --until <ISO>

erasure  one NX-029 erasure of a synthetic Consumer (scripts/deploy/erase_synthetic_consumer.py): the request's state, its object
         count by type before (the objects the purge pseudonymizes or deletes, counted from the ontology for a Consumer not yet
         purged), and the wall time from the request to completion plus the time the purge itself took (from the first to the
         last of its tombstones). The P2 trigger rides on the purge's property updates and deletes.
relay    the real conversation path, existing metrics only (no traffic): per relay step (runtime.nexloop_process_samples, kind
         relay_step - goal_create, step_create, assignment_create and the rest write the objects derivation follows) the samples
         in the window, their calls, and the p95 over windows (the median and the max of the per-window p95).
The sim generation is timed by the caller (`time` around sim_world.py generate): it writes runtime.nexloop_sim_objects only,
never ontology.objects, so derivation cannot change it - it is the control of the comparison.
"""
import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import s6_common as C  # noqa: E402

OWNED = (('Goal', 'consumer_id'), ('PlanStep', 'consumer_id'), ('EffectControl', 'consumer_id'), ('Conversation', 'consumer_id'),
         ('Commitment', 'made_to'), ('CommercialRecord', 'consumer_ref'), ('MessageAssignment', 'consumer_id'), ('ConsumerOwnership', 'consumer_id'),
         ('ConsumerRoleLink', 'consumer_id'), ('ConsumerServiceOffering', 'consumer_id'), ('ConsumerIncentiveOffering', 'consumer_id'),
         ('RoleAssignmentScope', 'consumer_id'))


def erasure(db, tenant, consumer):
    row = db.execute("""select request_id,state,created_at,completed_at,counts from control.nexloop_consumer_erasures
        where tenant_id=%s and world='real' and consumer_id=%s""", (tenant, consumer)).fetchone()
    objects = {}
    for type_name, prop in OWNED:
        n = db.execute("select count(*) from ontology.objects where tenant_id=%s and world='real' and type_name=%s and properties->>%s=%s",
                       (tenant, type_name, prop, consumer)).fetchone()[0]
        if n:
            objects[type_name] = n
    messages = db.execute("""select count(*) from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations c on c.tenant_id=m.tenant_id
        and c.world=m.world and c.conversation_id=m.conversation_id where c.tenant_id=%s and c.world='real' and c.consumer_id=%s""", (tenant, consumer)).fetchone()[0]
    if messages:
        objects['Message'] = messages
    doc = {'objects_now': objects, 'objects_total': sum(objects.values())}
    if row is None:
        return {**doc, 'erasure': None}
    request, state, created, completed, counts = row
    purge = db.execute("""select min(executed_at),max(executed_at),count(*) from runtime.nexloop_erasure_tombstones
        where tenant_id=%s and world='real' and reason='consumer_erasure' and (request_ref=%s or request_ref like %s)""",
                       (tenant, request, '%:' + request)).fetchone()
    return {**doc, 'erasure': {'state': state, 'request_to_completion_seconds': (completed - created).total_seconds() if completed else None,
                               'purge_seconds': (purge[1] - purge[0]).total_seconds() if purge[0] else None, 'tombstones': purge[2],
                               'counts': counts}}


def relay(db, since, until):
    rows = db.execute("""select service,(sample->>'calls')::numeric,(sample->>'p95_ms')::numeric from runtime.nexloop_process_samples
        where kind='relay_step' and recorded_at>=%s and recorded_at<%s and sample ? 'p95_ms'""", (since, until)).fetchall()
    steps = {}
    for service, calls, p95 in rows:
        s = steps.setdefault(service, {'samples': 0, 'calls': 0, 'p95': []})
        s['samples'] += 1
        s['calls'] += int(calls or 0)
        if calls:
            s['p95'].append(float(p95))
    return {k: {'samples': v['samples'], 'calls': v['calls'], 'p95_ms_median': statistics.median(v['p95']) if v['p95'] else None,
                'p95_ms_max': max(v['p95']) if v['p95'] else None} for k, v in sorted(steps.items())}


def main(argv=None, out=sys.stdout, connect=None):
    p = argparse.ArgumentParser(description='NX-054 stage baseline timings (read only)')
    p.add_argument('--dsn-file', required=True)
    p.add_argument('--tenant', required=True)
    sub = p.add_subparsers(dest='mode', required=True)
    e = sub.add_parser('erasure')
    e.add_argument('--consumer', required=True)
    r = sub.add_parser('relay')
    r.add_argument('--since', required=True, type=datetime.fromisoformat)
    r.add_argument('--until', required=True, type=datetime.fromisoformat)
    a = p.parse_args(argv)
    try:
        with (connect or (lambda: C.connect_read_only(a.dsn_file, who='inspector')))() as db:
            db.execute("select set_config('eios.tenant_id',%s,false)", (a.tenant,))
            doc = erasure(db, a.tenant, a.consumer) if a.mode == 'erasure' else relay(db, a.since, a.until)
    except C.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    print(json.dumps({'mode': a.mode, **({'result': doc} if a.mode == 'relay' else doc)}, ensure_ascii=False, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())

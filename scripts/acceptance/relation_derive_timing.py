#!/usr/bin/env python3
"""NX-054 stage baseline (NX-054-P1.md §4, stage form): read-only timings to compare before ("idle": P2 triggers on, no relation
type published) and after publication ("derived"). Counts and durations only.

    relation_derive_timing.py --dsn-file <nexloop_metrics DSN> --tenant T erasure --consumer <64 hex>
    relation_derive_timing.py --dsn-file <nexloop_metrics DSN> relay --since <ISO> --until <ISO>

As the read-only metrics login (relation_reads.connect_metrics: role checked, statement timeout 60 s):
erasure  authz.nexloop_relation_erasure_timing: the objects the Consumer's erasure purges (counted now, by type), and once it ran its
         state, request-to-completion time, `sync_purge_seconds` - the span of its synchronous tombstones (every item class but
         run_file, which the keeper writes later after the file deletion; L6 review R-3) - and the span per item class. The
         comparison uses sync_purge_seconds.
relay    authz.nexloop_relation_relay_p95: the real conversation path, existing metrics only (no traffic) - per relay step (kind
         relay_step: goal_create, step_create, assignment_create and the rest write the objects derivation follows) the samples,
         calls, and the median and max of the per-window p95.
The sim generation is timed by the caller (`time` around sim_world.py generate): it writes runtime.nexloop_sim_objects only,
never ontology.objects, so derivation cannot change it - it is the control of the comparison.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relation_reads as RR  # noqa: E402


def main(argv=None, out=sys.stdout, connect=None):
    p = argparse.ArgumentParser(description='NX-054 stage baseline timings (read only)')
    p.add_argument('--dsn-file', required=True)
    p.add_argument('--tenant')
    sub = p.add_subparsers(dest='mode', required=True)
    e = sub.add_parser('erasure')
    e.add_argument('--consumer', required=True)
    r = sub.add_parser('relay')
    r.add_argument('--since', required=True, type=datetime.fromisoformat)
    r.add_argument('--until', required=True, type=datetime.fromisoformat)
    a = p.parse_args(argv)
    try:
        if a.mode == 'erasure' and not a.tenant:
            raise RR.Refused('erasure: --tenant required')
        with (connect or (lambda: RR.connect_metrics(a.dsn_file)))() as db:
            if a.mode == 'erasure':
                doc = db.execute('select authz.nexloop_relation_erasure_timing(%s,%s)', (a.tenant, a.consumer)).fetchone()[0]
            else:
                doc = {'result': db.execute('select authz.nexloop_relation_relay_p95(%s,%s)', (a.since, a.until)).fetchone()[0]}
    except RR.Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    print(json.dumps({'mode': a.mode, **doc}, ensure_ascii=False, default=str), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())

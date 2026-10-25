#!/usr/bin/env python3
"""NX-054 P4a daily gap check on stage (docs/design/relation-read-p4.md §2.3, §2.5), read only.

    python3 scripts/acceptance/relation_read_gaps.py --dsn-file <nexloop_metrics DSN, 0600> --tenant <TENANT_ID> --evidence-dir <dir>

As the metrics login (relation_reads.checked: role checked, statement timeout 60 s), in a transaction set read only, one call of
authz.nexloop_relation_read_gaps (0360): per world and type, the objects whose property-rule Consumers the relations miss (gaps),
the same among erasing Consumers (gaps_erasing, apart: P5 looks at gaps only) and the reverse (relation_only). Counts only.
The answer is written to <evidence-dir>/<UTC date>.json (0640, replaced atomically) and printed.

Exit 0: gaps is 0. Exit 1: gaps > 0 (one <3> line on stderr for journalctl -p err; the alarm itself is the failed scheduled run:
host-health records it and timer_scheduled_failure fires). Exit 2: refused, or the evidence could not be written - a check without
its evidence is a failure too.
"""
import argparse
from datetime import UTC, datetime
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import relation_reads as RR  # noqa: E402


def check(conn, tenant):
    with conn.transaction():
        conn.execute('set transaction read only')
        return conn.execute('select authz.nexloop_relation_read_gaps(%s)', (tenant,)).fetchone()[0]


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
        doc = check(conn, a.tenant)
    finally:
        conn.close()
    now = (clock or (lambda: datetime.now(UTC)))()
    try:
        path = write_evidence(a.evidence_dir, doc, now)
    except OSError as error:
        print('<3>nexloop-relation-read-gaps evidence_unwritten error=' + type(error).__name__, file=err, flush=True)
        print(json.dumps({**doc, 'evidence': None}, sort_keys=True), file=out)
        return 2
    print(json.dumps({**doc, 'evidence': str(path)}, sort_keys=True), file=out)
    if doc['gaps']:
        worlds = len({r['world'] for r in doc['rows'] if r['gaps']})
        print(f"<3>nexloop-relation-read-gaps gaps={doc['gaps']} worlds={worlds}", file=err, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

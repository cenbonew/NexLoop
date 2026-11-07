#!/usr/bin/env python3
"""NX-064 measurement gate (design NX-064-effect-timeout-design.md §4.3, ADR-029c §1.2): the Host-side p99 of one effect operation,
from its log-spaced histogram (migration 0405: kind host_op, service = the operation, keys h_<i>), against the deployed effect limit:
p99 x 1.5 must not exceed it. Read-only: it reads one file and prints one JSON line; it connects to nothing.

    nx064_p99_gate.py --samples FILE --limit-ms 2000 [--operation effect_submit] [--kind host_op] [--since ISO] [--until ISO]
    nx064_p99_gate.py --host-metrics FILE --limit-ms 2000 [--operation effect_submit]

--samples       process samples as JSON lines {kind, service, window_start, sample}: the NX-031 load matrix's process_samples.jsonl
                on the CI host, or the stage export of runtime.nexloop_process_samples (docs/release/run-nx064-effect-request-metrics.md
                §6). Samples are per-minute deltas and are summed. --kind guard_op reads the guard-side histogram instead (the
                ADR-029c estimate; the output says so).
--host-metrics  one Host metrics document (the authenticated loopback /internal/v1/metrics answer, cumulative since that Host
                started): keys <operation>_h_<i>.

Buckets (nexloop_eios.observability.histogram_bucket): 0 holds <= 1 ms, i holds (1.1^(i-1), 1.1^i] ms, 116 everything above
1.1^115 ms. p50/p99 are reported as the bucket's geometric midpoint (as scripts/load/report.py) and as its upper bound; the gate uses
the upper bound, so it never passes on the 4.9 % estimate error (the open last bucket has no upper bound: fail).

Output: {operation, source, kind, samples, p50_ms, p99_ms, p99_upper_ms, limit_ms, gate, reason, suggested_effect_timeout_ms}.
gate: pass (p99_upper x 1.5 <= limit), fail, or insufficient - fewer than 1000 requests, an empty histogram, or nothing readable.
insufficient is never a pass. Exit 0 pass, 1 fail, 2 insufficient, 3 input refused.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

RATIO, LAST, MINIMUM = 1.1, 116, 1000
OPERATIONS = ('effect_submit', 'enterprise_submit', 'effect_find', 'incentive')
_BUCKET = re.compile(r'^h_(\d{1,3})$')


class Refused(ValueError):
    pass


def _count(value, where):
    if type(value) is not int or value < 0:
        raise Refused(f'{where}: not a count')
    return value


def _add(counts, index, value, where):
    if index > LAST:
        raise Refused(f'{where}: bucket out of range')
    counts[index] = counts.get(index, 0) + _count(value, where)


def from_samples(lines, operation, kind, since=None, until=None):
    counts = {}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise Refused(f'line {n}: not JSON') from None
        if type(row) is not dict or row.get('kind') != kind or row.get('service') != operation:
            continue
        if since or until:
            at = datetime.fromisoformat(str(row.get('window_start')).replace('Z', '+00:00'))
            if (since and at < since) or (until and at >= until):
                continue
        for key, value in (row.get('sample') or {}).items():
            match = _BUCKET.fullmatch(key)
            if match:
                _add(counts, int(match.group(1)), value, f'line {n} {key}')
    return counts


def from_host_metrics(document, operation):
    if type(document) is not dict:
        raise Refused('host metrics: not an object')
    pattern, counts = re.compile(rf'^{re.escape(operation)}_h_(\d{{1,3}})$'), {}
    for key, value in document.items():
        match = pattern.fullmatch(key)
        if match:
            _add(counts, int(match.group(1)), value, key)
    return counts


def _midpoint(index):
    return 1.0 if index == 0 else round(RATIO ** (LAST - 1), 1) if index >= LAST else round(RATIO ** (index - 0.5), 1)


def _upper(index):
    return 1.0 if index == 0 else math.inf if index >= LAST else round(RATIO ** index, 1)


def quantile_bucket(counts, q):
    total = sum(counts.values())
    target, running = max(1, math.ceil(q * total)), 0
    for index in sorted(counts):
        running += counts[index]
        if running >= target:
            return index
    return None


def judge(counts, limit_ms, *, operation, source, kind):
    total = sum(counts.values())
    out = {'operation': operation, 'source': source, 'kind': kind, 'samples': total, 'p50_ms': None, 'p99_ms': None, 'p99_upper_ms': None,
           'limit_ms': limit_ms, 'gate': 'insufficient', 'reason': None, 'suggested_effect_timeout_ms': None}
    if kind == 'guard_op':
        out['estimate'] = 'guard p99 + 300 ms (ADR-029c: no Host-side data)'
    if total == 0:
        return {**out, 'reason': 'empty_histogram'}
    p50, p99 = quantile_bucket(counts, 0.50), quantile_bucket(counts, 0.99)
    upper = _upper(p99) + (300 if kind == 'guard_op' else 0)
    out.update(p50_ms=_midpoint(p50), p99_ms=_midpoint(p99) + (300 if kind == 'guard_op' else 0), p99_upper_ms=None if math.isinf(upper) else round(upper, 1))
    if total < MINIMUM:
        return {**out, 'reason': f'fewer_than_{MINIMUM}_requests'}
    if math.isinf(upper):
        return {**out, 'gate': 'fail', 'reason': 'p99_in_the_open_last_bucket'}
    # ADR-029c §1.2: clamp(ceil(p99 x 1.5 / 100) x 100, 2000, 5000); above 5000 it is a performance problem, not a larger limit.
    raw = math.ceil(upper * 1.5 / 100) * 100
    out['suggested_effect_timeout_ms'] = min(max(raw, 2000), 5000) if raw <= 5000 else None
    return {**out, 'gate': 'pass' if upper * 1.5 <= limit_ms else 'fail', 'reason': None if upper * 1.5 <= limit_ms else 'p99_x_1.5_over_the_limit'}


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--samples', type=Path)
    source.add_argument('--host-metrics', type=Path)
    parser.add_argument('--limit-ms', type=int, required=True)
    parser.add_argument('--operation', choices=OPERATIONS, default='effect_submit')
    parser.add_argument('--kind', choices=('host_op', 'guard_op'), default='host_op')
    parser.add_argument('--since')
    parser.add_argument('--until')
    args = parser.parse_args(argv)
    try:
        if not 1000 <= args.limit_ms <= 5000:
            raise Refused('limit-ms must be 1000-5000 (runtime-effect-tools.ts)')
        since = datetime.fromisoformat(args.since) if args.since else None
        until = datetime.fromisoformat(args.until) if args.until else None
        path = args.samples or args.host_metrics
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            result = judge({}, args.limit_ms, operation=args.operation, source=str(path.name), kind=args.kind)
            result['reason'] = 'source_unreadable'
            print(json.dumps(result, sort_keys=True), file=out)
            return 2
        if args.samples:
            counts = from_samples(text.splitlines(), args.operation, args.kind, since, until)
        else:
            try:
                document = json.loads(text)
            except ValueError:
                raise Refused('host metrics: not JSON') from None
            counts = from_host_metrics(document, args.operation)
    except (Refused, ValueError) as error:
        print(json.dumps({'gate': 'refused', 'reason': str(error)[:200]}), file=out)
        return 3
    result = judge(counts, args.limit_ms, operation=args.operation, source='samples' if args.samples else 'host_metrics', kind=args.kind)
    print(json.dumps(result, sort_keys=True), file=out)
    return {'pass': 0, 'fail': 1}.get(result['gate'], 2)


if __name__ == '__main__':
    sys.exit(main())

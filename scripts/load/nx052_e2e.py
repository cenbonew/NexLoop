"""NX-052 end-to-end measurement on the 200-customer template (implementation note §11, dispatcher D-a 2026-10-13).

    matrix   write the matrix the lane runs for one state: the base matrix with one C4-low-class cell set to the given
             rate and host concurrency (every other cell unchanged, so matrix.py still validates; only that cell runs);
    compare  read the report.json of two lane runs (baseline = no tenant keys, candidate = keys, or keys + publications)
             and judge ack, end-to-end and dispatch p95 against the +5 % limit, plus the correctness and capacity gate items of the
             candidate (owner 2026-10-14, ROADMAP §7: at 0.1/s the cell's AT-060 gate verdict is not required, its idle dispatch
             gate being set for 0.05/s).

    uv run --frozen python scripts/load/nx052_e2e.py matrix --rate 0.1 --out <dir>/matrix.nx052.json
    uv run --frozen python scripts/load/nx052_e2e.py compare --baseline <runA>/report.json --candidate <runB>/report.json
"""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CELL = 'C4-low'
LIMIT = 1.05
METRICS = {'ack_p95_ms': ('metrics', 'ack_p95_ms'), 'e2e_p95_ms': ('decomposition', 'e2e_ms', 'p95'),
           'idle_dispatch_p95_ms': ('metrics', 'idle_dispatch_p95_ms')}
# Gate items of report.py that must still pass in the candidate (owner 2026-10-14, ROADMAP §7): identity (with its sample),
# unresolved work, guard timeout rate, connections, critical alerts, unexpected refusals. The latency gates (ack, idle dispatch,
# guard p95), drain and dead letters are not required in this C4-low-class cell at 0.1/s; the three p95 increases are judged above.
REQUIRED_GATE_ITEMS = ('identity_violations', 'model_context_sample', 'unresolved_work', 'guard_timeout_rate_5m_max', 'connections_peak',
                       'critical_alerts', 'unexpected_refusals')


def base_matrix():
    """The newest matrix in deploy/load (v2 once it lands, else v1)."""
    return sorted((ROOT / 'deploy/load').glob('matrix.v*.json'), key=lambda p: int(p.stem.split('.v')[1]))[-1]


def matrix(base, rate, concurrency=4, cell=CELL):
    value = copy.deepcopy(json.loads(Path(base).read_text()))
    cells = [c for c in value['cells'] if c['cell'] == cell]
    if len(cells) != 1 or not 0 < rate <= 1:
        raise ValueError('one C4-low-class cell and a rate in (0, 1] are required')
    cells[0].update(rate_per_s=rate, host_concurrency=concurrency)
    value['nx052'] = {'base': Path(base).name, 'cell': cell, 'rate_per_s': rate, 'host_concurrency': concurrency}
    return value


def _get(cell, path):
    for key in path:
        cell = (cell or {}).get(key)
    return cell


def compare(baseline, candidate, cell=CELL, limit=LIMIT):
    pick = lambda report: next(c for c in report['cells'] if c['cell'] == cell)  # noqa: E731
    a, b = pick(baseline), pick(candidate)
    rows, ok = {}, True
    for name, path in METRICS.items():
        before, after = _get(a, path), _get(b, path)
        if before is None or after is None or before <= 0:
            rows[name] = {'baseline': before, 'candidate': after, 'ratio': None, 'within': False}
            ok = False
            continue
        ratio = after / before
        rows[name] = {'baseline': before, 'candidate': after, 'ratio': round(ratio, 4), 'within': ratio <= limit}
        ok = ok and ratio <= limit
    # The required gate items must pass in the candidate (a missing item fails); the cell's overall gate verdict is reported only.
    items = (b.get('gate') or {}).get('items') or {}
    required = {name: (items.get(name) or {}).get('result', 'missing') for name in REQUIRED_GATE_ITEMS}
    gates = {'baseline': (a.get('gate') or {}).get('result'), 'candidate': (b.get('gate') or {}).get('result')}
    return {'cell': cell, 'limit': limit, 'metrics': rows, 'gate': gates, 'required_gate_items': required,
            'passed': ok and all(result == 'pass' for result in required.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    m = sub.add_parser('matrix')
    m.add_argument('--base', default=None)
    m.add_argument('--rate', type=float, required=True)
    m.add_argument('--concurrency', type=int, default=4)
    m.add_argument('--out', required=True)
    c = sub.add_parser('compare')
    c.add_argument('--baseline', required=True)
    c.add_argument('--candidate', required=True)
    c.add_argument('--cell', default=CELL)
    args = parser.parse_args(argv)
    if args.command == 'matrix':
        Path(args.out).write_text(json.dumps(matrix(args.base or base_matrix(), args.rate, args.concurrency), indent=2) + '\n')
        return 0
    result = compare(json.loads(Path(args.baseline).read_text()), json.loads(Path(args.candidate).read_text()), args.cell)
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())

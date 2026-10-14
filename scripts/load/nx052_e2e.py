"""NX-052 end-to-end measurement on the 200-customer template (implementation note §11, dispatcher D-a 2026-10-13).

    matrix   write the matrix the lane runs for one state: the base matrix with one C4-low-class cell set to the given
             rate and host concurrency (every other cell unchanged, so matrix.py still validates; only that cell runs);
    compare  read the report.json of lane runs (baseline = no tenant keys, candidate = keys, or keys + publications) and judge
             ack, end-to-end and dispatch p95 against the +5 % limit, plus the correctness and capacity gate items of every
             candidate run (owner 2026-10-14, ROADMAP §7: at 0.1/s the cell's AT-060 gate verdict is not required, its idle
             dispatch gate being set for 0.05/s). Several runs per side (1A, owner 2026-10-14): each metric is the median of
             the runs' p95, and the ratio is candidate median / baseline median. With --state P the ack p95 is recorded only, not
             judged (2A: the tenant row lock pulse raises ack by design, accepted_limitation).

    uv run --frozen python scripts/load/nx052_e2e.py matrix --rate 0.1 --out <dir>/matrix.nx052.json
    uv run --frozen python scripts/load/nx052_e2e.py compare --baseline <N1> <N2> <N3> --candidate <K1> <K2> <K3>   (report.json each)
    uv run --frozen python scripts/load/nx052_e2e.py compare --state P --baseline <N...> --candidate <P...>
"""
import argparse
import copy
import json
import statistics
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


# 2A (owner 2026-10-14, ROADMAP §7): in state P the tenant row lock pulse (0162 FOR NO KEY UPDATE) holds back the inbound
# authorization's FOR SHARE read (0045:178) for up to 0.2 s per pulse; its ack increase is an accepted limitation, recorded only.
RECORDED_ONLY = {'P': {'ack_p95_ms': 'accepted_limitation'}}


def _runs(value):
    return [value] if isinstance(value, dict) else list(value)


def compare(baseline, candidate, cell=CELL, limit=LIMIT, state='K'):
    """baseline and candidate: one report or a list of reports (1A: N and K three rounds each). Each metric is the median of
    the runs' p95 on each side; the ratio is candidate median / baseline median."""
    pick = lambda report: next(c for c in report['cells'] if c['cell'] == cell)  # noqa: E731
    before_runs, after_runs = [pick(r) for r in _runs(baseline)], [pick(r) for r in _runs(candidate)]
    if not before_runs or not after_runs:
        raise ValueError('at least one baseline and one candidate report')
    recorded_only = RECORDED_ONLY.get(state, {})
    rows, ok = {}, True
    for name, path in METRICS.items():
        before_values, after_values = [_get(c, path) for c in before_runs], [_get(c, path) for c in after_runs]
        row = {'baseline_runs': before_values, 'candidate_runs': after_values}
        if any(v is None for v in before_values + after_values):
            before = after = None
        else:
            before, after = statistics.median(before_values), statistics.median(after_values)
        row.update(baseline=before, candidate=after)
        if before is None or after is None or before <= 0:
            row.update(ratio=None, within=False)
        else:
            ratio = after / before
            row.update(ratio=round(ratio, 4), within=ratio <= limit)
        if name in recorded_only:
            row.update(judged=False, reason=recorded_only[name])
        else:
            row['judged'] = True
            ok = ok and row['within']
        rows[name] = row
    # The required gate items must pass in every candidate run (a missing item fails); the overall gate verdicts are reported only.
    required = {}
    for name in REQUIRED_GATE_ITEMS:
        results = [(((c.get('gate') or {}).get('items') or {}).get(name) or {}).get('result', 'missing') for c in after_runs]
        required[name] = 'pass' if all(r == 'pass' for r in results) else next(r for r in results if r != 'pass')
    gates = {'baseline': [(c.get('gate') or {}).get('result') for c in before_runs], 'candidate': [(c.get('gate') or {}).get('result') for c in after_runs]}
    if len(before_runs) == len(after_runs) == 1:
        gates = {k: v[0] for k, v in gates.items()}
    return {'cell': cell, 'limit': limit, 'state': state, 'runs': {'baseline': len(before_runs), 'candidate': len(after_runs)}, 'metrics': rows,
            'gate': gates, 'required_gate_items': required, 'passed': ok and all(result == 'pass' for result in required.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    m = sub.add_parser('matrix')
    m.add_argument('--base', default=None)
    m.add_argument('--rate', type=float, required=True)
    m.add_argument('--concurrency', type=int, default=4)
    m.add_argument('--out', required=True)
    c = sub.add_parser('compare')
    c.add_argument('--baseline', required=True, nargs='+')
    c.add_argument('--candidate', required=True, nargs='+')
    c.add_argument('--cell', default=CELL)
    c.add_argument('--state', choices=('K', 'P'), default='K', help='candidate state: K (keys) or P (keys + lock pulse, ack recorded only)')
    args = parser.parse_args(argv)
    if args.command == 'matrix':
        Path(args.out).write_text(json.dumps(matrix(args.base or base_matrix(), args.rate, args.concurrency), indent=2) + '\n')
        return 0
    load = lambda paths: [json.loads(Path(p).read_text()) for p in paths]  # noqa: E731
    result = compare(load(args.baseline), load(args.candidate), args.cell, state=args.state)
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())

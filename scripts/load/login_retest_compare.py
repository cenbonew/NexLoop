"""Login-peak retest comparison (docs/implementation/relay2-budget.md §4; runbook docs/implementation/login-peak-retest.md).

    uv run --frozen python scripts/load/login_retest_compare.py .ci-results/load/<pool4> .ci-results/load/<pool3> .ci-results/load/<pool2>

The first run directory is the baseline (identity pool 4); every later one is judged against it, cell by cell (C4-peak, C4), by
the §4 table: login p95 at most +20 % with no 503 and no unanswered login, steady ACK p95 at most 500 ms with no more 503 than the
baseline, every workbench event 200, and a connection peak of at most 60. Reads only cells/<cell>/cell.json and report.json
(numbers only). Prints a Markdown table and a JSON verdict; exit 0 when every candidate passes, 1 otherwise, 2 on missing input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CELLS = ('C4-peak', 'C4')
LOGIN_P95_GROWTH = 0.20
ACK_P95_MS = 500
CONNECTIONS_PEAK = 60


def _statuses(value):
    """HTTP status codes inside a workbench event value (an int, or a dict / list holding them)."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value] if 100 <= value <= 599 else []
    if isinstance(value, dict):
        return [s for v in value.values() for s in _statuses(v)]
    if isinstance(value, list):
        return [s for v in value for s in _statuses(v)]
    return []


def workbench_not_200(events):
    """How many workbench events carried a status other than 200 (matrix.py's _count labels: '<kind>:<json value>')."""
    bad = 0
    for label, count in (events or {}).items():
        _, _, raw = label.partition(':')
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if any(s != 200 for s in _statuses(value)):
            bad += count
    return bad


def read_run(directory):
    directory = Path(directory)
    report = json.loads((directory / 'report.json').read_text())
    by_cell = {c['cell']: c for c in report.get('cells', [])}
    cells = {}
    for name in CELLS:
        path = directory / 'cells' / name / 'cell.json'
        if not path.is_file() or name not in by_cell:
            continue
        cell, metrics = json.loads(path.read_text()), by_cell[name].get('metrics', {})
        logins = cell.get('logins') or {}
        login_statuses = logins.get('statuses') or {}
        cells[name] = {
            'identity_pool_max': cell.get('identity_pool_max'),
            'login_requests': logins.get('requests'), 'login_p95_ms': logins.get('p95_ms'), 'login_max_ms': logins.get('max_ms'),
            'login_503': login_statuses.get('503', 0), 'login_unanswered': login_statuses.get('0', 0),
            'ack_p95_ms': metrics.get('ack_p95_ms'), 'ack_p99_ms': metrics.get('ack_p99_ms'),
            'ack_503': (by_cell[name].get('ack_statuses') or {}).get('503', 0),
            'workbench_not_200': workbench_not_200(cell.get('workbench_events')),
            'connections_peak': metrics.get('connections_peak'),
        }
    return {'run': directory.name, 'cells': cells}


def judge(base, cand):
    """The §4 checks of one candidate cell against the baseline cell: {check: bool}."""
    checks = {}
    if base.get('login_p95_ms') and cand.get('login_p95_ms') is not None:
        checks['login_p95_growth_le_20pct'] = cand['login_p95_ms'] <= base['login_p95_ms'] * (1 + LOGIN_P95_GROWTH)
    else:
        checks['login_p95_growth_le_20pct'] = False
    checks['login_503_zero'] = cand.get('login_503', 0) == 0
    checks['login_unanswered_zero'] = cand.get('login_unanswered', 0) == 0
    checks['ack_p95_le_500ms'] = cand.get('ack_p95_ms') is not None and cand['ack_p95_ms'] <= ACK_P95_MS
    checks['ack_503_not_more'] = cand.get('ack_503', 0) <= base.get('ack_503', 0)
    checks['workbench_all_200'] = cand.get('workbench_not_200', 0) == 0
    checks['connections_peak_le_60'] = cand.get('connections_peak') is not None and cand['connections_peak'] <= CONNECTIONS_PEAK
    return checks


COLUMNS = ('identity_pool_max', 'login_requests', 'login_p95_ms', 'login_max_ms', 'login_503', 'login_unanswered',
           'ack_p95_ms', 'ack_p99_ms', 'ack_503', 'workbench_not_200', 'connections_peak')


def compare(runs):
    base, verdicts, lines = runs[0], [], []
    lines.append('| run | cell | ' + ' | '.join(COLUMNS) + ' | verdict |')
    lines.append('|' + '---|' * (len(COLUMNS) + 3))
    for index, run in enumerate(runs):
        for name in CELLS:
            cell = run['cells'].get(name)
            if cell is None:
                lines.append(f"| {run['run']} | {name} | " + ' | '.join('-' for _ in COLUMNS) + ' | missing |')
                if index:
                    verdicts.append({'run': run['run'], 'cell': name, 'passed': False, 'checks': {'cell_present': False}})
                continue
            if index == 0:
                verdict = 'baseline'
            else:
                checks = judge(base['cells'].get(name, {}), cell)
                passed = all(checks.values())
                verdicts.append({'run': run['run'], 'cell': name, 'passed': passed, 'checks': checks})
                verdict = 'pass' if passed else 'FAIL: ' + ', '.join(k for k, v in checks.items() if not v)
            lines.append(f"| {run['run']} | {name} | " + ' | '.join('' if cell[c] is None else str(cell[c]) for c in COLUMNS) + f' | {verdict} |')
    return '\n'.join(lines), verdicts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('runs', nargs='+', help='run directories; the first is the baseline (identity pool 4)')
    args = parser.parse_args(argv)
    if len(args.runs) < 2:
        print('at least a baseline and one candidate run', file=sys.stderr)
        return 2
    try:
        runs = [read_run(r) for r in args.runs]
    except (OSError, ValueError) as error:
        print(f'cannot read a run: {type(error).__name__}', file=sys.stderr)
        return 2
    table, verdicts = compare(runs)
    print(table)
    print()
    print(json.dumps({'baseline': runs[0]['run'], 'verdicts': verdicts, 'passed': all(v['passed'] for v in verdicts)}, indent=2))
    return 0 if all(v['passed'] for v in verdicts) else 1


if __name__ == '__main__':
    raise SystemExit(main())

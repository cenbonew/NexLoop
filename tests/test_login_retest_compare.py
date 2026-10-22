"""scripts/load/login_retest_compare.py: the relay2-budget §4 comparison over synthetic run directories (no lane, no database)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/load'))
import login_retest_compare as C  # noqa: E402


def run(tmp_path, name, pool, login_p95, *, login_statuses=None, ack_p95=300, ack_statuses=None, workbench=None, peak=52):
    d = tmp_path / name
    cells = []
    for cell in ('C4-peak', 'C4'):
        (d / 'cells' / cell).mkdir(parents=True)
        (d / 'cells' / cell / 'cell.json').write_text(json.dumps({
            'cell': cell, 'identity_pool_max': pool,
            'logins': {'requests': 1000, 'p50_ms': 80, 'p95_ms': login_p95, 'max_ms': login_p95 * 2, 'statuses': login_statuses or {'200': 1000}},
            'workbench_events': workbench or {'overview:200': 10, 'takeover_cycle:{"claim": 200, "release": 200}': 1}}))
        cells.append({'cell': cell, 'metrics': {'ack_p95_ms': ack_p95, 'ack_p99_ms': ack_p95 + 100, 'connections_peak': peak},
                      'ack_statuses': ack_statuses or {'202': 600}})
    (d / 'report.json').write_text(json.dumps({'cells': cells}))
    return str(d)


def test_candidates_within_the_section_4_table_pass(tmp_path, capsys):
    runs = [run(tmp_path, 'p4', 4, 100), run(tmp_path, 'p3', 3, 115), run(tmp_path, 'p2', 2, 120)]
    assert C.main(runs) == 0
    out = capsys.readouterr().out
    assert '| p4 | C4-peak |' in out and 'baseline' in out and out.count('| pass |') == 4


def test_each_section_4_check_fails_on_its_own(tmp_path):
    base = run(tmp_path, 'p4', 4, 100)
    cases = {
        'login_p95_growth_le_20pct': dict(login_p95=121),
        'login_429_zero': dict(login_p95=100, login_statuses={'200': 999, '429': 1}),
        'login_503_zero': dict(login_p95=100, login_statuses={'200': 999, '503': 1}),
        'login_unanswered_zero': dict(login_p95=100, login_statuses={'200': 999, '0': 1}),
        'ack_p95_le_500ms': dict(login_p95=100, ack_p95=501),
        'ack_503_not_more': dict(login_p95=100, ack_statuses={'202': 599, '503': 1}),
        'workbench_all_200': dict(login_p95=100, workbench={'overview:200': 9, 'overview:503': 1}),
        'connections_peak_le_60': dict(login_p95=100, peak=61),
    }
    for check, kwargs in cases.items():
        cand = C.read_run(run(tmp_path, f'c-{check}', 3, **kwargs))
        _, verdicts = C.compare([C.read_run(base), cand])
        failed = {k for v in verdicts for k, ok in v['checks'].items() if not ok}
        assert failed == {check}, check


def test_a_missing_cell_fails_and_missing_input_is_exit_2(tmp_path):
    base = run(tmp_path, 'p4', 4, 100)
    cand = run(tmp_path, 'p3', 3, 100)
    (Path(cand) / 'cells/C4/cell.json').unlink()
    assert C.main([base, cand]) == 1
    assert C.main([base, str(tmp_path / 'absent')]) == 2
    assert C.main([base]) == 2


def test_the_ack_threshold_applies_to_the_gate_cell_c4_only(tmp_path):
    """Retest 2026-10-23: C4-peak (3/s, an observe cell) had ACK p95 about 1.1 s in every run, the baseline included; the PRD's 500 ms
    is a gate item of C4 / C4-low only (report.py GATES)."""
    base = C.read_run(run(tmp_path, 'p4', 4, 100, ack_p95=1131))
    cand = C.read_run(run(tmp_path, 'p2', 2, 100, ack_p95=1129))
    _, verdicts = C.compare([base, cand])
    by = {v['cell']: v for v in verdicts}
    assert by['C4-peak']['passed'] and 'ack_p95_le_500ms' not in by['C4-peak']['checks']
    assert not by['C4']['passed'] and by['C4']['checks']['ack_p95_le_500ms'] is False

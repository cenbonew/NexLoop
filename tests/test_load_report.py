"""NX-031 report generator (scripts/load/report.py) on synthetic result directories (contract NX-031-results-contract.md).

Checks the §6.1 verdicts on the gate cells only, the AT-060 conclusion (passed / failed / blocked), the per-Run
platform / model / tool decomposition, the CI interference section, D8 (no text or IDs leave the tool) and contract errors.
"""
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'load'))
import report  # noqa: E402

START = datetime(2026, 10, 11, 22, 0, tzinfo=UTC)
CONFIG = {'C1': (1, 1.0, True, False), 'C2': (2, 1.0, True, False), 'C4': (4, 1.0, True, False), 'C8': (8, 1.0, True, False),
          'C4-low': (4, 0.2, True, False), 'C4-peak': (4, 3.0, True, False), 'C4-quiet': (4, 1.0, False, False), 'C4-ci': (4, 1.0, True, True)}
SENTINEL = '哨兵原文-请勿外传'


def iso(t):
    return t.isoformat().replace('+00:00', 'Z')


def write_lines(path, rows):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def snapshot(dead=0, pending=0, connections=40):
    return {'queues': {'operations': {'pending': pending, 'retry_wait': 0, 'dead_lettered': dead, 'oldest_pending_seconds': 3 if pending else None}},
            'feeds': {'recall-instance': {'pending': 0, 'dead_lettered': 0, 'oldest_pending_seconds': None}},
            'extraction': {'pending_messages': 0, 'oldest_pending_seconds': None}, 'connections': {'total': connections},
            'refusals': {'last_5m': {}, 'last_hour': {}}}


def make_cell(cells, name, *, guard=(100, 0, 900.0), ack=120.0, dispatch=600.0, dead_end=0, connections=40, critical=0,
              refusals=('NXC05',), identity_violation=0, unresolved=0, drained=True, drain_seconds=120, completed=True, skip=(), ids_compared=120):
    concurrency, rate, background, ci = CONFIG[name]
    d = cells / name
    d.mkdir(parents=True)
    steady_end = START + timedelta(minutes=10)
    (d / 'cell.json').write_text(json.dumps({'cell': name, 'role': 'gate' if name in ('C4', 'C4-low') else 'observe', 'host_concurrency': concurrency,
        'rate_per_s': rate, 'background': background, 'ci_background': ci, 'completed': completed, 'aborted': None if completed else 'timeout',
        'window': {'steady_start': iso(START), 'steady_end': iso(steady_end)}, 'drain': {'drained': drained, 'seconds': drain_seconds},
        'host_metrics': 'm09', 'ci': {'run_id': 'ci-20261011-77', 'wall_seconds': 5100, 'deadline_gate_failures': 1, 'failed': 1} if ci else None}))
    if 'snapshots' not in skip:
        write_lines(d / 'snapshots.jsonl', [{'at': iso(START), 'phase': 'steady', 'snapshot': snapshot()},
            {'at': iso(START + timedelta(minutes=5)), 'phase': 'steady', 'snapshot': snapshot(pending=7, connections=connections)},
            {'at': iso(steady_end + timedelta(minutes=2)), 'phase': 'drain', 'snapshot': snapshot(dead=dead_end)}])
    calls, timeouts, p95 = guard
    if 'samples' not in skip:
        rows = []
        for minute in range(10):
            for service in ('runtime-guard-1', 'runtime-guard-2'):
                rows.append({'kind': 'guard', 'service': service, 'instance': 'i', 'window_start': iso(START + timedelta(minutes=minute)), 'window_seconds': 60,
                             'sample': {'calls': calls, 'timeouts': timeouts if minute in (3, 4) else 0, 'errors': 0, 'p50_ms': 300.0, 'p95_ms': p95, 'max_ms': p95 + 100}})
            rows.append({'kind': 'host', 'service': 'agent-host', 'instance': 'h', 'window_start': iso(START + timedelta(minutes=minute)), 'window_seconds': 60,
                         'sample': {'active_runs': concurrency, 'waiting': 1, 'max_active_runs': concurrency, 'admission_timeouts': 0, 'available': True}})
            rows.append({'kind': 'pool', 'service': 'api', 'instance': 'p', 'window_start': iso(START + timedelta(minutes=minute)), 'window_seconds': 60,
                         'sample': {'requests_waiting': 2, 'requests_errors': 0, 'pool_size': 4}})
        rows.append({'kind': 'guard', 'service': 'runtime-guard-1', 'instance': 'i', 'window_start': iso(START + timedelta(minutes=30)), 'window_seconds': 60,
                     'sample': {'calls': 1, 'timeouts': 1, 'p95_ms': 9999}})  # outside the steady window: ignored
        write_lines(d / 'process_samples.jsonl', rows)
    if 'acks' not in skip:
        write_lines(d / 'acks.jsonl', [{'ack_ms': ack if i % 10 == 0 else 40.0, 'status': 202} for i in range(200)] + [{'ack_ms': 5.0, 'status': 409}])
    if 'dispatch' not in skip:
        write_lines(d / 'dispatch.jsonl', [{'dispatch_ms': dispatch if i % 10 == 0 else 300} for i in range(100)])
    write_lines(d / 'refusals.jsonl', [{'code': c, 'at': iso(START)} for c in refusals])
    write_lines(d / 'alerts.jsonl', [{'rule_id': 'guard_timeout_rate', 'severity': 'critical', 'kind': 'firing', 'at': iso(START)}] * critical
                + [{'rule_id': 'reply_pending_age', 'severity': 'warning', 'kind': 'firing', 'at': iso(START)}])
    if 'checks' not in skip:
        checks = {k: {'checked': 50, 'violations': 0} for k in report.IDENTITY}
        checks['run_binding']['violations'] = identity_violation
        if ids_compared is not None:
            checks['model_context']['ids_compared'] = ids_compared
        checks['unresolved_work'] = {'checked': 600, 'unresolved': unresolved}
        (d / 'checks.json').write_text(json.dumps(checks))
    if 'runs' not in skip:
        # An extra text field (contract violation by a writer) must never reach the report.
        write_lines(d / 'runs.jsonl', [{'e2e_ms': 5000 + i, 'run_ms': 4500 + i, 'model_ms': 3000, 'tool_ms': 800.0 if i % 10 else None, 'tool_calls': 3,
            'queue_wait_ms': 100, 'admission_wait_ms': 0, 'outcome': 'replied', 'body': SENTINEL} for i in range(50)])
    return d


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / 'run'
    (d / 'cells').mkdir(parents=True)
    (d / 'run.json').write_text(json.dumps({'run_id': '20261011T220000Z-abc1234', 'seed': 31, 'consumers': 1000, 'manifest_digest': 'a5ae0862' + '0' * 56,
        'commit': '7f5823f' + '0' * 33, 'catalog_digest': 'c' * 64, 'catalog_head': '0133', 'matrix_version': 1,
        'template': {'key': 'd' * 64, 'build_seconds': 1800}, 'started_at': iso(START), 'finished_at': iso(START + timedelta(hours=4)),
        'host': {'cpus': 16, 'mem_gib': 13}}))
    return d


def all_cells(run_dir, **overrides):
    for name in report.CELLS:
        make_cell(run_dir / 'cells', name, **overrides.get(name, {}))


def test_all_cells_pass_and_report_carries_no_text_or_ids(run_dir, tmp_path):
    all_cells(run_dir)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/load/report.py'), str(run_dir), '--out', str(tmp_path / 'out')], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {'written': True, 'at060': 'passed', 'gates': {'C4': 'passed', 'C4-low': 'passed'}}
    data = json.loads((tmp_path / 'out/report.json').read_text())
    c4 = next(c for c in data['cells'] if c['cell'] == 'C4')
    assert c4['metrics']['guard_timeout_rate_5m_max'] == 0.0 and c4['metrics']['guard_p95_ms_max'] == 900.0  # the out-of-window sample is ignored
    assert c4['metrics']['ack_p95_ms'] == 120.0 and c4['metrics']['connections_peak'] == 40 and c4['metrics']['backlog_pending_max'] == 7
    assert c4['refusals'] == {'expected': {'NXC05': 1}, 'unexpected': {}} and c4['alerts_firing'] == {'warning:reply_pending_age': 1}
    assert c4['decomposition']['runs'] == 50 and c4['decomposition']['runs_without_tool_timing'] == 5
    assert c4['decomposition']['tool_ms']['p95'] == 800.0 and c4['decomposition']['model_ms']['p50'] == 3000
    assert c4['host'] == {'available': True, 'active_runs_max': 4, 'waiting_max': 1, 'admission_timeouts': 0}
    c8 = next(c for c in data['cells'] if c['cell'] == 'C8')
    assert c8['role'] == 'observe' and c8['beyond_deployment_premise'] is True and 'gate' not in c8
    md = (tmp_path / 'out/report.md').read_text()
    for text in (json.dumps(data, ensure_ascii=False), md):
        assert SENTINEL not in text and '20261011T220000Z-abc1234' not in text  # D8: no text, no run IDs
    assert 'AT-060：passed' in md and '超出部署前提' in md and 'ci-20261011-77' in md


def test_gate_failure_is_recorded_not_relaxed(run_dir):
    all_cells(run_dir, **{'C4': {'ack': 900.0, 'guard': (100, 3, 1700.0), 'connections': 61, 'critical': 1, 'refusals': ('NXC05', 'NXB01'), 'dead_end': 2},
                          'C4-low': {'dispatch': 2500.0, 'drained': False, 'drain_seconds': None, 'unresolved': 1}})
    data = report.build(run_dir)
    gates = {c['cell']: c['gate'] for c in data['cells'] if c['role'] == 'gate'}
    c4 = gates['C4']['items']
    assert gates['C4']['result'] == 'failed' and c4['ack_p95_ms']['value'] == 900.0 and c4['ack_p95_ms']['result'] == 'fail'
    assert c4['guard_timeout_rate_5m_max']['value'] == 0.012 and c4['guard_timeout_rate_5m_max']['result'] == 'fail'  # 2 bad minutes / 5-minute window
    assert c4['guard_p95_ms_max']['result'] == 'fail' and c4['connections_peak']['result'] == 'fail' and c4['critical_alerts']['result'] == 'fail'
    assert c4['unexpected_refusals']['value'] == 1 and c4['dead_letters_delta']['value'] == 2
    low = gates['C4-low']['items']
    assert low['idle_dispatch_p95_ms']['result'] == 'fail' and low['drain_seconds']['result'] == 'fail' and low['unresolved_work']['result'] == 'fail'
    assert data['thresholds'] == report.THRESHOLDS and data['at060']['result'] == 'failed'
    assert any(r.startswith('C4:gate_') and 'ack_p95_ms' in r for r in data['at060']['reasons'])
    # Observation cells are never judged, however bad.
    assert all('gate' not in c for c in data['cells'] if c['role'] == 'observe')


def test_missing_measurement_on_a_gate_fails_and_missing_cell_blocks(run_dir):
    all_cells(run_dir, **{'C4': {'skip': ('acks', 'samples')}})
    data = report.build(run_dir)
    c4 = next(c for c in data['cells'] if c['cell'] == 'C4')['gate']
    assert c4['result'] == 'failed' and c4['items']['ack_p95_ms']['result'] == 'missing' and c4['items']['guard_p95_ms_max']['result'] == 'missing'
    import shutil
    shutil.rmtree(run_dir / 'cells' / 'C4-peak')
    assert report.build(run_dir)['at060'] == {'result': 'blocked', 'reasons': ['cells_not_run:C4-peak']}


def test_identity_violation_in_any_cell_fails_at060_and_aborted_cell_fails(run_dir):
    all_cells(run_dir, **{'C8': {'identity_violation': 1}, 'C4-quiet': {'completed': False, 'skip': ('runs',)}})
    data = report.build(run_dir)
    assert data['at060']['result'] == 'failed'
    assert 'C8:identity_violations' in data['at060']['reasons'] and 'C4-quiet:not_completed:timeout' in data['at060']['reasons']
    assert 'C4-quiet:no_decomposition' in data['at060']['reasons']


def test_an_empty_model_context_sample_fails_a_gate_cell_and_only_records_in_an_observe_cell(run_dir):
    """AT-060 §4.4 item 4 (dispatcher 2026-10-13): a gate cell whose model request context check compared 0 IDs fails as an
    empty sample; an observe cell only records the number; older results without the count are missing evidence on a gate."""
    all_cells(run_dir, **{'C4-low': {'ids_compared': 0}, 'C2': {'ids_compared': 0}})
    data = report.build(run_dir)
    cells = {c['cell']: c for c in data['cells']}
    item = cells['C4-low']['gate']['items']['model_context_sample']
    assert item == {'value': 0, 'limit': 1, 'result': 'fail', 'reason': 'empty_sample'} and cells['C4-low']['gate']['result'] == 'failed'
    assert cells['C4']['gate']['items']['model_context_sample']['result'] == 'pass' and cells['C4']['gate']['result'] == 'passed'
    assert cells['C2']['metrics']['model_context_ids_compared'] == 0  # recorded only
    assert data['at060'] == {'result': 'failed', 'reasons': ['C4-low:gate_model_context_sample:empty_sample']}  # nothing about C2
    import shutil
    for name in report.CELLS:
        shutil.rmtree(run_dir / 'cells' / name)
    all_cells(run_dir, **{'C2': {'ids_compared': 0}})
    assert report.build(run_dir)['at060'] == {'result': 'passed', 'reasons': []}  # an empty observe cell does not change the verdict
    for name in report.CELLS:
        shutil.rmtree(run_dir / 'cells' / name)
    all_cells(run_dir, **{'C4': {'ids_compared': None}})
    c4 = next(c for c in report.build(run_dir)['cells'] if c['cell'] == 'C4')['gate']
    assert c4['items']['model_context_sample'] == {'value': None, 'limit': 1, 'result': 'missing'} and c4['result'] == 'failed'


def test_guard_operations_table_per_cell_leaves_guard_p95_unchanged(run_dir):
    """AT-060: guard_op samples become a per-operation table (informational); guard_p95_ms_max still reads only kind guard."""
    all_cells(run_dir)
    before = next(c for c in report.build(run_dir)['cells'] if c['cell'] == 'C4')['metrics']['guard_p95_ms_max']
    path = run_dir / 'cells' / 'C4' / 'process_samples.jsonl'
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    buckets = lambda **k: {'le_50': 0, 'le_100': 0, 'le_250': 0, 'le_500': 0, 'le_1000': 0, 'le_1500': 0, 'le_2000': 0, 'gt_2000': 0, **k}
    for minute, (p95, slow) in enumerate(((1200.0, 1), (5000.0, 2))):
        at = iso(START + timedelta(minutes=minute))
        rows.append({'kind': 'guard_op', 'service': 'model', 'instance': 'i', 'window_start': at, 'window_seconds': 60,
                     'sample': {'calls': 20, 'timeouts': slow - 1, 'errors': 0, 'p50_ms': 300.0, 'p95_ms': p95, 'max_ms': p95 + 1,
                                **buckets(le_500=18, le_1500=2 - (slow - 1), gt_2000=slow - 1)}})
        rows.append({'kind': 'guard_op', 'service': 'inspect', 'instance': 'i', 'window_start': at, 'window_seconds': 60,
                     'sample': {'calls': 10, 'timeouts': 0, 'errors': 0, 'p50_ms': 20.0, 'p95_ms': 40.0, 'max_ms': 45.0, **buckets(le_50=10)}})
    rows.append({'kind': 'guard_op', 'service': 'model', 'instance': 'i', 'window_start': iso(START + timedelta(minutes=30)), 'window_seconds': 60,
                 'sample': {'calls': 99, 'p95_ms': 9999.0}})  # outside the steady window: ignored
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    data = report.build(run_dir)
    c4 = next(c for c in data['cells'] if c['cell'] == 'C4')
    assert c4['metrics']['guard_p95_ms_max'] == before
    assert c4['guard_operations'] == {
        'inspect': {'calls': 20, 'timeouts': 0, 'errors': 0, 'p50_ms_max': 20.0, 'p95_ms_max': 40.0, 'max_ms': 45.0, 'p95_le_ms': 50},
        'model': {'calls': 40, 'timeouts': 1, 'errors': 0, 'p50_ms_max': 300.0, 'p95_ms_max': 5000.0, 'max_ms': 5001.0, 'p95_le_ms': 1500}}
    assert next(c for c in data['cells'] if c['cell'] == 'C2')['guard_operations'] == {}
    text = report.markdown(data)
    assert '| C4 | model | 40 | 1 | 0 | 300 | 5000 | 1500 | 5001 |' in text and '| C2 | missing |' in text


def test_ci_interference_with_baseline(run_dir, tmp_path):
    all_cells(run_dir, **{'C4-ci': {'guard': (100, 0, 1200.0), 'ack': 300.0}})
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(json.dumps({'run_id': 'ci-20261011-76', 'wall_seconds': 4300, 'deadline_gate_failures': 0, 'failed': 0}))
    ci = report.build(run_dir, ci_baseline=baseline)['ci_interference']
    assert ci['load_side']['guard_p95_ms_max'] == {'C4': 900.0, 'C4-ci': 1200.0} and ci['load_side']['ack_p95_ms'] == {'C4': 120.0, 'C4-ci': 300.0}
    assert ci['ci_side'] == {'under_load': {'run_id': 'ci-20261011-77', 'wall_seconds': 5100, 'deadline_gate_failures': 1, 'failed': 1},
                             'baseline': {'run_id': 'ci-20261011-76', 'wall_seconds': 4300, 'deadline_gate_failures': 0, 'failed': 0}}


def test_contract_errors_exit_2_without_writing(run_dir, tmp_path):
    all_cells(run_dir)
    cell = run_dir / 'cells/C8/cell.json'
    cell.write_text(cell.read_text().replace('"observe"', '"gate"'))
    out = tmp_path / 'out'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/load/report.py'), str(run_dir), '--out', str(out)], capture_output=True, text=True)
    assert result.returncode == 2 and json.loads(result.stdout)['written'] is False and not out.exists()
    cell.write_text(cell.read_text().replace('"gate"', '"observe"'))
    # A free-text refusal code (or any text in a code field) is refused, never copied into the report.
    write_lines(run_dir / 'cells/C4/refusals.jsonl', [{'code': SENTINEL, 'at': iso(START)}])
    with pytest.raises(report.ContractError):
        report.build(run_dir)


def test_runs_without_end_to_end_time_are_counted_not_rejected():
    """Found on the s5a C4-low run: a restricted (or in-progress) message Run has no reply yet, so e2e_ms is null; the report counts
    it by outcome and still decomposes its Run duration (dispatcher 2026-10-11: platform = Run duration - model - tool)."""
    import importlib.util
    spec=importlib.util.spec_from_file_location('load_report_null',Path(__file__).resolve().parents[1]/'scripts/load/report.py')
    R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)
    runs=[{'e2e_ms':5000,'run_ms':4200,'model_ms':3000,'tool_ms':1000,'tool_calls':3,'queue_wait_ms':100,'admission_wait_ms':None,'outcome':'replied'},
          {'e2e_ms':None,'run_ms':3100,'model_ms':2000,'tool_ms':900,'tool_calls':2,'queue_wait_ms':80,'admission_wait_ms':None,'outcome':'restricted'}]
    d=R.decomposition(runs)
    assert d['runs']==2 and d['outcomes']=={'replied':1,'restricted':1} and d['e2e_ms']['p50']==5000 and d['anomalies']==0
    assert d['platform_ms']['p50']==200


def test_negative_platform_time_is_an_anomaly_not_zero():
    import importlib.util
    spec=importlib.util.spec_from_file_location('load_report_anomaly',Path(__file__).resolve().parents[1]/'scripts/load/report.py')
    R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)
    runs=[{'e2e_ms':5000,'run_ms':3000,'model_ms':2500,'tool_ms':1000,'tool_calls':3,'queue_wait_ms':1,'admission_wait_ms':None,'outcome':'replied'},
          {'e2e_ms':5000,'run_ms':4000,'model_ms':2500,'tool_ms':1000,'tool_calls':3,'queue_wait_ms':1,'admission_wait_ms':None,'outcome':'replied'},
          {'e2e_ms':5000,'run_ms':4000,'model_ms':None,'tool_ms':1000,'tool_calls':3,'queue_wait_ms':1,'admission_wait_ms':None,'outcome':'replied'}]
    d=R.decomposition(runs)
    assert d['anomalies']==1 and d['platform_ms']['p50']==500 and d['runs_without_model_timing']==1  # never clipped to 0
    cells=[{'cell':name,'completed':True,'aborted':None,'decomposition':d,'metrics':{'identity_violations':0},'role':'observe'} for name in R.CELLS]
    result=R.at060(cells)
    assert result['result']=='failed' and 'C4-low:decomposition_anomaly:1' in result['reasons']


def test_failed_runs_are_counted_by_cause():
    """NX-031 (dispatcher 2026-10-11, ruling 1): an unanswered guard and a denial are counted apart; a Run that did not fail has none."""
    import importlib.util
    spec=importlib.util.spec_from_file_location('load_report_failures',Path(__file__).resolve().parents[1]/'scripts/load/report.py')
    R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)
    base={'e2e_ms':None,'run_ms':3000,'model_ms':2000,'tool_ms':500,'tool_calls':1,'queue_wait_ms':1,'admission_wait_ms':None,'outcome':'in_progress'}
    runs=[{**base,'failure':'runtime_authorization_unavailable'},{**base,'failure':'runtime_authorization_unavailable'},
          {**base,'failure':'runtime_authorization_denied'},{**base,'failure':'runtime_failed'},{**base,'failure':None},{**base}]
    d=R.decomposition(runs)
    assert d['failures']=={'runtime_authorization_denied':1,'runtime_authorization_unavailable':2,'runtime_failed':1}
    with pytest.raises(R.ContractError):R.decomposition([{**base,'failure':'not a code!'}])

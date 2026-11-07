"""NX-064 measurement gate (scripts/acceptance/nx064_p99_gate.py; design §4.3, ADR-029c §1.2): the Host-side p99 from the 0405
histogram against the deployed effect limit. Enough samples (pass and fail), too few, an empty or unreadable histogram (insufficient,
never pass), bucket boundaries (rank and the gate's upper-bound rule), both sources, refused input. Pure: files only."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

from nexloop_eios.observability import HISTOGRAM_LAST, histogram_bucket

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nx064_p99_gate', ROOT / 'scripts/acceptance/nx064_p99_gate.py')
G = importlib.util.module_from_spec(spec);spec.loader.exec_module(G)


def samples_file(tmp_path, *minutes, service='effect_submit', kind='host_op'):
    """Per-minute deltas as process_samples.jsonl rows: each minute a {bucket: count} dict."""
    path = tmp_path / 'process_samples.jsonl'
    rows = [{'kind': kind, 'service': service, 'window_start': f'2026-11-07T10:{n:02d}:00+00:00',
             'sample': {'calls': sum(m.values()), **{f'h_{i}': c for i, c in m.items()}}} for n, m in enumerate(minutes)]
    rows.append({'kind': 'host', 'service': 'host', 'window_start': '2026-11-07T10:00:00+00:00', 'sample': {'effect_calls': 5, 'h_3': 99}})
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return path


def run(*args):
    out = io.StringIO()
    code = G.main([str(a) for a in args], out=out)
    return code, json.loads(out.getvalue())


def test_enough_samples_fast_enough_passes(tmp_path):
    fast = histogram_bucket(300)  # ~300 ms, as the gate load measured
    code, out = run('--samples', samples_file(tmp_path, {fast: 600}, {fast: 395, fast + 3: 5}), '--limit-ms', 2000)
    assert code == 0 and out['gate'] == 'pass' and out['samples'] == 1000 and out['reason'] is None
    assert out['p99_upper_ms'] * 1.5 <= 2000 and out['suggested_effect_timeout_ms'] == 2000


def test_enough_samples_too_slow_fails(tmp_path):
    slow = histogram_bucket(1500)
    code, out = run('--samples', samples_file(tmp_path, {histogram_bucket(300): 980, slow: 20}), '--limit-ms', 2000)
    assert code == 1 and out['gate'] == 'fail' and out['reason'] == 'p99_x_1.5_over_the_limit'
    assert out['suggested_effect_timeout_ms'] == 2400  # bucket 77, upper 1539.0 ms x 1.5 = 2308.5; ADR-029c: clamp(ceil(p99 x 1.5 / 100) x 100, 2000, 5000)


def test_too_few_samples_are_insufficient_even_when_fast(tmp_path):
    code, out = run('--samples', samples_file(tmp_path, {histogram_bucket(50): 999}), '--limit-ms', 2000)
    assert code == 2 and out['gate'] == 'insufficient' and out['reason'] == 'fewer_than_1000_requests' and out['samples'] == 999
    assert out['p99_ms'] is not None and out['suggested_effect_timeout_ms'] is None  # shown, never a value to configure


@pytest.mark.parametrize('label,write', [
    ('no rows of the operation', lambda p: p.write_text(json.dumps({'kind': 'host_op', 'service': 'effect_find', 'window_start': '2026-11-07T10:00:00+00:00',
                                                                         'sample': {'h_40': 5000}}) + '\n')),
    ('rows without a histogram', lambda p: p.write_text(json.dumps({'kind': 'host_op', 'service': 'effect_submit', 'window_start': '2026-11-07T10:00:00+00:00',
                                                                         'sample': {'calls': 5000}}) + '\n')),
    ('an empty file', lambda p: p.write_text('')),
])
def test_an_empty_histogram_is_insufficient_not_a_pass(tmp_path, label, write):
    path = tmp_path / 'samples.jsonl';write(path)
    code, out = run('--samples', path, '--limit-ms', 2000)
    assert code == 2 and out['gate'] == 'insufficient' and out['reason'] == 'empty_histogram' and out['samples'] == 0, label


def test_an_unreadable_source_is_insufficient_not_a_pass(tmp_path):
    code, out = run('--host-metrics', tmp_path / 'missing.json', '--limit-ms', 2000)
    assert code == 2 and out['gate'] == 'insufficient' and out['reason'] == 'source_unreadable'


def test_bucket_boundaries():
    # A value equal to a bucket's upper bound is in that bucket (observability.histogram_bucket), as the gate's upper bound assumes.
    for i in (1, 10, 60, 75, 76, 115):
        assert histogram_bucket(1.1 ** i) == i and histogram_bucket(1.1 ** i * 1.0001) == i + 1
    assert histogram_bucket(1) == 0 and histogram_bucket(10 ** 9) == HISTOGRAM_LAST
    # Rank ceil(0.99 n): 990 of 1000 in the lower bucket put p99 there; 989 move it up.
    assert G.quantile_bucket({50: 990, 60: 10}, 0.99) == 50 and G.quantile_bucket({50: 989, 60: 11}, 0.99) == 60
    # The gate's edge at 2000 ms: upper x 1.5 <= 2000 means upper <= 1333.3 - bucket 75 (1271.9) passes, bucket 76 (1399.1) fails,
    # although bucket 76's midpoint (1334.0) x 1.5 is barely over: the upper bound decides, never the 4.9 % estimate.
    edge = lambda i: G.judge({i: 1000}, 2000, operation='effect_submit', source='samples', kind='host_op')  # noqa: E731
    assert (edge(75)['gate'], edge(76)['gate']) == ('pass', 'fail')
    assert edge(0)['gate'] == 'pass' and edge(0)['p99_upper_ms'] == 1.0
    last = edge(HISTOGRAM_LAST)
    assert last['gate'] == 'fail' and last['reason'] == 'p99_in_the_open_last_bucket' and last['p99_upper_ms'] is None


def test_host_metrics_document_and_the_guard_estimate(tmp_path):
    doc = tmp_path / 'metrics.json'
    doc.write_text(json.dumps({'effect_calls': 1200, 'effect_submit_calls': 1200, f'effect_submit_h_{histogram_bucket(200)}': 1200,
                               f'effect_find_h_{histogram_bucket(3000)}': 5000}))
    code, out = run('--host-metrics', doc, '--limit-ms', 2000)
    assert code == 0 and out['source'] == 'host_metrics' and out['samples'] == 1200 and out['gate'] == 'pass'
    code, out = run('--host-metrics', doc, '--limit-ms', 2000, '--operation', 'effect_find')
    assert code == 1 and out['gate'] == 'fail'
    code, out = run('--samples', samples_file(tmp_path, {histogram_bucket(900): 1000}, kind='guard_op'), '--limit-ms', 2000, '--kind', 'guard_op')
    assert 'guard p99 + 300 ms' in out['estimate'] and out['p99_upper_ms'] > 1200 and out['gate'] == 'pass'


def test_the_window_filter(tmp_path):
    path = samples_file(tmp_path, {histogram_bucket(300): 600}, {histogram_bucket(300): 600})
    code, out = run('--samples', path, '--limit-ms', 2000, '--since', '2026-11-07T10:01:00+00:00')
    assert code == 2 and out['samples'] == 600


@pytest.mark.parametrize('label,row', [
    ('negative count', {'kind': 'host_op', 'service': 'effect_submit', 'window_start': '2026-11-07T10:00:00+00:00', 'sample': {'h_3': -1}}),
    ('fractional count', {'kind': 'host_op', 'service': 'effect_submit', 'window_start': '2026-11-07T10:00:00+00:00', 'sample': {'h_3': 1.5}}),
    ('bucket out of range', {'kind': 'host_op', 'service': 'effect_submit', 'window_start': '2026-11-07T10:00:00+00:00', 'sample': {'h_117': 1}}),
])
def test_refused_input_is_neither_pass_nor_insufficient(tmp_path, label, row):
    path = tmp_path / 'samples.jsonl';path.write_text(json.dumps(row) + '\n')
    code, out = run('--samples', path, '--limit-ms', 2000)
    assert code == 3 and out['gate'] == 'refused', label
    code, _ = run('--samples', path, '--limit-ms', 6000)
    assert code == 3


@pytest.mark.parametrize('flag', ['--since', '--until'])
def test_a_window_bound_without_a_time_zone_is_refused_not_a_failed_gate(tmp_path, flag):
    path = samples_file(tmp_path, {histogram_bucket(300): 1200})
    code, out = run('--samples', path, '--limit-ms', 2000, flag, '2026-11-07T10:01:00')
    assert code == 3 and out['gate'] == 'refused' and 'time zone' in out['reason']


def test_an_unexpected_error_is_refused_input_never_exit_1(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise TypeError('synthetic')
    monkeypatch.setattr(G, 'from_samples', broken)
    code, out = run('--samples', samples_file(tmp_path, {histogram_bucket(300): 1200}), '--limit-ms', 2000)
    assert code == 3 and out == {'gate': 'refused', 'reason': 'unexpected TypeError'}

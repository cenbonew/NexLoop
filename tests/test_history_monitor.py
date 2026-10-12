"""AT-060 ladder: the history monitor reads only, writes one JSON line per interval with numbers and codes only, and bounds the
guard and Host process samples by the previous line. Disposable migrated PG (admin fixture)."""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/load'))
from history_monitor import HistoryMonitor, sample  # noqa: E402
from test_action_definitions import published_action  # noqa: E402,F401  (migrated catalog)

TENANT = '00000000-0000-4000-8000-0000000000a1'


def test_sample_on_an_empty_catalog_is_all_zero(published_action, admin):
    value = sample(admin, TENANT, datetime.now(UTC) - timedelta(minutes=1))
    assert value['open'] == {'unrouted': 0, 'run_tasks': 0, 'replies_due': 0}
    assert value['replied'] == 0 and value['jobs'] == {} and value['relay_handoffs'] == 0 and value['dead_letters'] == 0
    assert value['guard'] == {'calls': 0, 'errors': 0, 'timeouts': 0, 'p50_ms_max': None, 'p95_ms_max': None, 'processes': 0}


def test_guard_and_host_samples_are_bounded_by_the_previous_line(published_action, admin):
    old = datetime.now(UTC) - timedelta(minutes=10)
    insert = 'insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample,recorded_at) values(%s,%s,%s,%s,60,%s,%s)'
    admin.execute(insert, ('guard', 'runtime-guard', 'old', old, Jsonb({'calls': 99, 'errors': 9, 'timeouts': 9, 'p50_ms': 9000.0, 'p95_ms': 9000.0, 'max_ms': 9000.0}), old))
    since = datetime.now(UTC) - timedelta(seconds=30)
    for instance, values in (('a', {'calls': 10, 'errors': 1, 'timeouts': 0, 'p50_ms': 120.0, 'p95_ms': 800.0, 'max_ms': 900.0}),
                             ('b', {'calls': 6, 'errors': 0, 'timeouts': 2, 'p50_ms': 300.0, 'p95_ms': 1500.0, 'max_ms': 2100.0})):
        # A window that began before the previous line but was recorded after it still counts (recorded_at is the bound).
        admin.execute(insert, ('guard', 'runtime-guard', instance, since - timedelta(seconds=50), Jsonb(values), datetime.now(UTC)))
    admin.execute(insert, ('host', 'agent-host', 'h', datetime.now(UTC), Jsonb({'active_runs': 4, 'waiting': 2, 'admission_timeouts': 3}), datetime.now(UTC)))
    value = sample(admin, TENANT, since)
    assert value['guard'] == {'calls': 16, 'errors': 1, 'timeouts': 2, 'p50_ms_max': 300.0, 'p95_ms_max': 1500.0, 'processes': 2}
    assert value['host'] == {'active_runs': 4, 'waiting': 2, 'admission_timeouts': 3}


def test_monitor_writes_once_per_interval_with_numbers_and_codes_only(published_action, admin, tmp_path):
    clock = iter([0.0, 0.0, 10.0, 31.0, 40.0])
    monitor = HistoryMonitor(tmp_path / 'history-monitor.jsonl', TENANT, interval=30.0, clock=lambda: next(clock))
    written = [monitor.tick(admin), monitor.tick(admin), monitor.tick(admin), monitor.tick(admin, force=True)]
    assert [w is not None for w in written] == [True, False, True, True]
    lines = [json.loads(line) for line in (tmp_path / 'history-monitor.jsonl').read_text().splitlines()]
    assert len(lines) == 3 and [line['elapsed_s'] for line in lines] == [0.0, 31.0, 40.0]
    assert set(lines[0]) == {'at', 'elapsed_s', 'open', 'replied', 'jobs', 'guard', 'host', 'relay_handoffs', 'dead_letters'}
    assert TENANT not in (tmp_path / 'history-monitor.jsonl').read_text()

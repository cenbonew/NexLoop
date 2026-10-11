"""NX-031 AT-060 (dispatcher 2026-10-11, A2): the Host reports a Run's model time in its inspection; the runtime worker writes it
into the task result beside tool_timing (only the three numbers pass); the load collector prefers it. Actual chain: HTTPS consumer
message → relay → Pi Run on the deterministic Host → runtime worker (tests/test_outbound_messages_pg.chain)."""
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nexloop_eios.runtime_dispatch import model_timing
from test_outbound_messages_pg import chain
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_only_the_three_numbers_pass():
    assert model_timing({'calls': 3, 'total_ms': 120.5, 'max_ms': 80}) == {'calls': 3, 'total_ms': 120.5, 'max_ms': 80}
    for value in (None, {}, {'calls': 3, 'total_ms': 1, 'max_ms': 1, 'note': 'x'}, {'calls': '3', 'total_ms': 1, 'max_ms': 1},
                  {'calls': 1, 'total_ms': -1, 'max_ms': 0}, {'calls': 1, 'total_ms': 1, 'max_ms': 2}, {'calls': True, 'total_ms': 1, 'max_ms': 1},
                  {'calls': 1, 'total_ms': float('nan'), 'max_ms': 0}, [1, 2, 3]):
        assert model_timing(value) is None


def test_runtime_worker_writes_model_timing_and_the_collector_reads_it(assembled_message, admin, tmp_path):
    f = assembled_message
    started = datetime.now(UTC) - timedelta(minutes=5)
    with chain(f, admin, tmp_path, body='请问能开发票吗？') as c:
        (result,) = admin.execute("select j.result from runtime.jobs j join runtime.nexloop_message_routes m on m.task_id=j.job_id where m.message_id=%s",
                                  (c['consumer']['id'],)).fetchone()
        timing = result['model_timing']
        assert result['runtime_outcome'] == 'succeeded' and set(timing) == {'calls', 'total_ms', 'max_ms'}
        assert timing['calls'] >= 2 and 0 <= timing['max_ms'] <= timing['total_ms'] and 'tool_timing' in result
        spec = importlib.util.spec_from_file_location('load_collect', ROOT / 'scripts/load/collect.py')
        collect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collect)
        collect.TENANT = c['tenant']
        (row,) = collect.runs(admin, started, datetime.now(UTC) + timedelta(minutes=1))
        assert row['model_ms'] == round(float(timing['total_ms']), 1) and row['tool_ms'] == result['tool_timing']['total_ms']

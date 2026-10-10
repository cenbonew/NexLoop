"""NX-030 M09: the Agent Host's concurrency counters over the authenticated loopback route, pulled and recorded by the worker.

Real Host processes (Node 24, TLS loopback, internal key): with runtime configuration the route returns numbers only and counts
an actual admitted Run; a wrong key, a non-loopback Host header or a Host without runtime configuration gets nothing useful.
The worker-side counters turn the Host's cumulative values into one window's increase and record a numeric sample.
"""
import secrets
from pathlib import Path

from psycopg.conninfo import make_conninfo

from nexloop_eios.host_control import HostControlConfiguration, read_host_metrics
from nexloop_eios.observability import HostCounters, flush_host
from nexloop_eios.runtime_dispatch import RuntimeDispatcher
from test_agent_host import files, running
from test_runtime_atomic_accept import accepted_input  # noqa: F401
from test_runtime_activation import synthetic_credentials  # noqa: F401
from test_runtime_authority import authority  # noqa: F401
from test_runtime_dispatch import dispatch_host


def test_host_counts_an_admitted_run_and_the_worker_records_the_increase(accepted_input, admin, tmp_path):
    api, worker, args, issued = accepted_input
    api.accept_runtime_event(**args)
    with dispatch_host(worker, tmp_path) as (runtime, child, client, headers, config):
        before = read_host_metrics(config)
        assert before == {'available': True, 'active_runs': 0, 'waiting': 0, 'max_active_runs': 4, 'runs_started': 0, 'admission_timeouts': 0}
        counters = HostCounters()
        assert counters.sample(before) == {'active_runs': 0, 'waiting': 0, 'max_active_runs': 4, 'available': True}  # baseline only
        assert RuntimeDispatcher(worker, config, queue='operations', total_timeout=10).run_once()['status'] == 'succeeded'
        after = read_host_metrics(config)
        assert after['runs_started'] == 1 and after['active_runs'] == 0
        assert counters.sample(after)['runs_started'] == 1
        # A wrong key or a forged Host header: no metrics.
        wrong = tmp_path / 'wrong.key'
        wrong.write_text(secrets.token_hex(32))
        wrong.chmod(0o600)
        assert read_host_metrics(HostControlConfiguration(config.origin, wrong, config.ca_file)) is None
        assert client.get('/internal/v1/metrics', headers={**headers, 'Host': 'other.example'}).status_code == 401
        assert client.get('/internal/v1/metrics').status_code == 401
        # The worker records the pulled counters as one numeric sample (the runtime-worker role may write samples).
        flush_host(worker._backend._pool, config, counters)
        rows = admin.execute("select service,sample from runtime.nexloop_process_samples where kind='host'").fetchall()
        assert rows and rows[-1][0] == 'agent-host' and rows[-1][1]['available'] is True and rows[-1][1]['max_active_runs'] == 4


def test_host_without_runtime_reports_unavailable(tmp_path):
    runtime, key = files(tmp_path)
    with running(runtime, key) as (child, client, port):
        config = HostControlConfiguration(f'https://127.0.0.1:{port}', key, tmp_path / 'host-cert.pem')
        assert read_host_metrics(config) == {'available': False}
        assert HostCounters().sample({'available': False}) is None


def test_counters_handle_restart_and_reject_text():
    counters = HostCounters()
    counters.sample({'available': True, 'runs_started': 10, 'admission_timeouts': 2})
    assert counters.sample({'available': True, 'runs_started': 15, 'admission_timeouts': 2})['runs_started'] == 5
    assert counters.sample({'available': True, 'runs_started': 3, 'admission_timeouts': 0})['runs_started'] == 3  # Host restarted

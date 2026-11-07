"""NX-055 E3 check against real PostgreSQL (NX-030 fixture, migration 0318): as the read-only nexloop_metrics login it reads the
real host recorder roles and the host_health block written by the two recorders. Before any tick it says no data (exit 3); with
every watched timer run on schedule by its own host's recorder it passes (exit 0); a stale timer fails (exit 1); any other login is
refused (exit 2). Output: counts and statuses only."""
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.conninfo import make_conninfo

from test_host_health_pg import run, tick
from test_observability_pg import observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, private, published_action, uow, workbench  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import nx055_e3_check as E  # noqa: E402

RULES = ROOT / 'deploy/configuration/alert-rules.v30.json'


def check(dsn):
    out = io.StringIO()
    with redirect_stdout(out):
        code = E.main(['--metrics-database-url-file', str(dsn), '--alert-rules', str(RULES), '--json'])
    return code, json.loads(out.getvalue())


def test_no_data_then_pass_then_stale_and_only_the_metrics_login(observed, tmp_path):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    dsn = private(tmp_path, 'metrics-dsn', make_conninfo(pg, user='nexloop_metrics'))
    code, before = check(dsn)
    assert code == 3 and before['status'] == 'no_data' and before['recorders']['roles_restricted'] == 2  # the real roles, no tick yet
    from nexloop_eios.observability import load_rules
    watched = E.watched_timers(load_rules(RULES))
    now = datetime.now(UTC) - timedelta(minutes=5)
    for host in ('app_host', 'data_host'):  # each host through its own recorder login
        timers = [t for h, t in watched if h == host]
        tick(pg, host=host, expected_timers=timers, timers=[run(now, name=t) for t in timers])
    code, result = check(dsn)
    assert code == 0 and result['status'] == 'pass', result
    assert result['freshness']['fresh'] == len(watched) and result['recorders']['hosts_recording'] == 2
    text = json.dumps(result)
    assert 'app_host' not in text and 'data_host' not in text and 'pg-diff' not in text
    # The journal timer (10 minutes) ages past its threshold: freshness fails.
    admin.execute("update runtime.nexloop_host_timer_runs set run_started_at=run_started_at-interval '1 hour',run_finished_at=run_finished_at-interval '1 hour' where timer_name='journal'")
    code, stale = check(dsn)
    assert code == 1 and stale['freshness']['stale'] == 1 and stale['recorders']['status'] == 'pass'
    other = private(tmp_path, 'api-dsn', make_conninfo(pg, user='nexloop_api'))
    code, refused = check(other)
    assert code == 2 and refused == {'check': 'nx055-e3', 'status': 'refused', 'error': 'PermissionError'}

"""EIOS gap E2 + E3 (0318, alert rules v12): the host probe's ticks recorded as nexloop_host_recorder, the host_health metrics and
the alerts they raise and clear - crash loop, timer freshness (a manual rerun never refreshes it; fail closed when no scheduled
success was ever seen), scheduled failures, probe staleness and blindness. Real PostgreSQL through the NX-030 fixture (the real
evaluator credential and configurator); admin only moves recorded times back to stand for elapsed time."""
import io
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.observability import apply_rules, load_rules, validate_rules
from test_host_health_probe import DATA_HOST_RUN, DATA_HOST_TIMER, JOURNAL, TRIGGER, P, config, runner
from test_observability_pg import RULES, TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

V11, V12 = RULES.parent / 'alert-rules.v11.json', RULES.parent / 'alert-rules.v12.json'
V13 = RULES.parent / 'alert-rules.v13.json'
ROOT = RULES.parents[2]
NEW = {'crash_loop', 'host_probe_stale', 'host_probe_blind', 'host_unit_missing', 'timer_scheduled_failure'}


def test_rules_v12_are_v11_plus_the_host_health_rules():
    v11, v12 = json.loads(V11.read_text()), load_rules(V12)
    assert v12['version'] == 12 and v12['rules'][:len(v11['rules'])] == v11['rules']
    added = v12['rules'][len(v11['rules']):]
    assert {r['rule_id'] for r in added} >= NEW and all(r['metric'].startswith('host_health.') for r in added)
    stale = {r['selector']: r['threshold'] for r in added if r['rule_id'].startswith('timer_stale_')}
    assert stale['data_host:pg-diff'] > 48 * 3600 and stale['data_host:pg-full'] > 7 * 86400 and stale['app_host:provider-drift'] > 3600
    validate_rules(v12)


def tick(pg, host='data_host', units=(), timers=(), expected_units=None, expected_timers=('pg-diff',), blind=False):
    entry = {'blind': blind, 'expected_units': list(expected_units if expected_units is not None else [u['name'] for u in units]),
             'expected_timers': list(expected_timers), 'units': list(units), 'timers': list(timers)}
    with psycopg.connect(make_conninfo(pg, user='nexloop_host_recorder_' + host), autocommit=True) as db:
        return db.execute('select authz.nexloop_record_host_observation(%s::jsonb)', (json.dumps(entry),)).fetchone()[0]


def unit(count, instance='c' * 12, name='api'):
    return {'name': name, 'kind': 'container', 'instance_id': instance, 'restart_count': count, 'running': True}


def run(started, *, scheduled=True, result='success', name='pg-diff', trigger=None):
    return {'name': name, 'run_started_at': started.isoformat(), 'run_finished_at': (started + timedelta(minutes=3)).isoformat(),
            'result': result, 'scheduled': scheduled, 'last_trigger_at': (trigger or started).isoformat() if scheduled or trigger else None}


def state(admin):
    return {(r, s): f for r, s, f in admin.execute("""select rule_id,selector,firing from control.nexloop_alert_state
        where rule_id=any(%s) or rule_id like 'timer_stale_%%'""", (list(NEW),)).fetchall()}


def metrics(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'host_health'", (TENANT,)).fetchone()[0]


def test_a_crash_loop_fires_and_clears(observed):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    assert apply_rules(load_rules(V12), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    now = datetime.now(UTC)
    for n in (0, 1):
        tick(pg, units=[unit(n)], timers=[run(now - timedelta(hours=1))])
    assert o['evaluate']()['rules_version'] == 12
    assert metrics(admin)['restarts_1h'] == {'data_host:api': 1} and not state(admin).get(('crash_loop', 'data_host:api'))
    # The container keeps restarting while 'running': three more within the hour, then recreated (a new instance counts one).
    for n in (2, 3):
        tick(pg, units=[unit(n)])
    tick(pg, units=[unit(0, instance='d' * 12)])
    assert metrics(admin)['restarts_1h'] == {'data_host:api': 4}
    o['evaluate']()
    assert state(admin)[('crash_loop', 'data_host:api')] is True
    # An hour later with no new restart (the samples age out of the window; the last one is the baseline): it clears.
    admin.execute("update runtime.nexloop_host_unit_samples set sampled_at=sampled_at-interval '2 hours'")
    tick(pg, units=[unit(0, instance='d' * 12)])
    assert metrics(admin)['restarts_1h'] == {'data_host:api': 0}
    o['evaluate']()
    assert state(admin)[('crash_loop', 'data_host:api')] is False


def test_timer_freshness_counts_only_scheduled_successes_and_fails_closed(observed):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    assert apply_rules(load_rules(V12), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    now = datetime.now(UTC)
    # The last scheduled success is three days old; today's scheduled round failed; a manual rerun succeeded afterwards.
    tick(pg, timers=[run(now - timedelta(days=3))], expected_timers=('pg-diff', 'pg-full'))
    tick(pg, timers=[run(now - timedelta(hours=2), result='failure')], expected_timers=('pg-diff', 'pg-full'))
    assert tick(pg, timers=[run(now - timedelta(hours=1), scheduled=False)], expected_timers=('pg-diff', 'pg-full'))['new_runs'] == 1
    assert tick(pg, timers=[run(now - timedelta(hours=1), scheduled=False)], expected_timers=('pg-diff', 'pg-full'))['new_runs'] == 0  # append-only ledger
    m = metrics(admin)
    assert m['timer_success_age_seconds']['data_host:pg-diff'] > 3 * 86400 - 600
    assert m['timer_scheduled_failures_24h']['data_host:pg-diff'] == 1
    # pg-full never ran: its age counts from the first tick that expected it (fail closed), here moved back 8 days.
    admin.execute("update runtime.nexloop_host_probe_ticks set observed_at=observed_at-interval '8 days' where tick_id=(select min(tick_id) from runtime.nexloop_host_probe_ticks)")
    tick(pg, expected_timers=('pg-diff', 'pg-full'))
    o['evaluate']()
    s = state(admin)
    assert s[('timer_stale_pg_diff', 'data_host:pg-diff')] is True and s[('timer_stale_pg_full', 'data_host:pg-full')] is True
    assert s[('timer_scheduled_failure', 'data_host:pg-diff')] is True
    # The next scheduled round succeeds: pg-diff is fresh again (the failure stays counted for 24 hours).
    tick(pg, timers=[run(now - timedelta(minutes=5))], expected_timers=('pg-diff', 'pg-full'))
    o['evaluate']()
    s = state(admin)
    assert s[('timer_stale_pg_diff', 'data_host:pg-diff')] is False and s[('timer_stale_pg_full', 'data_host:pg-full')] is True
    assert s[('timer_scheduled_failure', 'data_host:pg-diff')] is True


def test_a_silent_or_blind_probe_alerts(observed):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    assert apply_rules(load_rules(V12), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    tick(pg, host='app_host', units=[unit(0)], expected_timers=())
    o['evaluate']()
    assert not state(admin).get(('host_probe_stale', 'app_host')) and not state(admin).get(('host_probe_blind', 'app_host'))
    tick(pg, host='app_host', units=[], expected_units=['api', 'worker'], expected_timers=(), blind=True)
    o['evaluate']()
    assert state(admin)[('host_probe_blind', 'app_host')] is True
    admin.execute("update runtime.nexloop_host_probe_ticks set observed_at=observed_at-interval '10 minutes'")
    o['evaluate']()
    assert state(admin)[('host_probe_stale', 'app_host')] is True
    tick(pg, host='app_host', units=[unit(0)], expected_timers=())
    o['evaluate']()
    s = state(admin)
    assert s[('host_probe_stale', 'app_host')] is False and s[('host_probe_blind', 'app_host')] is False


def test_a_manual_rerun_inside_the_grace_is_not_the_scheduled_round(observed):  # noqa: F811
    """L6 review C1: the scheduled round fails quickly; a manual rerun starts inside the probe's grace after the same trigger. SQL
    keeps only the first run of a trigger as scheduled: the rerun is recorded as manual, the failure stays, freshness is not refreshed."""
    o = observed
    admin, pg = o['admin'], o['pg']
    assert apply_rules(load_rules(V12), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    trigger = datetime.now(UTC) - timedelta(days=3)
    tick(pg, timers=[run(trigger, result='failure')])
    tick(pg, timers=[run(trigger + timedelta(seconds=3), trigger=trigger)])  # offered as scheduled (inside the grace)
    rows = admin.execute("select result,scheduled from runtime.nexloop_host_timer_runs order by run_started_at").fetchall()
    assert rows == [('failure', True), ('success', False)]
    # A run long after its trigger (the probe offered it as scheduled by mistake) is not scheduled either.
    tick(pg, timers=[run(trigger + timedelta(hours=1), trigger=trigger + timedelta(minutes=50))])
    assert admin.execute("select scheduled from runtime.nexloop_host_timer_runs order by run_started_at desc limit 1").fetchone() == (False,)
    # No scheduled success at all: the age counts from the first tick that expected the timer (moved back to the trigger).
    admin.execute("update runtime.nexloop_host_probe_ticks set observed_at=%s where tick_id=(select min(tick_id) from runtime.nexloop_host_probe_ticks)", (trigger,))
    assert metrics(admin)['timer_success_age_seconds']['data_host:pg-diff'] > 3 * 86400 - 600
    o['evaluate']()
    s = state(admin)
    assert s[('timer_stale_pg_diff', 'data_host:pg-diff')] is True


def test_a_host_writes_only_its_own_ticks(observed):  # noqa: F811
    """L6 review C2: the host is the recorder role's; an entry naming a host is refused; the app host cannot write data_host rows."""
    o = observed
    admin, pg = o['admin'], o['pg']
    assert tick(pg, host='app_host', timers=[run(datetime.now(UTC))], expected_timers=('pg-diff',))['host_role'] == 'app_host'
    assert admin.execute("select distinct host_role from runtime.nexloop_host_timer_runs").fetchall() == [('app_host',)]
    forged = {'host_role': 'data_host', 'blind': False, 'expected_units': [], 'expected_timers': ['pg-diff'], 'units': [], 'timers': [run(datetime.now(UTC))]}
    with pytest.raises(psycopg.errors.InvalidParameterValue), psycopg.connect(make_conninfo(pg, user='nexloop_host_recorder_app_host'), autocommit=True) as db:
        db.execute('select authz.nexloop_record_host_observation(%s::jsonb)', (json.dumps(forged),))
    assert admin.execute("select count(*) from runtime.nexloop_host_probe_ticks where host_role='data_host'").fetchone() == (0,)


def test_a_missing_unit_alerts(observed):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    assert apply_rules(load_rules(V12), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    tick(pg, units=[unit(0), unit(0, name='valkey')], expected_timers=())
    assert metrics(admin)['missing_units'] == {'data_host:api': 0, 'data_host:valkey': 0}
    tick(pg, units=[unit(0)], expected_units=['api', 'valkey'], expected_timers=())
    assert metrics(admin)['missing_units'] == {'data_host:api': 0, 'data_host:valkey': 1}
    o['evaluate']()
    admin.execute("update control.nexloop_alert_state set breached_since=breached_since-interval '5 minutes' where rule_id='host_unit_missing'")
    o['evaluate']()
    assert state(admin)[('host_unit_missing', 'data_host:valkey')] is True
    tick(pg, units=[unit(0), unit(0, name='valkey')], expected_timers=())
    o['evaluate']()
    assert state(admin)[('host_unit_missing', 'data_host:valkey')] is False


def test_the_recorder_executes_one_function_takes_no_text_and_nothing_else_records(observed):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    for table in ('runtime.nexloop_host_probe_ticks', 'runtime.nexloop_host_unit_samples', 'runtime.nexloop_host_timer_runs'):
        for recorder in ('nexloop_host_recorder_app_host', 'nexloop_host_recorder_data_host'):
            assert admin.execute("select has_table_privilege(%s,%s,'select')", (recorder, table)).fetchone() == (False,)
        for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_runtime'):
            assert admin.execute("select has_table_privilege(%s,%s,'insert')", (role, table)).fetchone() == (False,)
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_runtime'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_record_host_observation(jsonb)','execute')", (role,)).fetchone() == (False,)
    for bad in ({'units': [{**unit(0), 'note': 'free text'}]}, {'units': [{**unit(0), 'name': 'Has Space'}]}, {'host_role': 'data_host'},
                {'expected_timers': ['../etc']}, {'timers': [{**run(datetime.now(UTC)), 'result': 'ok'}]}):
        entry = {'blind': False, 'expected_units': [], 'expected_timers': [], 'units': [], 'timers': [], **bad}
        with pytest.raises(psycopg.Error), psycopg.connect(make_conninfo(pg, user='nexloop_host_recorder_data_host'), autocommit=True) as db:
            db.execute('select authz.nexloop_record_host_observation(%s::jsonb)', (json.dumps(entry),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(make_conninfo(pg, user='nexloop_domain_worker'), autocommit=True) as db:
        db.execute('select authz.nexloop_record_host_observation(%s::jsonb)', ('{}',))


def test_rules_v13_are_v12_plus_the_file_backup_timer_and_the_app_host_example_names_it(tmp_path):
    """NX-061 B9: the 15-minute file backup gets the scheduled-run freshness rule of v12's timers (a manual nexloop-file-backup run
    refreshes backup.file_backup_age_seconds, not this); the APP_HOST example configuration lists the timer under the rule's name."""
    import importlib.util
    v12, v13 = json.loads(V12.read_text()), load_rules(V13)
    assert v13['version'] == 13 and v13['rules'][:len(v12['rules'])] == v12['rules']
    added = v13['rules'][len(v12['rules']):]
    assert [(r['rule_id'], r['metric'], r['selector']) for r in added] == [
        ('timer_stale_file_backup', 'host_health.timer_success_age_seconds', 'app_host:file-backup')]
    assert 15 * 60 < added[0]['threshold'] <= 3600  # more than a period, within the hour of file_backup_age
    validate_rules(v13)
    spec = importlib.util.spec_from_file_location('host_health_probe', ROOT / 'scripts/deploy/host_health_probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    examples = {}
    for name in ('host-health.example.json', 'host-health.app-host.example.json'):
        text = (ROOT / 'deploy/examples' / name).read_text().replace('<HOST_RECORDER_DSN_FILE_0600>', str(tmp_path / 'dsn'))
        for placeholder in ('<API_CONTAINER_NAME>', '<AGENT_HOST_CONTAINER_NAME>', '<GATEWAY_CONTAINER_NAME>', '<POSTGRES_CONTAINER_NAME>', '<VALKEY_CONTAINER_NAME>'):
            text = text.replace(placeholder, placeholder.strip('<>').lower().replace('_container_name', ''))
        path = tmp_path / name
        path.write_text(text)
        path.chmod(0o600)
        config = probe.load_config(path)
        examples[config['host_role']] = {t['name'] for t in config['timers']}
    # Every timer of the examples has its rule and back, in the newest rules (v19 adds data_host:relation-read-gaps, NX-054 P4a; v20 no timer).
    newest = load_rules(RULES.parent / 'alert-rules.v20.json')
    selectors = {r['selector'] for r in newest['rules'] if r['rule_id'].startswith('timer_stale_')}
    assert selectors == {f'{host}:{name}' for host, names in examples.items() for name in names}


def test_a_manual_file_backup_does_not_hide_a_failing_scheduled_round(observed):  # noqa: F811
    o = observed
    admin, pg = o['admin'], o['pg']
    assert apply_rules(load_rules(V13), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    now = datetime.now(UTC)
    expected = ('provider-drift', 'file-backup')
    tick(pg, host='app_host', timers=[run(now - timedelta(minutes=50), name='file-backup'), run(now - timedelta(minutes=5), name='provider-drift')],
         expected_timers=expected)
    tick(pg, host='app_host', timers=[run(now - timedelta(minutes=20), name='file-backup', result='failure')], expected_timers=expected)
    tick(pg, host='app_host', timers=[run(now - timedelta(minutes=10), name='file-backup', scheduled=False)], expected_timers=expected)
    m = metrics(admin)
    assert m['timer_success_age_seconds']['app_host:file-backup'] > 45 * 60
    assert m['timer_scheduled_failures_24h']['app_host:file-backup'] == 1
    assert o['evaluate']()['rules_version'] == 13
    s = state(admin)
    assert s[('timer_stale_file_backup', 'app_host:file-backup')] is True and s[('timer_scheduled_failure', 'app_host:file-backup')] is True
    tick(pg, host='app_host', timers=[run(now - timedelta(minutes=2), name='file-backup')], expected_timers=expected)
    o['evaluate']()
    assert state(admin)[('timer_stale_file_backup', 'app_host:file-backup')] is False


@pytest.mark.parametrize('bus', [f't {TRIGGER}000000', ''], ids=['trigger-read', 'trigger-unparsed'])
def test_the_probe_entry_as_observed_is_accepted_by_the_recorder(observed, tmp_path, bus):  # noqa: F811
    """Stage 2026-10-25: a field added to the probe's entry made every write fail (22023) and no offline test passed it to 0318.
    The probe's main, unchanged, writes through the real recorder role - both with a readable trigger and with an unreadable one."""
    pg, err, out = observed['pg'], io.StringIO(), io.StringIO()
    connect = lambda: psycopg.connect(make_conninfo(pg, user='nexloop_host_recorder_data_host'), autocommit=True)  # noqa: E731
    code = P.main(['--config', config(tmp_path, timers=JOURNAL)], runner=runner(timer=DATA_HOST_TIMER, run=DATA_HOST_RUN, bus=bus),
                  connect=connect, out=out, err=err)
    assert code == 0 and 'record_failed' not in err.getvalue(), err.getvalue()
    summary = json.loads(out.getvalue().splitlines()[-1])
    assert summary['trigger_unparsed'] == ([] if bus else ['journal'])
    runs = observed['admin'].execute("select count(*) from runtime.nexloop_host_timer_runs where timer_name='journal'").fetchone()[0]
    assert runs == (1 if bus else 0)

"""Timers not reported (NX-055 E3 on stage 2026-11-08; migration timers_not_reported, alert rules v33): a timer a timer_stale_ rule
watches but no host reports had no key in host_health.timer_success_age_seconds, and the evaluator yields nothing for an absent key,
so its staleness could never fire. host_health.timers_not_reported counts them (names in timers_not_reported_names) from the
tenant's applied rules, and timer_not_reported warns. Real PostgreSQL through the NX-030 fixture and the real evaluator."""
import json

from nexloop_eios.observability import apply_rules, load_rules
from test_host_health_pg import metrics, tick
from test_observability_pg import RULES, TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

V32, V33 = RULES.parent / 'alert-rules.v32.json', RULES.parent / 'alert-rules.v33.json'
RULE = 'timer_not_reported'


def watched():
    """{host: [timer names]} that v33's timer_stale_ rules watch."""
    out = {}
    for r in load_rules(V33)['rules']:
        if r['rule_id'].startswith('timer_stale_') and r['metric'] == 'host_health.timer_success_age_seconds':
            host, name = r['selector'].split(':', 1)
            out.setdefault(host, []).append(name)
    return out


def firing(o):
    o['evaluate']()
    row = o['admin'].execute('select firing from control.nexloop_alert_state where tenant_id=%s and rule_id=%s', (TENANT, RULE)).fetchone()
    return bool(row and row[0])


def test_v33_adds_exactly_timer_not_reported_on_v32():
    v32, v33 = json.loads(V32.read_text()), load_rules(V33)
    assert v33['version'] == 33 and v33['rules'][:-1] == v32['rules'] and v33['decision'].startswith(v32['decision'] + ' v33 ')
    r = v33['rules'][-1]
    assert (r['rule_id'], r['metric'], r['condition'], r['threshold'], r['severity']) == (RULE, 'host_health.timers_not_reported', 'gt', 0, 'warning')
    assert {'journal-restic-repo2'} <= set(watched()['data_host']) and {'provider-drift'} <= set(watched()['app_host'])


def test_a_watched_timer_no_host_reports_warns_and_clears_once_reported(observed):  # noqa: F811
    o = observed
    assert apply_rules(load_rules(V33), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    w = watched()
    # As on stage: each host's probe configuration lacks one watched timer (it was never installed).
    tick(o['pg'], host='data_host', expected_timers=[n for n in w['data_host'] if n != 'journal-restic-repo2'])
    tick(o['pg'], host='app_host', expected_timers=[n for n in w['app_host'] if n != 'provider-drift'])
    h = metrics(o['admin'])
    assert 'data_host:journal-restic-repo2' not in h['timer_success_age_seconds']  # absent: timer_stale_ alone sees nothing
    assert h['timers_not_reported'] == 2 and h['timers_not_reported_names'] == ['app_host:provider-drift', 'data_host:journal-restic-repo2']
    assert firing(o)
    # Installed and listed: the probe expects it, its age is measured (fail closed), nothing is unreported, the warning clears.
    tick(o['pg'], host='data_host', expected_timers=w['data_host'])
    tick(o['pg'], host='app_host', expected_timers=w['app_host'])
    h = metrics(o['admin'])
    assert (h['timers_not_reported'], h['timers_not_reported_names']) == (0, [])
    assert not firing(o)


def test_a_timer_no_rule_watches_is_not_counted(observed):  # noqa: F811
    o = observed
    w = watched()
    # The fixture's rules (v1) have no timer_stale_ rule: they watch nothing, so even a host reporting none of them counts 0.
    tick(o['pg'], host='data_host', expected_timers=[])
    assert metrics(o['admin'])['timers_not_reported'] == 0
    apply_rules(load_rules(V33), TENANT, database_url_file=o['w']['configurator'])
    # Every watched timer reported; an extra unwatched timer, expected or not, never counts.
    tick(o['pg'], host='data_host', expected_timers=w['data_host'] + ['synthetic-unwatched'])
    tick(o['pg'], host='app_host', expected_timers=w['app_host'])
    h = metrics(o['admin'])
    assert 'data_host:synthetic-unwatched' in h['timer_success_age_seconds'] and h['timers_not_reported'] == 0
    assert not firing(o)


def test_no_host_reporting_at_all_counts_every_watched_timer(observed):  # noqa: F811
    """L6 C1: with no probe tick from any host the report is empty, not absent: every timer the rules watch is unreported."""
    o = observed
    apply_rules(load_rules(V33), TENANT, database_url_file=o['w']['configurator'])
    assert o['admin'].execute('select count(*) from runtime.nexloop_host_probe_ticks').fetchone()[0] == 0
    h = metrics(o['admin'])
    expected = sorted(f'{host}:{name}' for host, names in watched().items() for name in names)
    assert h['timer_success_age_seconds'] == {} and h['timers_not_reported'] == len(expected) and h['timers_not_reported_names'] == expected
    assert firing(o)
    # 0318 writes the block even with no tick at all (the wrapped snapshot below), so this empty report is the reachable case;
    # 0424's branch for a snapshot without the block (treated as this same empty report) is defensive.
    snapshot = o['admin'].execute("""select runtime.nexloop_metrics_snapshot_before_timers_not_reported(%s,'real')""", (TENANT,)).fetchone()[0]
    assert snapshot['host_health']['timer_success_age_seconds'] == {}

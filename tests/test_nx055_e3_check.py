"""NX-055 E3 check (scripts/acceptance/nx055_e3_check.py): the judgement on synthetic metrics, without a database. Freshness
against the timer_stale_* thresholds of the published rules, scheduled failures, recorder roles and probe ages; no data is never
a pass; the output carries counts and statuses only."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import nx055_e3_check as E  # noqa: E402

from nexloop_eios.observability import load_rules  # noqa: E402

RULES = load_rules(ROOT / 'deploy/configuration/alert-rules.v30.json')
WATCHED = E.watched_timers(RULES)
HOSTS = sorted({h for h, _ in WATCHED})
GOOD_ROLE = (True, False, False, True, False)  # login, not superuser, no membership, EXECUTE on the recorder, no table SELECT


def healthy(**over):
    block = {'timer_success_age_seconds': {f'{h}:{t}': 60 for h, t in WATCHED}, 'timer_scheduled_failures_24h': {f'{h}:{t}': 0 for h, t in WATCHED},
             'probe_age_seconds': {h: 30 for h in HOSTS}, 'blind': {}, 'missing_units': {}, 'restarts_1h': {}}
    block.update(over)
    return [{'tenant_id': 't', 'world': 'test', 'host_health': {}}, {'tenant_id': 't', 'world': 'real', 'host_health': block}]


def roles(**over):
    out = {'nexloop_host_recorder_' + h: GOOD_ROLE for h in HOSTS}
    out.update(over)
    return out


def test_the_rules_watch_both_hosts():
    assert HOSTS == ['app_host', 'data_host'] and len(WATCHED) == 11


def test_all_fresh_and_both_recorders_pass():
    r = E.evaluate(healthy(), RULES, roles())
    assert r['status'] == 'pass' and r['freshness'] == {'status': 'pass', 'timers_watched': 11, 'timers_reported': 11, 'fresh': 11, 'stale': 0,
                                                        'not_reported': 0, 'scheduled_failures_24h': 0}
    assert r['recorders'] == {'status': 'pass', 'hosts_expected': 2, 'roles_present': 2, 'roles_restricted': 2, 'hosts_recording': 2, 'hosts_probe_stale': 0}


def test_a_stale_missing_or_failing_timer_fails():
    (host, timer), threshold = next(iter(WATCHED.items()))
    key = f'{host}:{timer}'
    ages = {f'{h}:{t}': 60 for h, t in WATCHED}
    assert E.evaluate(healthy(timer_success_age_seconds={**ages, key: threshold + 1}), RULES, roles())['freshness']['stale'] == 1
    assert E.evaluate(healthy(timer_success_age_seconds={**ages, key: threshold}), RULES, roles())['status'] == 'pass'  # at the threshold: not stale
    missing = E.evaluate(healthy(timer_success_age_seconds={k: v for k, v in ages.items() if k != key}), RULES, roles())
    assert (missing['status'], missing['freshness']['not_reported']) == ('fail', 1)
    failed = E.evaluate(healthy(timer_scheduled_failures_24h={key: 2}), RULES, roles())
    assert (failed['status'], failed['freshness']['scheduled_failures_24h']) == ('fail', 2)


def test_a_shared_privileged_or_missing_recorder_fails():
    assert E.evaluate(healthy(), RULES, roles(nexloop_host_recorder_app_host=(True, False, True, True, False)))['recorders']['status'] == 'fail'  # member of a role
    assert E.evaluate(healthy(), RULES, roles(nexloop_host_recorder_app_host=(True, True, False, True, False)))['recorders']['status'] == 'fail'  # superuser
    assert E.evaluate(healthy(), RULES, roles(nexloop_host_recorder_data_host=(True, False, False, True, True)))['recorders']['status'] == 'fail'  # reads a table
    gone = {'nexloop_host_recorder_app_host': GOOD_ROLE}
    assert E.evaluate(healthy(), RULES, gone)['recorders']['roles_present'] == 1 and E.evaluate(healthy(), RULES, gone)['status'] == 'fail'
    stale = E.evaluate(healthy(probe_age_seconds={'app_host': 30, 'data_host': E.PROBE_MAX_AGE + 1}), RULES, roles())
    assert (stale['recorders']['status'], stale['recorders']['hosts_probe_stale']) == ('fail', 1)


def test_no_data_is_never_a_pass():
    for export in ([], [{'tenant_id': 't', 'world': 'test', 'host_health': {}}]):
        r = E.evaluate(export, RULES, roles())
        assert (r['status'], r['freshness']['status'], r['recorders']['status']) == ('no_data', 'no_data', 'no_data')
    empty = E.evaluate(healthy(timer_success_age_seconds={}, probe_age_seconds={}), RULES, roles())
    assert (empty['status'], empty['freshness']['status'], empty['recorders']['status']) == ('no_data', 'no_data', 'no_data')
    one = E.evaluate(healthy(probe_age_seconds={'data_host': 30}), RULES, roles())  # one host never recorded
    assert (one['status'], one['recorders']['hosts_recording']) == ('no_data', 1)
    assert E.evaluate(healthy(), {'version': 30, 'rules': []}, {})['status'] == 'no_data'  # rules watching no timer


def test_the_output_has_counts_and_statuses_only():
    text = json.dumps(E.evaluate(healthy(), RULES, roles()))
    for word in [h for h in HOSTS] + [t for _, t in WATCHED] + ['nexloop_host_recorder', 'host=', '@']:
        assert word not in text


def test_an_older_rules_file_fails_with_its_reason():
    """An old rules file (v29) watches the same or fewer timers; a healthy snapshot must still not pass against it."""
    old = load_rules(ROOT / 'deploy/configuration/alert-rules.v29.json')
    r = E.evaluate(healthy(), old, roles())
    assert (r['status'], r['reason'], r['rules_version']) == ('fail', 'rules_version_below_30', 29)
    assert r['freshness']['status'] == 'pass' and r['recorders']['status'] == 'pass'  # the checks themselves passed
    assert 'reason' not in E.evaluate(healthy(), RULES, roles())

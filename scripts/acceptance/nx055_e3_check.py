#!/usr/bin/env python3
"""NX-055 E3 acceptance on stage (docs/release/run-nx055-stage.md §E3, docs/implementation/host-health.md), read only.

    python3 scripts/acceptance/nx055_e3_check.py --metrics-database-url-file <nexloop_metrics DSN, 0600> --alert-rules <alert-rules.vN.json> [--json]

As the read-only nexloop_metrics login, in a transaction set read only: the metrics export (its host_health block, 0318) and the
catalog rows of the two host recorder roles. Two checks:

1. freshness - every timer the alert rules watch (a timer_stale_* rule, selector '<host_role>:<timer>') is reported in
   host_health.timer_success_age_seconds and is at or below the rule's threshold; no scheduled round failed in 24 hours
   (timer_scheduled_failures_24h). The age is since the last scheduled success, a manual rerun never refreshes it (0318).
2. recorders - for every host role the rules name: the role nexloop_host_recorder_<host_role> exists, can log in, is no superuser,
   no member of another role, holds EXECUTE on authz.nexloop_record_host_observation and no SELECT on the host tables; and that
   host's probe recorded within 300 s (host_health.probe_age_seconds). The recorder writes under its own session_user (0318 derives
   host_role from it), so a recent tick for each host role means each host writes with its own identity.

Output: counts and statuses only (no host name, address, DSN or timer name). Each check is pass, fail or no_data; nothing to judge
(no real snapshot, no timer reported, no host recording) is no_data, which is not a pass.
A rules file older than v30 (MIN_RULES_VERSION) is a fail with reason rules_version_below_30, whatever the checks say.
Exit 0: both pass. Exit 1: a check failed or the rules file is too old. Exit 3: no failure but a check has no data. Exit 2: refused (role, rules file, connection).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

PROBE_MAX_AGE = 300  # host_probe_stale (alert rules v13 on)
# The rules file judged against: an older file watches fewer timers (v30: 11) and could pass on a subset (L6, batch 122).
MIN_RULES_VERSION = 30
RECORD_FUNCTION = ('authz', 'nexloop_record_host_observation')
HOST_TABLES = ('nexloop_host_probe_ticks', 'nexloop_host_unit_samples', 'nexloop_host_timer_runs')  # schema runtime
# From the catalog ACLs (pg_catalog is readable by the metrics login; has_*_privilege would need USAGE on authz and runtime). A grant
# to PUBLIC (grantee 0) counts for every role; a membership is reported on its own.
ROLE_SQL = """select r.rolname,r.rolcanlogin,r.rolsuper,
  exists(select 1 from pg_auth_members m where m.member=r.oid),
  exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace cross join aclexplode(p.proacl) a
   where n.nspname=%s and p.proname=%s and a.grantee=r.oid and a.privilege_type='EXECUTE'),
  exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace cross join aclexplode(c.relacl) a
   where n.nspname='runtime' and c.relname=any(%s) and a.grantee in (r.oid,0) and a.privilege_type='SELECT')
 from pg_roles r where r.rolname=any(%s)"""


def watched_timers(rules):
    """{(host_role, timer): threshold} from the timer_stale_* rules."""
    out = {}
    for r in rules['rules']:
        if r['rule_id'].startswith('timer_stale_') and r['metric'] == 'host_health.timer_success_age_seconds':
            out[tuple(r['selector'].split(':', 1))] = r['threshold']
    return out


def overall(*statuses):
    return 'fail' if 'fail' in statuses else 'no_data' if 'no_data' in statuses else 'pass'


def freshness(host_health, watched):
    ages = (host_health or {}).get('timer_success_age_seconds') or {}
    failures = (host_health or {}).get('timer_scheduled_failures_24h') or {}
    fresh = stale = missing = 0
    for (host, timer), threshold in watched.items():
        age = ages.get(f'{host}:{timer}')
        if type(age) not in (int, float):
            missing += 1
        elif age <= threshold:
            fresh += 1
        else:
            stale += 1
    failed = sum(v for k, v in failures.items() if tuple(k.split(':', 1)) in watched and type(v) in (int, float))
    reported = fresh + stale
    status = 'no_data' if not watched or reported == 0 else 'fail' if stale or missing or failed else 'pass'
    return {'status': status, 'timers_watched': len(watched), 'timers_reported': reported, 'fresh': fresh, 'stale': stale,
            'not_reported': missing, 'scheduled_failures_24h': failed}


def recorders(host_health, hosts, roles):
    """roles: {rolname: (login, superuser, member_of_other, execute_record, select_any_host_table)}."""
    probe = (host_health or {}).get('probe_age_seconds') or {}
    present = restricted = recording = stale = 0
    for host in hosts:
        row = roles.get('nexloop_host_recorder_' + host)
        if row is not None:
            present += 1
            login, superuser, member, execute, select = row
            restricted += bool(login and not superuser and not member and execute and not select)
        age = probe.get(host)
        if type(age) in (int, float):
            recording += age <= PROBE_MAX_AGE
            stale += age > PROBE_MAX_AGE
    n = len(hosts)
    if not n or (present == 0 and recording == 0 and stale == 0):
        status = 'no_data'
    elif present < n or restricted < n or stale:
        status = 'fail'
    elif recording < n:
        status = 'no_data'  # a host never recorded: nothing to judge its identity by
    else:
        status = 'pass'
    return {'status': status, 'hosts_expected': n, 'roles_present': present, 'roles_restricted': restricted,
            'hosts_recording': recording, 'hosts_probe_stale': stale}


def evaluate(export, rules, roles):
    watched = watched_timers(rules)
    hosts = sorted({h for h, _ in watched})
    real = [s for s in export or [] if s.get('world') == 'real' and type(s.get('host_health')) is dict]
    host_health = real[0]['host_health'] if real else None  # deployment-wide: the same block in every tenant's snapshot
    f, r = freshness(host_health, watched), recorders(host_health, hosts, roles)
    if host_health is None:
        f['status'] = r['status'] = 'no_data'
    out = {'check': 'nx055-e3', 'rules_version': rules['version'], 'status': overall(f['status'], r['status']),
           'freshness': f, 'recorders': r}
    if type(rules['version']) is not int or rules['version'] < MIN_RULES_VERSION:
        out.update(status='fail', reason=f'rules_version_below_{MIN_RULES_VERSION}')
    return out


def read(dsn_file, hosts):
    import psycopg
    from nexloop_eios.private_configuration import read_private_text
    with psycopg.connect(read_private_text(dsn_file, maximum=16384), connect_timeout=5) as db:
        db.execute('set transaction read only')
        if db.execute('select current_user').fetchone()[0] != 'nexloop_metrics':
            raise PermissionError('read-only metrics role required')
        export = db.execute('select authz.nexloop_metrics_export()').fetchone()[0]
        names = ['nexloop_host_recorder_' + h for h in hosts]
        roles = {row[0]: tuple(bool(x) for x in row[1:]) for row in db.execute(ROLE_SQL, (*RECORD_FUNCTION, list(HOST_TABLES), names)).fetchall()}
    return export, roles


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--metrics-database-url-file', type=Path, required=True)
    parser.add_argument('--alert-rules', type=Path, required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        from nexloop_eios.observability import load_rules
        rules = load_rules(args.alert_rules)
        export, roles = read(args.metrics_database_url_file, sorted({h for h, _ in watched_timers(rules)}))
    except Exception as error:  # the error type only: never a DSN
        print(json.dumps({'check': 'nx055-e3', 'status': 'refused', 'error': type(error).__name__}))
        return 2
    result = evaluate(export, rules, roles)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        f, r = result['freshness'], result['recorders']
        print(f"freshness {f['status']}: {f['fresh']}/{f['timers_watched']} fresh, {f['stale']} stale, {f['not_reported']} not reported, "
              f"{f['scheduled_failures_24h']} scheduled failures in 24 h")
        print(f"recorders {r['status']}: {r['roles_restricted']}/{r['hosts_expected']} restricted roles, "
              f"{r['hosts_recording']}/{r['hosts_expected']} hosts recording, {r['hosts_probe_stale']} stale")
        if 'reason' in result:
            print(f"rules {result['reason']}: alert rules v{result['rules_version']} (at least v{MIN_RULES_VERSION} required)")
        print({'pass': 'E3 PASS', 'fail': 'E3 FAIL', 'no_data': 'E3 无数据（不算通过）'}[result['status']])
    return {'pass': 0, 'fail': 1, 'no_data': 3}[result['status']]


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Host health probe (EIOS capability gap E2 + E3; docs/implementation/host-health.md): crash loops and scheduled-run freshness,
observed from outside the services.

    host_health_probe.py --config /etc/nexloop/host-health.json [--dry-run]

Run by a systemd timer on each host (deploy/stage/systemd/nexloop-host-health.*.example), every minute. It reads
  containers  docker inspect: RestartCount and the container Id (a recreated container is a new instance);
  services    systemctl show: NRestarts (a unit with Restart= keeps running while it loops - 'active' hides the loop);
  timers      systemctl show: the timer's LastTriggerUSec and its service's last run (InactiveExitTimestamp, StateChangeTimestamp,
              Result). A run that started within grace_seconds after the last trigger is the scheduled round; any other run (a
              manual `systemctl start`) is recorded as not scheduled, so it never refreshes the scheduled freshness.
and records one tick through authz.nexloop_record_host_observation (role nexloop_host_recorder: that function only). Thresholds
are alert rules (deploy/configuration/alert-rules.v12.json); the alert evaluator turns the recorded values into alerts.

Ported from NEX-EIOS ops/systemd/detect_crash_loops.py (frozen commit, see versions.lock.json provenance): judge restart-count
deltas, never the unit state; skip one-shot units for restarts; a probe that sees nothing is blind, not healthy (here: against the
configured list, not the previous run). The window and threshold moved from the script to SQL and the alert rules; there is no
state file. Standard library only, plus psycopg for the one recording call (imported late): detection works when the database or
the application environment is broken, and says so.

Configuration (JSON, 0600, root): {"schema": "nexloop-host-health/1", "host_role": "app_host", "recorder_dsn_file": "<0600 file>",
"grace_seconds": 120, "containers": [{"name": "api", "container": "<container name>"}], "services": [{"name": "runtime-worker",
"unit": "<unit>.service"}], "timers": [{"name": "pg-diff", "timer": "<unit>.timer", "service": "<unit>.service"}]}
Output: one JSON line (counts, names, flags; no DSN). Exit 0 recorded (or dry run); 1 the recording failed; 2 configuration refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime

SCHEMA = 'nexloop-host-health/1'
HOST_ROLE = re.compile(r'[a-z][a-z0-9_]{0,31}')
NAME = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')
UNIT = re.compile(r'[A-Za-z0-9@_.:-]{1,128}\.(service|timer)')
CONTAINER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


class Refused(ValueError):
    pass


def run(command):
    """stdout of a fixed read-only command; '' when it is not there or refuses (the caller decides what nothing means)."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return done.stdout


def read_private(path, maximum=8192):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller')
        return stream.read(maximum).decode('utf-8')


def load_config(path):
    try:
        config = json.loads(read_private(path, 65536))
    except (OSError, ValueError):
        raise Refused('configuration unreadable') from None
    if type(config) is not dict or config.get('schema') != SCHEMA or not HOST_ROLE.fullmatch(str(config.get('host_role', ''))):
        raise Refused(f'configuration: schema {SCHEMA}, host_role')
    if type(config.get('recorder_dsn_file')) is not str or not os.path.isabs(config['recorder_dsn_file']):
        raise Refused('configuration: recorder_dsn_file (absolute path)')
    if type(config.get('grace_seconds', 120)) is not int or not 10 <= config.get('grace_seconds', 120) <= 3600:
        raise Refused('configuration: grace_seconds 10..3600')
    names = set()
    for key, fields, patterns in (('containers', ('name', 'container'), (NAME, CONTAINER)), ('services', ('name', 'unit'), (NAME, UNIT)),
                                  ('timers', ('name', 'timer', 'service'), (NAME, UNIT, UNIT))):
        items = config.get(key, [])
        if type(items) is not list or len(items) > 100:
            raise Refused(f'configuration: {key}')
        for item in items:
            if (type(item) is not dict or set(item) != set(fields)
                    or any(type(item[f]) is not str or not p.fullmatch(item[f]) for f, p in zip(fields, patterns))
                    or (key, item['name']) in names):
                raise Refused(f'configuration: {key} item')
            names.add((key, item['name']))
        for item in items if key == 'timers' else ():
            if not item['timer'].endswith('.timer') or not item['service'].endswith('.service'):
                raise Refused('configuration: timers item')
    return config


def _blocks(output):
    """systemctl show of several units: one block per unit, in order, separated by a blank line."""
    blocks, current = [], {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
            current = {}
            continue
        key, _, value = line.partition('=')
        current[key] = value
    if current:
        blocks.append(current)
    return {b.get('Id'): b for b in blocks if b.get('Id')}


def _unix(value):
    """systemctl --timestamp=unix gives '@<seconds>'; empty or '@0' is never."""
    if not value or not value.startswith('@'):
        return None
    try:
        seconds = int(value[1:])
    except ValueError:
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


def sample_containers(items, runner=run):
    if not items:
        return []
    out = []
    try:
        found = json.loads(runner(['docker', 'inspect', '--type', 'container', *[i['container'] for i in items]]) or '[]')
    except ValueError:
        found = []
    by_name = {str(c.get('Name', '')).lstrip('/'): c for c in found if type(c) is dict}
    for item in items:
        c = by_name.get(item['container'])
        if c is None or type(c.get('RestartCount')) is not int or not isinstance(c.get('Id'), str):
            continue
        out.append({'name': item['name'], 'kind': 'container', 'instance_id': c['Id'][:64], 'restart_count': c['RestartCount'],
                    'running': bool((c.get('State') or {}).get('Running'))})
    return out


def sample_services(items, runner=run):
    if not items:
        return []
    shown = _blocks(runner(['systemctl', 'show', '--property=Id,NRestarts,ActiveState,Type', *[i['unit'] for i in items]]))
    out = []
    for item in items:
        b = shown.get(item['unit'])
        if not b or b.get('Type') == 'oneshot' or not b.get('NRestarts', '').isdigit() or b.get('ActiveState') in (None, ''):
            continue  # a one-shot unit has no restart semantics (timers are judged by their runs)
        # One instance per unit: NRestarts grows with every automatic restart and a reset (negative delta) counts nothing.
        out.append({'name': item['name'], 'kind': 'service', 'instance_id': item['unit'], 'restart_count': int(b['NRestarts']),
                    'running': b['ActiveState'] == 'active'})
    return out


def sample_timers(items, grace, runner=run):
    if not items:
        return []
    units = [u for i in items for u in (i['timer'], i['service'])]
    shown = _blocks(runner(['systemctl', 'show', '--timestamp=unix',
                            '--property=Id,LastTriggerUSec,ActiveState,Result,InactiveExitTimestamp,StateChangeTimestamp', *units]))
    out = []
    for item in items:
        t, s = shown.get(item['timer']), shown.get(item['service'])
        if not t or not s:
            continue
        started, finished, trigger = _unix(s.get('InactiveExitTimestamp')), _unix(s.get('StateChangeTimestamp')), _unix(t.get('LastTriggerUSec'))
        if s.get('ActiveState') not in ('inactive', 'failed') or started is None or finished is None or finished < started:
            out.append(None)  # seen, no finished run to record (running now, or never ran)
            continue
        delay = (started - trigger).total_seconds() if trigger else None
        out.append({'name': item['name'], 'run_started_at': started.isoformat(), 'run_finished_at': finished.isoformat(),
                    'result': 'success' if s.get('Result') == 'success' and s.get('ActiveState') == 'inactive' else 'failure',
                    'scheduled': delay is not None and -1 <= delay <= grace, 'last_trigger_at': trigger.isoformat() if trigger else None})
    return out


def observe(config, runner=run):
    containers, services, timers = config.get('containers', []), config.get('services', []), config.get('timers', [])
    units = sample_containers(containers, runner) + sample_services(services, runner)
    seen_timers = sample_timers(timers, config.get('grace_seconds', 120), runner)
    expected_units = [i['name'] for i in containers + services]
    # Blind (ported): what we expect to see and do not is not healthy. Under half of the expected units and timers seen.
    expected, seen = len(expected_units) + len(timers), len(units) + len(seen_timers)
    blind = expected > 0 and seen * 2 < expected
    return {'host_role': config['host_role'], 'blind': blind, 'expected_units': expected_units, 'expected_timers': [i['name'] for i in timers],
            'units': units, 'timers': [r for r in seen_timers if r is not None]}


def record(config, entry, connect=None):
    if connect is None:
        import psycopg

        def connect():
            return psycopg.connect(read_private(config['recorder_dsn_file']).strip(), autocommit=True, connect_timeout=10)
    with connect() as db:
        return db.execute('select authz.nexloop_record_host_observation(%s::jsonb)', (json.dumps(entry),)).fetchone()[0]


def main(argv=None, runner=run, connect=None, out=sys.stdout):
    p = argparse.ArgumentParser(description='Host health probe: crash loops and scheduled-run freshness (E2/E3)')
    p.add_argument('--config', required=True)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    try:
        config = load_config(a.config)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    entry = observe(config, runner)
    summary = {'host_role': entry['host_role'], 'blind': entry['blind'], 'units': len(entry['units']), 'expected_units': len(entry['expected_units']),
               'timer_runs': len(entry['timers']), 'expected_timers': len(entry['expected_timers'])}
    if a.dry_run:
        print(json.dumps({**summary, 'dry_run': True, 'entry': entry}, sort_keys=True), file=out)
        return 0
    try:
        recorded = record(config, entry, connect)
    except Exception as error:  # noqa: BLE001 - the detection is printed; the recording failure is named by type only
        print(json.dumps({**summary, 'recorded': False, 'error': type(error).__name__}, sort_keys=True), file=out)
        return 1
    print(json.dumps({**summary, 'recorded': True, 'new_runs': recorded.get('new_runs')}, sort_keys=True), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())

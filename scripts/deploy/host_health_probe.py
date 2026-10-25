#!/usr/bin/env python3
"""Host health probe (EIOS capability gap E2 + E3; docs/implementation/host-health.md): crash loops and scheduled-run freshness,
observed from outside the services.

    host_health_probe.py --config /etc/nexloop/host-health.json [--dry-run]

Run by a systemd timer on each host (deploy/stage/systemd/nexloop-host-health.*.example), every minute. It reads
  containers  docker inspect: RestartCount and the container Id (a recreated container is a new instance);
  services    systemctl show: NRestarts (a unit with Restart= keeps running while it loops - 'active' hides the loop);
  timers      systemctl show: the timer's LastTriggerUSec and its service's last run (InactiveExitTimestamp, StateChangeTimestamp,
              Result). A run that started within grace_seconds (default 5: a timer starts its service within a second) after
              the last trigger is offered as the scheduled round; SQL keeps only the first run of each trigger as scheduled, so a
              manual `systemctl start` - even right after a failed round - never refreshes the scheduled freshness.
and records one tick through authz.nexloop_record_host_observation as this host's recorder role (nexloop_host_recorder_<host
role>: that function only; SQL derives the host from the role, the entry names none). Thresholds are alert rules
(deploy/configuration/alert-rules.v12.json); the alert evaluator turns the recorded values into alerts.

Local channel without PostgreSQL (L6 review I1): when the database itself loops, nothing can be recorded or evaluated. So the probe
also judges crash loops locally (the upstream anchored-window delta, state in state_file) and writes every local finding - a crash
loop, blindness, a failed recording - to stderr as a journal line of priority err (sd-daemon `<3>` prefix), for an external watcher
of the host journal (`journalctl -p err -u nexloop-host-health`).

Ported from NEX-EIOS ops/systemd/detect_crash_loops.py (frozen commit, provenance in docs/implementation/host-health.md §5): judge
restart-count deltas, never the unit state; anchor the delta at the window's start; skip one-shot units for restarts; a probe that
sees nothing is blind, not healthy (here: against the configured list); damaged local state starts again. Standard library only,
plus psycopg for the one recording call (imported late): detection works when the database or the application environment is
broken, and says so.

Configuration (JSON, 0600, root): {"schema": "nexloop-host-health/1", "host_role": "app_host", "recorder_dsn_file": "<0600 file>",
"state_file": "/var/lib/nexloop-host-health/state.json", "grace_seconds": 5, "local_window_seconds": 900, "local_min_restarts": 3,
"containers": [{"name": "api", "container": "<container name>"}], "services": [{"name": "runtime-worker", "unit": "<unit>.service"}],
"timers": [{"name": "pg-diff", "timer": "<unit>.timer", "service": "<unit>.service"}]}
Output: one JSON line (counts, names, flags; no DSN); local findings on stderr. Exit 0 recorded (or dry run); 1 the recording
failed; 2 configuration refused.
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
HOST_ROLES = ('app_host', 'data_host')  # one recorder role each (nexloop_host_recorder_<host role>, 0318)
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


def _int(config, key, default, low, high):
    value = config.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise Refused(f'configuration: {key} {low}..{high}')
    return value


def load_config(path):
    try:
        config = json.loads(read_private(path, 65536))
    except (OSError, ValueError):
        raise Refused('configuration unreadable') from None
    if type(config) is not dict or config.get('schema') != SCHEMA or config.get('host_role') not in HOST_ROLES:
        raise Refused(f'configuration: schema {SCHEMA}, host_role app_host | data_host')
    for key in ('recorder_dsn_file', 'state_file'):
        if type(config.get(key)) is not str or not os.path.isabs(config[key]):
            raise Refused(f'configuration: {key} (absolute path)')
    _int(config, 'grace_seconds', 5, 1, 60)
    _int(config, 'local_window_seconds', 900, 60, 86400)
    _int(config, 'local_min_restarts', 3, 1, 100)
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


def bus_path(unit):
    """systemd's D-Bus object path of a unit: every byte outside [A-Za-z0-9] (and a leading digit) as _xx."""
    escaped = ''.join(c if c.isascii() and c.isalnum() and not (n == 0 and c.isdigit()) else f'_{ord(c):02x}' for n, c in enumerate(unit))
    return '/org/freedesktop/systemd1/unit/' + escaped


def _bus_usec(output):
    """busctl get-property of a 't' property: 't <microseconds>'; 0 is never."""
    parts = output.split()
    if len(parts) != 2 or parts[0] != 't' or not parts[1].isdigit():
        return False, None
    usec = int(parts[1])
    return True, (datetime.fromtimestamp(usec // 1_000_000, UTC) if usec > 0 else None)


def last_trigger(timer, shown, runner):
    """(parsed, when). Stage 2026-10-25 (DATA_HOST, Ubuntu): `systemctl show --timestamp=unix` gives '@<s>' for the service's
    timestamps but leaves LastTriggerUSec readable ('Sun 2026-10-25 11:38:00 CST'), so it is read from the bus as microseconds -
    no local time, zone abbreviation or locale. '@<s>' from systemctl is the fallback; anything else is unparsed, never a manual run."""
    parsed, when = _bus_usec(runner(['busctl', 'get-property', 'org.freedesktop.systemd1', bus_path(timer),
                                     'org.freedesktop.systemd1.Timer', 'LastTriggerUSec']))
    if parsed:
        return True, when
    value = (shown or '').strip()
    if value in ('', 'n/a', '0', '@0'):
        return True, None
    when = _unix(value)
    return when is not None, when


def sample_timers(items, grace, runner=run, unparsed=None):
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
        started, finished = _unix(s.get('InactiveExitTimestamp')), _unix(s.get('StateChangeTimestamp'))
        parsed, trigger = last_trigger(item['timer'], t.get('LastTriggerUSec'), runner)
        if not parsed and unparsed is not None:
            unparsed.append(item['name'])
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
    unparsed = []
    seen_timers = sample_timers(timers, config.get('grace_seconds', 5), runner, unparsed)
    expected_units = [i['name'] for i in containers + services]
    # Blind (ported): what we expect to see and do not is not healthy. Under half of the expected units and timers seen.
    expected, seen = len(expected_units) + len(timers), len(units) + len(seen_timers)
    blind = expected > 0 and seen * 2 < expected
    return {'blind': blind, 'expected_units': expected_units, 'expected_timers': [i['name'] for i in timers],
            'units': units, 'timers': [r for r in seen_timers if r is not None], 'trigger_unparsed': unparsed}


def load_state(path):
    """Local anchors {unit: {instance_id, restarts, changes, sampled_at}}; missing or damaged state starts again."""
    try:
        with open(path, encoding='utf-8') as stream:
            value = json.load(stream)
        return value if type(value) is dict else {}
    except (OSError, ValueError):
        return {}


def save_state(path, anchors):
    try:
        tmp = path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(anchors, stream)
        os.replace(tmp, path)
    except OSError:
        pass


def local_crash_loops(previous, units, now, *, window, minimum):
    """Ported from upstream evaluate(): the anchor stays at the window's start until the window ends, a loop is reported or the
    counter resets; otherwise a loop restarting faster than the probe ticks would never accumulate. An instance change (a
    recreated container) counts one restart and moves the count base to the new instance. Returns ([(name, delta)], anchors)."""
    loops, anchors = [], {}
    for u in units:
        entry = previous.get(u['name'])
        fresh = {'instance_id': u['instance_id'], 'restarts': u['restart_count'], 'changes': 0, 'sampled_at': now}
        if type(entry) is not dict or not isinstance(entry.get('sampled_at'), (int, float)) or now - entry['sampled_at'] <= 0:
            anchors[u['name']] = fresh  # first sight, damaged entry or a clock step back: a new baseline
            continue
        same = entry.get('instance_id') == u['instance_id']
        if same and u['restart_count'] < int(entry.get('restarts', 0)):
            anchors[u['name']] = fresh  # counter reset: neither recovery nor loop
            continue
        changes = int(entry.get('changes', 0)) + (0 if same else 1)
        base = int(entry.get('restarts', 0)) if same else 0
        delta = changes + u['restart_count'] - base
        if delta >= minimum:
            loops.append((u['name'], delta))
            anchors[u['name']] = fresh
        elif now - entry['sampled_at'] >= window:
            anchors[u['name']] = fresh
        else:
            anchors[u['name']] = {'instance_id': u['instance_id'], 'restarts': base, 'changes': changes, 'sampled_at': entry['sampled_at']}
    return loops, anchors


def local_alarm(err, message):
    """One journal line of priority err (sd-daemon prefix; StandardError=journal): names and numbers only."""
    print('<3>nexloop-host-health ' + message, file=err, flush=True)


def record(config, entry, connect=None):
    if connect is None:
        import psycopg

        def connect():
            return psycopg.connect(read_private(config['recorder_dsn_file']).strip(), autocommit=True, connect_timeout=10)
    with connect() as db:
        return db.execute('select authz.nexloop_record_host_observation(%s::jsonb)', (json.dumps(entry),)).fetchone()[0]


def main(argv=None, runner=run, connect=None, out=sys.stdout, err=sys.stderr, clock=None):
    p = argparse.ArgumentParser(description='Host health probe: crash loops and scheduled-run freshness (E2/E3)')
    p.add_argument('--config', required=True)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    try:
        config = load_config(a.config)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    host = config['host_role']
    entry = observe(config, runner)
    now = (clock or (lambda: datetime.now(UTC).timestamp()))()
    loops, anchors = local_crash_loops(load_state(config['state_file']), entry['units'], now,
                                       window=config.get('local_window_seconds', 900), minimum=config.get('local_min_restarts', 3))
    if not a.dry_run:
        save_state(config['state_file'], anchors)
    for name, delta in loops:
        local_alarm(err, f'crash_loop host={host} unit={name} restarts={delta}')
    if entry['blind']:
        local_alarm(err, f'blind host={host} seen={len(entry["units"]) + len(entry["timers"])} expected={len(entry["expected_units"]) + len(entry["expected_timers"])}')
    for name in entry['trigger_unparsed']:
        local_alarm(err, f'trigger_unparsed host={host} timer={name}')
    summary = {'trigger_unparsed': entry['trigger_unparsed'], 'host_role': host, 'blind': entry['blind'], 'local_crash_loops': [n for n, _ in loops], 'units': len(entry['units']),
               'expected_units': len(entry['expected_units']), 'timer_runs': len(entry['timers']), 'expected_timers': len(entry['expected_timers'])}
    if a.dry_run:
        print(json.dumps({**summary, 'dry_run': True, 'entry': entry}, sort_keys=True), file=out)
        return 0
    try:
        recorded = record(config, entry, connect)
    except Exception as error:  # noqa: BLE001 - the detection is printed; the recording failure is named by type only
        print(json.dumps({**summary, 'recorded': False, 'error': type(error).__name__}, sort_keys=True), file=out)
        local_alarm(err, f'record_failed host={host} error={type(error).__name__}')
        return 1
    print(json.dumps({**summary, 'recorded': bool(recorded.get('recorded')), 'new_runs': recorded.get('new_runs')}, sort_keys=True), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())

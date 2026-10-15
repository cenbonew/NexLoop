#!/usr/bin/env python3
"""NX-032 deploy gate input (run sheet 3.4 §4.7, gap G8): running.json for `nexloop-doctor --deploy-gate`, read-only.

    running_snapshot.py --project nexloop-app --out GATE_DIR/running.json        on APP_HOST, as a user of the docker group
    running_snapshot.py --capture CAPTURE.json --out running.json                 from a saved capture (tests, review)

What it reads: `docker ps` (the running containers of the Compose project and their labels) and `docker top <container> -eo
pid,args` (the process list inside each container). No root, no secret file, no environment of any container, no database.

What it writes (the shape deploy_checks.guard_and_registration reads):
  {"schema": "nexloop-running/1", "project": "...", "services": {<budget entry>: {"pg": true, "processes": <running containers
   naming it>, ["guard_children": <runtime guard worker processes>]}}}
- every running container carries the label nexloop.budget-service (the Compose example labels them all): each budget entry it
  names counts one process (the budget declares one process per container, e.g. api = api-1 + api-2); "none" opens no pool;
- a running container without the label is recorded under its Compose service name with pg true: fail closed, the gate then
  reports it as unregistered;
- guard_children of the runtime-worker entry: the processes running `nexloop_eios.runtime_guard_worker` in its container.
Prints one JSON line (counts only). Exit 0 written, 2 refused (no docker, an existing output file, an invalid capture).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

LABEL = 'nexloop.budget-service'
PROJECT_LABEL = 'com.docker.compose.project'
SERVICE_LABEL = 'com.docker.compose.service'
RUNTIME_WORKER = 'runtime-worker'
GUARD_MODULE = 'nexloop_eios.runtime_guard_worker'


class Refused(ValueError):
    pass


def _labels(value):
    """A label dict (docker inspect), or docker ps's "k=v,k=v" string where a part without "=" continues the previous value (the
    budget label lists entries with commas; no label value holds "=")."""
    if isinstance(value, dict):
        return value
    labels, key = {}, None
    for part in (value or '').split(','):
        if '=' in part:
            key, val = part.split('=', 1)
            labels[key] = val
        elif key is not None:
            labels[key] += ',' + part
    return labels


def snapshot(containers, tops, project):
    """containers: [{id, state, labels}] (docker ps); tops: {id: [args, ...]} (docker top). The running.json value."""
    services = {}
    for c in containers:
        labels = _labels(c.get('labels'))
        if labels.get(PROJECT_LABEL) != project or str(c.get('state', '')).lower() != 'running':
            continue
        label = (labels.get(LABEL) or '').strip()
        entries = [labels.get(SERVICE_LABEL) or c['id']] if not label else [] if label == 'none' else [e.strip() for e in label.split(',') if e.strip()]
        for entry in entries:
            row = services.setdefault(entry, {'pg': True, 'processes': 0})
            row['processes'] += 1
            if entry == RUNTIME_WORKER:
                row['guard_children'] = row.get('guard_children', 0) + sum(GUARD_MODULE in args for args in tops.get(c['id'], []))
    return {'schema': 'nexloop-running/1', 'project': project, 'services': dict(sorted(services.items()))}


def _docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Refused('docker ' + args[0] + ' failed')
    return result.stdout


def collect(project):
    """The capture from the docker CLI: the project's containers and each one's process arguments."""
    containers = []
    for line in _docker('ps', '--filter', f'label={PROJECT_LABEL}={project}', '--format', '{{json .}}').splitlines():
        row = json.loads(line)
        labels = json.loads(_docker('inspect', '--format', '{{json .Config.Labels}}', row['ID']) or 'null') or {}
        containers.append({'id': row['ID'], 'state': row.get('State', ''), 'labels': labels})
    tops = {}
    for c in containers:
        lines = _docker('top', c['id'], '-eo', 'pid,args').splitlines()[1:]
        tops[c['id']] = [line.strip().split(None, 1)[1] if len(line.strip().split(None, 1)) == 2 else '' for line in lines]
    return {'project': project, 'containers': containers, 'tops': tops}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--project', default='nexloop-app')
    parser.add_argument('--capture', type=Path, help='a saved capture {project, containers, tops} instead of the docker CLI')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise Refused('output exists')
        capture = json.loads(args.capture.read_text()) if args.capture else collect(args.project)
        if type(capture) is not dict or type(capture.get('containers')) is not list or type(capture.get('tops')) is not dict:
            raise Refused('capture invalid')
        value = snapshot(capture['containers'], capture['tops'], capture.get('project', args.project))
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(value, indent=1, sort_keys=True) + '\n')
    except (Refused, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, Refused) else None}))
        return 2
    runtime = value['services'].get(RUNTIME_WORKER, {})
    print(json.dumps({'ok': True, 'services': len(value['services']), 'processes': sum(r['processes'] for r in value['services'].values()),
                      'guard_children': runtime.get('guard_children')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())

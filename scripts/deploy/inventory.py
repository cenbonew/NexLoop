#!/usr/bin/env python3
"""NX-032 §3.4: read-only inventory of one host before and after a deployment step (run by the owner on the host, B class).

Runs only read-only commands (ss, docker ps, docker compose ls, systemctl list-units, nft list ruleset, a directory listing) and
writes one private JSON file (0600) that `nexloop-doctor --deploy-gate` compares. The file stays on the host: it contains
addresses and names of existing services. The NexLoop firewall table is excluded from the rule-set hash so that adding it is the
only allowed firewall change.

    inventory.py --phase before|after --planned-ports 55432,56379 --data-root <NEXLOOP_ROOT> --out <private.json>
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

NEXLOOP_TABLE = re.compile(r'^table inet nexloop_stage \{.*?^\}\n?', re.S | re.M)


def parse_ss(text, *, planned=(), phase='before'):
    """`ss -H -ltn` lines → {"addr:port": {"address", "port", "owner"}}. After deployment the planned ports are NexLoop's; before,
    anything on them is another service (a conflict)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        address, _, port = local.rpartition(':')
        address = address.strip('[]').split('%')[0]
        if not port.isdigit():
            continue
        owner = 'nexloop' if phase == 'after' and int(port) in planned else 'other'
        out[f'{address}:{port}'] = {'address': address, 'port': int(port), 'owner': owner}
    return out


def parse_lines(text):
    return {line.split()[0]: {} for line in text.splitlines() if line.strip()}


def parse_docker_ps(text):
    """`docker ps --format '{{.Names}} {{.Image}}'`: name → image (a changed image of an existing container is a change)."""
    out = {}
    for line in text.splitlines():
        name, _, image = line.partition(' ')
        if name:
            out[name] = {'image': image.strip()}
    return out


def firewall_hash(ruleset):
    return hashlib.sha256(NEXLOOP_TABLE.sub('', ruleset).encode()).hexdigest()


def _run(*argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ''


def collect(*, phase, planned, data_root):
    listening = parse_ss(_run('ss', '-H', '-ltn'), planned=planned, phase=phase)
    containers = parse_docker_ps(_run('docker', 'ps', '--format', '{{.Names}} {{.Image}}'))
    projects = {p['Name']: {} for p in json.loads(_run('docker', 'compose', 'ls', '--format', 'json') or '[]')}
    units = parse_lines(_run('systemctl', 'list-units', '--type=service', '--state=running', '--no-legend', '--plain'))
    parent = os.path.dirname(data_root.rstrip('/')) or '/'
    data_dirs = {('nexloop:' + e if os.path.join(parent, e) == data_root.rstrip('/') else e): {} for e in sorted(os.listdir(parent))} if os.path.isdir(parent) else {}
    return {'phase': phase, 'listening': listening, 'containers': containers, 'compose_projects': projects, 'units': units,
            'data_directories': data_dirs, 'firewall_without_nexloop_sha256': firewall_hash(_run('nft', 'list', 'ruleset'))}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--phase', choices=('before', 'after'), required=True)
    p.add_argument('--planned-ports', required=True)
    p.add_argument('--data-root', required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args(argv)
    planned = {int(x) for x in a.planned_ports.split(',') if x}
    value = collect(phase=a.phase, planned=planned, data_root=a.data_root)
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=1)
    print(json.dumps({'written': True, 'phase': a.phase, 'counts': {k: len(v) for k, v in value.items() if isinstance(v, dict)}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())

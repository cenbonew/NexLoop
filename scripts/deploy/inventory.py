#!/usr/bin/env python3
"""NX-032 §3.4: read-only inventory of one host before and after a deployment step (run by the owner on the host, B class).

Runs only read-only commands (ss, docker ps, docker compose ls, systemctl list-units, nft list ruleset, a directory listing) and
writes one private JSON file (0600) that `nexloop-doctor --deploy-gate` compares. The file stays on the host: it contains
addresses and names of existing services. The NexLoop firewall table is excluded from the rule-set hash so that adding it is the
only allowed firewall change. Run it as root (sudo): `nft list ruleset` needs it, and without root the firewall is recorded as
unreadable (firewall_unreadable), never as a hash of nothing.

    sudo inventory.py --phase before|after|baseline --planned-ports 55432,56379 --data-root <NEXLOOP_ROOT> --out <private.json>

`baseline` re-takes the reference after NexLoop is deployed (the planned ports and the nexloop* entries are NexLoop's); the gate then
compares only the rest.
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
    """`ss -H -ltn` lines → {"addr:port": {"address", "port", "owner"}}. After deployment (and in a baseline taken after it) the planned
    ports are NexLoop's; before, anything on them is another service (a conflict)."""
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
        owner = 'nexloop' if phase in ('after', 'baseline') and int(port) in planned else 'other'
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
    """Algorithm 1 (kept for inventories taken before 2026-10-16): the whole rule set without the NexLoop table."""
    return hashlib.sha256(NEXLOOP_TABLE.sub('', ruleset).encode()).hexdigest()


# Algorithm 2 (stage 2026-10-16: changed:firewall after G-APP-SUBNET and claim_egress, although UFW and every non-NexLoop rule were
# the same): Docker writes rules (iptables-nft) for every Compose network, so NexLoop's own networks moved the hash. The rules that
# name only a NexLoop network - its bridge, its subnet or an address in it - are left out; counters are reset (they change with
# traffic). What remains is compared, and kept as text so a difference can be read line by line in the private evidence.
FIREWALL_ALGORITHM = 2
NEXLOOP_PROJECT_PREFIX = 'nexloop'
_COUNTER = re.compile(r'counter packets \d+ bytes \d+')
_ADDRESS = re.compile(r'(?<![\w.:])(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)(?![\w.])')
_STRUCTURE = re.compile(r'^(table|chain|set|map|flowtable)\b|^\}$|^type |^policy |^elements |^flags ')


def nexloop_networks(inspected):
    """`docker network inspect` objects -> {network name: {"bridge", "subnets"}} of the Compose projects named nexloop*. The bridge is
    the network's bridge.name option or Docker's default br-<first 12 of the id>."""
    out = {}
    for n in inspected or []:
        project = ((n.get('Labels') or {}).get('com.docker.compose.project') or '')
        if not project.startswith(NEXLOOP_PROJECT_PREFIX):
            continue
        bridge = (n.get('Options') or {}).get('com.docker.network.bridge.name') or 'br-' + str(n.get('Id', ''))[:12]
        subnets = sorted(c['Subnet'] for c in ((n.get('IPAM') or {}).get('Config') or []) if c.get('Subnet'))
        out[n.get('Name', '')] = {'bridge': bridge, 'subnets': subnets}
    return out


def normalize_ruleset(ruleset, networks):
    """Algorithm 2: the rule set's lines without the NexLoop table, without rules naming a NexLoop network (bridge, subnet or an
    address inside one), with counters reset; table and chain structure stays."""
    import ipaddress
    bridges = {v['bridge'] for v in networks.values()}
    subnets = [ipaddress.ip_network(s, strict=False) for v in networks.values() for s in v['subnets']]
    def nexloop_rule(line):
        if any(re.search(r'(?<![\w-])' + re.escape(b) + r'(?![\w-])', line) for b in bridges):
            return True
        for token in _ADDRESS.findall(line):
            try:
                net = ipaddress.ip_network(token, strict=False)
            except ValueError:
                continue
            if any(net.overlaps(s) for s in subnets if s.version == net.version):
                return True
        return False
    lines = []
    for raw in NEXLOOP_TABLE.sub('', ruleset).splitlines():
        line = _COUNTER.sub('counter', raw.strip())
        if not line:
            continue
        if not _STRUCTURE.match(line) and nexloop_rule(line):
            continue
        lines.append(line)
    return lines


def firewall_v2(ruleset, networks):
    lines = normalize_ruleset(ruleset, networks)
    return {'firewall_algorithm': FIREWALL_ALGORITHM, 'firewall_normalized_sha256': hashlib.sha256('\n'.join(lines).encode()).hexdigest(),
            'firewall_normalized_rules': lines, 'nexloop_networks': networks}


def _docker_networks():
    ids = _run('docker', 'network', 'ls', '-q', '--filter', 'label=com.docker.compose.project').split()
    return json.loads(_run('docker', 'network', 'inspect', *ids) or '[]') if ids else []


def _run(*argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ''


def _ruleset():
    """`nft list ruleset`, or None when it cannot be read: not root, or a non-zero exit (2026-10-16: run as an ordinary account it
    printed nothing and the inventories compared sha256("") with sha256("") - a hash of nothing is never written)."""
    if os.geteuid() != 0:
        return None
    r = subprocess.run(['nft', 'list', 'ruleset'], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# Units that start and stop on their own (socket, D-Bus or timer activation: TriggeredBy is set) and login sessions (user@<uid>), so a
# reboot or a login changes them without anyone touching an existing service. Reported apart, never a change.
_USER_SESSION = re.compile(r'user(-runtime-dir)?@\d+\.service')


def on_demand_units():
    names = [line.split()[0] for line in _run('systemctl', 'list-units', '--type=service', '--all', '--no-legend', '--plain').splitlines() if line.strip()]
    if not names:
        return []
    out, current = set(), None
    for line in _run('systemctl', 'show', '-p', 'Id', '-p', 'TriggeredBy', *names).splitlines():
        key, _, value = line.partition('=')
        if key == 'Id':
            current = value
        elif key == 'TriggeredBy' and value.strip() and current:
            out.add(current)
    return sorted(out | {n for n in names if _USER_SESSION.fullmatch(n)})


def collect(*, phase, planned, data_root):
    listening = parse_ss(_run('ss', '-H', '-ltn'), planned=planned, phase=phase)
    containers = parse_docker_ps(_run('docker', 'ps', '--format', '{{.Names}} {{.Image}}'))
    projects = {p['Name']: {} for p in json.loads(_run('docker', 'compose', 'ls', '--format', 'json') or '[]')}
    units = parse_lines(_run('systemctl', 'list-units', '--type=service', '--state=running', '--no-legend', '--plain'))
    parent = os.path.dirname(data_root.rstrip('/')) or '/'
    data_dirs = {('nexloop:' + e if os.path.join(parent, e) == data_root.rstrip('/') else e): {} for e in sorted(os.listdir(parent))} if os.path.isdir(parent) else {}
    if phase == 'baseline':
        # A baseline is taken after NexLoop is deployed: its own entries are marked so the gate compares only the rest.
        for section in (containers, projects, units, data_dirs):
            for key, value in section.items():
                if key.startswith('nexloop'):
                    value['owner'] = 'nexloop'
    out = {'phase': phase, 'listening': listening, 'containers': containers, 'compose_projects': projects, 'units': units,
           'data_directories': data_dirs, 'on_demand_units': on_demand_units()}
    ruleset = _ruleset()
    if ruleset is None:
        out['firewall_unreadable'] = True
    else:
        out.update(firewall_without_nexloop_sha256=firewall_hash(ruleset), **firewall_v2(ruleset, nexloop_networks(_docker_networks())))
    return out


def rebaseline(old, current, *, now):
    """--rebaseline: an inventory taken before algorithm 2 cannot be recomputed. The owner may accept the current firewall (algorithm 2)
    as the new baseline: every other section of the old before stays, the firewall fields are the current ones, and the file says so."""
    if old.get('phase') != 'before':
        raise ValueError('--rebaseline needs a before inventory')
    if current.get('firewall_unreadable'):
        raise ValueError('the current firewall cannot be read (run as root)')
    if old.get('firewall_algorithm') == FIREWALL_ALGORITHM:
        raise ValueError('the before inventory already uses the current algorithm')
    out = dict(old)
    out.update({k: current[k] for k in ('firewall_algorithm', 'firewall_normalized_sha256', 'firewall_normalized_rules', 'nexloop_networks')})
    out['firewall_rebaselined'] = {'at': now, 'previous_algorithm': 1, 'previous_sha256': old.get('firewall_without_nexloop_sha256'),
                                   'decision': 'owner accepted the current firewall as the baseline (run sheet run-metrics-gate §2.3)'}
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--phase', choices=('before', 'after', 'baseline'), required=True,
                   help='baseline: taken after NexLoop is deployed (planned ports and nexloop* entries are NexLoop\'s); run as root')
    p.add_argument('--planned-ports', required=True)
    p.add_argument('--data-root', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--rebaseline', help='an old (algorithm 1) before inventory: write it to --out with the current firewall as its '
                   'baseline (owner decision; --phase before)')
    a = p.parse_args(argv)
    planned = {int(x) for x in a.planned_ports.split(',') if x}
    value = collect(phase=a.phase, planned=planned, data_root=a.data_root)
    if a.rebaseline:
        if a.phase != 'before':
            p.error('--rebaseline writes a before inventory (--phase before)')
        from datetime import UTC, datetime
        with open(a.rebaseline) as stream:
            value = rebaseline(json.load(stream), value, now=datetime.now(UTC).isoformat())
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=1)
    print(json.dumps({'written': True, 'phase': a.phase, 'counts': {k: len(v) for k, v in value.items() if isinstance(v, dict)}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""NX-032 slice 4 pre-check: can this machine (as this unprivileged user) simulate two hosts with user + network namespaces?

Read-only. It reads kernel settings under /proc/sys and runs probes that live only inside throwaway namespaces created for the
probe (`unshare -rn …`): bringing up loopback, creating a veth pair, nesting a second network namespace. Every namespace and
interface disappears when the probe process exits. It changes no system setting, installs nothing, needs no root and contacts no
host. It also reports which of the simulation's tools are present (ip, slirp4netns or pasta, podman, valkey-server, docker).

    netns_feasibility.py            prints one JSON object: {"verdict": "feasible" | "not_feasible", "reasons": [...], ...}
Exit 0 feasible, 1 not feasible.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SETTINGS = {
    'max_user_namespaces': 'user/max_user_namespaces',
    'max_net_namespaces': 'user/max_net_namespaces',
    'unprivileged_userns_clone': 'kernel/unprivileged_userns_clone',            # Debian / older Ubuntu kernels
    'apparmor_restrict_unprivileged_userns': 'kernel/apparmor_restrict_unprivileged_userns',  # Ubuntu 23.10+
}
TOOLS = ('unshare', 'ip', 'slirp4netns', 'pasta', 'podman', 'valkey-server', 'docker')
PROBES = {
    'userns_netns': ['unshare', '-rn', 'true'],
    'loopback_up': ['unshare', '-rn', 'sh', '-c', 'ip link set lo up && ip -o link show lo | grep -q UP'],
    'veth_pair': ['unshare', '-rn', 'sh', '-c', 'ip link add nxa type veth peer name nxb && ip -o link show nxa >/dev/null'],
    'nested_netns': ['unshare', '-rn', 'sh', '-c', 'unshare -n true'],
}


def read_settings(proc_root='/proc/sys'):
    out = {}
    for name, rel in SETTINGS.items():
        try:
            out[name] = int(Path(proc_root, rel).read_text().strip())
        except (OSError, ValueError):
            out[name] = None
    return out


def run_probe(argv, timeout=10):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {'ok': r.returncode == 0, 'exit': r.returncode, 'stderr_tail': r.stderr.strip()[-200:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'exit': None, 'stderr_tail': type(error).__name__}


def assess(*, platform=None, settings=None, tools=None, probes=None):
    platform = platform or sys.platform
    reasons = []
    if not platform.startswith('linux'):
        reasons.append('not_linux')
        return {'verdict': 'not_feasible', 'reasons': reasons, 'platform': platform}
    if settings.get('max_user_namespaces') == 0:
        reasons.append('max_user_namespaces_is_0')
    if settings.get('max_net_namespaces') == 0:
        reasons.append('max_net_namespaces_is_0')
    if settings.get('unprivileged_userns_clone') == 0:
        reasons.append('unprivileged_userns_clone_disabled')
    if settings.get('apparmor_restrict_unprivileged_userns') == 1 and not (probes or {}).get('userns_netns', {}).get('ok'):
        reasons.append('apparmor_restricts_unprivileged_userns')
    for name in ('unshare', 'ip'):
        if not tools.get(name):
            reasons.append('tool_missing:' + name)
    for name, result in (probes or {}).items():
        if not result['ok']:
            reasons.append('probe_failed:' + name)
    if not (tools.get('slirp4netns') or tools.get('pasta')):
        reasons.append('note:no_user_mode_network (only needed to reach outside the simulation)')
    blocking = [r for r in reasons if not r.startswith('note:')]
    return {'verdict': 'feasible' if not blocking else 'not_feasible', 'reasons': reasons, 'platform': platform, 'settings': settings,
            'tools': tools, 'probes': probes,
            'valkey_for_tls_negatives': 'binary' if tools.get('valkey-server') else ('container' if tools.get('podman') or tools.get('docker') else 'unavailable')}


def main(argv=None):
    tools = {name: shutil.which(name) is not None for name in TOOLS}
    if not sys.platform.startswith('linux'):
        out = assess(platform=sys.platform, tools=tools)
        out['tools'] = tools
    else:
        probes = {name: run_probe(cmd) for name, cmd in PROBES.items()} if tools['unshare'] and tools['ip'] else {}
        out = assess(platform=sys.platform, settings=read_settings(), tools=tools, probes=probes)
    out['euid_is_root'] = os.geteuid() == 0
    print(json.dumps(out, sort_keys=True))
    return 0 if out['verdict'] == 'feasible' else 1


if __name__ == '__main__':
    sys.exit(main())

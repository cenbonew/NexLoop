#!/usr/bin/env python3
"""Run sheet run-metrics-gate (2026-10-16) part 2: assemble GATE_DIR for one `nexloop-doctor --deploy-gate` run on APP_HOST.

Standard library only (the host's python3). Runs as root (sudo-A, B): inventory reads ss/nft, docker needs the socket. It never reads
a secret: certificate, key and DSN paths come from the rendered Compose (`docker compose config --format json`, its `secrets`
section), the gate reads the files itself. Writes into a new directory (0700, files 0600):

  stage.env       copy of the private stage env
  before.json     --before (the first deployment's inventory, preferred) or a fresh `inventory.py --phase before`
  after.json      `inventory.py --phase after`
  running.json    `running_snapshot.py`
  compose.json    `docker compose ... config --format json`
  simulation-compose.json  only when NEXLOOP_ROOT/config/compose.simulation.yaml exists (ADR-037, L6 review 2.1): the simulation
                  project rendered with --profile simulation; the gate then checks it against the budget's simulation section (and fails
                  when the project runs without it)
  tls.json        deploy_checks.tls input: the service certificates (gateway, API loopback, Agent Host, guard, webchat delivery) with
                  the names their clients verify, every *_dsn secret, and valkey.conf rendered from the same stage.env
  valkey.conf     rendered deploy/stage/config/valkey.conf.template (DATA_HOST's file is rendered from the same template and env)
  spec.json       deploy/stage/deploy-gate.app.example.json with these paths; roles_database_url_file = NEXLOOP_ROOT/ops/metrics/metrics.dsn

    gate_dir.py --nexloop-root R --release-checkout C --stage-env F --gate-dir NEW_DIR --planned-ports P1,P2,... [--before FILE]
Prints one JSON line of file names and counts; exit 0 written, 2 refused.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = 'nexloop-app'
SIMULATION_PROJECT = 'nexloop-simulation'
# Compose secret names (compose.app.example.yaml `secrets:`) of the service certificates the gate checks, with the names their clients
# verify: the Gateway is reached by APP_HOST_NAME (stage.env); the others only on loopback (Caddy's tls_server_name 127.0.0.1, the
# Host origin https://127.0.0.1:HOST_INTERNAL_PORT, the guard_url and the delivery origin on 127.0.0.1) - and the key owner.
SERVER_CERTIFICATES = [
    ('gateway', 'gateway_cert', 'gateway_key', 'APP_HOST_NAME', 0),
    ('api-loopback', 'api_loopback_cert', 'api_loopback_key', '127.0.0.1', 10001),
    ('agent-host', 'host_cert', 'host_key', '127.0.0.1', 10020),
    ('guard', 'guard_cert', 'guard_key', '127.0.0.1', 10001),
    ('webchat-delivery', 'webchat_delivery_cert', 'webchat_delivery_key', '127.0.0.1', 10001),
]
CA_SECRET = 'pg_ca'  # every *_ca file of the stage tree is the same intermediate+root bundle (stage_secrets ca_bundle)


class Refused(ValueError):
    pass


def load_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values


def secret_files(compose):
    """{secret name: host path} of the rendered Compose."""
    out = {}
    for name, spec in (compose.get('secrets') or {}).items():
        path = (spec or {}).get('file')
        if not path:
            raise Refused('secret without a file: ' + name)
        out[name] = path
    return out


def tls_spec(compose, stage_env, valkey_conf, secret_uids=None):
    """secret_uids: {service directory: uid} (the gate spec's secret_uids); each DSN is checked as owned by its service's uid, since
    the gate runs as root (deploy_checks._read_dsn_file)."""
    files = secret_files(compose)
    missing = sorted({CA_SECRET, *(s for _, c, k, _, _ in SERVER_CERTIFICATES for s in (c, k))} - set(files))
    if missing:
        raise Refused('Compose has no secret: ' + ', '.join(missing))
    certificates = []
    for name, cert, key, expected, uid in SERVER_CERTIFICATES:
        names = [stage_env[expected]] if expected in stage_env else [expected]
        if expected == 'APP_HOST_NAME' and expected not in stage_env:
            raise Refused('stage.env has no APP_HOST_NAME')
        certificates.append({'name': name, 'chain': files[cert], 'key': files[key], 'purpose': 'server', 'names': names, 'uid': uid})
    dsns = []
    for name, path in sorted(files.items()):
        if not name.endswith('_dsn'):
            continue
        item = {'name': name, 'path': path}
        service = Path(path).parent.name
        if secret_uids is not None:
            if service not in secret_uids:
                raise Refused('no secret_uids entry for the DSN of ' + service)
            item['uid'] = secret_uids[service]
        dsns.append(item)
    if not dsns:
        raise Refused('Compose has no *_dsn secret')
    return {'ca': files[CA_SECRET], 'certificates': certificates, 'dsn_files': dsns, 'valkey_conf': str(valkey_conf)}


def gate_spec(example, *, gate_dir, nexloop_root, release_checkout):
    spec = json.loads(Path(example).read_text())
    g, r = str(gate_dir), str(nexloop_root)
    spec.update(stage_env=f'{g}/stage.env', running=f'{g}/running.json', inventory_before=f'{g}/before.json', inventory_after=f'{g}/after.json',
                compose_json=f'{g}/compose.json', tls_spec=f'{g}/tls.json',
                connection_budget=f'{release_checkout}/deploy/stage/connection-budget.v1.json', secret_root=f'{r}/secrets',
                roles_database_url_file=f'{r}/ops/metrics/metrics.dsn', agent_host_config=f'{r}/secrets/agent-host/runtime-config.json')
    # The gate runs as root; the Host's runtime configuration is read as owned by the Host uid (deploy_checks._read_owned_file).
    spec['agent_host_config_uid'] = spec['secret_uids']['agent-host']
    left = [k for k, v in spec.items() if isinstance(v, str) and '<' in v]
    if left:
        raise Refused('placeholders left in spec: ' + ', '.join(left))
    return spec


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(data)


def _run(argv, **kw):
    done = subprocess.run(argv, capture_output=True, text=True, **kw)
    if done.returncode:
        raise Refused(f'{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ""} failed (exit {done.returncode})')
    return done.stdout


def assemble(a):
    root, checkout, gate = Path(a.nexloop_root), Path(a.release_checkout), Path(a.gate_dir)
    if gate.exists():
        raise Refused('--gate-dir exists (one directory per run)')
    compose_cmd = ['docker', 'compose', '-p', PROJECT, '-f', str(root / 'config/compose.app.yaml'), '--env-file', str(root / 'config/compose.app.env'),
                   '--env-file', str(root / 'config/compose.images.env')]
    gate.mkdir(mode=0o700, parents=True)
    _write(gate / 'stage.env', Path(a.stage_env).read_text())
    stage_env = load_env(gate / 'stage.env')
    inventory = [sys.executable, str(checkout / 'scripts/deploy/inventory.py'), '--planned-ports', a.planned_ports, '--data-root', str(root)]
    if a.before:
        _write(gate / 'before.json', Path(a.before).read_text())
    else:
        _run([*inventory, '--phase', 'before', '--out', str(gate / 'before.json')])
    _run([*inventory, '--phase', 'after', '--out', str(gate / 'after.json')])
    _run([sys.executable, str(checkout / 'scripts/deploy/running_snapshot.py'), '--project', PROJECT, '--out', str(gate / 'running.json')])
    compose_text = _run([*compose_cmd, 'config', '--format', 'json'])
    _write(gate / 'compose.json', compose_text)
    simulation = root / 'config/compose.simulation.yaml'
    if simulation.exists():
        sim_env = root / 'config/compose.simulation.env'
        _write(gate / 'simulation-compose.json', _run(['docker', 'compose', '-p', SIMULATION_PROJECT, '-f', str(simulation),
                                                        *(['--env-file', str(sim_env)] if sim_env.exists() else []), '--profile', 'simulation',
                                                        'config', '--format', 'json']))
    _run([sys.executable, str(checkout / 'scripts/deploy/render.py'), '--env', str(gate / 'stage.env'), '--template',
          str(checkout / 'deploy/stage/config/valkey.conf.template'), '--out', str(gate / 'valkey.conf')])
    spec = gate_spec(checkout / 'deploy/stage/deploy-gate.app.example.json', gate_dir=gate, nexloop_root=root, release_checkout=checkout)
    if (gate / 'simulation-compose.json').exists():
        spec['simulation_compose_json'] = str(gate / 'simulation-compose.json')
    tls = tls_spec(json.loads(compose_text), stage_env, gate / 'valkey.conf', spec['secret_uids'])
    _write(gate / 'tls.json', json.dumps(tls, indent=1))
    _write(gate / 'spec.json', json.dumps(spec, indent=1))
    for path in gate.iterdir():
        path.chmod(0o600)
    return {'gate_dir_files': sorted(p.name for p in gate.iterdir()), 'certificates': len(tls['certificates']), 'dsn_files': len(tls['dsn_files']),
            'before': 'given' if a.before else 'fresh'}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for name in ('--nexloop-root', '--release-checkout', '--stage-env', '--gate-dir', '--planned-ports'):
        p.add_argument(name, required=True)
    p.add_argument('--before', help="the first deployment's inventory before.json (preferred over a fresh one)")
    a = p.parse_args(argv)
    try:
        out = assemble(a)
    except (Refused, OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, Refused) else type(error).__name__}))
        return 2
    print(json.dumps({'ok': True, **out}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

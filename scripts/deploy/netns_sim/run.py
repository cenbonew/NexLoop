#!/usr/bin/env python3
"""NX-032 slice 4 (AT-058): run the isolation probes in a two-host simulation on rootless Docker (dispatcher ruling 2026-10-11).

Run by the CI user on the CI host. It uses only images already present locally (pull_policy never), creates one private temporary
work directory, starts the Compose project `nexloop-sim`, runs scripts/deploy/probe_isolation.py from four locations (app,
neighbour, outsider and the CI user on the host itself), stops the project and removes its volumes and the work directory. It
contacts no other host and changes no system setting.

    python3 -I scripts/deploy/netns_sim/run.py --out <report dir> [--keep] [--prepare-only]
        [--postgres-image REF] [--valkey-image REF] [--python-image REF]      (default: the digests in versions.lock.json)

Prints one JSON summary; the per-location reports (codes only) are written into --out. Exit 0 every probe as expected, 1 a probe
differed, 2 environment or preparation error (for example an image that is not present locally).
"""
import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import issue_certs  # noqa: E402
from nexloop_eios import stage_render  # noqa: E402

HERE = Path(__file__).resolve().parent
PROJECT = 'nexloop-sim'
DATA_ADDR, APP_DATA_ADDR, APP_MGMT_ADDR = '192.0.2.20', '192.0.2.10', '198.51.100.10'
DATA_NAME = 'data.sim.invalid'
DB = 'nexloop_stage'
SIM_STAGE = {
    'APP_HOST_ADDR': APP_DATA_ADDR, 'DATA_HOST_ADDR': DATA_ADDR, 'APP_HOST_NAME': 'app.sim.invalid', 'DATA_HOST_NAME': DATA_NAME,
    'TRUSTED_LAN_CIDR': '198.51.100.0/24', 'MGMT_SOURCE_CIDR': '198.51.100.0/24', 'APP_CONTAINER_CIDR': '192.0.2.0/24',
    'PG_PORT': '5432', 'VALKEY_PORT': '6379', 'GATEWAY_HTTPS_PORT': '8443', 'GATEWAY_REDIRECT_PORT': '8080', 'API_INTERNAL_PORT': '8000', 'API_INTERNAL_PORT_2': '8001',
    'HOST_INTERNAL_PORT': '8100', 'GUARD_PORT': '8200', 'NEXLOOP_ROOT': '/srv/nexloop', 'NEXLOOP_DB': DB,
    'APP_PG_ROLES': 'nexloop_api,nexloop_metrics', 'MIGRATION_PG_ROLE': 'nexloop_bootstrap', 'VALKEY_MAXMEMORY': '64mb',
    'AGENT_HOST_UID': '10002', 'CI_UID': '10003', 'MODEL_PROVIDER_ALLOWLIST': '203.0.113.5'}
# Which result is a real server decision and which only the simulation's topology (no nft rule is loaded in the simulation).
EVIDENCE = {
    'tls_and_certificates': 'real: PostgreSQL and Valkey servers with the stage templates (TLS 1.3, client certificates, verify-full on the client)',
    'pg_hba_source_and_roles': 'real: pg_hba.conf rendered from deploy/stage/config/pg_hba.conf.template (only APP_HOST_ADDR, hostnossl reject)',
    'valkey_acl': 'real: ACL file with the default user off',
    'internal_port_loopback': 'real: the internal API port is bound to 127.0.0.1 inside app',
    'data_unreachable_from_management_and_host': 'topology: outsider is not on data_net; the host CI user cannot route into rootless bridges '
                                                 '(equivalent of the DATA_HOST firewall rules, which are not loaded here)',
    'valkey_from_lan_neighbour': 'topology gap: Valkey has no source restriction of its own; in the deployment the firewall stops a LAN '
                                 'neighbour, in the simulation it reaches the TLS port (the probe records it as reachable)',
}


def lock_images():
    images = json.loads((ROOT / 'versions.lock.json').read_text())['images']
    ref = lambda name: images[name]['reference'].rsplit(':', 1)[0] + '@' + images[name]['digest']
    return {'postgres': ref('postgres'), 'valkey': ref('valkey'), 'python': ref('python-build-base')}


def private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    return path


def prepare(work):
    """Certificates, rendered server configuration, roles, ACL and one probe spec per location; nothing starts."""
    work = Path(work)
    pki, data, probe = work / 'pki', work / 'data', work / 'probe'
    for d in (data, probe, data / 'sim', data / 'initdb', data / 'tls-pg', data / 'tls-valkey'):
        d.mkdir(mode=0o700, parents=True)
    issue_certs.root(pki / 'root')
    issue_certs.intermediate(pki / 'root', pki / 'ca')
    issue_certs.root(pki / 'other-root')
    issue_certs.intermediate(pki / 'other-root', pki / 'other-ca')
    for name in ('pg-server', 'valkey-server'):
        issue_certs.leaf('server', pki / 'ca', pki / 'servers', name, sans=[f'DNS:{DATA_NAME}'])
    for role in ('nexloop_api', 'nexloop_metrics'):
        issue_certs.leaf('client', pki / 'ca', probe, f'pg-client-{role}', cn=role)
    issue_certs.leaf('client', pki / 'ca', probe, 'valkey-client-cache', cn='nexloop_cache')
    issue_certs.leaf('client', pki / 'other-ca', probe, 'other-client', cn='nexloop_api')
    for name, target in (('pg-server', data / 'tls-pg'), ('valkey-server', data / 'tls-valkey')):
        shutil.copy(pki / 'servers' / f'{name}.chain.crt', target / 'server.crt')
        shutil.copy(pki / 'servers' / f'{name}.key', target / 'server.key')
        shutil.copy(pki / 'ca' / 'bundle.crt', target / 'ca.crt')
    shutil.copy(pki / 'root' / 'ca.crt', probe / 'ca.crt')
    shutil.copy(pki / 'other-root' / 'ca.crt', probe / 'other-ca.crt')
    for template in ('postgresql.conf', 'pg_hba.conf', 'valkey.conf'):
        text, _ = stage_render.render((ROOT / 'deploy/stage/config' / f'{template}.template').read_text(), SIM_STAGE)
        (data / template).write_text(text)
    passwords = {k: secrets.token_hex(16) for k in ('bootstrap', 'api', 'metrics', 'cache')}
    private(data / 'sim' / 'bootstrap_password', passwords['bootstrap'])
    private(data / 'sim' / 'api_password', passwords['api'])
    private(data / 'sim' / 'metrics_password', passwords['metrics'])
    (data / 'initdb' / '10-roles.sh').write_text(
        '#!/bin/sh\nset -e\n'
        'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
        '-v api="$(cat /run/sim/api_password)" -v metrics="$(cat /run/sim/metrics_password)" <<-SQL\n'
        "create role nexloop_api login password :'api';\ncreate role nexloop_metrics login password :'metrics';\nSQL\n")
    acl = f'user default off\nuser nexloop-cache on #{hashlib.sha256(passwords["cache"].encode()).hexdigest()} ~nexloop:* &* -@all +ping +get +set +del\n'
    (data / 'valkey.acl').write_text(acl)
    for name in ('api', 'metrics', 'cache'):
        private(probe / f'{name}_password', passwords[name])
    private(probe / 'wrong_password', 'x' * 32)
    shutil.copy(ROOT / 'scripts/deploy/probe_isolation.py', probe / 'probe_isolation.py')
    for path in [*data.rglob('*'), *probe.rglob('*')]:
        if path.is_file():
            path.chmod(0o644 if path.suffix in ('.crt', '.conf', '.py', '.json', '.acl') or path.name == '10-roles.sh' else 0o600)
    (data / 'initdb' / '10-roles.sh').chmod(0o755)
    specs = probe_specs()
    for location, spec in specs.items():
        (probe / f'spec-{location}.json').write_text(json.dumps(spec, indent=1))
    return specs


def probe_specs(base='/sim'):
    """The expected class of every probe from every location of the simulation."""
    pg_targets = {'DATA_PG': {'host': DATA_NAME, 'port': 5432}, 'DATA_VALKEY': {'host': DATA_NAME, 'port': 6379}}
    by_addr = {'DATA_PG': {'host': DATA_ADDR, 'port': 5432}, 'DATA_VALKEY': {'host': DATA_ADDR, 'port': 6379},
               'APP_API_INTERNAL': {'host': APP_MGMT_ADDR, 'port': 8000}, 'APP_GATEWAY': {'host': APP_MGMT_ADDR, 'port': 8443}}
    f = lambda name: f'{base}/{name}'
    pg = lambda code, expect, **kw: {'code': code, 'kind': 'pg', 'target': 'DATA_PG', 'expect': expect, 'ca': f('ca.crt'), 'server_name': DATA_NAME,
                                     'user': 'nexloop_api', 'database': DB, 'cert': f('pg-client-nexloop_api.chain.crt'),
                                     'key': f('pg-client-nexloop_api.key'), **kw}
    vk = lambda code, expect, **kw: {'code': code, 'kind': 'valkey', 'target': 'DATA_VALKEY', 'expect': expect, 'ca': f('ca.crt'),
                                     'server_name': DATA_NAME, 'cert': f('valkey-client-cache.chain.crt'), 'key': f('valkey-client-cache.key'), **kw}
    tcp = lambda code, target, expect: {'code': code, 'kind': 'tcp', 'target': target, 'expect': expect}
    app = {'location': 'APP_HOST', 'targets': {**pg_targets, 'APP_API_LOOPBACK': {'host': '127.0.0.1', 'port': 8000}}, 'probes': [
        pg('pg_ok', 'authenticated', password_file=f('api_password')),
        pg('pg_plaintext', 'server_refused', sslmode='disable'),
        pg('pg_wrong_ca', 'client_refused', ca=f('other-ca.crt')),
        pg('pg_san_mismatch', 'client_refused', server_name='wrong.sim.invalid'),
        pg('pg_no_client_cert', 'server_refused', cert=None, key=None),
        pg('pg_other_ca_client_cert', 'server_refused', cert=f('other-client.chain.crt'), key=f('other-client.key')),
        pg('pg_other_role_cert', 'auth_failed', cert=f('pg-client-nexloop_metrics.chain.crt'), key=f('pg-client-nexloop_metrics.key'),
           password_file=f('api_password')),
        pg('pg_wrong_password', 'auth_failed', password_file=f('wrong_password')),
        vk('valkey_ok', 'pong', user='nexloop-cache', password_file=f('cache_password')),
        vk('valkey_default_user', 'acl_refused'),
        vk('valkey_wrong_password', 'acl_refused', user='nexloop-cache', password_file=f('wrong_password')),
        vk('valkey_no_client_cert', 'server_refused', cert=None, key=None),
        vk('valkey_wrong_ca', 'client_refused', ca=f('other-ca.crt')),
        vk('valkey_plaintext', 'server_refused', plaintext=True),
        tcp('app_api_loopback', 'APP_API_LOOPBACK', 'reachable')]}
    neighbour = {'location': 'LAN_NEIGHBOUR', 'targets': pg_targets, 'probes': [
        tcp('data_pg_tcp', 'DATA_PG', 'reachable'),
        pg('pg_valid_identity_wrong_source', 'server_refused', password_file=f('api_password')),
        tcp('data_valkey_tcp', 'DATA_VALKEY', 'reachable')]}
    outsider = {'location': 'MANAGEMENT', 'targets': by_addr, 'probes': [
        tcp('data_pg', 'DATA_PG', 'unreachable'), tcp('data_valkey', 'DATA_VALKEY', 'unreachable'),
        tcp('app_api_internal', 'APP_API_INTERNAL', 'unreachable'), tcp('app_gateway', 'APP_GATEWAY', 'reachable')]}
    host = {'location': 'CI_HOST_USER', 'targets': by_addr, 'probes': [
        tcp('data_pg', 'DATA_PG', 'unreachable'), tcp('data_valkey', 'DATA_VALKEY', 'unreachable'),
        tcp('app_gateway', 'APP_GATEWAY', 'unreachable')]}
    return {'app': app, 'neighbour': neighbour, 'outsider': outsider, 'host': host}


def docker(*args, env=None, check=True, timeout=600):
    return subprocess.run(['docker', *args], capture_output=True, text=True, env={**os.environ, **(env or {})}, check=check, timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--keep', action='store_true', help='leave the project running for inspection (stop it with docker compose -p nexloop-sim down -v)')
    parser.add_argument('--prepare-only', action='store_true', help='prepare the work directory and print the plan; start nothing')
    for name in ('postgres', 'valkey', 'python'):
        parser.add_argument(f'--{name}-image')
    a = parser.parse_args(argv)
    images = {**lock_images(), **{k: v for k, v in (('postgres', a.postgres_image), ('valkey', a.valkey_image), ('python', a.python_image)) if v}}
    if a.out.exists():
        print(json.dumps({'passed': False, 'error': 'out directory exists'}))
        return 2
    work = Path(tempfile.mkdtemp(prefix='nexloop-sim-'))
    env = {'SIM_WORK': str(work), 'SIM_DB': DB, 'SIM_POSTGRES_IMAGE': images['postgres'], 'SIM_VALKEY_IMAGE': images['valkey'], 'SIM_PYTHON_IMAGE': images['python']}
    compose = ['compose', '-p', PROJECT, '-f', str(HERE / 'compose.sim.yaml')]
    started = False
    try:
        specs = prepare(work)
        if a.prepare_only:
            print(json.dumps({'prepared': True, 'images': images, 'locations': {k: [p['code'] for p in v['probes']] for k, v in specs.items()}}))
            return 0
        missing = [name for name, ref in images.items() if docker('image', 'inspect', ref, check=False).returncode != 0]
        if missing:
            print(json.dumps({'passed': False, 'error': 'image_not_present_locally', 'images': missing}))
            return 2
        info = json.loads(docker('info', '--format', '{{json .SecurityOptions}}').stdout or '[]')
        started = True
        up = docker(*compose, 'up', '-d', '--wait', '--wait-timeout', '120', env=env, check=False, timeout=300)
        if up.returncode != 0:
            print(json.dumps({'passed': False, 'error': 'compose_up_failed', 'stderr_tail': up.stderr[-1500:]}))
            return 2
        a.out.mkdir(mode=0o700, parents=True)
        reports = {}
        for location in ('app', 'neighbour', 'outsider'):
            run = docker(*compose, 'exec', '-T', location, 'python3', '-I', '/sim/probe_isolation.py', '--spec', f'/sim/spec-{location}.json',
                         env=env, check=False, timeout=180)
            reports[location] = json.loads(run.stdout) if run.stdout.strip().startswith('{') else {'passed': False, 'error': run.stderr[-500:]}
        host_spec = work / 'probe' / 'spec-host.json'
        run = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/deploy/probe_isolation.py'), '--spec', str(host_spec)], capture_output=True, text=True, timeout=180)
        reports['host'] = json.loads(run.stdout)
        for location, report in reports.items():
            private(a.out / f'report-{location}.json', json.dumps(report, indent=1, sort_keys=True))
        summary = {'passed': all(r.get('passed') for r in reports.values()), 'rootless': any('rootless' in str(o) for o in info),
                   'locations': {k: {'passed': r.get('passed'), 'failed': [x['probe'] for x in r.get('results', []) if not x['passed']]} for k, r in reports.items()},
                   'evidence': EVIDENCE, 'images': images}
        private(a.out / 'summary.json', json.dumps(summary, indent=1, sort_keys=True))
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary['passed'] else 1
    finally:
        if started and not a.keep:
            docker(*compose, 'down', '-v', '--remove-orphans', env=env, check=False, timeout=300)
        if not a.keep:
            assert work.name.startswith('nexloop-sim-') and work.parent == Path(tempfile.gettempdir())
            shutil.rmtree(work)


if __name__ == '__main__':
    sys.exit(main())

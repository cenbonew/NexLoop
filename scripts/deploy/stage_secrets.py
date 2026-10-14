#!/usr/bin/env python3
"""NX-032 run sheet 1.6: the stage secret files, generated on the owner's management machine (docs/implementation/NX-032-stage-secrets.md).

    stage_secrets.py catalog  [--compose-app F] [--compose-data F]                  the inventory (paths, kinds, phases, owners)
    stage_secrets.py generate --out NEW_DIR --ca-dir CA_DIR --params PRIVATE_JSON    phase A files into a new private tree
    stage_secrets.py check    --out DIR [--uids]                                       secret_distribution and manifest digests

The catalog is read from the APP_HOST Compose file's `secrets:` section (so it cannot drift from what the services mount) plus
the fixed DATA_HOST files. `generate` writes, below a directory that must not exist:
  app/secrets/<service>/<file>, data/secrets/<service>/<file>   (directories 0700, files 0600, never overwritten)
  management/                                                    signing.key.hex (the configurator's encoding of the shared
                                                                 signing key), role-passwords.sql (SCRAM-SHA-256 verifiers only,
                                                                 run once by the owner in the bootstrap window), service-secrets.json
                                                                 (credential file -> token, for the trusted configuration of phase B)
  manifest.json                                                  path, kind, phase, intended owner and sha256 of every file; no value
Certificates come from issue_certs (the existing intermediate CA, --ca-dir); random material from the secrets module. Files of
phase C and the manual configuration files are listed, not fabricated. No value is ever printed. The files are written as the
current user; the intended owner (manifest `owner`) is applied when the owner installs them (`install -o <uid> -m 0600`).

The private params JSON: {"data_host_addr", "data_host_name", "pg_port", "db_name", "nexloop_root", "signing_key_id", "guard_port",
"sans": {"gateway": [...], "agent-host": [...], "guard": [...], "data": [...], "loopback": [...]}} (SANs as DNS:<name> or IP:<addr>).
Prints one JSON object (counts and codes only). Exit 0 ok, 1 problems found, 2 refused.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
import issue_certs  # noqa: E402

COMPOSE_APP = ROOT / 'deploy/stage/compose.app.example.yaml'
COMPOSE_DATA = ROOT / 'deploy/stage/compose.data.example.yaml'
# Owners (dispatcher 2026-10-14): backend services 10001, Agent Host 10020, gateway root (caddy runs as root with cap_drop ALL),
# DATA_HOST postgres/valkey/pgbackrest 999 (the image users), backup the nexloop-backup user (its uid is the host's).
OWNERS = {'agent-host': '10020', 'agent-host-model': '10020', 'gateway': '0', 'postgres': '999', 'valkey': '999', 'pgbackrest': '999', 'backup': 'nexloop-backup'}
BACKEND_OWNER = '10001'
# The PG role of each service DSN (the client certificate's CN must be the role: pg_hba clientcert=verify-full).
PRIMARY_ROLE = {'api': 'nexloop_api', 'domain-worker': 'nexloop_domain_worker', 'action-worker': 'nexloop_action_worker',
                'webchat-delivery': 'nexloop_action_worker', 'runtime-worker': 'nexloop_scheduler', 'message-relay': 'nexloop_api',
                'outbound-recorder': 'nexloop_api', 'claim-extraction-scheduler': 'nexloop_api'}
DSN_ROLE = {'identity-dsn': 'nexloop_identity', 'api-dsn': 'nexloop_api'}
CA_FILES = {'api-loopback-ca.crt', 'guard-ca.crt', 'db-ca.crt', 'host-ca.crt', 'ca.crt', 'webchat-provider-ca.crt', 'enterprise-provider-ca.crt'}
SERVER_CERTS = {('gateway', 'server'): 'gateway', ('agent-host', 'server'): 'agent-host', ('webchat-delivery', 'server'): 'loopback',
                ('api', 'loopback'): 'loopback', ('runtime-worker', 'guard'): 'guard', ('postgres', 'server'): 'data', ('valkey', 'server'): 'data'}
# Values that must be identical in several files (one secret, several readers).
SHARED_VALUES = {'host-control': ['app:shared/host-control.key', 'app:agent-host/transport.key', 'app:retention-keeper/host-control.key'],
                 # F2: the guard bearer key the runtime worker's guard checks and the Agent Host presents.
                 'guard-transport': ['app:runtime-worker/guard-transport.key', 'app:agent-host/guard-transport.key'],
                 'webchat-provider': ['app:action-worker/webchat-provider-credential', 'app:webchat-delivery/provider-credential']}
MANUAL = {'provider-routes': ('manual', 'NX-037 provider routing (effect_dispatch routes by Action)'),
          'webchat-provider-config': ('manual', 'the action worker\'s HttpEffectProvider configuration of webchat-delivery'),
          'enterprise-provider-config': ('C', 'the reference enterprise endpoint (run sheet 4.3)'),
          'enterprise-provider-credential': ('C', 'the reference endpoint service_token (run sheet 4.3)'),
          'model.env': ('C', 'model API key (owner, run sheet R6 pattern)'),
          # F2: the Agent Host's model provider key, outside secrets/agent-host (which holds no credential), owner 10020; the model
          # configuration's MODEL_CREDENTIALS_FILE names it (/run/secrets/host_model_key).
          'model.key': ('C', 'the Agent Host model provider key (owner, agent-host-model/, owner 10020)'),
          'pgbackrest.conf': ('manual', 'NX-035 runbook 2.3: rendered by the owner with the repository passphrases and object-store keys')}
DATA_FILES = ['postgres/bootstrap_password', 'postgres/server.crt', 'postgres/server.key', 'postgres/ca.crt',
              'valkey/valkey.acl', 'valkey/server.crt', 'valkey/server.key', 'valkey/ca.crt',
              'pgbackrest/pgbackrest.conf', 'pgbackrest/pgpass', 'backup/recorder.dsn']


class Refused(ValueError):
    pass


HOST_FILES = {'runtime-config.json': 'host_runtime_config', 'model-configuration.json': 'host_model_configuration',
              'model-tariff.cny.json': 'host_model_tariff'}
TARIFF = ROOT / 'deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json'
RUNTIME_EXAMPLE = ROOT / 'deploy/stage/agent-host.runtime-config.example.json'


def _client_dsn(name):
    """The DSN file a client certificate belongs to: pg-client.* -> dsn, <x>-pg-client.* -> <x>-dsn."""
    stem = name.rsplit('.', 1)[0]
    return 'dsn' if stem == 'pg-client' else stem.removesuffix('-pg-client') + '-dsn'


def _owner(service):
    return OWNERS.get(service, BACKEND_OWNER)


def _role(service, name):
    if name in DSN_ROLE:
        return DSN_ROLE[name]
    return PRIMARY_ROLE.get(service, 'nexloop_domain_worker')


def classify(host, service, name):
    """(kind, phase, note) of one secret file; kind decides how phase A generates it."""
    stem = name.rsplit('.', 1)[0]
    if stem in MANUAL or name in MANUAL:
        phase, note = MANUAL.get(stem) or MANUAL[name]
        return 'config', phase, note
    if name in CA_FILES:
        return 'ca_bundle', 'A', 'the intermediate CA bundle (intermediate + root)'
    if name.endswith(('pg-client.crt', 'pg-client.key')):
        # F1: a DSN's client certificate carries its role as CN (pg_hba clientcert=verify-full): pg-client.* for `dsn`,
        # identity-pg-client.* for `identity-dsn`, api-pg-client.* for `api-dsn`.
        return ('client_cert' if name.endswith('.crt') else 'client_key'), 'A', f'CN={_role(service, _client_dsn(name))}'
    if name in HOST_FILES:
        return HOST_FILES[name], 'A', 'the Agent Host runtime files (F2), no secret value'
    if name.endswith(('.crt', '.key')) and (service, stem) in SERVER_CERTS:
        return ('server_cert' if name.endswith('.crt') else 'server_key'), 'A', f'SANs: {SERVER_CERTS[(service, stem)]}'
    if name in ('dsn', 'identity-dsn', 'api-dsn'):
        return 'dsn', 'A/B', f'role {_role(service, name)}; its password takes effect when role-passwords.sql runs (bootstrap window)'
    if name == 'recorder.dsn':
        return 'socket_dsn', 'A/B', 'role nexloop_backup_recorder over the DATA_HOST socket; password from role-passwords.sql'
    if name == 'pgpass':
        return 'pgpass', 'A/B', 'nexloop_bootstrap (run sheet 2.4) and nexloop_backup (2.5b) lines'
    if name == 'service-credential' or name.endswith('-credential'):
        if name == 'provider-credential' or name == 'webchat-provider-credential':
            return 'token', 'A', 'shared between the action worker and webchat-delivery'
        return 'token', 'A/B', 'random token; registered by the trusted configuration after bootstrap (see the document §3)'
    if name == 'signing.key':
        return 'signing_key', 'A/B', '32 raw bytes; the hex copy is registered by the trusted configuration'
    if name.endswith('.key'):
        return 'hex_key', 'A', '32 bytes as 64 lower-case hex' + (' (one value: host-control)' if stem in ('host-control', 'transport') else '')
    if name == 'bootstrap_password':
        return 'password', 'A', 'POSTGRES_PASSWORD_FILE of the first start'
    if name == 'valkey.acl':
        return 'valkey_acl', 'A', 'no application user yet: `user default off`'
    raise Refused(f'unclassified secret file {host}:{service}/{name}')


def catalog(compose_app=COMPOSE_APP, compose_data=COMPOSE_DATA):
    app = yaml.safe_load(Path(compose_app).read_text())
    entries = []
    for secret, spec in sorted((app.get('secrets') or {}).items()):
        path = re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec['file'])
        service, name = path.split('/', 1)
        entries.append({'host': 'app', 'path': path, 'secret': secret, 'service': service, 'owner': _owner(service)})
    data_text = Path(compose_data).read_text()
    for path in DATA_FILES:
        service, name = path.split('/', 1)
        if service != 'backup' and f'/secrets/{path}' not in data_text:
            raise Refused(f'data compose no longer mounts {path}')
        entries.append({'host': 'data', 'path': path, 'secret': None, 'service': service, 'owner': _owner(service)})
    for e in entries:
        e['kind'], e['phase'], e['note'] = classify(e['host'], e['service'], e['path'].split('/', 1)[1])
    return entries


# ---- generation --------------------------------------------------------------------------------------------------------------
def scram_verifier(password, *, iterations=4096, salt=None):
    """The PostgreSQL SCRAM-SHA-256 verifier (RFC 5803 form) of `password`: what ALTER ROLE ... PASSWORD stores as is."""
    salt = salt or secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    client_key = hmac.new(salted, b'Client Key', 'sha256').digest()
    server_key = hmac.new(salted, b'Server Key', 'sha256').digest()
    def b64(value):
        return base64.b64encode(value).decode()
    return f'SCRAM-SHA-256${iterations}:{b64(salt)}${b64(hashlib.sha256(client_key).digest())}:{b64(server_key)}'


def _write(path, data, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(path, mode)


def _params(path):
    p = json.loads(Path(path).read_text())
    need = {'data_host_addr', 'data_host_name', 'pg_port', 'db_name', 'nexloop_root', 'signing_key_id', 'sans', 'guard_port'}
    if type(p) is not dict or not need <= set(p) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', str(p['signing_key_id'])):
        raise Refused('params: ' + ', '.join(sorted(need)) + ' (signing_key_id as the services accept it)')
    if type(p['guard_port']) is not int or not 1024 <= p['guard_port'] <= 65535:
        raise Refused('params.guard_port: the Compose GUARD_PORT (the Agent Host runtime configuration\'s guard_url port)')
    if not {'gateway', 'agent-host', 'guard', 'data', 'loopback'} <= set(p['sans']):
        raise Refused('params.sans: gateway, agent-host, guard, data and loopback SAN lists')
    return p


def generate(out, ca_dir, params_file, *, compose_app=COMPOSE_APP, compose_data=COMPOSE_DATA):
    out, ca_dir = Path(out), Path(ca_dir)
    if out.exists():
        raise Refused('output directory exists (secrets are never overwritten)')
    if not (ca_dir / 'ca.key').is_file() or not (ca_dir / 'bundle.crt').is_file():
        raise Refused('--ca-dir: the intermediate CA directory of issue_certs (ca.key, ca.crt, bundle.crt)')
    p = _params(params_file)
    entries = catalog(compose_app, compose_data)
    out.mkdir(mode=0o700, parents=True)
    work = out / '.issue'
    shared = {name: secrets.token_hex(32) for name in SHARED_VALUES}
    shared['webchat-provider'] = secrets.token_urlsafe(48)
    roles = sorted({_role(e['service'], e['path'].split('/', 1)[1]) for e in entries if e['kind'] == 'dsn'} | {'nexloop_backup', 'nexloop_backup_recorder'})
    passwords = {role: secrets.token_hex(24) for role in roles}
    signing = secrets.token_bytes(32)
    bundle = (ca_dir / 'bundle.crt').read_bytes()
    issued, tokens = {}, {}
    for e in entries:
        target = out / e['host'] / 'secrets' / e['path']
        service, name = e['path'].split('/', 1)
        kind = e['kind']
        if kind == 'config':
            e['generated'] = False
            continue
        if kind in ('server_cert', 'server_key', 'client_cert', 'client_key'):
            server = kind.startswith('server')
            stem = name.rsplit('.', 1)[0]
            cert_name = f'{service}-{stem}'
            if cert_name not in issued:
                if server:
                    issue_certs.leaf('server', ca_dir, work, cert_name, sans=p['sans'][SERVER_CERTS[(service, stem)]])
                else:
                    issue_certs.leaf('client', ca_dir, work, cert_name, cn=_role(service, _client_dsn(name)))
                issued[cert_name] = True
            source = work / (f'{cert_name}.chain.crt' if name.endswith('.crt') else f'{cert_name}.key')
            data = source.read_bytes()
        elif kind == 'ca_bundle':
            data = bundle
        elif kind == 'dsn':
            role = _role(service, name)
            # F1: each DSN uses the client certificate whose CN is its own role: <secret without _dsn>_pg_client_cert.
            prefix = (e['secret'] or '').removesuffix('_dsn')
            if f'{prefix}_pg_client_cert' not in {x['secret'] for x in entries}:
                raise Refused(f'no client certificate secret for {e["path"]}')
            cert_entry = next(x for x in entries if x['secret'] == f'{prefix}_pg_client_cert')
            if _role(cert_entry['service'], _client_dsn(cert_entry['path'].split('/', 1)[1])) != role:
                raise Refused(f'client certificate CN differs from the role of {e["path"]}')
            data = (f"host={p['data_host_name']} hostaddr={p['data_host_addr']} port={p['pg_port']} dbname={p['db_name']} user={role} "
                    f"password={passwords[role]} sslmode=verify-full sslrootcert=/run/secrets/pg_ca "
                    f"sslcert=/run/secrets/{prefix}_pg_client_cert sslkey=/run/secrets/{prefix}_pg_client_key\n")
        elif kind == 'socket_dsn':
            data = (f"host={p['nexloop_root']}/run/postgresql dbname={p['db_name']} user=nexloop_backup_recorder "
                    f"password={passwords['nexloop_backup_recorder']}\n")
        elif kind == 'pgpass':
            data = '/var/run/postgresql:5432:*:nexloop_backup:' + passwords['nexloop_backup'] + '\n'
            e['note'] = 'nexloop_backup line; for run sheet 2.4 the owner adds the nexloop_bootstrap line temporarily'
        elif kind == 'token':
            if name in ('provider-credential', 'webchat-provider-credential'):
                data = shared['webchat-provider']
            else:
                data = secrets.token_urlsafe(48)
                tokens[f"{e['host']}:{e['path']}"] = data
        elif kind == 'signing_key':
            data = signing
        elif kind == 'hex_key':
            stem = name.rsplit('.', 1)[0]
            data = shared['host-control'] if stem in ('host-control', 'transport') else shared['guard-transport'] if stem == 'guard-transport' \
                else secrets.token_hex(32)
        elif kind == 'host_runtime_config':
            # The shipped example with this stage's guard port (params guard_port = the Compose GUARD_PORT).
            config = json.loads(RUNTIME_EXAMPLE.read_text())
            config['guard_url'] = f"https://127.0.0.1:{p['guard_port']}/internal/v1/runtime/authorize"
            data = json.dumps(config, indent=1) + '\n'
        elif kind == 'host_model_configuration':
            data = json.dumps({'MODEL_PROVIDER': 'deepseek', 'MODEL_ID': 'deepseek-flash', 'MODEL_BASE_URL': 'https://api.deepseek.com',
                               'MODEL_CREDENTIALS_FILE': '/run/secrets/host_model_key', 'stage': True}, indent=1) + '\n'
        elif kind == 'host_model_tariff':
            data = TARIFF.read_bytes()
        elif kind == 'password':
            data = secrets.token_urlsafe(32)
        elif kind == 'valkey_acl':
            data = 'user default off\n'
        else:
            raise Refused('kind ' + kind)
        _write(target, data)
        e['generated'] = True
        e['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    shutil.rmtree(work, ignore_errors=True)
    management = out / 'management'
    _write(management / 'signing.key.hex', signing.hex())
    _write(management / 'role-passwords.sql', '-- Run once by the owner as nexloop_bootstrap in the bootstrap window (run sheet 2.5b): SCRAM verifiers only.\n'
           + ''.join(f'ALTER ROLE {role} PASSWORD \'{scram_verifier(pw)}\';\n' for role, pw in passwords.items()))
    _write(management / 'service-secrets.json', json.dumps(tokens, indent=1, sort_keys=True))
    manifest = {'schema': 'nexloop-stage-secrets/1', 'signing_key_id': p['signing_key_id'], 'roles': roles,
                'shared_values': SHARED_VALUES, 'files': entries,
                'management': ['signing.key.hex', 'role-passwords.sql', 'service-secrets.json']}
    _write(out / 'manifest.json', json.dumps(manifest, indent=1, sort_keys=True))
    return summary(manifest)


def summary(manifest):
    files = manifest['files']
    def count(**kw):
        return sum(all(f.get(k) == v for k, v in kw.items()) for f in files)
    return {'files': len(files), 'generated': count(generated=True), 'manual_or_phase_c': count(generated=False),
            'roles': len(manifest['roles']), 'by_host': {h: count(host=h) for h in ('app', 'data')}}


def check(out, *, uids=False, compose_app=COMPOSE_APP):
    """deploy_checks.secret_distribution for each host tree, and every generated file unchanged against the manifest."""
    from nexloop_eios import deploy_checks
    out = Path(out)
    manifest = json.loads((out / 'manifest.json').read_text())
    problems = []
    compose = yaml.safe_load(Path(compose_app).read_text())
    for host in ('app', 'data'):
        root = out / host / 'secrets'
        owner_map = None
        if uids:
            owner_map = {f['service']: int(f['owner']) for f in manifest['files'] if f['host'] == host and f['owner'].isdigit()}
        result = deploy_checks.secret_distribution(root, compose=compose if host == 'app' else None, uids=owner_map)
        problems += [f'{host}:{p}' for p in result['details']['secrets']['problems']]
    for f in manifest['files']:
        path = out / f['host'] / 'secrets' / f['path']
        if f.get('generated'):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != f['sha256']:
                problems.append(f"changed_or_missing:{f['host']}:{f['path']}")
    for host in ('app', 'data'):
        listed = {f['path'] for f in manifest['files'] if f['host'] == host and f.get('generated')}
        present = {str(p.relative_to(out / host / 'secrets')) for p in (out / host / 'secrets').rglob('*') if p.is_file()}
        problems += [f'unlisted:{host}:{p}' for p in sorted(present - listed)]
    pending = sorted(f"{f['host']}:{f['path']}" for f in manifest['files'] if not f.get('generated'))
    return {'ok': not problems, 'problems': problems, 'pending_manual_or_phase_c': pending}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('catalog')
    c.add_argument('--compose-app', type=Path, default=COMPOSE_APP)
    c.add_argument('--compose-data', type=Path, default=COMPOSE_DATA)
    g = sub.add_parser('generate')
    g.add_argument('--out', type=Path, required=True)
    g.add_argument('--ca-dir', type=Path, required=True)
    g.add_argument('--params', type=Path, required=True)
    k = sub.add_parser('check')
    k.add_argument('--out', type=Path, required=True)
    k.add_argument('--uids', action='store_true', help='also require the intended owners (on the host after install)')
    args = parser.parse_args(argv)
    try:
        if args.command == 'catalog':
            out = {'files': [{k: v for k, v in e.items() if k != 'secret'} for e in catalog(args.compose_app, args.compose_data)]}
            code = 0
        elif args.command == 'generate':
            out, code = generate(args.out, args.ca_dir, args.params), 0
        else:
            out = check(args.out, uids=args.uids)
            code = 0 if out['ok'] else 1
    except (Refused, issue_certs.Refused, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, (Refused, issue_certs.Refused)) else None}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())

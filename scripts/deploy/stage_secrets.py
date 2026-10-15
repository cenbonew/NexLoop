#!/usr/bin/env python3
"""NX-032 run sheet 1.6: the stage secret files, generated on the owner's management machine (docs/implementation/NX-032-stage-secrets.md).

    stage_secrets.py catalog  [--compose-app F] [--compose-data F]                  the inventory (paths, kinds, phases, owners)
    stage_secrets.py generate --out NEW_DIR --ca-dir CA_DIR --params PRIVATE_JSON    phase A files into a new private tree
    stage_secrets.py signing-key --out DIR --tenant UUID --signing-key-id ID --bootstrap-dsn-file F   one more Authority signing key
    stage_secrets.py setup-credentials --out DIR                                   setup/ credentials of business/catalog setup
    stage_secrets.py check    --out DIR [--uids]                                       secret_distribution and manifest digests
    stage_secrets.py metrics  --out DIR --ca-dir CA_DIR --params PRIVATE_JSON         G-METRICS-DSN: add the metrics client to an older tree
    stage_secrets.py reissue-role --out DIR --ca-dir CA_DIR --params PRIVATE_JSON --service S   one service's DSN and certificate for its role

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
import hashlib
import json
import os
import re
import secrets
import string
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
import issue_certs  # noqa: E402
from configurator_window import CONFIGURATOR, scram_verifier, write_private  # noqa: E402,F401
from configurator_window import configurator_window as _configurator_window  # noqa: E402

COMPOSE_APP = ROOT / 'deploy/stage/compose.app.example.yaml'
GRANTS = ROOT / 'deploy/authorization/service-grants.v1.json'
PRINCIPALS = ROOT / 'deploy/authorization/stage-principals.v1.json'
COMPOSE_DATA = ROOT / 'deploy/stage/compose.data.example.yaml'
# Owners (dispatcher 2026-10-14): backend services 10001, Agent Host 10020, gateway root (caddy runs as root with cap_drop ALL),
# DATA_HOST postgres/valkey/pgbackrest 999 (the image users), backup the nexloop-backup user (its uid is the host's).
OWNERS = {'agent-host': '10020', 'agent-host-model': '10020', 'gateway': '0', 'postgres': '999', 'valkey': '999', 'pgbackrest': '999', 'backup': 'nexloop-backup',
          'pgbackrest-server': 'nexloop-pgbackrest'}
BACKEND_OWNER = '10001'
# The PG role of each service DSN (the client certificate's CN must be the role: pg_hba clientcert=verify-full).
# runtime-worker: nexloop_domain_worker (2026-10-16: as nexloop_scheduler the activation path was refused - read_action_bundle,
# role_run_hint and most runtime functions are domain_worker's, 0063 revokes them from the scheduler - and every runtime test runs
# the worker as nexloop_domain_worker; tests/test_stage_secrets.py pins the mapping to the roles each entry point accepts).
PRIMARY_ROLE = {'api': 'nexloop_api', 'domain-worker': 'nexloop_domain_worker', 'action-worker': 'nexloop_action_worker',
                'webchat-delivery': 'nexloop_action_worker', 'runtime-worker': 'nexloop_domain_worker', 'message-relay': 'nexloop_api',
                'outbound-recorder': 'nexloop_api', 'claim-extraction-scheduler': 'nexloop_api'}
DSN_ROLE = {'identity-dsn': 'nexloop_identity', 'api-dsn': 'nexloop_api'}
CA_FILES = {'api-loopback-ca.crt', 'guard-ca.crt', 'db-ca.crt', 'host-ca.crt', 'ca.crt', 'webchat-provider-ca.crt', 'enterprise-provider-ca.crt'}
SERVER_CERTS = {('gateway', 'server'): 'gateway', ('agent-host', 'server'): 'agent-host', ('webchat-delivery', 'server'): 'loopback',
                ('api', 'loopback'): 'loopback', ('runtime-worker', 'guard'): 'guard', ('postgres', 'server'): 'data', ('valkey', 'server'): 'data',
                # M8: the pgBackRest TLS server on APP_HOST answers on APP_HOST's name and address (the gateway SANs).
                ('pgbackrest-server', 'server'): 'gateway'}
# M8: client certificates that are not a DSN's (CN = what pgbackrest-server.conf tls-server-auth admits).
CLIENT_CERTS = {('pgbackrest', 'client'): 'pgbackrest-data-client'}
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
              'pgbackrest/pgbackrest.conf', 'pgbackrest/pgpass', 'pgbackrest/client.crt', 'pgbackrest/client.key', 'pgbackrest/ca.crt',
              'backup/recorder.dsn',
              # G-METRICS-DSN: the read-only metrics role for `nexloop-doctor --backup` on DATA_HOST (owner nexloop-backup).
              'backup/metrics.dsn']
# M8: APP_HOST host files outside Compose, read by the systemd unit nexloop-pgbackrest-server (owner nexloop-pgbackrest).
APP_HOST_FILES = ['pgbackrest-server/server.crt', 'pgbackrest-server/server.key', 'pgbackrest-server/ca.crt']


# DATA_HOST socket DSN files (host-local socket directory, SCRAM): file name -> role.
SOCKET_DSN_ROLES = {'recorder.dsn': 'nexloop_backup_recorder', 'metrics.dsn': 'nexloop_metrics'}
# G-METRICS-DSN: the APP_HOST copy of the read-only metrics client (deploy-gate roles check, real_tenant_init verify). Not a service
# secret: written to management/metrics/ and installed by the owner under NEXLOOP_ROOT/ops/metrics (0700 root, files 0600 root).
METRICS_ROLE = 'nexloop_metrics'
METRICS_DIR = 'ops/metrics'
METRICS_FILES = ['metrics.dsn', 'metrics-pg-client.crt', 'metrics-pg-client.key', 'pg-ca.crt']


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
    if name.endswith(('.crt', '.key')) and (service, stem) in CLIENT_CERTS:
        return ('client_cert' if name.endswith('.crt') else 'client_key'), 'A', f'CN={CLIENT_CERTS[(service, stem)]}'
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
    if name in SOCKET_DSN_ROLES:
        return 'socket_dsn', 'A/B', f'role {SOCKET_DSN_ROLES[name]} over the DATA_HOST socket; password from role-passwords.sql'
    if name == 'pgpass':
        return 'pgpass', 'A/B', 'nexloop_bootstrap (run sheet 2.4) and nexloop_backup (2.5b) lines'
    if name == 'service-credential' or name.endswith('-credential'):
        if name == 'provider-credential' or name == 'webchat-provider-credential':
            return 'token', 'A', 'shared between the action worker and webchat-delivery'
        return 'token', 'A/B', 'random token; registered by the trusted configuration after bootstrap (see the document §3)'
    if name == 'signing.key':
        return 'signing_key', 'A/B', '32 bytes of random URL-safe ASCII (about 192 bits); the hex copy is registered by the trusted configuration'
    if name.endswith('.key'):
        return 'hex_key', 'A', '32 bytes as 64 lower-case hex' + (' (one value: host-control)' if stem in ('host-control', 'transport') else '')
    if name == 'bootstrap_password':
        return 'password', 'A', 'POSTGRES_PASSWORD_FILE of the first start'
    if name == 'valkey.acl':
        return 'valkey_acl', 'A', 'no application user yet: `user default off`'
    raise Refused(f'unclassified secret file {host}:{service}/{name}')


def catalog(compose_app=COMPOSE_APP, compose_data=COMPOSE_DATA):
    import yaml  # not in the backend image; register (the one-off container, document §3b) does not need it
    app = yaml.safe_load(Path(compose_app).read_text())
    entries = []
    for secret, spec in sorted((app.get('secrets') or {}).items()):
        path = re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec['file'])
        service, name = path.split('/', 1)
        entries.append({'host': 'app', 'path': path, 'secret': secret, 'service': service, 'owner': _owner(service)})
    for path in APP_HOST_FILES:
        service = path.split('/', 1)[0]
        entries.append({'host': 'app', 'path': path, 'secret': None, 'service': service, 'owner': _owner(service)})
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
# scram_verifier and the private file writer live with the configurator window (configurator_window.py).
_write = write_private


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
    roles = sorted({_role(e['service'], e['path'].split('/', 1)[1]) for e in entries if e['kind'] == 'dsn'} | {'nexloop_backup'}
                   | set(SOCKET_DSN_ROLES.values()))
    passwords = {role: secrets.token_hex(24) for role in roles}
    # 32 bytes for AuthoritySigner (exactly 32), and valid UTF-8 for the stage preflight of the API and the workers, which reads
    # every private file as text (2026-10-15 first start: random binary bytes were refused). URL-safe ASCII keeps ~192 bits.
    signing = ''.join(secrets.choice(string.ascii_letters + string.digits + '-_') for _ in range(32)).encode()
    bundle = (ca_dir / 'bundle.crt').read_bytes()
    issued, tokens, role_tokens = {}, {}, {}
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
                    issue_certs.leaf('client', ca_dir, work, cert_name, cn=CLIENT_CERTS.get((service, stem)) or _role(service, _client_dsn(name)))
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
            role = SOCKET_DSN_ROLES[name]
            data = (f"host={p['nexloop_root']}/run/postgresql dbname={p['db_name']} user={role} "
                    f"password={passwords[role]}\n")
        elif kind == 'pgpass':
            # libpq looks up a connection over its default socket directory (/var/run/postgresql in the image) as host
            # `localhost`, never as the directory path (2026-10-14 first start: "no password supplied").
            data = 'localhost:5432:*:nexloop_backup:' + passwords['nexloop_backup'] + '\n'
            e['note'] = 'nexloop_backup line; for run sheet 2.4 the owner adds the nexloop_bootstrap line temporarily'
        elif kind == 'token':
            if name in ('provider-credential', 'webchat-provider-credential'):
                data = shared['webchat-provider']
            else:
                # One token per principal (stage-principals.v1.json credential_files): several files of one principal are copies.
                role = credential_role(e['path'])
                if role is None:
                    raise Refused(f"credential file without a principal in {PRINCIPALS.name}: {e['path']}")
                data = role_tokens.setdefault(role, secrets.token_urlsafe(48))
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
    management = out / 'management'
    _metrics_client(management / 'metrics', ca_dir, work, bundle, p)
    shutil.rmtree(work, ignore_errors=True)
    _write(management / 'signing.key.hex', signing.hex())
    _write(management / 'role-passwords.sql', '-- Run once by the owner as nexloop_bootstrap in the bootstrap window (run sheet 2.5b): SCRAM verifiers only.\n'
           + ''.join(f'ALTER ROLE {role} PASSWORD \'{scram_verifier(pw)}\';\n' for role, pw in passwords.items()))
    _write(management / 'service-secrets.json', json.dumps(tokens, indent=1, sort_keys=True))
    manifest = {'schema': 'nexloop-stage-secrets/1', 'signing_key_id': p['signing_key_id'], 'roles': roles,
                'shared_values': SHARED_VALUES, 'files': entries,
                'management': ['signing.key.hex', 'role-passwords.sql', 'service-secrets.json', *(f'metrics/{n}' for n in METRICS_FILES)]}
    _write(out / 'manifest.json', json.dumps(manifest, indent=1, sort_keys=True))
    return summary(manifest)


def _metrics_client(target, ca_dir, work, bundle, p):
    """G-METRICS-DSN: the APP_HOST read-only metrics client. The certificate's CN is nexloop_metrics; pg_hba admits it from APP_HOST
    with certificate authentication (no password, `hostssl ... nexloop_metrics ... cert`). The DSN names the installed location
    NEXLOOP_ROOT/ops/metrics; nothing of it is mounted into a service."""
    issue_certs.leaf('client', ca_dir, work, 'metrics-pg-client', cn=METRICS_ROLE)
    installed = f"{p['nexloop_root']}/{METRICS_DIR}"
    _write(target / 'metrics-pg-client.crt', (work / 'metrics-pg-client.chain.crt').read_bytes())
    _write(target / 'metrics-pg-client.key', (work / 'metrics-pg-client.key').read_bytes())
    _write(target / 'pg-ca.crt', bundle)
    _write(target / 'metrics.dsn', f"host={p['data_host_name']} hostaddr={p['data_host_addr']} port={p['pg_port']} dbname={p['db_name']} "
           f"user={METRICS_ROLE} sslmode=verify-full sslrootcert={installed}/pg-ca.crt sslcert={installed}/metrics-pg-client.crt "
           f"sslkey={installed}/metrics-pg-client.key\n")


def add_metrics(out, ca_dir, params_file):
    """G-METRICS-DSN for a tree generated before the metrics client existed (generate writes it for new trees): management/metrics
    (APP_HOST, certificate only), data/secrets/backup/metrics.dsn (DATA_HOST socket, SCRAM) and management/metrics-role-password.sql,
    the one ALTER ROLE nexloop_metrics the owner runs in the bootstrap window. Nothing existing is overwritten; the manifest records
    the new files. No value is printed."""
    out, ca_dir = Path(out), Path(ca_dir)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if (out / 'management' / 'metrics').exists() or any(f['path'] == 'backup/metrics.dsn' for f in manifest['files']):
        raise Refused('the tree already holds the metrics client')
    p = _params(params_file)
    work = out / '.issue-metrics'
    try:
        _metrics_client(out / 'management' / 'metrics', ca_dir, work, (ca_dir / 'bundle.crt').read_bytes(), p)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    password = secrets.token_hex(24)
    target = out / 'data' / 'secrets' / 'backup' / 'metrics.dsn'
    _write(target, f"host={p['nexloop_root']}/run/postgresql dbname={p['db_name']} user={METRICS_ROLE} password={password}\n")
    _write(out / 'management' / 'metrics-role-password.sql', '-- Run once by the owner as nexloop_bootstrap in the bootstrap window: SCRAM verifier only.\n'
           f"ALTER ROLE {METRICS_ROLE} PASSWORD '{scram_verifier(password)}';\n")
    entry = {'host': 'data', 'path': 'backup/metrics.dsn', 'secret': None, 'service': 'backup', 'owner': _owner('backup')}
    entry['kind'], entry['phase'], entry['note'] = classify('data', 'backup', 'metrics.dsn')
    entry.update(generated=True, sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    manifest['files'].append(entry)
    manifest['roles'] = sorted(set(manifest['roles']) | {METRICS_ROLE})
    manifest['management'] = [*manifest['management'], *(f'metrics/{n}' for n in METRICS_FILES), 'metrics-role-password.sql']
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return {'added': ['management/metrics', 'data:backup/metrics.dsn', 'management/metrics-role-password.sql']}


def reissue_role(out, ca_dir, params_file, service):
    """Re-issue one service's DSN and client certificate for the role PRIMARY_ROLE now names (for example runtime-worker after its role
    changed): `dsn` names the role with that role's password (read from another DSN of the same role in this tree; one password
    per role), `pg-client.crt/.key` are a new certificate with CN = the role from the intermediate CA. Nothing else changes; the
    manifest records the new digests. Refused when the service's DSN already names the role (run once per role change). No value is
    printed."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    out, ca_dir = Path(out), Path(ca_dir)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    role = _role(service, 'dsn')
    entries = {f['path']: f for f in manifest['files'] if f['host'] == 'app' and f['service'] == service}
    paths = [f'{service}/dsn', f'{service}/pg-client.crt', f'{service}/pg-client.key']
    if any(p not in entries for p in paths):
        raise Refused(f'{service} has no dsn and pg-client certificate in this tree')
    secrets_root = out / 'app' / 'secrets'
    current = conninfo_to_dict((secrets_root / f'{service}/dsn').read_text().strip())
    if current.get('user') == role:
        raise Refused(f'{service}/dsn already names {role}')
    donors = [f for f in manifest['files'] if f['host'] == 'app' and f['kind'] == 'dsn' and f['service'] != service
              and _role(f['service'], f['path'].split('/', 1)[1]) == role and (secrets_root / f['path']).is_file()]
    if not donors:
        raise Refused(f'no other DSN of {role} in this tree to take its password from')
    password = conninfo_to_dict((secrets_root / donors[0]['path']).read_text().strip()).get('password')
    if not password:
        raise Refused(f'the DSN of {role} carries no password')
    _params(params_file)  # the tree's private params must still be valid (same host, port and database as the DSN below)
    work = out / f'.reissue-{service}'
    try:
        issue_certs.leaf('client', ca_dir, work, f'{service}-pg-client', cn=role)
        new = {f'{service}/pg-client.crt': (work / f'{service}-pg-client.chain.crt').read_bytes(),
               f'{service}/pg-client.key': (work / f'{service}-pg-client.key').read_bytes(),
               f'{service}/dsn': (make_conninfo(**{**current, 'user': role, 'password': password}) + '\n').encode()}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for path, data in new.items():
        target = secrets_root / path
        staged = target.with_name(target.name + '.reissue')
        _write(staged, data)
        os.replace(staged, target)  # the management tree, not a running bind mount; on the host the files are installed anew
        entries[path]['sha256'] = hashlib.sha256(data).hexdigest()
        if path.endswith(('.crt', '.key')):
            entries[path]['note'] = f'CN={role}'
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return {'service': service, 'role': role, 'reissued': paths}


def summary(manifest):
    files = manifest['files']
    def count(**kw):
        return sum(all(f.get(k) == v for k, v in kw.items()) for f in files)
    return {'files': len(files), 'generated': count(generated=True), 'manual_or_phase_c': count(generated=False),
            'roles': len(manifest['roles']), 'by_host': {h: count(host=h) for h in ('app', 'data')}}


# ---- phase B: registration (trusted configuration, configurator role only) ---------------------------------------------------
STAGE_SCHEMA = 'nexloop-stage-principals/1'
# Resource type -> operations a stage principal may hold (the service grants manifest allows action execute and object_type read
# only; the Run holders and the effect executor also need what ADR-020 §3 keeps out of that manifest).
STAGE_ALLOWED = {'action': {'execute'}, 'object_type': {'read'}, 'artifact': {'create', 'read', 'delete'}}


def _stage_principals(path=PRINCIPALS):
    value = json.loads(Path(path).read_text())
    if type(value) is not dict or value.get('schema_version') != STAGE_SCHEMA:
        raise Refused('stage principals: schema_version ' + STAGE_SCHEMA)
    return value


def credential_role(path, principals=None):
    """The principal role of one credential file (`<service>/<file>` below secrets/), or None."""
    return (principals or _stage_principals())['credential_files'].get(path)


def merged_manifest(grants_path=GRANTS, principals_path=PRINCIPALS):
    """service-grants.v1.json (validated as published) plus the stage principals and their grants (validated here)."""
    from nexloop_eios import service_grants as G
    base = G.load(grants_path)
    stage = _stage_principals(principals_path)
    effect_roles = set(stage['effect_roles'])
    roles = {p['role'] for p in base['principals']}
    identifiers = {p[k] for p in base['principals'] for k in ('subject_id', 'principal_id', 'credential_id', 'credential_reference')}
    for item in stage['principals']:
        extra = item.get(G.EXTRA_SCOPES, [])
        if set(item) - {G.EXTRA_SCOPES} != G.PRINCIPAL or item['role'] in roles or item['database_role'] not in G.DATABASE_ROLES \
                or (G.EXTRA_SCOPES in item and (type(extra) is not list or not extra or len(set(extra)) != len(extra) or not set(extra) <= G.ALLOWED_EXTRA_SCOPES)):
            raise Refused('stage principal shape, duplicate role or database role: ' + str(item.get('role')))
        for k in ('subject_id', 'principal_id', 'credential_id', 'credential_reference'):
            if not G._ID.fullmatch(item[k]) or item[k] in identifiers:
                raise Refused(f"stage principal identifier {k} duplicate or invalid: {item['role']}")
            identifiers.add(item[k])
        roles.add(item['role'])
    seen = {(g['principal'], g['resource_id']) for g in base['grants']}
    for g in stage['grants']:
        if set(g) != G.GRANT or g['principal'] not in roles:
            raise Refused('stage grant shape or principal: ' + str(g.get('principal')))
        if g['resource_type'] not in STAGE_ALLOWED or not set(g['operations']) <= STAGE_ALLOWED[g['resource_type']] or not g['operations']:
            raise Refused(f"stage grant resource type or operation: {g['principal']} {g['resource_id']}")
        effect = any(m in g['resource_id'] for m in G.FORBIDDEN_MARKERS)
        if effect and g['principal'] not in effect_roles:
            raise Refused(f"effect Action granted outside effect_roles: {g['principal']} {g['resource_id']}")
        if (g['principal'], g['resource_id']) in seen:
            raise Refused(f"duplicate grant: {g['principal']} {g['resource_id']}")
        seen.add((g['principal'], g['resource_id']))
    for role in {p['role'] for p in stage['principals']}:
        if not any(g['principal'] == role for g in stage['grants']):
            raise Refused('stage principal without grant: ' + role)
    files = stage['credential_files']
    if not set(files.values()) <= roles or roles - set(files.values()):
        raise Refused('credential_files must name every principal and only known ones')
    for rule in stage.get('source_message_read_rules', []):
        if set(rule) != {'principal', 'fields', 'valid_until', 'purpose', 'source_task'} or rule['principal'] not in {p['role'] for p in stage['principals']}:
            raise Refused('source_message_read_rules shape')
    merged = {**base, 'principals': base['principals'] + stage['principals'], 'grants': base['grants'] + stage['grants']}
    return merged, stage


def _message_read_facts(manifest, stage, tenant):
    from nexloop_eios.message_read import MessageReadRule
    ids = {p['role']: p['principal_id'] for p in manifest['principals']}
    return [('message_read_rule', [ids[r['principal']]], MessageReadRule(tenant_id=tenant, principal_id=ids[r['principal']], type_name='Message',
             fields=tuple(sorted(r['fields'])), active=True, valid_until=r['valid_until'])) for r in stage.get('source_message_read_rules', [])]


def _apply_extra_facts(facts, tenant, *, database_url_file, signing_key_file, signing_key_id):
    """Write trusted configuration facts the service grants compiler does not produce (message_read_rule), only when they
    differ from the current ones (idempotent); configurator role through apply_manifest_with_secrets."""
    import uuid
    from nexloop_eios import service_grants as G
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    current = G.inventory(database_url_file, tenant)
    stored = {(r['kind'], tuple(r['key'])): r['payload'] for r in current['facts']}
    writes = [(k, key, f) for k, key, f in facts if stored.get((k, tuple(key))) != json.loads(f.model_dump_json())]
    if not writes:
        return 0
    body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'] or 'active',
            'object_types': [], 'actions': [], 'functions': [], 'service_credentials': [], 'browser_applications': [], 'browser_business_applications': [],
            'browser_rate_policies': [], 'identity_allowances': [],
            'authority_facts': [{'kind': k, 'key': list(key), 'payload': json.loads(f.model_dump_json())} for k, key, f in writes]}
    body['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-extra:' + str(current['authority_revision']) + ':' + canonical_payload(body)))
    apply_manifest_with_secrets(body, database_url_file=database_url_file, signing_key_file=signing_key_file, signing_key_id=signing_key_id, service_secrets={})
    return len(writes)


# G1 (run-34 §2): the browser login chain of the stage api. Values follow the readers, not names:
# - control.nexloop_read_browser_identity (0021) needs an active control.nexloop_browser_applications row per application;
# - browser_http.login_work consumes the rate policy 'local_login' (operator nexloop_identity) before authenticating (0032);
# - control.nexloop_initial_identity_create (0162) needs an enabled allowance per application for initial_identity --create.
# browser_business_applications is read only by the business authority snapshot (0045), not by login: not registered here.
LOGIN_RATE = {'action': 'local_login', 'maximum_attempts': 20, 'window_seconds': 60}
_APP_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


def browser_entries(*, browser_application_id, workbench_application_id, seal_digest, webchat_accounts=10, workbench_accounts=5,
                    operator_label='stage-owner'):
    """The 0050 browser categories for the WebChat application and the workbench application (http_api requires two ids)."""
    if browser_application_id == workbench_application_id or not all(_APP_ID.fullmatch(x or '') for x in (browser_application_id, workbench_application_id)):
        raise Refused('two different browser application ids are required (http_api: workbench id differs from the browser id)')
    if not re.fullmatch('[0-9a-f]{64}', seal_digest or '') or not _APP_ID.fullmatch(operator_label or ''):
        raise Refused('seal digest (sha256 of the initial identity idempotency key) and operator label are required')
    if not all(type(n) is int and 1 <= n <= 100 for n in (webchat_accounts, workbench_accounts)):
        raise Refused('maximum accounts per application: 1..100')
    apps = [(browser_application_id, webchat_accounts), (workbench_application_id, workbench_accounts)]
    return {'browser_applications': [{'application_id': a, 'active': True} for a, _ in apps],
            'browser_rate_policies': [{**LOGIN_RATE, 'operator_principal_id': 'nexloop_identity', 'active': True}],
            'identity_allowances': [{'application_id': a, 'enabled': True, 'maximum_accounts': n, 'operator_label': operator_label,
                                     'idempotency_key_digest': seal_digest} for a, n in apps]}


def browser_state(connection, tenant):
    """Read-only current values of the managed browser rows (for the idempotency check; the window's bootstrap session)."""
    apps = {a: act for a, act in connection.execute('select application_id, active from control.nexloop_browser_applications where tenant_id=%s', (tenant,))}
    rates = {a: (m, w, act) for a, m, w, act in connection.execute(
        "select action, maximum_attempts, window_seconds, active from control.nexloop_browser_rate_policies where tenant_id=%s and operator_principal_id='nexloop_identity'", (tenant,))}
    allowances = {a: (e, m, label, d) for a, e, m, label, d in connection.execute(
        'select application_id, enabled, maximum_accounts, operator_label, idempotency_key_digest from control.nexloop_initial_identity_allowances where tenant_id=%s', (tenant,))}
    return {'applications': apps, 'rates': rates, 'allowances': allowances}


def _browser_pending(entries, state):
    """Entries that differ from the current rows; an allowance that exists with other values is refused (0050 never resets it)."""
    if state is None:
        return entries, []
    pending = {'browser_applications': [e for e in entries['browser_applications'] if state['applications'].get(e['application_id']) is not e['active']],
               'browser_rate_policies': [e for e in entries['browser_rate_policies']
                                         if state['rates'].get(e['action']) != (e['maximum_attempts'], e['window_seconds'], e['active'])],
               'identity_allowances': []}
    conflicts = []
    for e in entries['identity_allowances']:
        current = state['allowances'].get(e['application_id'])
        wanted = (e['enabled'], e['maximum_accounts'], e['operator_label'], e['idempotency_key_digest'])
        if current is None:
            pending['identity_allowances'].append(e)
        elif current != wanted:
            conflicts.append(e['application_id'])
    return pending, conflicts


def apply_browser(out, *, tenant, entries, database_url_file, signing_key_id, state=None, dry_run=False):
    """Register the browser applications, the login rate policy and the initial identity allowances (0050, configurator only).
    With `state` (the current rows) only differing entries are sent and an unchanged configuration publishes nothing."""
    import uuid
    from nexloop_eios import service_grants as G
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    pending, conflicts = _browser_pending(entries, state)
    summary = {'applications': [e['application_id'] for e in entries['browser_applications']], 'rate_policies': [e['action'] for e in entries['browser_rate_policies']],
               'allowances': {e['application_id']: e['maximum_accounts'] for e in entries['identity_allowances']}}
    if conflicts:
        raise Refused('identity allowance exists with other values (never reset): ' + ', '.join(sorted(conflicts)))
    if dry_run:
        return {'dry_run': True, **summary}
    if not any(pending.values()):
        return {'changed': False, **summary}
    current = G.inventory(database_url_file, tenant)
    if not current['tenant_present']:
        raise Refused('tenant not configured yet: run register first')
    body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
            'object_types': [], 'actions': [], 'functions': [], 'authority_facts': [], 'service_credentials': [], 'browser_business_applications': [],
            **pending}
    body['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-browser:' + str(current['authority_revision']) + ':' + canonical_payload(body)))
    result = apply_manifest_with_secrets(body, database_url_file=database_url_file, signing_key_file=Path(out) / 'management' / 'signing.key.hex',
                                         signing_key_id=signing_key_id, service_secrets={})
    return {'changed': True, 'authority_revision': result['authority_revision'], **summary}


def _reference_secrets(out, manifest, stage):
    """credential_reference -> token from management/service-secrets.json (file -> token); a principal's files share one token."""
    tokens = json.loads((Path(out) / 'management' / 'service-secrets.json').read_text())
    by_role = {}
    for key, token in tokens.items():
        path = key.split(':', 1)[1]
        role = stage['credential_files'].get(path)
        if role is None:
            raise Refused('a generated credential file has no principal: ' + path)
        if by_role.setdefault(role, token) != token:
            raise Refused('the credential files of one principal must hold the same token: ' + role)
    reference = {p['role']: p['credential_reference'] for p in manifest['principals']}
    missing = sorted(set(reference) - set(by_role))
    if missing:
        raise Refused('no token generated for principals: ' + ','.join(missing))
    return {reference[role]: token for role, token in by_role.items()}


def register(out, *, tenant, database_url_file, signing_key_id, credential_expires_at, dry_run=False,
             grants_path=GRANTS, principals_path=PRINCIPALS):
    """Phase B: the service principals of the stage, their grants and their credentials through the trusted configuration
    (service_grants.plan/apply: 0050 configurator definer, deterministic facts, idempotent). Tokens are read from
    management/service-secrets.json and never printed; the signing key is the management hex copy."""
    from datetime import datetime
    from nexloop_eios import service_grants as G
    manifest, stage = merged_manifest(grants_path, principals_path)
    expires = datetime.fromisoformat(credential_expires_at) if isinstance(credential_expires_at, str) else credential_expires_at
    if dry_run:
        return {'dry_run': True, 'principals': sorted(p['role'] for p in manifest['principals']),
                'grants': sorted(f"{g['principal']} {g['resource_type']} {g['resource_id']} {','.join(g['operations'])}" for g in manifest['grants']),
                'message_read_rules': [r['principal'] for r in stage.get('source_message_read_rules', [])],
                'credential_files': len(stage['credential_files'])}
    secrets_map = _reference_secrets(out, manifest, stage)
    # service_grants.apply re-validates a manifest whose grants follow the latest Action version, and that validation refuses the
    # effect Actions of the stage roles; resolve those grants first (same lineage, configurator role) and apply the resolved list.
    if any(g.get(G.FOLLOW) for g in manifest['grants']):
        lineage = G.action_lineage(database_url_file, tenant)
        derived, refused = G.resolve_follow(manifest, lineage)
        if refused:
            raise G.ServiceGrantsRejected('follow_latest_refused')
        manifest = {**manifest, 'grants': [{k: v for k, v in g.items() if k != G.FOLLOW} for g in manifest['grants'] + derived]}
    management = Path(out) / 'management'
    # A private temporary directory: management/ may be mounted read-only (document §3b, one-off container with --tmpfs /tmp).
    import tempfile
    scratch = Path(tempfile.mkdtemp(prefix='nexloop-register-'))
    staged = scratch / 'register-secrets.json'
    _write(staged, json.dumps(secrets_map))
    try:
        report = G.apply(manifest, tenant, database_url_file=database_url_file, signing_key_file=management / 'signing.key.hex',
                         signing_key_id=signing_key_id, service_secrets_file=staged, credential_expires_at=expires,
                         owner_restrictions=G.load_owner_restrictions(ROOT / 'deploy/authorization/owner-property-restrictions.json'))
    finally:
        staged.unlink(missing_ok=True)
        scratch.rmdir()
    extra = _apply_extra_facts(_message_read_facts(manifest, stage, tenant), tenant, database_url_file=database_url_file,
                               signing_key_file=management / 'signing.key.hex', signing_key_id=signing_key_id)
    return {'changed': report['changed'] or bool(extra), 'message_read_rules_written': extra, 'credentials_created': sorted(report['credentials_created']),
            'grants_added': len(report['grants_added']), 'grants_revoked': len(report['grants_revoked']),
            'extra_grants_outside_manifest': len(report['extra_grants_outside_manifest']), 'authority_revision': report['authority_revision']}


def configurator_window(bootstrap_dsn_file):
    """The configurator login window over the bootstrap socket (document §3b; configurator_window.py)."""
    return _configurator_window(bootstrap_dsn_file, refused=Refused)


def check(out, *, uids=False, compose_app=COMPOSE_APP):
    """deploy_checks.secret_distribution for each host tree, and every generated file unchanged against the manifest."""
    from nexloop_eios import deploy_checks
    out = Path(out)
    manifest = json.loads((out / 'manifest.json').read_text())
    problems = []
    import yaml
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
    problems += metrics_problems(out / 'management' / 'metrics')
    pending = sorted(f"{f['host']}:{f['path']}" for f in manifest['files'] if not f.get('generated'))
    return {'ok': not problems, 'problems': problems, 'pending_manual_or_phase_c': pending}


def metrics_problems(directory):
    """G-METRICS-DSN: the APP_HOST metrics client is complete, owner-only (0600 files, 0700 directory) and certificate-only:
    verify-full, user nexloop_metrics, no password."""
    import stat
    problems = []
    if not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
        return ['metrics:directory']
    for name in METRICS_FILES:
        path = directory / name
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            problems.append('metrics:file:' + name)
    if (directory / 'metrics.dsn').is_file():
        fields = dict(item.split('=', 1) for item in (directory / 'metrics.dsn').read_text().split())
        if fields.get('sslmode') != 'verify-full' or fields.get('user') != METRICS_ROLE or 'password' in fields:
            problems.append('metrics:dsn')
    return problems


def browser_main(args):
    import hashlib as _h

    import psycopg
    from nexloop_eios.trusted_configuration import read_private_text
    seal = bytes.fromhex(read_private_text(args.identity_seal_file, maximum=128).strip())
    if len(seal) != 32:
        raise Refused('--identity-seal-file: 32 bytes as 64 hex')
    entries = browser_entries(browser_application_id=args.browser_application_id, workbench_application_id=args.workbench_application_id,
                              seal_digest=_h.sha256(seal).hexdigest(), webchat_accounts=args.webchat_accounts,
                              workbench_accounts=args.workbench_accounts, operator_label=args.operator_label)
    if args.dry_run:
        return apply_browser(args.out, tenant=args.tenant, entries=entries, database_url_file=None, signing_key_id=None, dry_run=True)
    if not args.signing_key_id or bool(args.configurator_dsn_file) == bool(args.bootstrap_dsn_file):
        raise Refused('browser needs one of --configurator-dsn-file / --bootstrap-dsn-file and --signing-key-id (or --dry-run)')
    if args.configurator_dsn_file:
        return apply_browser(args.out, tenant=args.tenant, entries=entries, database_url_file=args.configurator_dsn_file, signing_key_id=args.signing_key_id)
    # Window mode: the current rows are read (read-only transaction) through the bootstrap socket, so an unchanged configuration
    # publishes nothing; the writes go through the configurator's own login only.
    with psycopg.connect(read_private_text(args.bootstrap_dsn_file, maximum=16384).strip(), connect_timeout=5) as reader:
        reader.execute('set transaction read only')
        state = browser_state(reader, args.tenant)
    if not any(_browser_pending(entries, state)[0].values()) and not _browser_pending(entries, state)[1]:
        return apply_browser(args.out, tenant=args.tenant, entries=entries, database_url_file=None, signing_key_id=args.signing_key_id, state=state)
    with configurator_window(args.bootstrap_dsn_file) as dsn:
        return apply_browser(args.out, tenant=args.tenant, entries=entries, database_url_file=dsn, signing_key_id=args.signing_key_id, state=state)


# Signing key rotation (run-34, 2026-10-15): register one more Authority signing key without changing any authority. 0050/0162
# control.nexloop_configure_manifest inserts (key_id, material, active) on conflict(key_id) do nothing and requires that exact
# row afterwards; every list of the manifest is processed item by item, so a manifest whose lists are all empty writes and revokes
# nothing else (it only appends a publication row and advances authority_revision by one). Existing keys stay active.
def signing_key_state(connection, key_id):
    """The current row of `key_id` (read-only): (material, active) or None."""
    row = connection.execute('select key_material, active from authz.nexloop_authority_signing_keys where key_id=%s', (key_id,)).fetchone()
    return None if row is None else (bytes(row[0]), row[1])


def register_signing_key(out, *, tenant, signing_key_id, database_url_file, state=None):
    """Publish an empty manifest carrying management/signing.key.hex under `signing_key_id`. With `state` (the current row of that
    key id) an already registered identical key publishes nothing; a key id held by other material is refused."""
    import uuid
    from nexloop_eios import service_grants as G
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets, read_private_text
    hex_file = Path(out) / 'management' / 'signing.key.hex'
    material = bytes.fromhex(read_private_text(hex_file, maximum=128))
    if len(material) != 32 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', signing_key_id or ''):
        raise Refused('signing key: 32 bytes in management/signing.key.hex and a key id without colons')
    if state is not None:
        if state[0] != material:
            raise Refused('the key id is registered with other key material (key ids are immutable): choose a new key id')
        if state[1]:
            return {'changed': False, 'signing_key_id': signing_key_id}
        raise Refused('the key id is registered but inactive; 0050 never reactivates a key')
    current = G.inventory(database_url_file, tenant)
    if not current['tenant_present']:
        raise Refused('tenant not configured yet: run register first')
    body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
            'object_types': [], 'actions': [], 'functions': [], 'authority_facts': [], 'service_credentials': [], 'browser_applications': [],
            'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
    body['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':stage-signing-key:' + signing_key_id + ':' + hashlib.sha256(material).hexdigest()))
    result = apply_manifest_with_secrets(body, database_url_file=database_url_file, signing_key_file=hex_file, signing_key_id=signing_key_id, service_secrets={})
    return {'changed': True, 'signing_key_id': signing_key_id, 'authority_revision': result['authority_revision']}


def signing_key_main(args):
    import psycopg
    from nexloop_eios.trusted_configuration import read_private_text
    if bool(args.configurator_dsn_file) == bool(args.bootstrap_dsn_file):
        raise Refused('signing-key needs one of --configurator-dsn-file / --bootstrap-dsn-file')
    if args.configurator_dsn_file:
        return register_signing_key(args.out, tenant=args.tenant, signing_key_id=args.signing_key_id, database_url_file=args.configurator_dsn_file)
    # Window mode: the current row is read (read-only transaction) through the bootstrap socket; an identical registered key opens
    # no window and publishes nothing.
    with psycopg.connect(read_private_text(args.bootstrap_dsn_file, maximum=16384).strip(), connect_timeout=5) as reader:
        reader.execute('set transaction read only')
        state = signing_key_state(reader, args.signing_key_id)
    if state is not None:
        return register_signing_key(args.out, tenant=args.tenant, signing_key_id=args.signing_key_id, database_url_file=None, state=state)
    with configurator_window(args.bootstrap_dsn_file) as dsn:
        return register_signing_key(args.out, tenant=args.tenant, signing_key_id=args.signing_key_id, database_url_file=dsn)


# Setup credentials (NX-032 A1 design §4.4, T6): business_setup and catalog_setup run once per consumer as their own service
# principals (stage-principals.v1.json manifest_version 2). No Compose service mounts their credentials, so they are not in the
# phase A catalog: this adds them to an existing tree below setup/ (outside app/ and data/, which `check` keeps exact) and to
# management/service-secrets.json, where register reads every principal's token. A principal that already has a token (the effect
# executor, the relay Source) gets a copy of it, never a second one. Idempotent: present entries are kept as they are.
SETUP_PREFIX = 'setup/'


def setup_credentials(out, principals_path=PRINCIPALS):
    import os
    out = Path(out)
    stage = _stage_principals(principals_path)
    store = out / 'management' / 'service-secrets.json'
    tokens = json.loads(store.read_text())
    by_role = {}
    for key, token in tokens.items():
        role = stage['credential_files'].get(key.split(':', 1)[1])
        if role is not None:
            by_role.setdefault(role, token)
    wanted = sorted(path for path in stage['credential_files'] if path.startswith(SETUP_PREFIX))
    added = []
    for path in wanted:
        key, target = 'setup:' + path, out / path
        if key in tokens:
            if not target.is_file() or target.read_text() != tokens[key]:
                raise Refused('setup credential file differs from service-secrets.json: ' + path)
            continue
        if target.exists():
            raise Refused('setup credential file exists without a service-secrets.json entry: ' + path)
        role = stage['credential_files'][path]
        token = by_role.setdefault(role, secrets.token_urlsafe(48))
        _write(target, token)
        tokens[key] = token
        added.append(path)
    if added:
        # Replace the store atomically (0600, same directory); earlier entries are unchanged.
        temporary = store.with_name('.service-secrets.json.new')
        temporary.unlink(missing_ok=True)
        _write(temporary, json.dumps(tokens, indent=1, sort_keys=True))
        os.replace(temporary, store)
    return {'changed': bool(added), 'added': added, 'setup_files': len(wanted)}


CATALOG = {'capabilities_path': ROOT / 'deploy/configuration/capabilities.v1.json', 'system_actions_path': ROOT / 'deploy/configuration/system-actions.v1.json',
           'business_actions_path': ROOT / 'deploy/configuration/business-actions.v1.json',
           'system_types_path': ROOT / 'deploy/ontology/system-object-types.v1.json', 'business_types_path': ROOT / 'deploy/ontology/business-object-types.v1.json'}
# Worker configuration files that need no database read (run-41 §4.1, design §4): the shipped settings and an empty match
# configuration. recall-types.json and relay-run-profile.json read the published state: scripts/deploy/tenant_config.py (T6).
WORKER_CONFIG = {'commitments.json': 'deploy/configuration/commitments.v1.json', 'commercial.json': 'deploy/configuration/commercial.v1.json',
                 'plan-reevaluation.json': 'deploy/configuration/plan-reevaluation.v2.json', 'reply-guarantee.json': 'deploy/configuration/reply-guarantee.v3.json'}


def emit_worker_config(directory):
    directory = Path(directory)
    written = []
    for name, source in WORKER_CONFIG.items():
        _write(directory / name, (ROOT / source).read_text())
        written.append(name)
    _write(directory / 'claim-match.json', '{}\n')
    return written + ['claim-match.json']


def publish_main(args):
    from nexloop_eios import catalog_publish as P
    compiled = P.compile_catalog(args.tenant, **CATALOG)
    counts = {'object_types': len(compiled['object_types']), 'actions': len(compiled['actions']), 'functions': len(compiled['functions'])}
    emitted = emit_worker_config(args.emit_config) if args.emit_config else None
    if args.dry_run and not (args.configurator_dsn_file or args.bootstrap_dsn_file):
        return {'dry_run': True, 'compared': False, **counts, 'emitted': emitted}
    if not args.signing_key_id or bool(args.configurator_dsn_file) == bool(args.bootstrap_dsn_file):
        raise Refused('publish needs one of --configurator-dsn-file / --bootstrap-dsn-file and --signing-key-id (or --dry-run alone)')
    signing = Path(args.out) / 'management' / 'signing.key.hex'

    def run(dsn):
        return P.publish(compiled, args.tenant, database_url_file=dsn, signing_key_file=signing, signing_key_id=args.signing_key_id, dry_run=args.dry_run)
    if args.configurator_dsn_file:
        result = run(args.configurator_dsn_file)
    else:
        with configurator_window(args.bootstrap_dsn_file) as dsn:
            result = run(dsn)
    return {**result, 'emitted': emitted}


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
    r = sub.add_parser('register')
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--tenant', required=True)
    r.add_argument('--configurator-dsn-file', type=Path)
    r.add_argument('--bootstrap-dsn-file', type=Path, help='open a configurator login window over this bootstrap socket DSN (document §3b)')
    r.add_argument('--signing-key-id')
    r.add_argument('--credential-expires-at')
    r.add_argument('--dry-run', action='store_true')
    b = sub.add_parser('browser', help='G1: browser applications, login rate policy and initial identity allowances (0050)')
    b.add_argument('--out', type=Path, required=True)
    b.add_argument('--tenant', required=True)
    b.add_argument('--browser-application-id', required=True)
    b.add_argument('--workbench-application-id', required=True)
    b.add_argument('--identity-seal-file', type=Path, required=True, help='the 32-byte hex idempotency key initial_identity --create will use')
    b.add_argument('--webchat-accounts', type=int, default=10)
    b.add_argument('--workbench-accounts', type=int, default=5)
    b.add_argument('--operator-label', default='stage-owner')
    b.add_argument('--configurator-dsn-file', type=Path)
    b.add_argument('--bootstrap-dsn-file', type=Path)
    b.add_argument('--signing-key-id')
    b.add_argument('--dry-run', action='store_true')
    sk = sub.add_parser('signing-key', help='register management/signing.key.hex under a new key id (empty manifest; no authority change)')
    sk.add_argument('--out', type=Path, required=True)
    sk.add_argument('--tenant', required=True)
    sk.add_argument('--signing-key-id', required=True)
    sk.add_argument('--configurator-dsn-file', type=Path)
    sk.add_argument('--bootstrap-dsn-file', type=Path)
    sc = sub.add_parser('setup-credentials', help='A3: the credential files of business_setup/catalog_setup below setup/ (before register)')
    sc.add_argument('--out', type=Path, required=True)
    q = sub.add_parser('publish', help='A1: the tenant catalog (object types, Actions, Functions) through 0050')
    q.add_argument('--out', type=Path, required=True)
    q.add_argument('--tenant', required=True)
    q.add_argument('--configurator-dsn-file', type=Path)
    q.add_argument('--bootstrap-dsn-file', type=Path)
    q.add_argument('--signing-key-id')
    q.add_argument('--dry-run', action='store_true', help='compare only; without a DSN file only the compiled counts')
    q.add_argument('--emit-config', type=Path, help='write the worker configuration files the catalog makes valid (design §4)')
    rr = sub.add_parser('reissue-role', help='re-issue one service\'s DSN and client certificate for its current PRIMARY_ROLE')
    rr.add_argument('--out', type=Path, required=True)
    rr.add_argument('--ca-dir', type=Path, required=True)
    rr.add_argument('--params', type=Path, required=True)
    rr.add_argument('--service', required=True)
    m = sub.add_parser('metrics', help='G-METRICS-DSN: add the read-only metrics client to a tree generated before it existed')
    m.add_argument('--out', type=Path, required=True)
    m.add_argument('--ca-dir', type=Path, required=True)
    m.add_argument('--params', type=Path, required=True)
    k = sub.add_parser('check')
    k.add_argument('--out', type=Path, required=True)
    k.add_argument('--uids', action='store_true', help='also require the intended owners (on the host after install)')
    args = parser.parse_args(argv)
    try:
        if args.command == 'catalog':
            out = {'files': [{k: v for k, v in e.items() if k != 'secret'} for e in catalog(args.compose_app, args.compose_data)]}
            code = 0
        elif args.command == 'register':
            if not args.dry_run and (not (args.signing_key_id and args.credential_expires_at)
                                     or bool(args.configurator_dsn_file) == bool(args.bootstrap_dsn_file)):
                raise Refused('register needs one of --configurator-dsn-file / --bootstrap-dsn-file, --signing-key-id and --credential-expires-at (or --dry-run)')
            def run(dsn):
                return register(args.out, tenant=args.tenant, database_url_file=dsn, signing_key_id=args.signing_key_id,
                                credential_expires_at=args.credential_expires_at, dry_run=args.dry_run)
            if args.bootstrap_dsn_file and not args.dry_run:
                with configurator_window(args.bootstrap_dsn_file) as dsn:
                    out = run(dsn)
            else:
                out = run(args.configurator_dsn_file)
            code = 0
        elif args.command == 'browser':
            out, code = browser_main(args), 0
        elif args.command == 'signing-key':
            out, code = signing_key_main(args), 0
        elif args.command == 'setup-credentials':
            out, code = setup_credentials(args.out), 0
        elif args.command == 'publish':
            out, code = publish_main(args), 0
        elif args.command == 'reissue-role':
            out, code = reissue_role(args.out, args.ca_dir, args.params, args.service), 0
        elif args.command == 'metrics':
            out, code = add_metrics(args.out, args.ca_dir, args.params), 0
        elif args.command == 'generate':
            out, code = generate(args.out, args.ca_dir, args.params), 0
        else:
            out = check(args.out, uids=args.uids)
            code = 0 if out['ok'] else 1
    except Exception as error:
        from nexloop_eios.service_grants import ServiceGrantsRejected
        if not isinstance(error, (Refused, issue_certs.Refused, ServiceGrantsRejected, OSError, ValueError, KeyError)):
            raise
        if isinstance(error, ServiceGrantsRejected):
            print(json.dumps({'ok': False, 'error': 'ServiceGrantsRejected', 'detail': error.reason}))
            return 2
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, (Refused, issue_certs.Refused)) or type(error).__name__ in ('CatalogPublishRefused', 'CatalogManifestRejected') else None}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())

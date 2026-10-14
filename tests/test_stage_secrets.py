"""NX-032 run sheet 1.6: scripts/deploy/stage_secrets.py builds the phase A secret tree from the Compose files and the existing
intermediate CA; the tree passes deploy_checks.secret_distribution, the Agent Host directory holds no service secret, every file
a Compose file mounts is in the catalog, no value appears in the manifest, and the role passwords leave the management machine
as SCRAM verifiers only. Temporary CA and directories; addresses are built at run time (documentation range, loopback)."""
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
import issue_certs  # noqa: E402
import stage_secrets as S  # noqa: E402
from nexloop_eios import deploy_checks as D  # noqa: E402

DATA_ADDR = str(ipaddress.IPv4Address(int(ipaddress.IPv4Address('192.0.2.0')) + 20))
LOOPBACK = str(ipaddress.IPv4Address(2130706433))


@pytest.fixture(scope='module')
def tree(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('stage-secrets')
    issue_certs.root(tmp / 'root')
    issue_certs.intermediate(tmp / 'root', tmp / 'ca')
    params = tmp / 'params.json'
    params.write_text(json.dumps({'data_host_addr': DATA_ADDR, 'data_host_name': 'data.stage.invalid', 'pg_port': 55432, 'db_name': 'nexloop_stage',
                                  'nexloop_root': '/srv/nexloop', 'signing_key_id': 'stage-1', 'guard_port': 8200,
                                  'sans': {'gateway': ['DNS:app.stage.invalid'], 'agent-host': ['DNS:agent-host.stage.invalid'],
                                           'guard': ['DNS:guard.stage.invalid'], 'data': ['DNS:data.stage.invalid', 'IP:' + DATA_ADDR],
                                           'loopback': ['DNS:localhost', 'IP:' + LOOPBACK]}}))
    out = tmp / 'out'
    summary = S.generate(out, tmp / 'ca', params)
    return {'out': out, 'ca': tmp / 'ca', 'params': params, 'summary': summary, 'tmp': tmp,
            'manifest': json.loads((out / 'manifest.json').read_text())}


def compose_files():
    """Every secret file the two Compose files mount, read from the files themselves (the catalog must follow them)."""
    app = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    files = {('app', re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', s['file'])) for s in app['secrets'].values()}
    data_text = (ROOT / 'deploy/stage/compose.data.example.yaml').read_text()
    files |= {('data', p) for p in re.findall(r'\$\{NEXLOOP_ROOT:\?root\}/secrets/([A-Za-z0-9_./-]+?)(?::|$|\s)', data_text, re.M)}
    return files


def test_every_compose_secret_file_is_in_the_catalog_with_an_owner_and_a_phase():
    catalog = S.catalog()
    listed = {(e['host'], e['path']) for e in catalog}
    assert compose_files() <= listed, sorted(compose_files() - listed)
    assert len({(e['host'], e['path']) for e in catalog}) == len(catalog)
    assert all(e['phase'] in ('A', 'A/B', 'C', 'manual') and e['owner'] for e in catalog)
    owners = {e['service']: e['owner'] for e in catalog}
    assert owners['agent-host'] == '10020' and owners['gateway'] == '0' and owners['api'] == '10001'
    assert owners['postgres'] == owners['valkey'] == owners['pgbackrest'] == '999' and owners['backup'] == 'nexloop-backup'


def test_the_generated_tree_passes_the_distribution_check_and_holds_no_value_in_the_manifest(tree):
    out = tree['out']
    assert tree['summary']['generated'] + tree['summary']['manual_or_phase_c'] == tree['summary']['files'] == len(S.catalog())
    checked = S.check(out)
    assert checked['ok'] is True, checked['problems']
    assert set(checked['pending_manual_or_phase_c']) == {f"{e['host']}:{e['path']}" for e in S.catalog() if e['kind'] == 'config'}
    app = D.secret_distribution(out / 'app' / 'secrets', compose=yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text()))
    assert app['checks']['secret_distribution'] is True and app['checks']['real_dispatch_closed'] is True
    # The Agent Host directory: no DSN, credential, signing or channel name.
    assert not [p.name for p in (out / 'app' / 'secrets' / 'agent-host').iterdir() if D._SECRET_NAME.search(p.name)]
    # No value of any token, password or key is in the manifest.
    manifest_text = (out / 'manifest.json').read_text()
    for token in json.loads((out / 'management' / 'service-secrets.json').read_text()).values():
        assert token not in manifest_text
    for dsn in (out / 'app' / 'secrets').rglob('*dsn'):
        password = re.search(r'password=(\S+)', dsn.read_text()).group(1)
        assert password not in manifest_text and password not in (out / 'management' / 'role-passwords.sql').read_text()
    # A second run never overwrites.
    with pytest.raises(S.Refused, match='exists'):
        S.generate(out, tree['ca'], tree['params'])


def test_every_dsn_is_verify_full_and_every_certificate_verifies_against_the_bundle(tree):
    secrets_root = tree['out'] / 'app' / 'secrets'
    for dsn in secrets_root.rglob('*dsn'):
        assert D.check_dsn(dsn.read_text()) == [], dsn.name
    bundle = tree['ca'] / 'bundle.crt'
    for cert in [*secrets_root.rglob('*.crt'), *(tree['out'] / 'data' / 'secrets').rglob('*.crt')]:
        if cert.name in S.CA_FILES:
            assert cert.read_bytes() == bundle.read_bytes()
            continue
        first = cert.read_text().split('-----END CERTIFICATE-----')[0] + '-----END CERTIFICATE-----\n'
        leaf = tree['tmp'] / 'leaf.pem'
        leaf.write_text(first)
        assert subprocess.run(['openssl', 'verify', '-CAfile', str(bundle), str(leaf)], capture_output=True).returncode == 0, cert
    # Client certificates name the role of the service's DSN.
    subject = subprocess.run(['openssl', 'x509', '-noout', '-subject', '-in', str(secrets_root / 'domain-worker' / 'pg-client.crt')], capture_output=True, text=True).stdout
    assert 'CN=nexloop_domain_worker' in subject.replace(' ', '')


def test_shared_values_are_identical_and_key_formats_are_what_the_readers_accept(tree):
    root = tree['out'] / 'app' / 'secrets'
    def digest(path):
        return hashlib.sha256((root / path).read_bytes()).hexdigest()
    assert len({digest(p.split(':', 1)[1]) for p in S.SHARED_VALUES['host-control']}) == 1
    assert digest('action-worker/webchat-provider-credential') == digest('webchat-delivery/provider-credential')
    for path in ('shared/host-control.key', 'runtime-worker/guard-transport.key', 'api/browser-rate.key'):
        assert re.fullmatch('[0-9a-f]{64}', (root / path).read_text())
    raw = (root / 'shared' / 'signing.key').read_bytes()
    assert len(raw) == 32 and (tree['out'] / 'management' / 'signing.key.hex').read_text() == raw.hex()


def test_role_passwords_leave_as_scram_verifiers_that_postgresql_stores_as_given(tree, admin):
    sql = (tree['out'] / 'management' / 'role-passwords.sql').read_text()
    verifiers = dict(re.findall(r"ALTER ROLE (\w+) PASSWORD '([^']+)';", sql))
    assert set(verifiers) == set(tree['manifest']['roles']) and all(v.startswith('SCRAM-SHA-256$4096:') for v in verifiers.values())
    admin.execute("set password_encryption='scram-sha-256'")
    admin.execute('create role zz_scram_probe login')
    admin.execute(f"alter role zz_scram_probe password '{verifiers['nexloop_api']}'")
    assert admin.execute("select rolpassword from pg_authid where rolname='zz_scram_probe'").fetchone() == (verifiers['nexloop_api'],)


def cn(path):
    subject = subprocess.run(['openssl', 'x509', '-noout', '-subject', '-in', str(path)], capture_output=True, text=True, check=True).stdout
    return re.search(r'CN\s*=\s*([A-Za-z0-9_]+)', subject).group(1)


def test_every_dsn_names_a_client_certificate_whose_cn_is_its_role(tree):
    """F1 (NX-032 stage secrets §4): pg_hba clientcert=verify-full needs CN = user for every DSN, including the API's identity pool
    and the plan reevaluator's and reply guarantor's launcher pools (each now has its own second certificate)."""
    root = tree['out'] / 'app' / 'secrets'
    compose = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    file_of = {name: re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec['file']) for name, spec in compose['secrets'].items()}
    checked = {}
    for dsn in sorted(root.rglob('*dsn')):
        text = dsn.read_text()
        user = re.search(r'\buser=(\S+)', text).group(1)
        cert = re.search(r'sslcert=/run/secrets/(\S+)', text).group(1)
        key = re.search(r'sslkey=/run/secrets/(\S+)', text).group(1)
        assert cn(root / file_of[cert]) == user, dsn
        assert file_of[key] == file_of[cert].removesuffix('.crt') + '.key'
        checked[str(dsn.relative_to(root))] = (user, file_of[cert])
    assert checked['api/identity-dsn'] == ('nexloop_identity', 'api/identity-pg-client.crt')
    assert checked['plan-reevaluator/api-dsn'] == ('nexloop_api', 'plan-reevaluator/api-pg-client.crt')
    assert checked['reply-guarantor/api-dsn'] == ('nexloop_api', 'reply-guarantor/api-pg-client.crt')
    assert checked['api/dsn'] == ('nexloop_api', 'api/pg-client.crt') and checked['plan-reevaluator/dsn'][1] == 'plan-reevaluator/pg-client.crt'
    # Each service mounts the certificates its DSNs name.
    services = compose['services']
    for service, secret in (('api-1', 'api_identity'), ('api-2', 'api_identity'), ('plan-reevaluator', 'plan_reevaluator_api'), ('reply-guarantor', 'reply_guarantor_api')):
        assert {f'{secret}_dsn', f'{secret}_pg_client_cert', f'{secret}_pg_client_key'} <= set(services[service]['secrets']), service
    assert not any('client certificate CN is' in f['note'] for f in tree['manifest']['files'])


def test_the_agent_host_runtime_files_are_generated_mounted_and_consistent(tree):
    """F2: the runtime configuration and every file it names are mounted; the guard key equals the runtime worker's guard
    transport key; the model key is not in secrets/agent-host but in agent-host-model/ (owner 10020, phase C)."""
    root = tree['out'] / 'app' / 'secrets'
    compose = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    host = compose['services']['agent-host']
    mounted = {f'/run/secrets/{name}' for name in host['secrets']}
    file_of = {f'/run/secrets/{name}': re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec['file']) for name, spec in compose['secrets'].items()}
    command = host['command']
    config_path = command[command.index('--runtime-config-file') + 1]
    assert config_path in mounted and file_of[config_path] == 'agent-host/runtime-config.json'
    for flag in ('--internal-key-file', '--tls-certificate-file', '--tls-key-file'):
        assert command[command.index(flag) + 1] in mounted
    config = json.loads((root / 'agent-host' / 'runtime-config.json').read_text())
    for key in ('guard_key_file', 'guard_ca_file', 'model_configuration_file', 'model_tariff_file'):
        assert config[key] in mounted, key
    assert config['guard_url'] == 'https://127.0.0.1:8200/internal/v1/runtime/authorize'  # params guard_port = Compose GUARD_PORT
    assert {k: v for k, v in config.items() if k != 'guard_url'} == {k: v for k, v in json.loads((ROOT / 'deploy/stage/agent-host.runtime-config.example.json').read_text()).items() if k != 'guard_url'}
    assert (root / 'agent-host' / 'guard-transport.key').read_bytes() == (root / 'runtime-worker' / 'guard-transport.key').read_bytes()
    assert S.SHARED_VALUES['guard-transport'] == ['app:runtime-worker/guard-transport.key', 'app:agent-host/guard-transport.key']
    model = json.loads((root / 'agent-host' / 'model-configuration.json').read_text())
    assert model['stage'] is True and model['MODEL_CREDENTIALS_FILE'] in mounted and file_of[model['MODEL_CREDENTIALS_FILE']] == 'agent-host-model/model.key'
    assert (root / 'agent-host' / 'model-tariff.cny.json').read_bytes() == (ROOT / 'deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json').read_bytes()
    entry = next(e for e in S.catalog() if e['path'] == 'agent-host-model/model.key')
    assert entry['owner'] == '10020' and entry['phase'] == 'C' and not (root / 'agent-host-model').exists()
    # The Agent Host directory and mounts hold no DSN, credential, signing, channel or Artifact.
    assert not [p.name for p in (root / 'agent-host').iterdir() if D._SECRET_NAME.search(p.name)]
    assert not [s for s in host['secrets'] if D._SECRET_NAME.search(s)] and not any('artifact' in str(v) for v in host.get('volumes', []))
    assert D.secret_distribution(root, compose=compose)['checks']['secret_distribution'] is True


def test_the_distribution_check_still_refuses_a_credential_in_the_agent_host_tree(tree, tmp_path):
    import shutil
    copy = tmp_path / 'secrets'
    shutil.copytree(tree['out'] / 'app' / 'secrets', copy)
    planted = copy / 'agent-host' / 'model-credential'
    planted.write_text('synthetic')
    planted.chmod(0o600)
    assert 'agent_host_holds_service_secret' in D.secret_distribution(copy)['details']['secrets']['problems']


def test_cli_refuses_without_a_ca_and_prints_no_value(tree, tmp_path):
    script = ROOT / 'scripts/deploy/stage_secrets.py'
    run = subprocess.run([sys.executable, str(script), 'generate', '--out', str(tmp_path / 'o'), '--ca-dir', str(tmp_path / 'none'), '--params', str(tree['params'])],
                         capture_output=True, text=True)
    assert run.returncode == 2 and 'intermediate CA' in json.loads(run.stdout)['detail'] and not (tmp_path / 'o').exists()
    ok = subprocess.run([sys.executable, str(script), 'check', '--out', str(tree['out'])], capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)['ok'] is True
    for token in json.loads((tree['out'] / 'management' / 'service-secrets.json').read_text()).values():
        assert token not in ok.stdout


def test_the_generated_dsns_connect_under_clientcert_verify_full_and_the_old_certificates_are_refused(tree, tmp_path_factory):
    """F1 on a real PostgreSQL: a scratch cluster with TLS and `hostssl ... cert clientcert=verify-full` (the stage pg_hba rule
    for client certificates: CN must be the user). Each of the three formerly mismatched DSNs connects with the certificate it now
    names; the same DSN with the service's main certificate (what it named before) is refused."""
    import os
    import psycopg
    bindir = Path(os.environ.get('NEXLOOP_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@18/bin'))
    tmp = tmp_path_factory.mktemp('stage-tls-pg')
    issue_certs.leaf('server', tree['ca'], tmp / 'srv', 'local-pg', sans=['DNS:localhost', 'IP:127.0.0.1'])
    import tempfile
    data, sock = tmp / 'data', Path(tempfile.mkdtemp(prefix='nexloop-tls-'))  # a short socket path (conftest's pattern)
    subprocess.run([str(bindir / 'initdb'), '-D', str(data), '-U', 'nexloop_bootstrap', '--auth=trust', '--no-locale', '-E', 'UTF8'], check=True, stdout=subprocess.DEVNULL)
    for name, source in (('server.crt', tmp / 'srv/local-pg.chain.crt'), ('server.key', tmp / 'srv/local-pg.key'), ('ca.crt', tree['ca'] / 'bundle.crt')):
        (data / name).write_bytes(source.read_bytes())
        (data / name).chmod(0o600)
    (data / 'pg_hba.conf').write_text('local all nexloop_bootstrap trust\nhostssl all all 127.0.0.1/32 cert clientcert=verify-full\n')
    port = free_port()
    subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-l', str(tmp / 'pg.log'), '-o',
                    f"-k {sock} -p {port} -c listen_addresses=127.0.0.1 -c ssl=on -c ssl_cert_file=server.crt -c ssl_key_file=server.key -c ssl_ca_file=ca.crt",
                    '-w', 'start'], check=True, stdout=subprocess.DEVNULL)
    try:
        with psycopg.connect(host=str(sock), port=port, user='nexloop_bootstrap', dbname='postgres', autocommit=True) as admin:
            for role in ('nexloop_identity', 'nexloop_api'):
                admin.execute(f'create role {role} login')
        root = tree['out'] / 'app' / 'secrets'
        compose = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
        file_of = {name: re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec['file']) for name, spec in compose['secrets'].items()}

        def connect(dsn_path, cert=None):
            text = (root / dsn_path).read_text()
            user = re.search(r'\buser=(\S+)', text).group(1)
            cert = cert or file_of[re.search(r'sslcert=/run/secrets/(\S+)', text).group(1)]
            with psycopg.connect(host='localhost', hostaddr='127.0.0.1', port=port, dbname='postgres', user=user, sslmode='verify-full',
                                 sslrootcert=str(tree['ca'] / 'bundle.crt'), sslcert=str(root / cert),
                                 sslkey=str(root / (cert.removesuffix('.crt') + '.key')), connect_timeout=5) as db:
                return db.execute('select current_user').fetchone()[0]
        assert connect('api/identity-dsn') == 'nexloop_identity'
        assert connect('plan-reevaluator/api-dsn') == 'nexloop_api'
        assert connect('reply-guarantor/api-dsn') == 'nexloop_api'
        for dsn, old in (('api/identity-dsn', 'api/pg-client.crt'), ('plan-reevaluator/api-dsn', 'plan-reevaluator/pg-client.crt'),
                         ('reply-guarantor/api-dsn', 'reply-guarantor/pg-client.crt')):
            with pytest.raises(psycopg.OperationalError, match='certificate authentication failed'):
                connect(dsn, old)
    finally:
        subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL)
        import shutil
        shutil.rmtree(sock, ignore_errors=True)


def test_the_agent_host_starts_with_the_generated_runtime_files(tree, tmp_path):
    """F2: the actual Agent Host (Node 24, built dist) starts with the generated runtime configuration, model configuration,
    tariff and guard key, each read from where Compose mounts it (/run/secrets/<name> mapped to a private directory here), with a
    synthetic model key; it answers its liveness probe. No model call is made at start."""
    import shutil
    import time
    import httpx
    from test_agent_host import files, trusted_tls
    if not shutil.which('node') or not (ROOT / 'apps/agent-host/dist/main.js').is_file():
        pytest.fail('Node 24 and the built Agent Host are required')
    root = tree['out'] / 'app' / 'secrets'
    compose = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    file_of = {name: re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec['file']) for name, spec in compose['secrets'].items()}
    mounts = tmp_path / 'run-secrets'
    mounts.mkdir(mode=0o700)
    for name in compose['services']['agent-host']['secrets']:
        if name == 'host_model_key':
            (mounts / name).write_text('synthetic-model-key-' + 'x' * 24)  # phase C file: the owner's in production
        else:
            (mounts / name).write_bytes((root / file_of[name]).read_bytes())
        (mounts / name).chmod(0o600)
    remap = lambda text: text.replace('/run/secrets/', str(mounts) + '/')  # noqa: E731
    for name in ('host_runtime_config', 'host_model_configuration'):
        (mounts / name).write_text(remap((mounts / name).read_text()))
    (tmp_path / 'host').mkdir(mode=0o700)
    runtime, key = files(tmp_path / 'host')
    port = free_port()
    child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/agent_host.py'), '--node', shutil.which('node'), '--runtime-root', str(runtime),
                              '--internal-key-file', str(key), '--port', str(port), '--tls-certificate-file', str(key.parent / 'host-cert.pem'),
                              '--tls-key-file', str(key.parent / 'host-key.pem'), '--runtime-config-file', str(mounts / 'host_runtime_config')],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with httpx.Client(base_url=f'https://127.0.0.1:{port}', verify=trusted_tls(key), trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 15
            while True:
                assert child.poll() is None, 'the Host refused the generated runtime files'
                try:
                    if client.get('/health/live').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.1)
    finally:
        if child.poll() is None:
            child.terminate()
        out, err = child.communicate(timeout=10)
    assert 'synthetic-model-key' not in out + err
    # Counter-check: with a file the runtime configuration names not mounted (the F2 gap; the tariff is read at start, the guard
    # key on every guard call), the Host does not start.
    (mounts / 'host_model_tariff').unlink()
    (tmp_path / 'host2').mkdir(mode=0o700)
    runtime2, key2 = files(tmp_path / 'host2')
    refused = subprocess.run([sys.executable, str(ROOT / 'scripts/agent_host.py'), '--node', shutil.which('node'), '--runtime-root', str(runtime2),
                              '--internal-key-file', str(key2), '--port', str(free_port()), '--tls-certificate-file', str(key2.parent / 'host-cert.pem'),
                              '--tls-key-file', str(key2.parent / 'host-key.pem'), '--runtime-config-file', str(mounts / 'host_runtime_config')],
                             capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

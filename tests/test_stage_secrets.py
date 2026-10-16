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
    S.setup_credentials(out)  # A3: the setup tools' principals need a token before register
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
            for role in ('nexloop_identity', 'nexloop_api', 'nexloop_domain_worker'):
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
        # G-METRICS-DSN: the metrics client connects with its certificate alone (the cert-only DSN, no password).
        admin_create = psycopg.connect(host=str(sock), port=port, user='nexloop_bootstrap', dbname='postgres', autocommit=True)
        with admin_create:
            admin_create.execute('create role nexloop_metrics login')
        metrics = tree['out'] / 'management' / 'metrics'
        with psycopg.connect(host='localhost', hostaddr='127.0.0.1', port=port, dbname='postgres', user='nexloop_metrics', sslmode='verify-full',
                             sslrootcert=str(metrics / 'pg-ca.crt'), sslcert=str(metrics / 'metrics-pg-client.crt'),
                             sslkey=str(metrics / 'metrics-pg-client.key'), connect_timeout=5) as db:
            assert db.execute('select current_user').fetchone()[0] == 'nexloop_metrics'
        with pytest.raises(psycopg.OperationalError, match='certificate authentication failed'):
            psycopg.connect(host='localhost', hostaddr='127.0.0.1', port=port, dbname='postgres', user='nexloop_api', sslmode='verify-full',
                            sslrootcert=str(metrics / 'pg-ca.crt'), sslcert=str(metrics / 'metrics-pg-client.crt'),
                            sslkey=str(metrics / 'metrics-pg-client.key'), connect_timeout=5)
        # 2026-10-16: the runtime worker's DSN and certificate are the domain worker's (stage pg_hba: certificate CN = role).
        assert connect('runtime-worker/dsn') == 'nexloop_domain_worker'
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


def test_the_metrics_client_is_generated_certificate_only_owner_only_and_checked(tree, tmp_path):
    """G-METRICS-DSN: management/metrics holds the APP_HOST read-only metrics client (verify-full, user nexloop_metrics, no
    password, paths of its installed location NEXLOOP_ROOT/ops/metrics, CN nexloop_metrics chained to the bundle), never a
    service secret; DATA_HOST's copy is the backup executor's socket DSN with a SCRAM password in role-passwords.sql."""
    import stat
    metrics = tree['out'] / 'management' / 'metrics'
    assert sorted(p.name for p in metrics.iterdir()) == sorted(S.METRICS_FILES) and stat.S_IMODE(metrics.stat().st_mode) == 0o700
    assert all(stat.S_IMODE((metrics / n).stat().st_mode) == 0o600 for n in S.METRICS_FILES)
    fields = dict(item.split('=', 1) for item in (metrics / 'metrics.dsn').read_text().split())
    assert fields['sslmode'] == 'verify-full' and fields['user'] == 'nexloop_metrics' and 'password' not in fields
    assert fields['host'] == 'data.stage.invalid' and fields['hostaddr'] == DATA_ADDR and fields['port'] == '55432'
    assert {fields[k] for k in ('sslrootcert', 'sslcert', 'sslkey')} == {f'/srv/nexloop/ops/metrics/{n}' for n in ('pg-ca.crt', 'metrics-pg-client.crt', 'metrics-pg-client.key')}
    assert cn(metrics / 'metrics-pg-client.crt') == 'nexloop_metrics'
    verified = subprocess.run(['openssl', 'verify', '-CAfile', str(tree['ca'] / 'bundle.crt'), str(metrics / 'metrics-pg-client.crt')], capture_output=True, text=True)
    assert verified.returncode == 0, verified.stderr
    # Not a service secret: no compose file mounts it, and no service tree holds a nexloop_metrics DSN or certificate.
    for host in ('app', 'data'):
        for path in (tree['out'] / host / 'secrets').rglob('*'):
            if path.is_file() and path.name != 'metrics.dsn' and path.suffix in ('', '.dsn') and 'dsn' in path.name:
                assert 'user=nexloop_metrics' not in path.read_text(), path
    data = (tree['out'] / 'data' / 'secrets' / 'backup' / 'metrics.dsn').read_text()
    assert 'user=nexloop_metrics' in data and 'host=/srv/nexloop/run/postgresql' in data and 'password=' in data and 'sslmode' not in data
    entry = next(f for f in tree['manifest']['files'] if f['path'] == 'backup/metrics.dsn')
    assert entry['host'] == 'data' and entry['owner'] == 'nexloop-backup' and entry['kind'] == 'socket_dsn'
    assert "ALTER ROLE nexloop_metrics PASSWORD 'SCRAM-SHA-256$" in (tree['out'] / 'management' / 'role-passwords.sql').read_text()
    assert S.check(tree['out'])['ok']
    # The check refuses a widened mode and a password in the APP_HOST DSN (on a copy of the tree).
    import shutil
    copy = tmp_path / 'tree'
    shutil.copytree(tree['out'], copy)
    (copy / 'management' / 'metrics' / 'metrics-pg-client.key').chmod(0o644)
    dsn = copy / 'management' / 'metrics' / 'metrics.dsn'
    dsn.write_text(dsn.read_text().strip() + ' password=x\n')
    assert set(S.metrics_problems(copy / 'management' / 'metrics')) == {'metrics:file:metrics-pg-client.key', 'metrics:dsn'}


def test_the_agent_host_starts_with_the_generated_runtime_files(tree, tmp_path):
    """F2: the actual Agent Host (Node 24, built dist) starts with the generated runtime configuration, model configuration,
    tariff and guard key, each read from where Compose mounts it (/run/secrets/<name> mapped to a private directory here), with a
    synthetic model key; it answers its liveness probe. No model call is made at start.

    Requires Node 24 and apps/agent-host/dist rebuilt from this checkout; with another Node major or a stale dist the Host exits
    at start and this test fails with 'the Host refused the generated runtime files' (dispatcher 2026-10-15). Without node or dist
    it fails at once with 'Node 24 and the built Agent Host are required'. Rebuild: pnpm -C apps/agent-host build."""
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
                assert child.poll() is None, ('the Host refused the generated runtime files (needs Node 24 and a dist rebuilt from this '
                                              'checkout: pnpm -C apps/agent-host build; a stale dist or another Node major also exits here)')
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


def test_pgbackrest_tls_files_match_the_compose_mounts_the_server_conf_and_the_data_conf_example(tree):
    """M8: DATA_HOST client certificate (CN pgbackrest-data-client, tls-server-auth) mounted read-only for the PG container;
    APP_HOST host files pgbackrest-server/ (server chain with the gateway SANs) where pgbackrest-server.conf reads them."""
    files = {(e['host'], e['path']): e for e in S.catalog()}
    for path in ('pgbackrest/client.crt', 'pgbackrest/client.key', 'pgbackrest/ca.crt'):
        assert files[('data', path)]['owner'] == '999'
    for path in S.APP_HOST_FILES:
        assert files[('app', path)]['owner'] == 'nexloop-pgbackrest' and files[('app', path)]['secret'] is None
    data = yaml.safe_load((ROOT / 'deploy/stage/compose.data.example.yaml').read_text())
    mounts = [v for v in data['services']['postgres']['volumes'] if '/secrets/pgbackrest/' in v]
    tls = {v.split(':')[-2]: v for v in mounts if '/etc/pgbackrest/tls/' in v}
    assert set(tls) == {f'/etc/pgbackrest/tls/{n}' for n in ('client.crt', 'client.key', 'ca.crt')} and all(v.endswith(':ro') for v in tls.values())
    conf = (ROOT / 'deploy/stage/config/pgbackrest.conf.example').read_text()
    settings = dict(re.findall(r'^([a-z0-9-]+)=(.*)$', conf, re.M))
    assert settings['pg1-path'] == data['services']['postgres']['environment']['PGDATA'] and settings['repo1-host-type'] == 'tls'
    assert {settings[f'repo1-host-{k}-file'] for k in ('cert', 'key', 'ca')} == set(tls)
    assert not [k for k in settings if k.startswith('repo2')] and all(v.startswith('__') for k, v in settings.items() if 'cipher-pass' in k)
    secrets_root = tree['out']
    client = subprocess.run(['openssl', 'x509', '-noout', '-subject', '-in', str(secrets_root / 'data/secrets/pgbackrest/client.crt')],
                            capture_output=True, text=True).stdout
    assert 'CN=pgbackrest-data-client' in client.replace(' ', '')
    server = subprocess.run(['openssl', 'x509', '-noout', '-ext', 'subjectAltName,extendedKeyUsage', '-in', str(secrets_root / 'app/secrets/pgbackrest-server/server.crt')],
                            capture_output=True, text=True).stdout
    assert 'app.stage.invalid' in server and 'TLS Web Server Authentication' in server
    assert (secrets_root / 'app/secrets/pgbackrest-server/ca.crt').read_bytes() == (tree['ca'] / 'bundle.crt').read_bytes()


def test_pgpass_uses_localhost_for_the_default_socket_directory():
    """libpq matches pgpass host `localhost` for its default socket directory, not the path (2026-10-14 first start)."""
    src = (ROOT / 'scripts/deploy/stage_secrets.py').read_text()
    assert "'localhost:5432:*:nexloop_backup:'" in src and "'/var/run/postgresql:5432:" not in src


def test_the_signing_key_is_32_bytes_of_text(tree):
    """AuthoritySigner needs exactly 32 bytes and the stage preflight reads every private file as UTF-8 text (2026-10-15)."""
    out = tree['out'] if isinstance(tree, dict) else tree
    raw = (Path(out) / 'app/secrets/shared/signing.key').read_bytes()
    assert len(raw) == 32 and raw.decode('ascii').strip() == raw.decode('ascii')
    assert (Path(out) / 'management/signing.key.hex').read_text().strip() == raw.hex()


def test_setup_credentials_reuse_the_principal_token_and_are_idempotent(tree, tmp_path):
    """A3: the setup tools' credential files live below setup/ (no Compose service mounts them; `check` stays exact) and share the
    token of a principal that already has one; a second run adds nothing; a file that differs from the store is refused."""
    import shutil
    out = tmp_path / 'out'
    shutil.copytree(tree['out'], out)
    tokens = json.loads((out / 'management' / 'service-secrets.json').read_text())
    setup = {k: v for k, v in tokens.items() if k.startswith('setup:')}
    assert sorted(setup) == ['setup:setup/business-executor-credential', 'setup:setup/business-owner-credential',
                             'setup:setup/catalog-maintainer-credential', 'setup:setup/catalog-source-credential', 'setup:setup/role-assembler-credential']
    assert setup['setup:setup/business-executor-credential'] == tokens['app:action-worker/service-credential']
    assert setup['setup:setup/catalog-source-credential'] == tokens['app:message-relay/source-credential']
    assert len({setup['setup:setup/business-owner-credential'], setup['setup:setup/catalog-maintainer-credential'], setup['setup:setup/role-assembler-credential'], *[
        v for k, v in tokens.items() if not k.startswith('setup:')]}) == len({v for k, v in tokens.items() if not k.startswith('setup:')}) + 3
    for key, token in setup.items():
        path = out / key.split(':', 1)[1]
        assert path.read_text() == token and path.stat().st_mode & 0o777 == 0o600
    assert (out / 'management' / 'service-secrets.json').stat().st_mode & 0o777 == 0o600
    assert S.setup_credentials(out) == {'changed': False, 'added': [], 'setup_files': 5}
    assert S.check(out)['ok'] is True
    (out / 'setup' / 'business-owner-credential').unlink()
    S.write_private(out / 'setup' / 'business-owner-credential', 'tampered')
    with pytest.raises(S.Refused, match='differs'):
        S.setup_credentials(out)


def test_metrics_adds_the_client_to_a_tree_generated_without_it(tree, tmp_path):
    """G-METRICS-DSN on the stage's existing tree: `metrics` adds management/metrics, the DATA_HOST socket DSN and the one ALTER ROLE
    for the bootstrap window, records them in the manifest (check passes), prints no value and refuses a second run."""
    import shutil
    old = tmp_path / 'old'
    shutil.copytree(tree['out'], old)
    shutil.rmtree(old / 'management' / 'metrics')
    (old / 'data' / 'secrets' / 'backup' / 'metrics.dsn').unlink()
    manifest = json.loads((old / 'manifest.json').read_text())
    manifest['files'] = [f for f in manifest['files'] if f['path'] != 'backup/metrics.dsn']
    manifest['management'] = [m for m in manifest['management'] if not m.startswith('metrics')]
    (old / 'manifest.json').write_text(json.dumps(manifest))
    assert S.check(old)['problems'] == ['metrics:directory']
    printed = []
    assert S.main(['metrics', '--out', str(old), '--ca-dir', str(tree['ca']), '--params', str(tree['params'])]) == 0 or printed
    assert S.check(old)['ok']
    sql = (old / 'management' / 'metrics-role-password.sql').read_text()
    assert sql.count('ALTER ROLE') == 1 and "ALTER ROLE nexloop_metrics PASSWORD 'SCRAM-SHA-256$" in sql
    password = re.search(r'password=(\S+)', (old / 'data' / 'secrets' / 'backup' / 'metrics.dsn').read_text()).group(1)
    assert password not in sql
    assert S.main(['metrics', '--out', str(old), '--ca-dir', str(tree['ca']), '--params', str(tree['params'])]) == 2


def _accepted_roles():
    """The SQL login each stage entry point accepts, read from the code (verify_application_role checks)."""
    from nexloop_eios.background_services import SERVICES
    src = ROOT / 'packages/eios-core/src/nexloop_eios'
    def literal(module, role):
        assert f"'{role}'" in (src / module).read_text().replace(' ', ''), (module, role)
        return {role}
    def role_set(module):
        found = re.findall(r"verify_application_role\([a-z]+\)\s*not in\s*\{([^}]*)\}", (src / module).read_text())
        assert len(found) == 1, module
        return set(re.findall(r"'([a-z_]+)'", found[0]))
    accepted = {name: {role} for name, (_, role) in SERVICES.items()}
    accepted.update({
        'runtime-worker': role_set('runtime_worker.py') & role_set('runtime_guard_worker.py'),
        'action-worker': literal('effect_worker.py', 'nexloop_action_worker'),
        'webchat-delivery': literal('local_json_delivery.py', 'nexloop_action_worker'),
        'message-relay': literal('message_relay_cli.py', 'nexloop_api'),
        'outbound-recorder': literal('outbound_messages.py', 'nexloop_api'),
        'api': literal('run_credentials.py', 'nexloop_api'),
        'domain-worker': literal('artifact_orphans.py', 'nexloop_domain_worker'),
    })
    return accepted


def test_every_stage_dsn_role_is_one_its_entry_point_accepts_and_the_runtime_worker_runs_as_its_tests_do():
    """2026-10-16: the stage ran the runtime worker as nexloop_scheduler (accepted by the entry point) while every runtime test ran it
    as nexloop_domain_worker, and the scheduler lacks most of the runtime path's functions. Each service's `dsn` role must be one its
    entry point accepts, and the runtime worker's must be the role the runtime tests use."""
    accepted = _accepted_roles()
    services = sorted({e['service'] for e in S.catalog() if e['host'] == 'app' and e['path'].endswith('/dsn')})
    assert set(services) <= set(accepted), sorted(set(services) - set(accepted))
    for service in services:
        assert S._role(service, 'dsn') in accepted[service], (service, S._role(service, 'dsn'), accepted[service])
    tests_role = set()
    for fixture in ('test_runtime_authority.py', 'runtime_effect_fixture.py'):
        tests_role |= set(re.findall(r"user='(nexloop_[a-z_]+)'\),artifact_root=tmp_path/'worker", (ROOT / 'tests' / fixture).read_text().replace(' ', '')))
    assert tests_role == {'nexloop_domain_worker'} and S._role('runtime-worker', 'dsn') == 'nexloop_domain_worker'


def test_reissue_role_gives_the_runtime_worker_a_domain_worker_dsn_and_certificate_once(tree, tmp_path):
    """The stage tree was generated with runtime-worker as nexloop_scheduler; reissue-role re-issues its DSN (the domain worker's
    password, same host and certificates paths) and client certificate (CN nexloop_domain_worker), changes nothing else, and refuses a
    second run."""
    import shutil
    from psycopg.conninfo import conninfo_to_dict
    old = tmp_path / 'tree'
    shutil.copytree(tree['out'], old)
    secrets_root = old / 'app' / 'secrets'
    # Make it the tree as generated before the mapping change.
    dsn = secrets_root / 'runtime-worker' / 'dsn'
    d = conninfo_to_dict(dsn.read_text().strip())
    from psycopg.conninfo import make_conninfo
    dsn.write_text(make_conninfo(**{**d, 'user': 'nexloop_scheduler', 'password': 'old-scheduler-password'}) + '\n')
    manifest = json.loads((old / 'manifest.json').read_text())
    import hashlib
    for f in manifest['files']:
        if f['path'] == 'runtime-worker/dsn':
            f['sha256'] = hashlib.sha256(dsn.read_bytes()).hexdigest()
    (old / 'manifest.json').write_text(json.dumps(manifest))
    before = {p: p.read_bytes() for p in secrets_root.rglob('*') if p.is_file()}
    assert S.main(['reissue-role', '--out', str(old), '--ca-dir', str(tree['ca']), '--params', str(tree['params']), '--service', 'runtime-worker']) == 0
    new = conninfo_to_dict(dsn.read_text().strip())
    donor = conninfo_to_dict((secrets_root / 'domain-worker' / 'dsn').read_text().strip())
    assert new['user'] == 'nexloop_domain_worker' and new['password'] == donor['password'] and new['sslmode'] == 'verify-full'
    assert {k: v for k, v in new.items() if k not in ('user', 'password')} == {k: v for k, v in d.items() if k not in ('user', 'password')}
    assert cn(secrets_root / 'runtime-worker' / 'pg-client.crt') == 'nexloop_domain_worker'
    changed = sorted(str(p.relative_to(secrets_root)) for p, data in before.items() if p.read_bytes() != data)
    assert changed == ['runtime-worker/dsn', 'runtime-worker/pg-client.crt', 'runtime-worker/pg-client.key']
    assert oct((secrets_root / 'runtime-worker/pg-client.key').stat().st_mode)[-3:] == '600'
    assert S.check(old)['ok']
    assert S.main(['reissue-role', '--out', str(old), '--ca-dir', str(tree['ca']), '--params', str(tree['params']), '--service', 'runtime-worker']) == 2


def test_the_delivery_service_gets_the_same_ca_as_the_action_worker_route(tree):
    """Delivery CA (2026-10-16): webchat-delivery/provider-ca.crt is generated, with the same content as
    action-worker/webchat-provider-ca.crt, owned like the other webchat-delivery files."""
    root = tree['out'] / 'app' / 'secrets'
    assert (root / 'webchat-delivery/provider-ca.crt').read_bytes() == (root / 'action-worker/webchat-provider-ca.crt').read_bytes()
    entry = next(f for f in tree['manifest']['files'] if f['path'] == 'webchat-delivery/provider-ca.crt')
    other = next(f for f in tree['manifest']['files'] if f['path'] == 'webchat-delivery/dsn')
    assert entry['kind'] == 'ca_bundle' and entry['owner'] == other['owner'] and oct((root / 'webchat-delivery/provider-ca.crt').stat().st_mode)[-3:] == '600'

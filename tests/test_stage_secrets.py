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
                                  'nexloop_root': '/srv/nexloop', 'signing_key_id': 'stage-1',
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


def test_the_cn_role_mismatches_are_reported_not_hidden(tree):
    notes = {f['path']: f['note'] for f in tree['manifest']['files'] if f['kind'] == 'dsn'}
    mismatched = sorted(p for p, n in notes.items() if 'client certificate CN is' in n)
    assert mismatched == ['api/identity-dsn', 'plan-reevaluator/api-dsn', 'reply-guarantor/api-dsn']
    assert 'CN is nexloop_api, not nexloop_identity' in notes['api/identity-dsn']


def test_cli_refuses_without_a_ca_and_prints_no_value(tree, tmp_path):
    script = ROOT / 'scripts/deploy/stage_secrets.py'
    run = subprocess.run([sys.executable, str(script), 'generate', '--out', str(tmp_path / 'o'), '--ca-dir', str(tmp_path / 'none'), '--params', str(tree['params'])],
                         capture_output=True, text=True)
    assert run.returncode == 2 and 'intermediate CA' in json.loads(run.stdout)['detail'] and not (tmp_path / 'o').exists()
    ok = subprocess.run([sys.executable, str(script), 'check', '--out', str(tree['out'])], capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)['ok'] is True
    for token in json.loads((tree['out'] / 'management' / 'service-secrets.json').read_text()).values():
        assert token not in ok.stdout

"""Run sheet run-metrics-gate part 2: scripts/deploy/gate_dir.py derives tls.json and spec.json from the rendered Compose's secrets
section (no guessed path) and the deploy-gate spec example; the tls input passes deploy_checks' shape and every *_dsn is listed."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import gate_dir as G  # noqa: E402

STAGE = ROOT / 'deploy/stage'


def rendered_compose(root='/srv/nexloop'):
    """What `docker compose config --format json` gives for the secrets section: ${NEXLOOP_ROOT:?...} resolved."""
    doc = yaml.safe_load((STAGE / 'compose.app.example.yaml').read_text())
    return {'secrets': {name: {'file': re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}', root, spec['file'])} for name, spec in doc['secrets'].items()}}


def test_tls_spec_names_the_compose_secret_files_and_every_dsn(tmp_path):
    env = G.load_env(STAGE / 'stage.env.example')
    spec = G.tls_spec(rendered_compose(), env, tmp_path / 'valkey.conf')
    compose = rendered_compose()['secrets']
    by = {c['name']: c for c in spec['certificates']}
    assert set(by) == {'gateway', 'api-loopback', 'agent-host', 'guard', 'webchat-delivery'}
    assert by['gateway'] == {'name': 'gateway', 'chain': '/srv/nexloop/secrets/gateway/server.crt', 'key': '/srv/nexloop/secrets/gateway/server.key',
                             'purpose': 'server', 'names': [env['APP_HOST_NAME']], 'uid': 0}
    assert by['agent-host']['uid'] == 10020 and all(by[n]['names'] == ['127.0.0.1'] for n in ('api-loopback', 'agent-host', 'guard', 'webchat-delivery'))
    assert all(c['chain'] == compose[s]['file'] for c, s in zip(spec['certificates'], [x[1] for x in G.SERVER_CERTIFICATES]))
    dsn_secrets = sorted(n for n in compose if n.endswith('_dsn'))
    assert [d['name'] for d in spec['dsn_files']] == dsn_secrets and len(dsn_secrets) >= 15
    assert all(d['path'] == compose[d['name']]['file'] for d in spec['dsn_files'])
    assert spec['ca'] == compose['pg_ca']['file'] and spec['valkey_conf'] == str(tmp_path / 'valkey.conf')
    # The shape deploy_checks.tls reads.
    from nexloop_eios import deploy_checks
    assert set(spec) == {'ca', 'certificates', 'dsn_files', 'valkey_conf'} and 'def tls(spec' in Path(deploy_checks.__file__).read_text()


def test_a_missing_certificate_secret_or_host_name_is_refused(tmp_path):
    compose = rendered_compose()
    del compose['secrets']['guard_key']
    with pytest.raises(G.Refused, match='guard_key'):
        G.tls_spec(compose, G.load_env(STAGE / 'stage.env.example'), tmp_path / 'v')
    env = {k: v for k, v in G.load_env(STAGE / 'stage.env.example').items() if k != 'APP_HOST_NAME'}
    with pytest.raises(G.Refused, match='APP_HOST_NAME'):
        G.tls_spec(rendered_compose(), env, tmp_path / 'v')


def test_the_gate_spec_fills_every_placeholder_and_points_roles_at_the_metrics_dsn(tmp_path):
    spec = G.gate_spec(STAGE / 'deploy-gate.app.example.json', gate_dir=tmp_path / 'gate', nexloop_root='/srv/nexloop', release_checkout='/opt/checkout')
    assert spec['roles_database_url_file'] == '/srv/nexloop/ops/metrics/metrics.dsn' and spec['host_role'] == 'app'
    assert spec['tls_spec'] == str(tmp_path / 'gate' / 'tls.json') and spec['connection_budget'] == '/opt/checkout/deploy/stage/connection-budget.v1.json'
    assert not [v for v in spec.values() if isinstance(v, str) and '<' in v]
    from nexloop_eios.deploy_checks import REQUIRED_INPUTS
    assert set(REQUIRED_INPUTS['app']) <= set(spec)


def test_the_valkey_template_renders_from_the_app_stage_env(tmp_path):
    env = tmp_path / 'stage.env'
    env.write_text((STAGE / 'stage.env.example').read_text())
    env.chmod(0o600)
    done = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy/render.py'), '--env', str(env), '--template', str(STAGE / 'config/valkey.conf.template'),
                           '--out', str(tmp_path / 'valkey.conf')], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    from nexloop_eios.deploy_checks import check_valkey_conf
    assert check_valkey_conf((tmp_path / 'valkey.conf').read_text()) == []


def test_an_existing_gate_dir_is_refused(tmp_path, capsys):
    (tmp_path / 'gate').mkdir()
    assert G.main(['--nexloop-root', str(tmp_path), '--release-checkout', str(ROOT), '--stage-env', str(STAGE / 'stage.env.example'),
                   '--gate-dir', str(tmp_path / 'gate'), '--planned-ports', '8443']) == 2
    assert 'exists' in capsys.readouterr().out


def test_each_dsn_carries_its_service_uid_and_the_gate_reads_it_as_root_would(tmp_path):
    """The gate runs as root (it must see every service directory), and read_private_text requires the reading process to own the
    file; a DSN entry with `uid` is read when the file is that uid's, owner-only and regular - and refused otherwise."""
    import os
    example = json.loads((STAGE / 'deploy-gate.app.example.json').read_text())
    spec = G.tls_spec(rendered_compose(), G.load_env(STAGE / 'stage.env.example'), tmp_path / 'v', example['secret_uids'])
    assert {d['uid'] for d in spec['dsn_files']} == {10001}
    from nexloop_eios import deploy_checks as D
    dsn = tmp_path / 'dsn'
    dsn.write_text('host=data.stage.invalid user=nexloop_api dbname=nexloop_stage sslmode=verify-full sslrootcert=/a sslcert=/b sslkey=/c\n')
    dsn.chmod(0o600)
    me = os.geteuid()
    def verdict(uid):
        return D.tls({'ca': str(tmp_path / 'none'), 'dsn_files': [{'name': 'x', 'path': str(dsn), 'uid': uid}]})['details']['tls']['dsn']['x']
    assert verdict(me) == []
    assert verdict(me + 1) == ['dsn_file_unreadable_or_not_private']
    dsn.chmod(0o640)
    assert verdict(me) == ['dsn_file_unreadable_or_not_private']
    dsn.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(dsn)
    assert D.tls({'ca': str(tmp_path / 'none'), 'dsn_files': [{'name': 'x', 'path': str(link), 'uid': me}]})['details']['tls']['dsn']['x'] == ['dsn_file_unreadable_or_not_private']
    # Without uid the old rule holds (the reading process owns it).
    assert D.tls({'ca': str(tmp_path / 'none'), 'dsn_files': [{'name': 'x', 'path': str(dsn)}]})['details']['tls']['dsn']['x'] == []


def test_the_data_host_script_is_valid_bash_and_its_pg_hba_expectation_holds(tmp_path):
    """scripts/deploy/metrics_data_host.sh: bash syntax, and the rule it relies on - the G-METRICS-DSN template differs from the
    template before it by exactly two rule lines, both nexloop_metrics."""
    script = ROOT / 'scripts/deploy/metrics_data_host.sh'
    assert subprocess.run(['bash', '-n', str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    assert 'set -euo pipefail' in text and 'pg_hba.conf.before-metrics' in text and '-m 0600' in text and 'nexloop-backup' in text
    def rules(template_text):
        return [' '.join(l.split()) for l in template_text.splitlines() if l.strip() and not l.lstrip().startswith('#')]
    now = (STAGE / 'config/pg_hba.conf.template').read_text()
    before = '\n'.join(l for l in now.splitlines() if 'nexloop_metrics' not in l or l.lstrip().startswith('#'))
    added = [l for l in rules(now) if l not in rules(before)]
    assert len(added) == 2 and all('nexloop_metrics' in l for l in added) and set(rules(before)) <= set(rules(now))


def _function(name):
    """One shell function of metrics_data_host.sh (and the helpers it calls), runnable on its own."""
    text = (ROOT / 'scripts/deploy/metrics_data_host.sh').read_text()
    return '\n'.join(line for line in text.splitlines() if line.startswith(('inode_of()', 'write_in_place()')))


def test_write_in_place_keeps_the_inode_a_bind_mount_holds(tmp_path):
    """2026-10-16 stage: install gave config/pg_hba.conf a new inode, the container's single-file bind mount kept the old one and a
    HUP reloaded the old rules. A hard link stands in for the bind mount: after write_in_place it shows the new content and the
    inode is unchanged; replacing the file (install, the old way) leaves the link on the old content."""
    host, mount = tmp_path / 'pg_hba.conf', tmp_path / 'container-view'
    host.write_text('old rules\n')
    host.chmod(0o640)
    os.link(host, mount)
    new = tmp_path / 'new'
    new.write_text('old rules\nhostssl db nexloop_metrics 192.0.2.10/32 cert\n')
    inode = host.stat().st_ino
    done = subprocess.run(['bash', '-c', _function('write_in_place') + '\nwrite_in_place "$1" "$2"', '_', str(new), str(host)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert host.stat().st_ino == inode and oct(host.stat().st_mode)[-3:] == '640'
    assert mount.read_text() == new.read_text()  # what the container sees
    # The restore path is the same function: the old content comes back on the same inode.
    backup = tmp_path / 'backup'
    backup.write_text('old rules\n')
    assert subprocess.run(['bash', '-c', _function('write_in_place') + '\nwrite_in_place "$1" "$2"', '_', str(backup), str(host)]).returncode == 0
    assert mount.read_text() == 'old rules\n' and host.stat().st_ino == inode
    # Negative control: replacing the file (a new inode) leaves the "container" on the old content.
    replacement = tmp_path / 'replacement'
    replacement.write_text('replaced\n')
    os.replace(replacement, host)
    assert host.stat().st_ino != inode and mount.read_text() == 'old rules\n'


def test_step_3_checks_the_container_view_and_never_replaces_the_file():
    text = (ROOT / 'scripts/deploy/metrics_data_host.sh').read_text()
    step3 = text[text.index('step 3 '):text.index('step 4 ')]
    assert 'install -o' not in step3 and 'mv ' not in step3
    assert 'write_in_place "$WORK/pg_hba.conf" "$HBA"' in step3 and 'write_in_place "$HBA.before-metrics" "$HBA"' in step3
    assert 'exec -T postgres cat /etc/postgresql/pg_hba.conf' in step3 and 'grep -q nexloop_metrics /etc/postgresql/pg_hba.conf' in step3
    assert 'restart the postgres container' in step3

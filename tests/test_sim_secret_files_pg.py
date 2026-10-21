"""Simulation stage enablement, the compose secret files (docs/implementation/sim-stage-enable.md):
- scripts/deploy/sim_secret_files.py (APP_HOST, sim-setup): every secret file 0600 and never overwritten, a rerun changes nothing; the
  simulation CA apart from the real one, the Host and guard certificates verify against it on the loopback; the runtime configuration
  is the simulation Host's; the env draft; the dry run writes nothing; no value in any output; the registration export only under the
  pipeline's marker.
- scripts/deploy/sim_secret_register.py (DATA_HOST, sim-issue, real PostgreSQL over the socket): the keys from the pipe are checked
  against APP_HOST's digests, the signing key is registered through the trusted configuration (a configurator window), the Host by the
  bootstrap session; the self-check; a rerun registers nothing; other material under the same ids is refused.
- scripts/deploy/sim_secret_registration_transfer.sh (the dispatcher's machine): one pipeline ssh APP_HOST | ssh DATA_HOST.
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import issue_certs  # noqa: E402
import sim_secret_files as F  # noqa: E402
import sim_secret_register as R  # noqa: E402

from test_service_grants_pg import TENANT, deployment  # noqa: E402,F401


def private(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)
    return path


def run(main, argv, **kw):
    out = io.StringIO()
    code = main(argv, out=out, **kw)
    return code, json.loads(out.getvalue().strip().splitlines()[-1]), out.getvalue()


@pytest.fixture
def setup(tmp_path):
    secret = tmp_path / 'sim-secrets'
    secret.mkdir(mode=0o700)
    real_ca = tmp_path / 'real-ca'
    issue_certs.root(real_ca, 30)  # stands in for the real PostgreSQL CA bundle
    conf = private(tmp_path / 'secret-files.json', {'schema': F.SCHEMA, 'secret_dir': str(secret), 'ca_dir': str(tmp_path / 'tokens' / 'sim-ca'),
                   'host_id': 'sim-host-test', 'signing_key_id': 'sim-test-key', 'guard_port': 8201, 'real_pg_ca_file': str(real_ca / 'ca.crt'),
                   'sans': {'host': ['IP:127.0.0.1'], 'guard': ['IP:127.0.0.1']}, 'env_draft_file': str(tmp_path / 'tokens' / 'sim.env.draft'),
                   'env': {'NEXLOOP_SIM_UID': 10031, 'NEXLOOP_SIM_SECRET_DIR': '/srv/nexloop/sim-secrets'}})
    return {'secret': secret, 'conf': conf, 'real_ca': real_ca / 'ca.crt', 'tmp': tmp_path}


def test_the_secret_files_are_written_once_0600_and_a_rerun_changes_nothing(setup, monkeypatch):
    s = setup
    code, dry, _ = run(F.main, ['--config', str(s['conf']), '--dry-run'])
    assert code == 0 and dry['to_write'] == sorted(F.FILES) and not any(s['secret'].iterdir()) and dry['ca_present'] is False
    code, out, text = run(F.main, ['--config', str(s['conf'])])
    assert code == 0 and out['written'] == sorted(F.FILES) and out['changed'] is True and out['ca_created'] is True and out['env_draft'] == 'written'
    files = {p.name: p for p in s['secret'].iterdir()}
    assert set(files) == set(F.FILES) and all(p.stat().st_mode & 0o777 == 0o600 for p in files.values())
    # No value in the output: neither a key nor a certificate.
    for name in (*F.HEX_KEYS, 'sim_signing_key', 'sim_host_key', 'sim_guard_tls_key'):
        assert files[name].read_text().strip()[:24] not in text
    assert all(len(files[n].read_text().strip()) == 64 and int(files[n].read_text(), 16) >= 0 for n in F.HEX_KEYS)
    assert len(files['sim_signing_key'].read_bytes()) == 32 and files['sim_signing_key'].read_text().isascii()
    assert files['sim_pg_ca'].read_bytes() == s['real_ca'].read_bytes()
    # The simulation Host's runtime configuration (as tests/test_nx041_sim_pi_run_pg runs the Host).
    config = json.loads(files['sim_host_runtime_config'].read_text())
    assert config['world_group'] == 'simulation' and config['context_input_protocol'] == 'nexloop.context-pack.sim.v1' and config['host_id'] == 'sim-host-test'
    assert config['runtime_profile'] == 'deterministic-test' and 'model_configuration_file' not in config  # the Host refuses it in that profile
    assert config['world_purge_key_file'] == '/run/secrets/sim_host_purge_key' and config['guard_url'].startswith('https://127.0.0.1:8201/')
    assert json.loads(files['sim_host_model_configuration'].read_text())['runtime_profile'] == 'deterministic-test'
    # The certificates: issued by the simulation CA, not the real one; served on the loopback.
    for cert, ca in (('sim_host_cert', 'sim_host_ca'), ('sim_guard_certificate', 'sim_guard_ca')):
        ok = subprocess.run(['openssl', 'verify', '-CAfile', str(files[ca]), '-untrusted', str(files[cert]), str(files[cert])], capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        bad = subprocess.run(['openssl', 'verify', '-CAfile', str(s['real_ca']), str(files[cert])], capture_output=True, text=True)
        assert bad.returncode != 0
        assert '127.0.0.1' in subprocess.run(['openssl', 'x509', '-in', str(files[cert]), '-noout', '-ext', 'subjectAltName'], capture_output=True, text=True).stdout
    # The env draft: every NEXLOOP_SIM_* name, configured values, placeholders for the rest.
    draft = (s['tmp'] / 'tokens' / 'sim.env.draft').read_text()
    assert all(f'{n}=' in draft for n in F.ENV_NAMES) and 'NEXLOOP_SIM_UID=10031\n' in draft and 'NEXLOOP_SIM_HOST_ID=sim-host-test\n' in draft
    assert 'NEXLOOP_SIM_SIGNING_KEY_ID=sim-test-key\n' in draft and 'NEXLOOP_SIM_WORLD=<nexloop_sim_world>\n' in draft
    # A rerun writes nothing and keeps every file as it is.
    before = {n: p.read_bytes() for n, p in files.items()}
    code, again, _ = run(F.main, ['--config', str(s['conf'])])
    assert code == 0 and again['written'] == [] and again['changed'] is False and again['ca_created'] is False and again['env_draft'] == 'kept'
    assert {n: p.read_bytes() for n, p in files.items()} == before and again['registration'] == out['registration']
    # A certificate without its key is refused, nothing written.
    files['sim_host_key'].unlink()
    code, refused, _ = run(F.main, ['--config', str(s['conf'])])
    assert code == 2 and 'sim_host_cert' in refused['detail'] and not (s['secret'] / 'sim_host_key').exists()


def test_the_registration_export_runs_only_in_the_pipeline_and_bad_input_is_refused(setup, monkeypatch):
    s = setup
    assert run(F.main, ['--config', str(s['conf'])])[0] == 0
    monkeypatch.delenv('NEXLOOP_SIM_TRANSFER_PIPE', raising=False)
    code, out, text = run(F.main, ['--config', str(s['conf']), '--export-registration'])
    assert code == 2 and 'pipeline' in out['detail']
    monkeypatch.setenv('NEXLOOP_SIM_TRANSFER_PIPE', '1')
    sink = io.StringIO()
    assert F.main(['--config', str(s['conf']), '--export-registration'], out=sink) == 0
    line = json.loads(sink.getvalue())
    assert bytes.fromhex(line['signing_key_hex']) == (s['secret'] / 'sim_signing_key').read_bytes()
    assert line['purge_key_hex'] == (s['secret'] / 'sim_host_purge_key').read_text().strip()
    base = json.loads(s['conf'].read_text())
    for over in ({'signing_key_id': 'real-key'}, {'host_id': 'X'}, {'guard_port': 80}, {'ca_dir': str(s['secret'] / 'ca')},
                 {'sans': {'host': ['127.0.0.1'], 'guard': ['IP:127.0.0.1']}}, {'env': {'PATH': '/bin'}}, {'extra': 1}):
        private(s['conf'], {**base, **over})
        assert run(F.main, ['--config', str(s['conf']), '--dry-run'])[0] == 2, over
    example = F.load_config((ROOT / 'deploy/examples/sim-secret-files.example.json').read_text().replace('<SIGNING_KEY_ID>', 'x')
                            .replace('<REAL_PG_CA_BUNDLE>', 'pg-ca.crt'))
    assert example['secret_dir'] == '/sim-secrets' and example['ca_dir'].startswith('/sim-tokens/')


def test_the_keys_are_registered_from_the_pipe_and_self_checked(setup, deployment, admin, monkeypatch):  # noqa: F811
    s, d = setup, deployment
    d['apply']()  # the tenant and its trusted configuration
    code, generated, _ = run(F.main, ['--config', str(s['conf'])])
    reg = generated['registration']
    monkeypatch.setenv('NEXLOOP_SIM_TRANSFER_PIPE', '1')
    sink = io.StringIO()
    assert F.main(['--config', str(s['conf']), '--export-registration'], out=sink) == 0
    line = sink.getvalue()
    bootstrap = private(s['tmp'] / 'bootstrap-dsn', d['pg'])
    conf = private(s['tmp'] / 'register.json', {'schema': R.SCHEMA, 'tenant_id': TENANT, 'bootstrap_dsn_file': str(bootstrap),
                   'signing_key_id': 'sim-test-key', 'host_id': 'sim-host-test',
                   'expect': {'signing_key_sha256': reg['signing_key_sha256'], 'purge_key_sha256': reg['purge_key_sha256']}})
    code, dry, _ = run(R.main, ['--config', str(conf), '--dry-run'])
    assert code == 0 and dry['signing_key_registered'] is False and dry['host_registered'] is False and dry['check']['pass'] is False
    code, out, text = run(R.main, ['--config', str(conf)], stdin=io.StringIO(line))
    assert code == 0 and out['registered'] == ['signing_key', 'host'] and out['check']['pass'] is True, out
    assert json.loads(line)['signing_key_hex'] not in text and json.loads(line)['purge_key_hex'] not in text
    assert admin.execute("select active from authz.nexloop_authority_signing_keys where key_id='sim-test-key'").fetchone() == (True,)
    assert admin.execute("select active from authz.nexloop_sim_hosts where host_id='sim-host-test'").fetchone() == (True,)
    # The configurator window closed again: nexloop_configurator logs in only for the publish.
    assert admin.execute("select rolcanlogin from pg_roles where rolname='nexloop_configurator'").fetchone() == (False,)
    # A rerun registers nothing; --check alone passes.
    code, again, _ = run(R.main, ['--config', str(conf)], stdin=io.StringIO(line))
    assert code == 0 and again['registered'] == [] and again['changed'] is False
    assert run(R.main, ['--config', str(conf), '--check'])[0] == 0
    # Keys that are not APP_HOST's (the expect digests), another key id on the line, a TCP bootstrap DSN: refused before any write.
    forged = json.loads(line)
    forged['purge_key_hex'] = 'ab' * 32
    assert run(R.main, ['--config', str(conf)], stdin=io.StringIO(json.dumps(forged)))[1]['detail'].startswith('stdin: the keys are not')
    assert run(R.main, ['--config', str(conf)], stdin=io.StringIO(line.replace('sim-test-key', 'sim-other')))[0] == 2
    private(bootstrap, 'host=127.0.0.1 port=5432 dbname=x user=nexloop_bootstrap')
    assert 'socket' in run(R.main, ['--config', str(conf), '--check'])[1]['detail']
    private(bootstrap, d['pg'])
    # Other material under the registered ids (a second APP_HOST tree with the same ids): refused, the rows unchanged.
    other = s['tmp'] / 'other-secrets'
    other.mkdir(mode=0o700)
    private(s['conf'], {**json.loads(s['conf'].read_text()), 'secret_dir': str(other), 'env_draft_file': str(s['tmp'] / 'other.env')})
    other_reg = run(F.main, ['--config', str(s['conf'])])[1]['registration']
    sink = io.StringIO()
    F.main(['--config', str(s['conf']), '--export-registration'], out=sink)
    private(conf, {**json.loads(conf.read_text()), 'expect': {k: other_reg[k] for k in ('signing_key_sha256', 'purge_key_sha256')}})
    code, out, _ = run(R.main, ['--config', str(conf)], stdin=io.StringIO(sink.getvalue()))
    assert code == 2 and 'new sim- key id' in out['detail']
    assert admin.execute("select encode(sha256(key_material),'hex') from authz.nexloop_sim_hosts where host_id='sim-host-test'").fetchone()[0] == reg['purge_key_sha256']
    # A deactivated Host fails the self-check (the original configuration again).
    private(conf, {**json.loads(conf.read_text()), 'expect': {k: reg[k] for k in ('signing_key_sha256', 'purge_key_sha256')}})
    admin.execute("update authz.nexloop_sim_hosts set active=false where host_id='sim-host-test'")
    code, checked, _ = run(R.main, ['--config', str(conf), '--check'])
    assert code == 1 and checked['check']['items']['host_active'] is False


def test_the_registration_moves_through_one_dispatcher_pipeline(tmp_path):
    """ssh APP_HOST '... --export-registration' | ssh DATA_HOST '... sim_secret_register.py' (the hosts never ssh to each other)."""
    bin_dir, log = tmp_path / 'bin', tmp_path / 'log'
    bin_dir.mkdir()
    line = json.dumps({'schema': 'nexloop-sim-registration/1', 'signing_key_hex': 'K' * 8})
    (tmp_path / 'line').write_text(line + '\n')
    (bin_dir / 'ssh').write_text(f"""#!/bin/bash
host="${{@: -2:1}}"; cmd="${{@: -1}}"
printf '%s | %s\\n' "$host" "$cmd" >> {log}
case "$host:$cmd" in
  app-stage:*--export-registration*) cat {tmp_path / 'line'} ;;
  data-stage:*sim_secret_register.py*) cat > {tmp_path / 'register-stdin'}; printf '{{"ok": true}}\\n' ;;
  *) exit 9 ;;
esac
""")
    (bin_dir / 'ssh').chmod(0o755)
    conf = private(tmp_path / 'c', 'APP_HOST_SSH=app-stage\nDATA_HOST_SSH=data-stage\nAPP_RELEASE=/srv/release\nAPP_ENV=/srv/private/sim.env\n'
                   'SECRET_CONFIG=/setup/secret-files.json\nDATA_RELEASE=/srv/release\nDATA_ENV=/srv/private/sim-issue.env\nREGISTER_CONFIG=/setup/register.json\n')
    env = {**os.environ, 'PATH': f"{bin_dir}:{os.environ['PATH']}"}
    done = subprocess.run(['bash', str(ROOT / 'scripts/deploy/sim_secret_registration_transfer.sh'), str(conf)], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / 'register-stdin').read_text() == line + '\n' and 'K' * 8 not in done.stdout + done.stderr + log.read_text()
    calls = log.read_text().splitlines()
    assert sorted(c.split(' | ')[0] for c in calls) == ['app-stage', 'data-stage']
    assert any('NEXLOOP_SIM_TRANSFER_PIPE=1' in c and c.startswith('app-stage') for c in calls)
    private(conf, conf.read_text().replace('app-stage', 'data-stage'))
    log.write_text('')
    assert subprocess.run(['bash', str(ROOT / 'scripts/deploy/sim_secret_registration_transfer.sh'), str(conf)], capture_output=True, text=True, env=env).returncode == 1
    assert log.read_text() == ''


def test_the_simulation_agent_host_starts_with_the_generated_files(setup, tmp_path):
    """The actual Agent Host (Node 24, built dist; as test_stage_secrets F2) starts with the generated simulation files, each read
    where compose.simulation mounts it (/run/secrets/<name> mapped to a private directory here): the runtime configuration (simulation
    group, its context protocol, host id, purge key), its own TLS certificate (verified against the simulation CA bundle) and transport
    key; it answers its liveness probe. Rebuild with pnpm -C apps/agent-host build when it fails at start."""
    import shutil
    import socket
    import time
    import httpx
    import yaml
    if not shutil.which('node') or not (ROOT / 'apps/agent-host/dist/main.js').is_file():
        pytest.fail('Node 24 and the built Agent Host are required')
    s = setup
    assert run(F.main, ['--config', str(s['conf'])])[0] == 0
    compose = yaml.safe_load((ROOT / 'deploy/stage/compose.simulation.example.yaml').read_text())
    mounts = tmp_path / 'run-secrets'
    mounts.mkdir(mode=0o700)
    for name in compose['services']['sim-agent-host']['secrets']:
        (mounts / name).write_bytes((s['secret'] / name).read_bytes())
        (mounts / name).chmod(0o600)
    (mounts / 'sim_host_runtime_config').write_text((mounts / 'sim_host_runtime_config').read_text().replace('/run/secrets/', str(mounts) + '/'))
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/agent_host.py'), '--node', shutil.which('node'), '--runtime-root', str(runtime),
                              '--internal-key-file', str(mounts / 'sim_host_transport_key'), '--port', str(port),
                              '--tls-certificate-file', str(mounts / 'sim_host_cert'), '--tls-key-file', str(mounts / 'sim_host_key'),
                              '--runtime-config-file', str(mounts / 'sim_host_runtime_config')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with httpx.Client(base_url=f'https://127.0.0.1:{port}', verify=str(s['secret'] / 'sim_host_ca'), trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 15
            while True:
                assert child.poll() is None, 'the Host refused the generated simulation files (Node 24 and a rebuilt dist are needed)'
                try:
                    if client.get('/health/live').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.1)
        # The compose healthcheck (stage finding 2026-10-22: dependents wait for it): healthy here, unhealthy on another port.
        test = compose['services']['sim-agent-host']['healthcheck']['test']
        assert test[0] == 'CMD' and test[-1] == '${NEXLOOP_SIM_HOST_PORT}'
        probe_cmd = [shutil.which('node'), *test[2:-1]]
        assert subprocess.run([*probe_cmd, str(port)], capture_output=True, timeout=20).returncode == 0
        with socket.socket() as other:
            other.bind(('127.0.0.1', 0))
            closed = other.getsockname()[1]
        assert subprocess.run([*probe_cmd, str(closed)], capture_output=True, timeout=20).returncode == 1
    finally:
        if child.poll() is None:
            child.terminate()
        out, err = child.communicate(timeout=10)
    secret_values = [(s['secret'] / n).read_text().strip() for n in (*F.HEX_KEYS, 'sim_signing_key')]
    assert not any(v in out + err for v in secret_values)

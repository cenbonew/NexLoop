"""NX-032 slice 0: the whitelist renderer and the stage templates (Compose for DATA_HOST and APP_HOST, pg_hba, PostgreSQL, Valkey,
firewall). Synthetic documentation-range values only; nothing connects anywhere."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'deploy/stage'
from nexloop_eios import stage_render as render  # noqa: E402

TEMPLATES = sorted([*(STAGE / 'config').glob('*.template'), *(STAGE / 'firewall').glob('*.template')])


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'stage.env'
    path.write_text((STAGE / 'stage.env.example').read_text())
    path.chmod(0o600)
    return path


def test_every_placeholder_is_whitelisted_and_every_template_renders_completely(env, tmp_path):
    assert {p.name for p in TEMPLATES} == {'pg_hba.conf.template', 'postgresql.conf.template', 'valkey.conf.template', 'Caddyfile.template',
                                           'data-host.nft.template', 'app-host.nft.template'}
    values = render.load_env(env)
    for template in TEMPLATES:
        used = set(render.PLACEHOLDER.findall(template.read_text()))
        assert used <= set(render.PARAMETERS), template.name
        out = tmp_path / template.name.removesuffix('.template')
        assert render.main(['--env', str(env), '--template', str(template), '--out', str(out)]) == 0
        assert not re.search(r'__[A-Z0-9_]+__', out.read_text()), template.name
    hba = (tmp_path / 'pg_hba.conf').read_text()
    assert f"hostssl nexloop_stage {values['APP_PG_ROLES']} {values['APP_HOST_ADDR']}/32 scram-sha-256 clientcert=verify-full" in hba
    lines = [l for l in hba.splitlines() if l and not l.startswith('#')]
    assert lines[-4:] == [l for l in lines if 'reject' in l] and all('reject' in l for l in lines[-4:]) and lines[-4].startswith('hostnossl')
    assert not any(re.search(r'\b(trust|password|md5)\b', l) for l in lines)
    valkey = (tmp_path / 'valkey.conf').read_text()
    assert 'port 0' in valkey and 'tls-auth-clients yes' in valkey and 'maxmemory 512mb' in valkey
    pg = (tmp_path / 'postgresql.conf').read_text()
    assert "ssl = on" in pg and "ssl_min_protocol_version = 'TLSv1.3'" in pg and "ssl_ca_file = '/run/tls/ca.crt'" in pg
    data_fw = (tmp_path / 'data-host.nft').read_text()
    assert 'table inet nexloop_stage' in data_fw and f"ip saddr {values['APP_HOST_ADDR']} accept" in data_fw
    assert '#ct original' in data_fw  # the management window stays commented by default


def test_the_renderer_refuses_unknown_missing_and_malformed_values(env, tmp_path, capsys):
    good = env.read_text()
    bad_values = {'APP_HOST_ADDR': '0.0.0.0/0', 'TRUSTED_LAN_CIDR': '192.0.2.1/24', 'PG_PORT': '5432; drop', 'NEXLOOP_ROOT': '/srv/../etc',
                  'APP_PG_ROLES': 'all', 'VALKEY_MAXMEMORY': '1 gb', 'MODEL_PROVIDER_ALLOWLIST': '203.0.113.5,example.invalid',
                  'DATA_HOST_NAME': 'host name', 'MIGRATION_PG_ROLE': 'postgres'}
    for key, value in bad_values.items():
        env.write_text(re.sub(rf'^{key}=.*$', f'{key}={value}', good, flags=re.M))
        with pytest.raises(render.Refused, match=key):
            render.load_env(env)
    env.write_text(good + 'EXTRA_PARAMETER=1\n')
    with pytest.raises(render.Refused, match='EXTRA_PARAMETER'):
        render.load_env(env)
    env.write_text(good)
    env.chmod(0o644)
    with pytest.raises(render.Refused, match='0600'):
        render.load_env(env)
    env.chmod(0o600)
    values = render.load_env(env)
    with pytest.raises(render.Refused, match='UNKNOWN_THING'):
        render.render('listen __UNKNOWN_THING__\n', values)
    with pytest.raises(render.Refused, match='GUARD_PORT'):
        render.render('port __GUARD_PORT__\n', {k: v for k, v in values.items() if k != 'GUARD_PORT'})
    template = tmp_path / 'x.template'
    template.write_text('port __PG_PORT__\n')
    out = tmp_path / 'x'
    out.write_text('existing')
    assert render.main(['--env', str(env), '--template', str(template), '--out', str(out)]) == 2
    printed = capsys.readouterr().out
    assert 'output exists' in printed and values['APP_HOST_ADDR'] not in printed  # parameter names only, never values


def compose(name):
    return yaml.safe_load((STAGE / name).read_text())


def test_compose_examples_publish_only_the_planned_ports_and_keep_secrets_per_service():
    data, app = compose('compose.data.example.yaml'), compose('compose.app.example.yaml')
    assert data['name'] == 'nexloop-data' and app['name'] == 'nexloop-app'
    assert data['services']['postgres']['ports'] == ['${DATA_HOST_ADDR:?DATA_HOST address}:${PG_PORT:?PostgreSQL port}:5432']
    assert data['services']['valkey']['ports'] == ['${DATA_HOST_ADDR:?DATA_HOST address}:${VALKEY_PORT:?Valkey TLS port}:6379']
    published = {name: s.get('ports') for name, s in app['services'].items() if s.get('ports')}
    assert list(published) == ['app-net'] and all(p.startswith('${APP_HOST_ADDR') for p in published['app-net'])
    for name, s in app['services'].items():
        if name not in ('app-net', 'agent-host', 'agent-link-relay'):  # NX-037 D4: the Agent Host has its own namespace
            assert s['network_mode'] == 'service:app-net' and s.get('read_only') is True and s['environment']['REAL_DISPATCH_ENABLED'] == 'false', name
        image = s['image']
        assert image.startswith('${') and 'by digest' in image, name
    # The Agent Host holds only its own TLS, transport key and guard CA: no DSN, credential, signing key or Artifact directory.
    host = app['services']['agent-host']
    assert set(host['secrets']) == {'host_cert', 'host_key', 'host_transport_key', 'guard_ca'}
    assert not any('artifact' in v for v in host.get('volumes', []))
    for name, s in app['services'].items():
        for secret in s.get('secrets', []):
            assert app['secrets'][secret]['file'].startswith('${NEXLOOP_ROOT:?root}/secrets/'), secret
    # Runtime Worker as deployed: ADR-024 guard 4, NX-031 dispatch loops 4.
    cmd = app['services']['runtime-worker']['command']
    assert cmd[cmd.index('--guard-workers') + 1] == '4' and cmd[cmd.index('--dispatch-loops') + 1] == '4'


def interpolate(text, env_file):
    """The Compose rules this file uses: ${VAR:?message} must be set, ${VAR} substituted, $$ is a literal $."""
    values = dict(l.split('=', 1) for l in env_file.read_text().splitlines() if l and not l.startswith('#'))
    def sub(m):
        name, required = m.group(1), m.group(2) is not None
        if required and not values.get(name):
            raise AssertionError('required variable unset: ' + name)
        return values.get(name, '')
    return re.sub(r'\$\{([A-Z0-9_]+)(:\?[^}]*)?\}', sub, text.replace('$$', '\0')).replace('\0', '$')


def test_compose_examples_render_with_their_env_files_and_docker_compose_config():
    """With docker installed, `docker compose config` (no daemon needed) must accept both files. Without docker (the rootless CI
    user has none) the same interpolation is checked by this test itself: every required variable set, every image by digest."""
    for name in ('data', 'app'):
        rendered = yaml.safe_load(interpolate((STAGE / f'compose.{name}.example.yaml').read_text(), STAGE / f'compose.{name}.env.example'))
        assert all(re.fullmatch(r'[a-z0-9./-]+@sha256:[0-9a-f]{64}', s['image']) for s in rendered['services'].values()), name
        docker = shutil.which('docker')
        if docker:
            run = subprocess.run([docker, 'compose', '-f', f'compose.{name}.example.yaml', '--env-file', f'compose.{name}.env.example', 'config', '--format', 'json'],
                                 cwd=STAGE, capture_output=True, text=True, timeout=60)
            assert run.returncode == 0, run.stderr[-2000:]
            assert {k: v['image'] for k, v in json.loads(run.stdout)['services'].items()} == {k: v['image'] for k, v in rendered['services'].items()}
    # A missing required variable is refused by the same rule docker applies.
    def without(key):
        lines = [l for l in (STAGE / 'compose.data.env.example').read_text().splitlines() if not l.startswith(key + '=')]
        return lines
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / 'env'
        env.write_text('\n'.join(without('DATA_HOST_ADDR')) + '\n')
        with pytest.raises(AssertionError, match='DATA_HOST_ADDR'):
            interpolate((STAGE / 'compose.data.example.yaml').read_text(), env)

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
        if name not in ('app-net', 'agent-host', 'agent-link-relay', 'action-worker', 'webchat-delivery'):  # NX-037: own namespaces
            assert s['network_mode'] == 'service:app-net' and s.get('read_only') is True and s['environment']['REAL_DISPATCH_ENABLED'] == 'false', name
        if name in ('action-worker', 'webchat-delivery'):
            assert s.get('read_only') is True and s['environment']['REAL_DISPATCH_ENABLED'] == 'false', name
        image = s['image']
        assert image.startswith('${') and 'by digest' in image, name
    # The Agent Host holds only its own TLS, transport key and guard CA: no DSN, credential, signing key or Artifact directory.
    host = app['services']['agent-host']
    assert set(host['secrets']) == {'host_cert', 'host_key', 'host_transport_key', 'guard_ca', 'host_guard_key', 'host_runtime_config', 'host_model_configuration', 'host_model_tariff', 'host_model_key'}
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


def test_the_effect_worker_reaches_only_postgresql_and_the_enterprise_endpoint(env, tmp_path):
    """NX-037 provider routing: the three new parameters are whitelisted and checked (a remote port such as 443 is allowed, the
    local service ports keep their 1024+ rule), and the rendered APP_HOST rules give the effect worker namespace exactly
    PostgreSQL on DATA_HOST and the one enterprise endpoint, after dropping model addresses and before its own default drop."""
    good = env.read_text()
    values = render.load_env(env)
    assert values['ENTERPRISE_ENDPOINT_PORT'] == '443' and {'EFFECT_EGRESS_CIDR', 'ENTERPRISE_ENDPOINT_ADDR'} <= set(values)
    for key, value in {'EFFECT_EGRESS_CIDR': '203.0.113.65/27', 'ENTERPRISE_ENDPOINT_ADDR': 'enterprise.invalid',
                       'ENTERPRISE_ENDPOINT_PORT': '0', 'PG_PORT': '443'}.items():
        env.write_text(re.sub(rf'^{key}=.*$', f'{key}={value}', good, flags=re.M))
        with pytest.raises(render.Refused, match=key):
            render.load_env(env)
    env.write_text(good)
    rules, _ = render.render((STAGE / 'firewall' / 'app-host.nft.template').read_text(), render.load_env(env))
    effect = [l.strip() for l in rules.splitlines() if l.strip().startswith('ip saddr ' + values['EFFECT_EGRESS_CIDR'])]
    assert effect == [
        f"ip saddr {values['EFFECT_EGRESS_CIDR']} ip daddr {{ {values['MODEL_PROVIDER_ALLOWLIST']} }} drop",
        f"ip saddr {values['EFFECT_EGRESS_CIDR']} ip daddr {values['DATA_HOST_ADDR']} tcp dport {values['PG_PORT']} accept",
        f"ip saddr {values['EFFECT_EGRESS_CIDR']} ip daddr {values['ENTERPRISE_ENDPOINT_ADDR']} tcp dport {values['ENTERPRISE_ENDPOINT_PORT']} accept",
        f"ip saddr {values['EFFECT_EGRESS_CIDR']} ct state new drop"]
    # The application namespace still never reaches the enterprise endpoint: its default drop is unchanged.
    app = [l.strip() for l in rules.splitlines() if l.strip().startswith('ip saddr ' + values['APP_CONTAINER_CIDR'])]
    assert not any(values['ENTERPRISE_ENDPOINT_ADDR'] in l for l in app) and app[-1].endswith('ct state new drop')
    # The Compose example's effect network is the same parameter (one value in both private files).
    compose_env = (STAGE / 'compose.app.env.example').read_text()
    assert f"EFFECT_EGRESS_CIDR={values['EFFECT_EGRESS_CIDR']}" in compose_env


def test_the_pgbackrest_tls_port_is_open_to_data_host_only(env, tmp_path):
    """NX-035 M8: APP_HOST's pgBackRest TLS server (repo1) accepts DATA_HOST and nothing else; the port is a whitelisted parameter."""
    values = render.load_env(env)
    assert values['PGBACKREST_TLS_PORT'] == '58432'
    out = tmp_path / 'app-host.nft'
    assert render.main(['--env', str(env), '--template', str(STAGE / 'firewall/app-host.nft.template'), '--out', str(out)]) == 0
    text = out.read_text()
    port, data = values['PGBACKREST_TLS_PORT'], values['DATA_HOST_ADDR']
    lines = [l.strip() for l in text.splitlines()]
    accept, drop = f'ip saddr {data} tcp dport {port} accept', f'tcp dport {port} drop'
    assert accept in lines and drop in lines and lines.index(accept) < lines.index(drop)
    for bad in ('80', 'x', '70000'):
        broken = tmp_path / f'bad-{bad}.env'
        broken.write_text(env.read_text().replace(f'PGBACKREST_TLS_PORT={port}', f'PGBACKREST_TLS_PORT={bad}'))
        broken.chmod(0o600)
        with pytest.raises(render.Refused, match='PGBACKREST_TLS_PORT'):
            render.load_env(broken)


def _reference_verdict(rules, *, saddr, daddr, dport, reply=False):
    """First-match verdict of the rendered APP_HOST forward chain for a new connection to a DNAT'ed port (or its reply), over the
    rules that can match it: the reply rule and the rules keyed on the reference endpoint's original destination."""
    import ipaddress
    chain = rules[rules.index('chain forward'):rules.index('chain output')]
    for line in (l.strip() for l in chain.splitlines()):
        if line == 'ct direction reply accept':
            if reply:
                return 'accept'
            continue
        m = re.fullmatch(r'ct original ip daddr (\S+) ct original proto-dst (\d+)(?: ip saddr (\S+))? (accept|drop)', line)
        if m and not reply and m.group(1) == daddr and int(m.group(2)) == dport:
            if m.group(3) is None or ipaddress.ip_address(saddr) in ipaddress.ip_network(m.group(3)):
                return m.group(4)
    return 'accept'  # policy accept: no rule of this table matched


def test_the_reference_endpoint_admits_the_effect_worker_network_only(env, tmp_path):
    """Owner ruling 2026-10-15 (run sheet 4.2): the reference endpoint published on APP_HOST (REFERENCE_ENDPOINT_PORT, a whitelisted
    1024+ port) accepts new connections from EFFECT_EGRESS_CIDR only and drops every other source; replies still pass first."""
    import ipaddress
    values = render.load_env(env)
    assert values['REFERENCE_ENDPOINT_PORT'] == '9643'
    out = tmp_path / 'app-host.nft'
    assert render.main(['--env', str(env), '--template', str(STAGE / 'firewall/app-host.nft.template'), '--out', str(out)]) == 0
    rules = out.read_text()
    port, app, effect = int(values['REFERENCE_ENDPOINT_PORT']), values['APP_HOST_ADDR'], values['EFFECT_EGRESS_CIDR']
    lines = [l.strip() for l in rules.splitlines()]
    accept = f'ct original ip daddr {app} ct original proto-dst {port} ip saddr {effect} accept'
    drop = f'ct original ip daddr {app} ct original proto-dst {port} drop'
    assert lines.index('ct direction reply accept') < lines.index(accept) < lines.index(drop)
    effect_host = str(next(ipaddress.ip_network(effect).hosts()))
    lan_host = str(next(ipaddress.ip_network(values['TRUSTED_LAN_CIDR']).hosts()))
    app_host = str(next(ipaddress.ip_network(values['APP_CONTAINER_CIDR']).hosts()))
    agent_host = str(next(ipaddress.ip_network(values['AGENT_EGRESS_CIDR']).hosts()))
    # Positive: the effect worker network reaches the endpoint, and every reply passes.
    assert _reference_verdict(rules, saddr=effect_host, daddr=app, dport=port) == 'accept'
    assert _reference_verdict(rules, saddr=lan_host, daddr=app, dport=port, reply=True) == 'accept'
    # Negative: the management LAN, the application namespace, the Agent Host namespace and DATA_HOST are dropped.
    for source in (lan_host, app_host, agent_host, values['DATA_HOST_ADDR']):
        assert _reference_verdict(rules, saddr=source, daddr=app, dport=port) == 'drop', source
    # The port is a whitelisted parameter: a privileged or malformed value is refused.
    for bad in ('443', 'x', '70000'):
        broken = tmp_path / f'bad-ref-{bad}.env'
        broken.write_text(env.read_text().replace(f'REFERENCE_ENDPOINT_PORT={port}', f'REFERENCE_ENDPOINT_PORT={bad}'))
        broken.chmod(0o600)
        with pytest.raises(render.Refused, match='REFERENCE_ENDPOINT_PORT'):
            render.load_env(broken)


def test_replies_of_admitted_connections_pass_the_forward_chains(tmp_path):
    """2026-10-14 first start: the drop rules keyed on the original destination also matched the reply direction of a DNAT'ed
    connection (the reply carries the container's source address), so APP_HOST never received PostgreSQL's SYN-ACK."""
    for name in ('data-host.nft', 'app-host.nft'):
        text = (ROOT / 'deploy/stage/firewall' / (name + '.template')).read_text()
        chain = text[text.index('chain forward'):]
        rules = [l.strip() for l in chain.splitlines()[1:] if l.strip() and not l.strip().startswith('#')]
        assert rules[0].startswith('type filter hook forward') and rules[1] == 'ct direction reply accept', (name, rules[:2])


def test_the_gateway_passes_the_public_host_to_the_api():
    """2026-10-15 first start: for an HTTPS upstream Caddy rewrites Host to the upstream address and the API's browser session
    route refused every request (403); the public host:port is passed on explicitly."""
    text = (ROOT / 'deploy/stage/config/Caddyfile.template').read_text()
    proxy = text[text.index('reverse_proxy'):text.index('header {')]
    assert 'header_up Host {http.request.hostport}' in proxy


# G-APP-SUBNET (run sheet 4.2 external probe, 2026-10-15): the nft rules match container networks by source CIDR; a Compose network
# without a fixed subnet gets whatever Docker picks and every rule for it silently misses. Host-side sources are not container networks.
HOST_SOURCE_CIDRS = {'TRUSTED_LAN_CIDR', 'MGMT_SOURCE_CIDR'}


def nft_container_cidrs(template):
    return set(re.findall(r'ip saddr __([A-Z0-9_]+_CIDR)__', template.read_text())) - HOST_SOURCE_CIDRS


def compose_subnet_variables(compose_doc):
    """{network: variable} for networks whose ipam subnet is exactly ${VARIABLE:?...}."""
    out = {}
    for name, spec in (compose_doc.get('networks') or {}).items():
        for config in ((spec or {}).get('ipam') or {}).get('config') or []:
            m = re.fullmatch(r'\$\{([A-Z0-9_]+):\?[^}]*\}', str(config.get('subnet', '')))
            if m:
                out[name] = m.group(1)
    return out


def subnet_guard_problems(template, compose_doc, compose_env_text, stage_env_text):
    """Every container CIDR of the nft template is the fixed ipam subnet of a Compose network, the Compose env file sets it, and to the
    same value as stage.env (the renderer's input)."""
    fixed = set(compose_subnet_variables(compose_doc).values())
    compose_env = dict(l.split('=', 1) for l in compose_env_text.splitlines() if l and not l.startswith('#') and '=' in l)
    stage_env = dict(l.split('=', 1) for l in stage_env_text.splitlines() if l and not l.startswith('#') and '=' in l)
    problems = []
    for var in sorted(nft_container_cidrs(template)):
        if var not in fixed:
            problems.append('no Compose network with a fixed subnet: ' + var)
        elif var not in compose_env:
            problems.append('Compose env does not set: ' + var)
        elif compose_env[var] != stage_env.get(var):
            problems.append('Compose env and stage.env differ: ' + var)
    return problems


def test_every_container_cidr_of_the_app_firewall_is_a_fixed_compose_subnet():
    template = STAGE / 'firewall/app-host.nft.template'
    assert nft_container_cidrs(template) == {'APP_CONTAINER_CIDR', 'EFFECT_EGRESS_CIDR', 'AGENT_EGRESS_CIDR'}
    assert subnet_guard_problems(template, compose('compose.app.example.yaml'), (STAGE / 'compose.app.env.example').read_text(),
                                 (STAGE / 'stage.env.example').read_text()) == []
    assert compose_subnet_variables(compose('compose.app.example.yaml'))['app'] == 'APP_CONTAINER_CIDR'


def test_the_subnet_guard_catches_the_unfixed_application_network():
    """Reverse check: the Compose file of 1fcc2d6 (app: {}) and its env file without APP_CONTAINER_CIDR are refused."""
    doc = compose('compose.app.example.yaml')
    doc['networks']['app'] = {}
    env = '\n'.join(l for l in (STAGE / 'compose.app.env.example').read_text().splitlines() if not l.startswith('APP_CONTAINER_CIDR='))
    template, stage_env = STAGE / 'firewall/app-host.nft.template', (STAGE / 'stage.env.example').read_text()
    assert subnet_guard_problems(template, doc, env, stage_env) == ['no Compose network with a fixed subnet: APP_CONTAINER_CIDR']
    fixed = compose('compose.app.example.yaml')
    assert subnet_guard_problems(template, fixed, env, stage_env) == ['Compose env does not set: APP_CONTAINER_CIDR']
    other = (STAGE / 'compose.app.env.example').read_text().replace('APP_CONTAINER_CIDR=', 'APP_CONTAINER_CIDR=203.0.113.0/28\n#')
    assert subnet_guard_problems(template, fixed, other, stage_env) == ['Compose env and stage.env differ: APP_CONTAINER_CIDR']

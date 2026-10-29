"""NX-032 assembly dry run (local, clean catalog PostgreSQL): the stage path from bootstrap to the worker configuration files,
with production tools and the stage's own manifests wherever they exist.

    bootstrap -> stage_secrets generate + setup-credentials -> register (bootstrap window) -> browser (G1, identity allowance)
    -> initial_identity --create (the consumer's human) -> publish (system catalog through 0050, configurator window)
    -> business_setup -> catalog_setup -> enterprise_setup -> nexloop-relay-enroll -> tenant_config (recall types, run profile)

Test-only pieces, each named here so the stage run sheet can replace it:
- publish: `publish_catalog` compiles the catalog (T3's compile_catalog once merged, else the system and business catalogs) and
  applies it through the configurator window; it stands in for `stage_secrets.py publish` (T3) until that is merged.
- the RoleDefinition the run profile names: no production tool creates one yet (gap), so it is created here with the published
  RoleDefinition.create Action through a governed call of the business owner, if that principal may; otherwise it is reported.
Recipes: deploy/examples/nx037/{business,catalog,enterprise-setup}-recipe.example.json and identity-human.example.json, with
their placeholders filled by synthetic values (tests/support/tenants.py tenant; no real names)."""
import json
import re
import secrets
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'deploy/examples/nx037'
CATALOG_AUTHOR = 'nexloop-system-catalog'
CATALOG_FROM = datetime(2026, 10, 15, tzinfo=UTC)


def filled(name, **values):
    text = (EXAMPLES / name).read_text()
    for key, value in values.items():
        text = text.replace('<' + key + '>', value)
    assert '<' not in text, re.findall(r'<[A-Z_]+>', text)
    return json.loads(text)


def private(directory, name, text):
    from configurator_window import write_private
    path = Path(directory) / name
    write_private(path, text)
    return path


def compile_catalog(tenant):
    """The tenant catalog in the 0050 shape. Uses T3's nexloop_eios.catalog_publish.compile_catalog (the manifests) when it is
    merged; until then the system catalog (system_catalog, T1) plus the business types and Actions (business-object-types.v1.json,
    business-actions.v1.json with the capabilities.v1.json snapshots, T2), compiled the same way."""
    try:
        from nexloop_eios import catalog_publish
    except ImportError:
        catalog_publish = None
    if catalog_publish is not None:
        return catalog_publish.compile_catalog(tenant, capabilities_path=ROOT / 'deploy/configuration/capabilities.v1.json',
                                               system_actions_path=ROOT / 'deploy/configuration/system-actions.v1.json',
                                               business_actions_path=ROOT / 'deploy/configuration/business-actions.v1.json',
                                               system_types_path=ROOT / 'deploy/ontology/system-object-types.v1.json',
                                               business_types_path=ROOT / 'deploy/ontology/business-object-types.v1.json')
    from nexloop_eios import business_actions, business_object_types, catalog_manifests, system_object_types
    from nexloop_eios import system_catalog as C
    types = {name: t.model_dump(mode='json') for name, t in C.object_types().items()}
    for row in system_object_types.trusted_object_types(ROOT / 'deploy/ontology/system-object-types.v1.json') + \
            business_object_types.trusted_object_types(ROOT / 'deploy/ontology/business-object-types.v1.json'):
        assert row['type_name'] not in types, row['type_name']
        types[row['type_name']] = row
    actions = C.compile_system_actions(tenant=tenant, created_by=CATALOG_AUTHOR, created_at=CATALOG_FROM)
    business = business_actions.load(ROOT / 'deploy/configuration/business-actions.v1.json')
    by_name = {}
    for (name, _version), snapshot in catalog_manifests.snapshots(catalog_manifests.load(ROOT / 'deploy/configuration/capabilities.v1.json')).items():
        by_name.setdefault(name, snapshot)
    used = {a['capability_name'] for a in business['actions']}
    system_names = {a['definition']['stable_name'] for a in actions}
    actions += [a for a in business_actions.compile_actions(business, tenant=tenant, created_by=CATALOG_AUTHOR, created_at=CATALOG_FROM,
                                                            object_types=list(types.values()), capabilities={n: by_name[n] for n in used})
                if a['definition']['stable_name'] not in system_names]
    functions = C.compile_system_functions(tenant=tenant, created_by=CATALOG_AUTHOR, created_at=CATALOG_FROM)
    return {'object_types': [types[n] for n in sorted(types)], 'actions': actions, 'functions': functions}


def publish_catalog(out, *, tenant, bootstrap_dsn_file, signing_key_id):
    """The compiled catalog in one 0050 manifest through the configurator window: stand-in for stage_secrets.py publish (T3)."""
    from configurator_window import configurator_window
    from nexloop_eios import service_grants as G
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    compiled = compile_catalog(tenant)
    with configurator_window(bootstrap_dsn_file) as dsn:
        current = G.inventory(dsn, tenant)
        body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
                'manifest_id': str(uuid.uuid4()), **compiled, 'authority_facts': [], 'service_credentials': [], 'browser_applications': [],
                'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
        apply_manifest_with_secrets(body, database_url_file=dsn, signing_key_file=Path(out) / 'management' / 'signing.key.hex',
                                    signing_key_id=signing_key_id, service_secrets={})
    return {k: len(v) for k, v in compiled.items()}


def run_tool(module, argv, timeout=60):
    """A production CLI as its own process (the stage runs each as a one-off container)."""
    result = subprocess.run([sys.executable, '-m', module, *argv], capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


class Stage:
    """Paths and identities of one dry run (tmp directory, PG conninfo)."""

    def __init__(self, pg, root, out, tenant):
        self.pg, self.root, self.out, self.tenant = pg, Path(root), Path(out), tenant
        self.api_dsn = private(root, 'api-dsn', make_conninfo(pg, user='nexloop_api'))
        self.bootstrap_dsn = private(root, 'bootstrap-dsn', make_conninfo(pg, user='nexloop_bootstrap'))
        material = bytes.fromhex((self.out / 'management' / 'signing.key.hex').read_text())
        self.signing_key = self.root / 'signing.key'
        from configurator_window import write_private
        write_private(self.signing_key, material)
        self.artifacts = self.root / 'artifacts'

    def setup_file(self, name):
        return self.out / 'setup' / name

    def backend_args(self):
        return ['--database-url-file', str(self.api_dsn), '--signing-key-file', str(self.signing_key), '--signing-key-id', 'stage-1',
                '--artifact-root', str(self.artifacts)]


def end_of_trial(days=3):
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


def new_uuid():
    return str(uuid.uuid4())


def token():
    return secrets.token_urlsafe(24)


# ---- workers: the stage Compose command of each service, once ------------------------------------------------------------------
COMPOSE_APP = ROOT / 'deploy/stage/compose.app.example.yaml'
# /etc/nexloop/<file> of the stage (run-41 §4.1): repository configuration files, the claim-match example and tenant_config output.
REPOSITORY_CONFIG = {'commitments.json': 'deploy/configuration/commitments.v1.json', 'commercial.json': 'deploy/configuration/commercial.v1.json',
                     'plan-reevaluation.json': 'deploy/configuration/plan-reevaluation.v2.json',
                     'reply-guarantee.json': 'deploy/configuration/reply-guarantee.v3.json', 'claim-match.json': 'deploy/stage/claim-match.example.json'}
VARIABLES = {'SIGNING_KEY_ID': 'stage-1'}


class Skip(Exception):
    """A service whose command needs something this dry run does not provide (named in the message)."""


def compose_services():
    import yaml
    return yaml.safe_load(COMPOSE_APP.read_text())


# Services that are servers (no --once): started, ready line awaited, then stopped. domain-worker is a sh loop around one run.
SERVERS = {'webchat-delivery': 'WEBCHAT_DELIVERY_PORT', 'api-1': 'API_INTERNAL_PORT'}  # ready when its loopback port accepts a connection


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def manual_files(st, tmp, variables):
    """The manual (no generator) files of phase A this dry run writes itself, as NX-037-provider-routing.md §2 describes them:
    the action worker's provider routes with the webchat route only (the enterprise route needs the enterprise's endpoint, phase C).
    Paths inside the files are the dry run's, not /run/secrets."""
    secrets = st.out / 'app' / 'secrets'
    webchat = {'origin': 'https://127.0.0.1:' + variables['WEBCHAT_DELIVERY_PORT'], 'credential_file': str(secrets / 'action-worker/webchat-provider-credential'),
               'ca_file': str(secrets / 'action-worker/webchat-provider-ca.crt'), 'timeout': 2.0}
    config = private(tmp, 'webchat-provider-config', json.dumps(webchat))
    routes = private(tmp, 'provider-routes', json.dumps({'schema': 'nexloop-effect-provider-routes/1', 'routes': {'nexloop.service.request': str(config)}}))
    return {'action_worker_provider_routes': routes, 'action_worker_webchat_provider': config}


def worker_command(st, name, compose, config_dir, tmp, variables=None, overlay=None, replace=None):
    """(argv, env) of service `name` for this dry run: /run/secrets/* -> the generated tree (DSNs replaced by a local trust DSN
    for the same role), /etc/nexloop/* -> config_dir, /var/lib/nexloop/* -> tmp, ${VAR} -> VARIABLES; plus --once."""
    import os
    import shutil
    variables, overlay = {**VARIABLES, **(variables or {})}, overlay or {}
    tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    service = compose['services'][name]
    entry, args = service.get('entrypoint'), [str(a) for a in service.get('command') or []]
    if entry == ['sh', '-c']:
        # One iteration of the loop: the python command between `python -m` and the first `;`.
        script = args[0]
        for key, value in variables.items():
            script = re.sub(r'\$\{' + key + r'(:[?-][^}]*)?\}', value, script)
        loop = re.search(r'python -m (\S+) ([^;]*);', script)
        if loop is None:
            raise Skip('shell loop without one python command')
        entry, args = ['python', '-m', loop.group(1)], loop.group(2).split()
    if not entry or entry[0] in ('sh', 'sleep'):
        raise Skip('no single-process entrypoint')
    if entry[:2] == ['python', '-m']:
        argv = [sys.executable, '-m', entry[2], *entry[3:]]
    else:
        script = Path(sys.executable).parent / entry[0]
        if not script.is_file():
            raise Skip('console script not installed: ' + entry[0])
        argv = [str(script), *entry[1:]]
    for a in args:
        for key, value in variables.items():
            a = re.sub(r'\$\{' + key + r'(:[?-][^}]*)?\}', value, a)
        # Unset optional deployment variables take their compose default (${VAR:-default}).
        a = re.sub(r'\$\{[A-Z_][A-Z0-9_]*:-([^}]*)\}', r'\1', a)
        if '${' in a:
            raise Skip('deployment variable: ' + a)
        if a.startswith('/run/secrets/'):
            secret = a.removeprefix('/run/secrets/')
            if secret in overlay:
                argv.append(str(overlay[secret]))
                continue
            spec = compose['secrets'][secret]['file']
            path = st.out / 'app' / 'secrets' / re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', spec)
            if secret.endswith('_dsn'):
                role = re.search(r'user=(\S+)', path.read_text()).group(1)
                local = tmp / ('dsn-' + secret)
                if not local.exists():
                    private(tmp, local.name, make_conninfo(st.pg, user=role))
                a = str(local)
            elif not path.is_file():
                raise Skip('secret not generated in phase A: ' + secret)
            else:
                a = str(path)
        elif a.startswith('/etc/nexloop/'):
            path = Path(config_dir) / a.removeprefix('/etc/nexloop/')
            if not path.is_file():
                raise Skip('configuration file missing: ' + path.name)
            a = str(path)
        elif a.startswith('/var/lib/nexloop/'):
            path = tmp / 'var' / a.removeprefix('/var/lib/nexloop/')
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            a = str(path)
        argv.append(a)
    for option, value in (replace or {}).items():
        argv[argv.index(option) + 1] = value
    env = {k: v for k, v in os.environ.items() if not k.startswith('NEXLOOP_TEST_')}
    env.update({k: str(v) for k, v in (service.get('environment') or {}).items()})
    env['PYTHONPATH'] = str(ROOT / 'packages/eios-core/src')
    if shutil.which('node'):
        env['PATH'] = os.environ['PATH']
    once = name not in SERVERS and not (service.get('entrypoint') == ['sh', '-c'])
    return argv + (['--once'] if once else []), env


def install_config(config_dir, extra):
    """The repository configuration files as the services' private files (0600), plus the tenant_config outputs."""
    for name, source in REPOSITORY_CONFIG.items():
        private(config_dir, name, (ROOT / source).read_text())
    for name, path in extra.items():
        private(config_dir, name, Path(path).read_text())

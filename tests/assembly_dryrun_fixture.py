"""NX-032 assembly dry run (local, clean catalog PostgreSQL): the stage path from bootstrap to the worker configuration files,
with production tools and the stage's own manifests wherever they exist.

    bootstrap -> stage_secrets generate + setup-credentials -> register (bootstrap window) -> browser (G1, identity allowance)
    -> initial_identity --create (the consumer's human) -> publish (system catalog through 0050, configurator window)
    -> business_setup -> catalog_setup -> enterprise_setup -> nexloop-relay-enroll -> tenant_config (recall types, run profile)

Test-only pieces, each named here so the stage run sheet can replace it:
- publish: `publish_system_catalog` builds the 0050 manifest from nexloop_eios.system_catalog and applies it through the
  configurator window; it stands in for `stage_secrets.py publish` (T3) until that is merged.
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


def publish_system_catalog(out, *, tenant, bootstrap_dsn_file, signing_key_id):
    """The system object types, Actions and Functions (system_catalog, T1) in one 0050 manifest through the configurator window.
    Stand-in for stage_secrets.py publish (T3)."""
    from configurator_window import configurator_window
    from nexloop_eios import service_grants as G
    from nexloop_eios import system_catalog as C
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    types = C.object_types()
    actions = C.compile_system_actions(tenant=tenant, created_by=CATALOG_AUTHOR, created_at=CATALOG_FROM)
    functions = C.compile_system_functions(tenant=tenant, created_by=CATALOG_AUTHOR, created_at=CATALOG_FROM)
    with configurator_window(bootstrap_dsn_file) as dsn:
        current = G.inventory(dsn, tenant)
        body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
                'manifest_id': str(uuid.uuid4()), 'object_types': [t.model_dump(mode='json') for t in types.values()], 'actions': actions,
                'functions': functions, 'authority_facts': [], 'service_credentials': [], 'browser_applications': [],
                'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
        apply_manifest_with_secrets(body, database_url_file=dsn, signing_key_file=Path(out) / 'management' / 'signing.key.hex',
                                    signing_key_id=signing_key_id, service_secrets={})
    return {'object_types': len(types), 'actions': len(actions), 'functions': len(functions)}


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

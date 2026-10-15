"""NX-032 A1 design §4.2/§4.3 (T6): scripts/deploy/tenant_config.py writes recall-types.json and relay-run-profile.json from a
tenant's published state, read-only, in the shapes the workers load (background_services.load_types,
message_relay.validate_run_profile), and refuses what is not published.

Clean catalog PostgreSQL: bootstrap and the stage register (test_stage_register_pg's fixture), then the object types published
through 0050 by the configurator (an otherwise empty manifest). Until T3 publishes Actions and tenant assembly creates the Role, the
fixture stands in for that published state: a RoleDefinition object row and the NexLoop.queue.operations:1 definition row are
inserted by the test administrator (test state only; production publishes them through 0050 and RoleDefinition.create)."""
import importlib.util
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.background_services import load_types
from nexloop_eios.message_relay import validate_run_profile
from test_stage_register_pg import TENANT, registered  # noqa: F401
from test_stage_secrets import S, tree  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('tenant_config', ROOT / 'scripts/deploy/tenant_config.py')
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)
ROLE = 'e' * 64


def publish_types(admin, configurator_dsn, out, *schemas):
    from nexloop_eios import service_grants as G
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    current = G.inventory(configurator_dsn, TENANT)
    body = {'schema_version': '1.0', 'tenant_id': TENANT, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
            'manifest_id': str(uuid.uuid4()), 'object_types': [s.model_dump(mode='json') for s in schemas], 'actions': [], 'functions': [],
            'authority_facts': [], 'service_credentials': [], 'browser_applications': [], 'browser_business_applications': [],
            'browser_rate_policies': [], 'identity_allowances': []}
    apply_manifest_with_secrets(body, database_url_file=configurator_dsn, signing_key_file=out / 'management' / 'signing.key.hex',
                                signing_key_id='stage-1', service_secrets={})


@pytest.fixture
def published(pg, admin, registered, tree, tmp_path):  # noqa: F811
    from eios.ontology.models import ObjectTypeDefinition
    from nexloop_eios.role_mapping import role_mapping_schemas
    publish_types(admin, tree['tmp'] / 'configurator-dsn', tree['out'], ObjectTypeDefinition(type_name='Consumer', version=1, only_edit_via_actions=True),
                  *role_mapping_schemas())
    now = datetime.now(UTC)
    properties = {'name': 'operations', 'responsibility': 'reply to consumers', 'ceiling_ref': 'ceiling:stage', 'active': True,
                  'valid_from': (now - timedelta(days=1)).isoformat(), 'valid_until': (now + timedelta(days=30)).isoformat()}
    admin.execute("insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,created_at,updated_at) "
                  "values(%s,'real','RoleDefinition',%s,1,%s,now(),now())", (TENANT, ROLE, Jsonb(properties)))
    socket_dsn = tmp_path / 'bootstrap-socket-dsn'
    socket_dsn.write_text(make_conninfo(pg, user='nexloop_bootstrap'))
    socket_dsn.chmod(0o600)
    return {'dsn': socket_dsn, 'properties': properties}


def publish_queue(admin, queue='operations', active=True):
    definition = {'tenant_id': TENANT, 'status': 'published', 'stable_name': f'NexLoop.queue.{queue}', 'version': 1}
    # catalog-exception: stub NexLoop.queue.* rows with toggled active (queue configuration cases)
    admin.execute("insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active) values(%s,'real',%s,%s,'{}',%s)",
                  (TENANT, f'eios:action:NexLoop.queue.{queue}:1', Jsonb(definition), active))


def run(published, tmp_path, capsys, *argv):
    out = tmp_path / ('config-' + uuid.uuid4().hex[:8] + '.json')
    code = T.main([argv[0], '--tenant', TENANT, '--bootstrap-dsn-file', str(published['dsn']), '--out', str(out), *argv[1:]])
    return code, json.loads(capsys.readouterr().out), out


def test_recall_types_selects_published_types_in_the_loader_shape(published, tmp_path, capsys):
    code, printed, out = run(published, tmp_path, capsys, 'recall-types')
    assert code == 0 and printed == {'ok': True, 'command': 'recall-types', 'types': ['Consumer']}
    assert json.loads(out.read_text()) == {'Consumer': None} and load_types(out) == {'Consumer': None}
    assert out.stat().st_mode & 0o777 == 0o600
    code, printed, out = run(published, tmp_path, capsys, 'recall-types', '--types', 'RoleDefinition,Consumer')
    assert code == 0 and load_types(out) == {'Consumer': None, 'RoleDefinition': None}
    # A type that is not published (or not a type name) is refused and nothing is written.
    for types, detail in (('Consumer,Commitment', 'types not published for the tenant: Commitment'), ('../x', '--types')):
        code, printed, out = run(published, tmp_path, capsys, 'recall-types', '--types', types)
        assert code == 2 and printed['detail'].startswith(detail) and not out.exists()


def test_relay_run_profile_names_the_published_role_and_passes_the_relay_validator(published, admin, tmp_path, capsys):
    cost = ['--maximum-cost', '2', '--currency', 'CNY']
    code, printed, out = run(published, tmp_path, capsys, 'relay-run-profile', '--role-id', ROLE, *cost)
    assert code == 2 and printed['detail'] == 'NexLoop.queue.operations:1 is not published (active) for the tenant' and not out.exists()
    publish_queue(admin)
    code, printed, out = run(published, tmp_path, capsys, 'relay-run-profile', '--role-id', ROLE, *cost)
    assert code == 0 and printed == {'ok': True, 'command': 'relay-run-profile', 'queue': 'operations', 'runtime_profile': 'deepseek-flash'}
    profile = json.loads(out.read_text())
    assert validate_run_profile(profile) == profile and out.stat().st_mode & 0o777 == 0o600
    assert profile == {'role_ref': 'role:' + ROLE, 'context_manifest_ref': 'artifact:context-bind-pending', 'runtime_profile': 'deepseek-flash',
                       'budget': {'maximum_model_turns': 8, 'maximum_tool_calls': 8, 'active_timeout_seconds': 120, 'maximum_cost': '2', 'currency': 'CNY'},
                       'runtime_owner_epoch': 1, 'queue': 'operations'}
    # The output is never overwritten.
    assert T.main(['relay-run-profile', '--tenant', TENANT, '--bootstrap-dsn-file', str(published['dsn']), '--out', str(out), '--role-id', ROLE, *cost]) == 2
    assert json.loads(capsys.readouterr().out)['detail'] == 'output exists' and json.loads(out.read_text()) == profile


@pytest.mark.parametrize('change,detail', [
    ({'role': 'f' * 64}, 'RoleDefinition not found in the real world of the tenant'),
    ({'role': 'not-an-id'}, '--role-id: the 64-hex object id of a RoleDefinition'),
    ({'inactive': True}, 'RoleDefinition is not active and currently valid'),
    ({'expired': True}, 'RoleDefinition is not active and currently valid'),
    ({'queue': 'missing'}, 'NexLoop.queue.missing:1 is not published (active) for the tenant'),
    ({'args': ['--maximum-tool-calls', '500']}, 'the run profile does not pass message_relay.validate_run_profile (budget bounds, queue name, owner epoch)'),
    ({'args': ['--runtime-owner-epoch', '0']}, 'the run profile does not pass message_relay.validate_run_profile (budget bounds, queue name, owner epoch)'),
    ({'args': ['--currency', 'yuan']}, 'the run profile does not pass message_relay.validate_run_profile (budget bounds, queue name, owner epoch)'),
])
def test_relay_run_profile_refuses_what_is_not_published_or_not_valid(published, admin, tmp_path, capsys, change, detail):
    publish_queue(admin)
    if change.get('inactive') or change.get('expired'):
        properties = dict(published['properties'])
        if change.get('inactive'):
            properties['active'] = False
        else:
            properties['valid_until'] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        admin.execute("update ontology.objects set properties=%s where tenant_id=%s and object_id=%s", (Jsonb(properties), TENANT, ROLE))
    argv = ['--role-id', change.get('role', ROLE), '--queue', change.get('queue', 'operations'), '--maximum-cost', '2', '--currency', 'CNY', *change.get('args', [])]
    code, printed, out = run(published, tmp_path, capsys, 'relay-run-profile', *argv)
    assert code == 2 and printed['detail'] == detail and not out.exists()


def test_tenant_config_never_writes_the_database(published, admin, tmp_path, capsys):
    publish_queue(admin)
    before = admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()
    assert run(published, tmp_path, capsys, 'recall-types')[0] == 0
    assert run(published, tmp_path, capsys, 'relay-run-profile', '--role-id', ROLE, '--maximum-cost', '2', '--currency', 'CNY')[0] == 0
    assert admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone() == before
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s", (TENANT,)).fetchone() == (1,)


def test_the_claim_match_example_is_the_minimal_configuration_the_matcher_loads(tmp_path):
    import yaml
    from nexloop_eios.background_services import load_match_configuration
    example = ROOT / 'deploy/stage/claim-match.example.json'
    assert json.loads(example.read_text()) == {}
    installed = tmp_path / 'claim-match.json'  # installed as the service's private file (0600), as the loader requires
    S.write_private(installed, example.read_text())
    configuration = load_match_configuration(installed)
    assert (configuration.edit_actions, configuration.create_actions) == ({}, {})
    compose = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    assert any(v.endswith('/config/claim-match.json:/etc/nexloop/claim-match.json:ro') for v in compose['services']['claim-matcher']['volumes'])

"""G-ROLE (dispatcher ruling A, 2026-10-15): a service principal may declare requested_scopes beyond its grants' resource_type.operation,
only ontology.roles.manage, which 0057/0081 require in the caller credential for the role Actions.

- validation: any other scope (or a malformed list) is refused by service_grants and by stage_secrets.merged_manifest;
- clean PostgreSQL: two principals with the same grant (RoleDefinition.create) registered through `register` from a principals file,
  one with requested_scopes [ontology.roles.manage]: after the catalog is published, it creates a RoleDefinition through the 0057 path
  (RoleMappingPort, a governed Action); the other is refused and nothing is written."""
import copy
import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from nexloop_eios import service_grants as G
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_stage_secrets import S, tree  # noqa: F401
import assembly_dryrun_fixture as A  # noqa: E402

TENANT = TENANT_A
ROLES_MANAGE = 'ontology.roles.manage'


def probe(role, scopes=None):
    item = {'role': role, 'subject_id': f'nexloop-service-{role}', 'principal_id': f'nexloop-service-{role}-principal',
            'credential_id': f'nexloop-service-{role}-key', 'credential_reference': role, 'application_id': f'eios:application:nexloop-{role}',
            'database_role': 'nexloop_api', 'purpose': 'G-ROLE scope probe (test only).', 'source_task': 'NX-032 G-ROLE'}
    if scopes is not None:
        item['requested_scopes'] = scopes
    return item


def principals_file(directory, *items):
    stage = json.loads(S.PRINCIPALS.read_text())
    stage['principals'] += list(items)
    for item in items:
        stage['grants'].append({'principal': item['role'], 'resource_type': 'action', 'resource_id': 'eios:action:RoleDefinition.create:1',
                                'operations': ['execute'], 'purpose': 'probe', 'source_task': 'NX-032 G-ROLE'})
        stage['credential_files']['setup/' + item['role'].replace('_', '-') + '-credential'] = item['role']
    path = directory / 'stage-principals.json'
    path.write_text(json.dumps(stage))
    return path


@pytest.mark.parametrize('scopes', [['action.execute'], ['ontology.roles.manage', 'ontology.roles.manage'], [], 'ontology.roles.manage',
                                    ['ontology.admin']])
def test_only_ontology_roles_manage_may_be_requested(tmp_path, scopes):
    base = G.load(S.GRANTS)
    bad = copy.deepcopy(base)
    bad['principals'][0]['requested_scopes'] = scopes
    with pytest.raises(G.ServiceGrantsRejected, match='principal_requested_scopes'):
        G.validate(bad)
    with pytest.raises(S.Refused, match='stage principal shape'):
        S.merged_manifest(principals_path=principals_file(tmp_path, probe('role_probe', scopes)))


def test_the_requested_scope_enters_the_credential_binding_and_scope_facts():
    manifest, _ = S.merged_manifest()
    expires = datetime.now(UTC) + timedelta(days=1)
    with_scope = {**probe('role_probe', [ROLES_MANAGE])}
    grants = [{'principal': 'role_probe', 'resource_type': 'action', 'resource_id': 'eios:action:RoleDefinition.create:1', 'operations': ['execute'],
               'purpose': 'probe', 'source_task': 'probe'}]
    for item, expected in ((with_scope, {'action.execute', ROLES_MANAGE}), (probe('role_probe'), {'action.execute'})):
        binding, facts = G.compile_principal({**manifest, 'grants': grants}, TENANT, item, credential_expires_at=expires)
        assert set(binding.requested_scopes) == expected
        scope = [f for kind, _, f in facts if kind == 'scope']
        assert scope and all(set(f.authorized_scopes) == expected and set(f.catalog_scopes) == expected for f in scope)


def test_a_registered_principal_with_the_scope_creates_a_role_and_one_without_is_refused(pg, admin, tree, tmp_path):  # noqa: F811
    from nexloop_eios.backend import open_backend
    from nexloop_eios.role_mapping import RoleMappingPort
    out = tmp_path / 'stage'
    shutil.copytree(tree['out'], out)
    path = principals_file(tmp_path, probe('role_probe', [ROLES_MANAGE]), probe('role_plain'))
    S.setup_credentials(out, principals_path=path)
    bootstrap(admin)
    st = A.Stage(pg, tmp_path, out, TENANT)
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    from configurator_window import configurator_window
    with configurator_window(st.bootstrap_dsn) as dsn:
        S.register(out, tenant=TENANT, database_url_file=dsn, signing_key_id='stage-1', credential_expires_at=expires, principals_path=path)
    A.publish_catalog(out, tenant=TENANT, bootstrap_dsn_file=st.bootstrap_dsn, signing_key_id='stage-1')
    now = datetime.now(UTC)
    valid = dict(valid_from=(now - timedelta(seconds=1)).isoformat(), valid_until=(now + timedelta(days=1)).isoformat())
    with open_backend(database_url=st.api_dsn.read_text(), artifact_root=st.artifacts, signing_key_file=st.signing_key, signing_key_id='stage-1') as api:
        plain = api.authenticate(st.setup_file('role-plain-credential').read_text(), world='real')
        with pytest.raises(Exception):
            RoleMappingPort(plain).create_role(intent_id='g-role-plain', name='plain', responsibility='probe', ceiling_ref='ceiling:probe', **valid)
        assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone() == (0,)
        scoped = api.authenticate(st.setup_file('role-probe-credential').read_text(), world='real')
        created = RoleMappingPort(scoped).create_role(intent_id='g-role-scoped', name='scoped', responsibility='probe', ceiling_ref='ceiling:probe', **valid)
    assert created['type_name'] == 'RoleDefinition'
    assert admin.execute("select count(*) from ontology.objects where type_name='RoleDefinition'").fetchone() == (1,)

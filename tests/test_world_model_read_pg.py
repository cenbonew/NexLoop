"""Enterprise world model W1 (0260; docs/design/workbench-world-model-design.md, owner 2026-10-19 D1-D7): GET /api/v1/workbench/world-model.

Real PostgreSQL and real workbench login cookies (the slice 1 fixture). Covers: owner and operator read the same metadata snapshot (D1),
it validates against packages/contracts/workbench-world-model.schema.json; the current version of each type wins; instance counts are
counts only; a property of an owner-restricted group is listed by name with withheld=true and nothing else; the customer and an
outsider get 403; revoking the read Action is immediate; query parameters are refused; every other verb still answers (the wrapper
delegates); nothing is written."""
import json
from pathlib import Path

import jsonschema
from psycopg.types.json import Jsonb

from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_workbench_read_pg import READ, ROLES, TENANT, provision, revoke_member, workbench, workbench_login, write_facts  # noqa: F401

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'packages/contracts/workbench-world-model.schema.json').read_text())
PROFILE = {'type_name': 'SyntheticProfile', 'properties': [
    {'property_name': 'nickname', 'value_type': 'string', 'required': True, 'nullable': False, 'description': 'synthetic'},
    {'property_name': 'spending_band', 'value_type': 'string', 'required': False, 'nullable': True, 'description': 'synthetic restricted'}],
    'property_groups': [{'group_name': 'basic', 'property_names': ['nickname']}, {'group_name': 'spending', 'property_names': ['spending_band']}],
    'title_property': 'nickname', 'primary_key': []}


def seed_model(admin):  # noqa: F811
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'SyntheticProfile',1,%s),(%s,'SyntheticProfile',2,%s)",
                  (TENANT, Jsonb({**PROFILE, 'properties': PROFILE['properties'][:1], 'property_groups': PROFILE['property_groups'][:1]}), TENANT, Jsonb(PROFILE)))
    admin.execute("insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,'synthetic_profile_of',1,%s)",
                  (TENANT, Jsonb({'relation_name': 'synthetic_profile_of', 'source_type': 'SyntheticProfile', 'target_type': 'Consumer', 'cardinality': 'many_to_one'})))
    write_facts(admin, [{'kind': 'property_group_restriction', 'key': ['SyntheticProfile'],
                         'payload': {'tenant_id': TENANT, 'type_name': 'SyntheticProfile', 'restricted_groups': ['spending'], 'decision': 'synthetic'}}])


def test_members_read_published_model_metadata_with_restricted_properties_withheld(workbench):
    w, admin = workbench, workbench['admin']
    provision(w)
    seed_model(admin)
    before = admin.execute("select count(*) from runtime.nexloop_workbench_read_audit").fetchone()[0]
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        response = client.get('/api/v1/workbench/world-model')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        model = response.json()
        jsonschema.validate(model, SCHEMA)
        types = {t['name']: t for t in model['object_types']}
        profile = types['SyntheticProfile']
        assert profile['version'] == 2 and profile['objects'] == 0 and profile['title_property'] == 'nickname'
        assert profile['properties'] == [
            {'name': 'nickname', 'withheld': False, 'value_type': 'string', 'required': True, 'nullable': False, 'group': 'basic', 'visibility': 'normal', 'description': 'synthetic'},
            {'name': 'spending_band', 'withheld': True}]
        assert 'synthetic restricted' not in response.text
        # Instance counts only: the fixture's Consumer and Conversation exist, their content is not in the answer.
        assert types['Consumer']['objects'] >= 1 and w['f']['consumer'] not in response.text and '短信' not in response.text
        relation = next(r for r in model['relation_types'] if r['name'] == 'synthetic_profile_of')
        assert (relation['source_type'], relation['target_type'], relation['cardinality']) == ('SyntheticProfile', 'Consumer', 'many_to_one')
        assert model['actions'] and all(a['resource_id'].startswith('eios:action:') for a in model['actions'])
        assert client.get('/api/v1/workbench/world-model?view=graph').status_code == 422
        # The wrapper delegates every other verb unchanged.
        assert client.get('/api/v1/workbench/goals').status_code == 200 and client.get('/api/v1/workbench/settings').status_code == 200
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        operator = client.get('/api/v1/workbench/world-model')
        assert operator.status_code == 200
        assert {k: v for k, v in operator.json().items() if k != 'computed_at'} == {k: v for k, v in model.items() if k != 'computed_at'}
        # Revoking the read Action takes effect on the next request.
        write_facts(admin, revoke_member(ROLES, TENANT, w['operator']['principal_id'], [READ]))
        assert client.get('/api/v1/workbench/world-model').status_code == 403
    for who in ('customer', 'outsider'):
        with w['client']() as client:
            assert workbench_login(client, w[who]).status_code == 200
            assert client.get('/api/v1/workbench/world-model').status_code == 403
    # Metadata reads write no read-audit row (no instance content is read) and nothing else.
    assert admin.execute("select count(*) from runtime.nexloop_workbench_read_audit").fetchone()[0] == before


def test_world_model_verb_is_refused_to_the_service_role_directly(workbench, pg):  # noqa: F811
    import psycopg
    from psycopg.conninfo import make_conninfo
    with psycopg.connect(make_conninfo(pg, user='nexloop_domain_worker')) as db:
        try:
            db.execute("select authz.nexloop_workbench_world_model(%s,'real')", (TENANT,))
            raise AssertionError('the snapshot function must not be executable by a service role')
        except psycopg.errors.InsufficientPrivilege:
            pass

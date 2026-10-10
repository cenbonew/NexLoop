"""NX-028 D2: the public workbench role manifest and its compilation to Human grant facts (pure, no database)."""
import copy
import json
from pathlib import Path

import pytest

from nexloop_eios.trusted_configuration import FACT_MODELS
from nexloop_eios.workbench_roles import WorkbenchRolesRejected, compile_members, configuration_payloads, load, main, revoke_member, validate_roles

ROOT = Path(__file__).resolve().parents[1]
ROLES = load(ROOT / 'deploy/authorization/workbench-roles.v1.json')
MEMBERS = {'schema_version': 'nexloop-workbench-members/1', 'tenant_id': 'synthetic-a', 'application_id': 'synthetic-workbench-app',
           'caller_application_id': 'eios:application:synthetic-workbench',
           'members': [{'principal_id': 'owner-principal', 'subject_id': 'owner-subject', 'role': 'owner'},
                       {'principal_id': 'operator-principal', 'subject_id': 'operator-subject', 'role': 'operator'}]}


def test_d2_owner_only_actions_stay_with_the_owner():
    validate_roles(ROLES)
    for action in ('Contact.release', 'Commitment.cancel', 'Commitment.mark_communication', 'Goal.publish', 'Budget.set', 'Control.set', 'Metric.approve'):
        resource = f'eios:action:{action}:1'
        assert resource in ROLES['roles']['owner'] and resource not in ROLES['roles']['operator'] + ROLES['roles']['reviewer']
    assert ROLES['roles']['reviewer'] == ['eios:action:ontology.schema.review:1']
    forged = copy.deepcopy(ROLES)
    forged['roles']['operator'].append('eios:action:Contact.release:1')
    with pytest.raises(WorkbenchRolesRejected):
        validate_roles(forged)
    for broken in ({**ROLES, 'roles': {**ROLES['roles'], 'admin': ['eios:action:x:1']}}, {**ROLES, 'schema_version': 'x'},
                   {**ROLES, 'roles': {**ROLES['roles'], 'owner': ['not-an-action']}}):
        with pytest.raises(WorkbenchRolesRejected):
            validate_roles(broken)


def test_compiled_facts_are_human_typed_deterministic_and_never_identity_facts():
    facts = compile_members(ROLES, MEMBERS)
    assert facts == compile_members(ROLES, MEMBERS)
    assert {f['kind'] for f in facts} == {'actor', 'subject_authority', 'application', 'resource_graph', 'grants', 'scope', 'controls', 'policies'}
    for f in facts:
        FACT_MODELS[f['kind']].model_validate_json(json.dumps(f['payload']))
    grants = {(f['key'][0], f['key'][1]) for f in facts if f['kind'] == 'grants'}
    assert ('operator-principal', 'eios:action:Contact.release:1') not in grants and ('owner-principal', 'eios:action:Contact.release:1') in grants
    assert all(f['payload']['subject_kind'] == 'human' for f in facts if f['kind'] in ('grants', 'subject_authority'))
    application = next(f for f in facts if f['kind'] == 'application')
    assert {r['resource_id'] for r in application['payload']['resources']} == {a for items in ROLES['roles'].values() for a in items}
    assert all(f['payload']['grants'] == [] for f in (dict(kind=k, key=key, payload=x.model_dump(mode='json')) for k, key, x in revoke_member(ROLES, 'synthetic-a', 'operator-principal')))


def test_members_file_is_validated_and_subject_ids_stay_out_of_the_stored_map(tmp_path, capsys):
    _, stored = configuration_payloads(ROLES, MEMBERS)
    assert stored['members'] == [{'principal_id': 'owner-principal', 'role': 'owner'}, {'principal_id': 'operator-principal', 'role': 'operator'}]
    for broken in ({**MEMBERS, 'members': MEMBERS['members'] * 2}, {**MEMBERS, 'members': [{**MEMBERS['members'][0], 'role': 'admin'}]},
                   {**MEMBERS, 'caller_application_id': 'synthetic'}, {**MEMBERS, 'extra': 1}):
        with pytest.raises(WorkbenchRolesRejected):
            configuration_payloads(ROLES, broken)
    members = tmp_path / 'members.json'
    members.write_text(json.dumps(MEMBERS))
    assert main(['--roles', str(ROOT / 'deploy/authorization/workbench-roles.v1.json'), '--members', str(members), '--check']) == 0
    assert main(['--roles', str(ROOT / 'deploy/authorization/workbench-roles.v1.json'), '--members', str(members), '--apply-members']) == 2
    assert 'database url file required' in capsys.readouterr().err

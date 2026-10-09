"""Role Context v5 (0087): v3 + governed Role policy provenance. Synthetic PG only."""
import copy
import json
import psycopg
import pytest
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from nexloop_eios.role_context_pack import encode_role_pack


def section(admin, plan, index=0):
    # Tenant context as set by the Role context command before it builds the snapshot.
    admin.execute("select set_config('eios.tenant_id',%s,false)", (plan['tenant'],))
    return admin.execute('select authz.nexloop_role_policy_context_section(%s,%s,%s)', (plan['commands'][index]['run_id'], plan['tenant'], 'real')).fetchone()[0]


def test_section_matches_binding_then_fails_closed_after_change(role_runtime_plan, admin):
    plan = role_runtime_plan
    value = section(admin, plan)
    pack = json.loads(plan['context_packs'][plan['commands'][0]['run_id']]['input'])
    assert pack['schema_version'] == 'nexloop.context-pack.v5' and pack['role_policy'] == value and value['grants_authority'] is False
    plan['edit_policy'](0, 'RoleExecutionCeiling', {'action_resources': ['eios:action:Goal.create:1']})
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Role ceiling Context changed'):
        section(admin, plan)
    # The other Run's Scope deactivation and binding expiry fail closed too.
    plan['edit_policy'](1, 'RoleAssignmentScope', {'active': False})
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Role scope Context changed'):
        section(admin, plan, 1)


def test_expired_binding_fails_closed(role_runtime_plan, admin):
    plan = role_runtime_plan
    admin.execute("update authz.nexloop_role_policy_bindings set expires_at=clock_timestamp()-interval '1 second' where run_id=%s", (plan['commands'][0]['run_id'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Role policy Context unavailable'):
        section(admin, plan)


def test_v5_copy_read_needs_current_policy_read(role_runtime_plan, admin):
    plan = role_runtime_plan; command = plan['commands'][0]
    pack = plan['context_packs'][command['run_id']]; artifact = pack['artifact_ref'].removeprefix('artifact:')
    principal = admin.execute('select source_principal from runtime.nexloop_role_trigger_events where run_id=%s', (command['run_id'],)).fetchone()[0]
    index = next(i for i, s in enumerate(plan['sources']) if s._session.authentication.subject_principal_id == principal)
    assert plan['sources'][index].read_artifact(artifact) == pack['input'].encode()
    ceiling = plan['role_policies'][principal]['ceiling_id']
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]'::jsonb) where tenant_id=%s and fact_kind='grants' and entity_key=%s",
                  (plan['tenant'], [principal, 'eios:property:RoleExecutionCeiling/' + ceiling + '/effect_units']))
    fresh = plan['api'].authenticate(plan['source_tokens'][index], world='real')
    with pytest.raises(Exception):
        fresh.read_artifact(artifact)


TAMPER = {
    'grants_authority': lambda p: p['role_policy'].__setitem__('grants_authority', True),
    'provenance': lambda p: p['role_policy'].__setitem__('ceiling_provenance', 'eios:object:' + 'f' * 64),
    'budget': lambda p: p['role_policy']['binding']['budget'].__setitem__('maximum_tool_calls', 99),
    'goal_outside_scope': lambda p: p['role_policy']['scope'].__setitem__('goal_ids', ['f' * 64]),
    'ceiling_ref': lambda p: p['role_binding']['definition'].__setitem__('ceiling_ref', 'f' * 64),
    'missing_section': lambda p: p.pop('role_policy'),
}


@pytest.mark.parametrize('tamper', sorted(TAMPER))
def test_encoder_rejects_tampered_v5(role_runtime_plan, tamper):
    plan = role_runtime_plan; command = plan['commands'][0]  # activated command (updated in place by the fixture)
    stored = plan['context_packs'][command['run_id']]['input']; pack = json.loads(stored)
    assert encode_role_pack(pack, command) == stored
    changed = copy.deepcopy(pack); TAMPER[tamper](changed)
    with pytest.raises(Exception):
        encode_role_pack(changed, command)

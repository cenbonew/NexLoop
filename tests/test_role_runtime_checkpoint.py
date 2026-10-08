"""Actual Source selection→Run registration→activation, legacy Context explicit."""
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
import pytest

def test_actual_two_role_runs_share_governed_plan_step(role_runtime_plan,admin):
    plan=role_runtime_plan
    assert len(plan['role_bindings'])==2
    assert len(set(binding['role_id'] for binding in plan['role_bindings'].values()))==2
    assert {binding['step_id'] for binding in plan['role_bindings'].values()}=={plan['step']}
    for command,activation in zip(plan['commands'],plan['activations']):
        binding=plan['role_bindings'][command['run_id']]
        assert command['role_ref']==binding['role_ref']
        assert plan['worker'].authorize_runtime_activation(activation_ref=activation,command=command,operation='model')['authorized'] is True
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings').fetchone()==(2,)

def test_actual_governed_role_end_denies_runtime_model_and_new_submit(role_runtime_plan,admin):
    plan=role_runtime_plan;run=plan['runs'][0]
    ended=plan['end_role'](run.run_id)
    assert ended['revision']==2
    from eios.authz.errors import AuthorizationUnavailable
    with pytest.raises(AuthorizationUnavailable):plan['worker'].authorize_runtime_activation(activation_ref=plan['activations'][0],command=plan['commands'][0],operation='model')
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_role_trigger_events').fetchone()==(2,)
    assert admin.execute('select count(*) from runtime.nexloop_role_context_artifacts').fetchone()==(2,)


def test_real_pg_trigger_and_fsynced_context_bind(role_runtime_plan,admin):
    import hashlib,json
    plan=role_runtime_plan
    for command in plan['commands']:
        pack=plan['context_packs'][command['run_id']];body=json.loads(pack['input'])
        assert body['schema_version']=='nexloop.context-pack.v3'
        event=admin.execute('select event_id,source_principal,body,command_binding from runtime.nexloop_role_trigger_events where run_id=%s',(command['run_id'],)).fetchone()
        assert body['trigger_statement']==dict(kind='service_trigger',event_id=str(event[0]),source_principal=event[1],body=event[2],provenance='eios:role-trigger:'+str(event[0]))
        artifact_id=pack['artifact_ref'].removeprefix('artifact:')
        row=admin.execute('select sha256,size_bytes,status from runtime.nexloop_local_artifacts where artifact_id=%s',(artifact_id,)).fetchone()
        assert row==(hashlib.sha256(pack['input'].encode()).hexdigest(),len(pack['input'].encode()),'available')
        source=next(s for s in plan['sources'] if s._session.authentication.subject_principal_id==event[1])
        assert source.read_artifact(artifact_id)==pack['input'].encode()
        assert {f['type'] for f in body['formal_facts']}=={'Consumer','Goal','PlanStep','EffectControl'}
        assert body['role_binding']['definition']['responsibility']=='governed service responsibility'
        assert body['role_binding']['grants_authority'] is False

@pytest.mark.parametrize('kind,field',[('RoleDefinition','responsibility'),('ConsumerRoleLink','scope')])
def test_context_copy_current_reader_source_property_revocation(role_runtime_plan,admin,kind,field):
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    plan=role_runtime_plan;source=plan['sources'][0];run=plan['runs'][0]
    binding=plan['role_bindings'][run.run_id]
    object_id=binding['role_id' if kind=='RoleDefinition' else 'link_id']
    artifact=plan['context_packs'][run.run_id]['artifact_ref'].removeprefix('artifact:')
    assert source.read_artifact(artifact)
    replace_fact(admin,plan['tenant'],'grants',[source._session.authentication.subject_principal_id,'eios:property:'+kind+'/'+object_id+'/'+field],F.GrantFacts,grants=[])
    fresh=plan['api'].authenticate(plan['source_tokens'][0],world='real')
    from eios.authz.errors import AuthorizationUnavailable
    from psycopg.errors import InsufficientPrivilege
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    with pytest.raises(ActionAuthorizationDenied,match='object/property read denied'):fresh.read_artifact(artifact)
    assert admin.execute('select status from runtime.nexloop_local_artifacts where artifact_id=%s',(artifact,)).fetchone()==('available',)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(0,)


def test_role_trigger_copy_cannot_borrow_original_source_identity(role_runtime_plan):
    from psycopg.errors import InsufficientPrivilege
    plan=role_runtime_plan
    artifact=plan['context_packs'][plan['runs'][0].run_id]['artifact_ref'].removeprefix('artifact:')
    other=plan['sources'][1]
    with pytest.raises(InsufficientPrivilege,match='artifact unavailable'):
        other.read_artifact(artifact)

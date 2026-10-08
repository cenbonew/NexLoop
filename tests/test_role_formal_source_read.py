"""Real Configurator facts; deny copy before snapshot/Artifact binding."""
import pytest
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan

@pytest.mark.parametrize('kind,field',[('Consumer',None),('Goal',None),('PlanStep',None),('EffectControl',None),('EffectControl','allow_effect'),('EffectControl','budget_units'),('EffectControl','executor_principal'),('EffectControl','valid_until')])
def test_prepare_requires_each_source_formal_read(request,monkeypatch,admin,kind,field):
    from nexloop_eios.role_context_artifacts import RoleContextArtifactProducer,ContextArtifactUnavailable
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    original=RoleContextArtifactProducer._call
    denied=[]
    def call(self,parameters,run,definition,capability):
        if parameters['verb']=='snapshot' and not denied:
            # The issued Run and Role binding are actual. Revoke only this
            # independent Source READ before producing a formal snapshot.
            import json
            role=json.loads(__import__('nexloop_eios.role_runs',fromlist=['role_envelope_for_run']).role_envelope_for_run(self.pool,self.signer,'real',run.token_digest)['payload'])
            refs={'Consumer':role['consumer_id'],'Goal':parameters['command']['goal_version_ref'].split(':')[1],'PlanStep':role['step_id'],'EffectControl':parameters['control_id']}
            target='eios:'+('property' if field else 'object')+':'+kind+'/'+refs[kind]+('/'+field if field else '')
            replace_fact(admin,self.session.authentication.tenant_id,'grants',[self.session.authentication.subject_principal_id,target],F.GrantFacts,grants=[])
            # Refresh the same authenticated credential from actual current
            # repository facts, so denial cannot be stale epoch attribution.
            from nexloop_eios.authorization import _identity
            from nexloop_eios.backend import AuthenticatedServices
            from nexloop_eios.runtime_activation import RuntimeActivationPort
            with self.pool.connection() as db,db.transaction():
                self.session=_identity(db,self.session.token_digest,'real')
            self.source=AuthenticatedServices(self.backend,self.session)
            self.authority=RuntimeActivationPort(self.pool,self.session,self.signer)
            denied.append(target)
        return original(self,parameters,run,definition,capability)
    monkeypatch.setattr(RoleContextArtifactProducer,'_call',call)
    with pytest.raises(ContextArtifactUnavailable):request.getfixturevalue('role_runtime_plan')
    assert len(denied)==1
    assert admin.execute('select count(*) from runtime.nexloop_role_context_artifacts').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()==(0,)

@pytest.mark.parametrize('proof_kind',['Action','Run','Artifact','Object','Property'])
@pytest.mark.parametrize('expiry',[None,'2000-01-01T00:00:00+00:00'])
def test_source_proof_null_or_expired_fails_closed(request,monkeypatch,admin,proof_kind,expiry):
    import json,hmac
    from nexloop_eios import role_context_artifacts as module
    from nexloop_eios.runtime_activation import RuntimeActivationPort
    from nexloop_eios.postgres_artifacts import canonical_payload
    original=RuntimeActivationPort._proof
    def altered(self,session,target):
        result=original(self,session,target)
        if (proof_kind=='Action' and target=='eios:action:nexloop.context.bind_role:1') or (proof_kind=='Run' and session.run_context is not None):result={**result,'expires_at':expiry}
        return result
    monkeypatch.setattr(RuntimeActivationPort,'_proof',altered)
    original_artifact=module.artifact_authority_proof
    def artifact(*args,**kwargs):
        result=original_artifact(*args,**kwargs)
        return {**result,'expires_at':expiry} if proof_kind=='Artifact' else result
    monkeypatch.setattr(module,'artifact_authority_proof',artifact)
    original_read=module._read_envelope
    def read(source,kind,ref,fields):
        env=original_read(source,kind,ref,fields)
        if kind=='EffectControl' and proof_kind in ('Object','Property'):
            body=json.loads(env['text'])
            if proof_kind=='Object':body['expires_at']=expiry
            else:body['property_authorities'][0]['expires_at']=expiry
            text=canonical_payload(body)
            env={'text':text,'signature':hmac.new(source._backend._signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()}
        return env
    monkeypatch.setattr(module,'_read_envelope',read)
    with pytest.raises(module.ContextArtifactUnavailable):request.getfixturevalue('role_runtime_plan')
    assert admin.execute('select count(*) from runtime.nexloop_role_context_artifacts').fetchone()==(0,)

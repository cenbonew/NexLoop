"""Cached genuine READ signatures cannot outlive current grants."""
import pytest,psycopg
from test_role_formal_current import formal_plan,revoke,KINDS
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from eios.authz.errors import AuthorizationUnavailable

@pytest.mark.parametrize('kind',KINDS)
def test_cached_genuine_source_read_denied_in_sql(formal_plan,admin,monkeypatch,kind):
    from nexloop_eios.runtime_activation import RuntimeActivationPort
    plan=formal_plan;captured=[];execute=RuntimeActivationPort._execute
    def capture(self,db,envelope,**kwargs):
        result=execute(self,db,envelope,**kwargs)
        import json
        if json.loads(envelope[2]).get('verb')=='authorize':captured.append((self,envelope,kwargs))
        return result
    monkeypatch.setattr(RuntimeActivationPort,'_execute',capture)
    assert plan['worker'].authorize_runtime_activation(activation_ref=plan['activations'][0],command=plan['commands'][0],operation='model')['authorized']
    assert captured
    port,envelope,kwargs=captured[-1]
    revoke(plan,admin,kind)
    with port.pool.connection() as db,db.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
            execute(port,db,envelope,**kwargs)
        assert 'nexloop_context_formal_current' in denied.value.diag.context
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(plan['tenant'],)).fetchone()==(0,)

def test_role_end_and_source_formal_revoke_preserve_independent_query(formal_plan,admin,tmp_path):
    import time
    from nexloop_eios.backend import open_backend
    from nexloop_eios.effect_dispatch import EffectDispatcher
    from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
    from psycopg.conninfo import make_conninfo
    from support.effect_provider import effect_provider
    plan=formal_plan
    receipt=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'actual independent QUERY'})
    intent=receipt['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        with effect_provider(tmp_path/'provider.sqlite') as provider:
            config=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
            dispatcher=EffectDispatcher(backend.authenticate(plan['executor_token'],world='real'),config,lease_seconds=3)
            assert dispatcher.run_once()['provider_state']=='accepted'
            plan['end_role'](plan['runs'][0].run_id);revoke(plan,admin,'Consumer')
            provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            # Authority fact metadata updates invalidate existing session's
            # directory hash. Genuine same worker freshly authenticates QUERY.
            executor=backend.authenticate(plan['executor_token'],world='real');executor_principal=executor._session.authentication.subject_principal_id
            dispatcher=EffectDispatcher(executor,config,lease_seconds=3)
            assert dispatcher.run_once()['business_action_success'] is True
            assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]
    assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('fulfilled',True)
    audit=admin.execute('select recovery_actor from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()
    assert audit==(executor_principal,)
    assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where tenant_id=%s and intent_id=%s and action_name='nexloop.service.request'",(plan['tenant'],intent)).fetchone()==('terminal',)

def test_revoke_after_admit_proofs_before_sql_prohibits_post(formal_plan,admin,tmp_path,monkeypatch):
    import json
    from nexloop_eios.backend import open_backend
    from nexloop_eios.effect_execution import EffectExecutionPort
    from nexloop_eios.effect_dispatch import EffectDispatcher
    from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
    from psycopg.conninfo import make_conninfo
    from support.effect_provider import effect_provider
    plan=formal_plan
    plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'actual admissibility race'})
    execute=EffectExecutionPort._execute;triggered=[]
    def race(self,db,verb,**kwargs):
        if verb=='admit' and not triggered:
            # Technical fault hook at exact private SQL boundary: current proofs
            # were made but no provider effect or business SQL has executed.
            original=self._signed
            def signed(*a,**k):
                result=original(*a,**k);revoke(plan,admin,'Goal');triggered.append(True);return result
            monkeypatch.setattr(self,'_signed',signed)
        return execute(self,db,verb,**kwargs)
    monkeypatch.setattr(EffectExecutionPort,'_execute',race)
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        executor=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'provider.sqlite') as provider:
            dispatcher=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)))
            assert dispatcher.run_once()['status']=='admission_unavailable'
            assert triggered==[True]
            assert provider.control('snapshot')['requests']==[]
    assert admin.execute("select count(*) from runtime.nexloop_effect_attempts where tenant_id=%s",(plan['tenant'],)).fetchone()==(0,)

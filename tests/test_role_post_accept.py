"""Actual POST acceptance→governed role.end→QUERY evidence, no blind resend."""
import time
import pytest
from psycopg.conninfo import make_conninfo
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from nexloop_eios.effect_dispatch import EffectDispatcher
from support.effect_provider import effect_provider

def test_actual_provider_accept_role_end_preserves_current_query_evidence(role_runtime_plan,admin,tmp_path):
    plan=role_runtime_plan
    receipt=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'real role-end recovery evidence'})
    intent=receipt['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'postaccept-worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        executor=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'postaccept-provider.sqlite') as provider:
            dispatcher=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3)
            first=dispatcher.run_once();assert first['provider_state']=='accepted'
            assert provider.control('snapshot')['requests']==[('POST',intent,202)]
            plan['end_role'](plan['runs'][0].run_id)
            provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            reconciled=dispatcher.run_once()
            assert reconciled['business_action_success'] is True
            state=admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()
            assert state==('fulfilled',True)
            assert admin.execute("select count(*) from runtime.nexloop_effect_observations where intent_id=%s and provider_state='fulfilled'",(intent,)).fetchone()==(1,)
            claim=admin.execute("select claim->>'state',claim->'terminal_outcome' from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()
            assert claim[0]=='terminal' and claim[1] is not None
            audit=admin.execute('select recovery_actor,original_claim,recovery_claim,recovery_decision from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()
            assert audit[0]==executor._session.authentication.subject_principal_id and audit[3]
            current=admin.execute("select principal_id,claim from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()
            assert current[0]==audit[0] and audit[2]['state']=='terminal'
            for key in ('binding','claim_revision','fencing_token','lease_expires_at'):
                assert current[1][key]==audit[1][key]
            assert executor.read_effect_receipt(intent_id=intent)['business_action_success'] is True

            observed=provider.control('snapshot');assert observed['effects']==1 and observed['requests']==[('POST',intent,202),('GET',intent,200)]
            time.sleep(3.1);again=dispatcher.run_once();assert again=={'claimed':False}
            observed=provider.control('snapshot');assert observed['effects']==1 and len([r for r in observed['requests'] if r[0]=='POST'])==1


@pytest.mark.parametrize('boundary',['recovery_grant','query_grant','stale_fence','unbound_query_id','rollback'])
def test_recovery_current_authority_and_atomicity(role_runtime_plan,admin,tmp_path,monkeypatch,boundary):
    from nexloop_eios.effect_execution import EffectExecutionPort,EffectExecutionUnavailable
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    from eios.actions.governance import ActionGovernor
    import uuid
    plan=role_runtime_plan
    submitted=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'current recovery authorization boundary'})
    intent=submitted['receipt']['intent_id']
    config=dict(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'boundary-worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id'])
    with open_backend(**config) as backend:
        worker=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'boundary-provider.sqlite') as provider:
            dispatcher=EffectDispatcher(worker,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3)
            assert dispatcher.run_once()['provider_state']=='accepted'
            plan['end_role'](plan['runs'][0].run_id);provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            # Withhold independent recovery only; GET observation must still persist.
            original=EffectExecutionPort.reconcile_effect_receipt
            def unavailable(*args,**kwargs):raise EffectExecutionUnavailable()
            monkeypatch.setattr(EffectExecutionPort,'reconcile_effect_receipt',unavailable)
            observed=dispatcher.run_once();assert observed['status']=='observed_fulfilled'
            monkeypatch.setattr(EffectExecutionPort,'reconcile_effect_receipt',original)
            query_id,fence=admin.execute('select query_id,effect_fence from runtime.nexloop_effect_query_admissions where intent_id=%s',(intent,)).fetchone()
            original_claim=admin.execute("select claim from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()[0]
            tenant=worker._session.authentication.tenant_id;actor=worker._session.authentication.subject_principal_id
            if boundary in ('recovery_grant','query_grant'):
                action='nexloop.service.receipt_reconcile' if boundary=='recovery_grant' else 'nexloop.service.query'
                replace_fact(admin,tenant,'grants',[actor,'eios:action:'+action+':1'],F.GrantFacts,grants=[])
                worker=backend.authenticate(plan['executor_token'],world='real')
            if boundary=='rollback':
                govern=ActionGovernor.govern
                def rollback(self,**arguments):
                    result=govern(self,**arguments)
                    if arguments['action_reference'].stable_name=='nexloop.service.receipt_reconcile':raise RuntimeError('synthetic rollback after actual governed atomic write')
                    return result
                monkeypatch.setattr(ActionGovernor,'govern',rollback)
            with pytest.raises(EffectExecutionUnavailable):
                worker.reconcile_effect_receipt(intent_id=intent,fence=fence+1 if boundary=='stale_fence' else fence,
                    query_id=str(uuid.uuid4()) if boundary=='unbound_query_id' else str(query_id))
            assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('observed_fulfilled',False)
            assert admin.execute("select claim from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()[0]==original_claim
            assert admin.execute('select count(*) from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()==(0,)
            assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='nexloop.service.receipt_reconcile'",()).fetchone()==(0,)
            snapshot=provider.control('snapshot');assert snapshot['requests']==[('POST',intent,202),('GET',intent,200)] and snapshot['effects']==1


def test_active_role_query_keeps_original_governed_finalize(role_runtime_plan,admin,tmp_path):
    plan=role_runtime_plan
    submitted=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'normal active Role receipt'})
    intent=submitted['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'normal-worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        with effect_provider(tmp_path/'normal-provider.sqlite') as provider:
            worker=backend.authenticate(plan['executor_token'],world='real')
            dispatcher=EffectDispatcher(worker,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3)
            assert dispatcher.run_once()['provider_state']=='accepted'
            provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            assert dispatcher.run_once()['business_action_success'] is True
            assert admin.execute('select count(*) from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()==(0,)
            assert worker.read_effect_receipt(intent_id=intent)['business_action_success'] is True
            assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]

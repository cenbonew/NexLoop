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
            assert reconciled['business_action_success'] is False
            state=admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()
            assert state==('observed_fulfilled',False)
            assert admin.execute("select count(*) from runtime.nexloop_effect_observations where intent_id=%s and provider_state='fulfilled'",(intent,)).fetchone()==(1,)
            claim=admin.execute("select claim->>'state',claim->'terminal_outcome' from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()
            assert claim[0]!='terminal' and claim[1] is None
            observed=provider.control('snapshot');assert observed['effects']==1 and observed['requests']==[('POST',intent,202),('GET',intent,200)]
            time.sleep(3.1);again=dispatcher.run_once();assert again['business_action_success'] is False
            observed=provider.control('snapshot');assert observed['effects']==1 and len([r for r in observed['requests'] if r[0]=='POST'])==1

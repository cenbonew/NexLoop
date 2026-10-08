"""Post-snapshot current Source formal READ; synthetic PG and owned HTTP only."""
import pytest
from runtime_effect_fixture import runtime_effect_plan
from role_run_fixture import role_runtime_plan
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.effect_intents import EffectIntentUnavailable
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from psycopg.conninfo import make_conninfo
from support.effect_provider import effect_provider

@pytest.fixture
def formal_plan(request):
    # Root-promoted catalog owns 0066; no manual DRAFT apply.
    return request.getfixturevalue('role_runtime_plan')

KINDS=['Consumer','Goal','PlanStep','EffectControl','allow_effect','budget_units','executor_principal','valid_until']
def revoke(plan,admin,kind):
    import json
    facts=json.loads(plan['context_packs'][plan['commands'][0]['run_id']]['input'])['formal_facts']
    by={f['type']:f['id'] for f in facts}
    resource=('eios:object:'+kind+'/'+by[kind]) if kind in by else ('eios:property:EffectControl/'+by['EffectControl']+'/'+kind)
    principal=plan['sources'][0]._session.authentication.subject_principal_id
    row=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key=%s",(plan['tenant'],[principal,resource])).fetchone()
    assert row
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]'::jsonb) where tenant_id=%s and fact_kind='grants' and entity_key=%s",(plan['tenant'],[principal,resource]))

@pytest.mark.parametrize('kind',KINDS)
def test_snapshot_then_source_formal_revoke_stops_model_start_submit(formal_plan,admin,tmp_path,kind):
    plan=formal_plan
    assert plan['worker'].authorize_runtime_activation(activation_ref=plan['activations'][0],command=plan['commands'][0],operation='model')['authorized']
    revoke(plan,admin,kind)
    # Same genuine Worker, refreshed directory hash: isolate Source formal READ.
    plan['worker']=plan['backend_worker'].authenticate(plan['worker_token'],world='real')
    for operation in ['model','start']:
        with pytest.raises(AuthorizationUnavailable):
            plan['worker'].authorize_runtime_activation(activation_ref=plan['activations'][0],command=plan['commands'][0],operation=operation)
    with pytest.raises(Exception):
        plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'revoked Source copied Context'})
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s',(plan['tenant'],)).fetchone()==(0,)
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        executor=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'provider.sqlite') as provider:
            dispatcher=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)))
            dispatcher.run_once()
            assert provider.control('snapshot')['requests']==[]

def test_formal_current_normal_submit_dispatch(formal_plan,admin,tmp_path):
    plan=formal_plan
    receipt=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'fresh actual formal READ'})
    assert receipt['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        executor=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'provider.sqlite') as provider:
            dispatcher=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)))
            assert dispatcher.run_once()['provider_state']=='accepted'
            assert len(provider.control('snapshot')['requests'])==1

"""NX-028 slice 2: the workbench write entry through the production app, a real login cookie and the governed entry.

Production create_app (workbench_actions_http + 0150 registry + 0151 lookup), real PG identity login and CSRF, browser
HUMAN authority for the human Actions; the actual governed effect executor and loopback provider for the dispatch
consistency check (AT-006 UI part). Synthetic data; admin seeds configuration fixtures and probes.
"""
import json
import secrets
from datetime import UTC,datetime,timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration,create_app
from commitment_fixture import HUMAN_ACTIONS,REQUEST_ACTIONS,TENANT,commitments,deadline,publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_http import ORIGIN,login
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import active_plan,fresh,plan_triggers
from test_governed_entry_pg import predict

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
GOAL_ACTIONS={'Control.set':'goals.control.set','Contact.release':'goals.contact.release'}


def publish_goal_actions(admin):
    """Owner control and contact release Actions, derived from the tenant's compiled human request Action (same Consumer v1
    schema reference, so the published contract stays valid), like goal_fixture.register_goal_actions does."""
    from eios.ontology.definitions import ActionDefinition
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    row=admin.execute("select definition,capability from control.nexloop_action_definitions where resource_id='eios:action:nexloop.plan.request_reevaluation:1'").fetchone()
    definition=ActionDefinition.model_validate_json(json.dumps(row[0]));capability=CapabilityContractSnapshot.model_validate_json(json.dumps(row[1]))
    for name,cap in GOAL_ACTIONS.items():
        binding=definition.capability_binding.model_copy(update={'capability_name':cap})
        d=definition.model_copy(update={'stable_name':name,'capability_binding':binding,'contract_digest':None})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (TENANT,'real',f'eios:action:{name}:1',Jsonb(d.model_dump(mode='json')),Jsonb(capability.model_copy(update={'capability_name':cap}).model_dump(mode='json'))))


@pytest.fixture
def workbench(commitments,identity,uow,admin,pg,tmp_path):
    c=commitments;publish_request_actions(admin);publish_goal_actions(admin)
    c['extra_human_actions']=tuple(GOAL_ACTIONS)+REQUEST_ACTIONS
    c.owner(identity,uow)  # the human owner's grants (re-authenticated per request by the app)
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing",(TENANT,))
    def private(name,content):
        path=tmp_path/name;path.write_bytes(content if isinstance(content,bytes) else content.encode());path.chmod(0o600);return path
    signer=c['signer'];(tmp_path/'app-artifacts').mkdir(mode=0o700)
    config=ApiConfiguration(private('business-dsn',make_conninfo(pg,user='nexloop_api')),private('authority-key',signer.material),
        tmp_path/'app-artifacts',signer.key_id,BrowserConfiguration(private('identity-dsn',identity[0]),private('rate-key',secrets.token_hex(32)),
        TENANT,'synthetic-browser-app',ORIGIN),execution_profile='deterministic-test')
    c['app']=lambda:TestClient(create_app(config),base_url=ORIGIN);c['identity']=identity
    return c


def act(client,csrf,operation,body,*,key=None):
    return client.post('/api/v1/workbench/actions/'+operation,json=body,
        headers={'Origin':ORIGIN,'X-CSRF-Token':csrf,'Idempotency-Key':key or 'wb-'+secrets.token_hex(12)})


def session(client,identity):
    response=login(client,identity);assert response.status_code==200
    return response.json()['csrf_token']


def test_entry_refuses_unauthenticated_forged_and_malformed_requests(workbench):
    c=workbench
    with c['app']() as client:
        assert act(client,'x','set_control',{'scope_kind':'tenant','scope_ref':'*','paused':True,'reason':'x'}).status_code==401
        csrf=session(client,c['identity'])
        assert act(client,'not-the-token','set_control',{'scope_kind':'tenant','scope_ref':'*','paused':True,'reason':'x'}).status_code in (401,403)
        assert act(client,csrf,'unknown_operation',{}).status_code==422
        assert act(client,csrf,'set_control',{'scope_kind':'tenant','scope_ref':'*','paused':'yes','reason':'x'}).status_code==422
        assert act(client,csrf,'set_control',{'scope_kind':'tenant','scope_ref':'*','paused':True,'reason':'x','tenant_id':'other'}).status_code==422
        assert act(client,csrf,'extend_commitment',{'commitment_id':'a'*64,'due_at':'2099-01-01T00:00:00+08:00','reason':'x'}).status_code==422
        no_key=client.post('/api/v1/workbench/actions/set_control',json={'scope_kind':'tenant','scope_ref':'*','paused':True,'reason':'x'},
            headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})
        assert no_key.status_code==422
        assert c['admin'].execute('select count(*) from control.nexloop_control_events').fetchone()==(0,)


def test_pause_and_resume_from_the_workbench_match_dispatch(workbench,admin,tmp_path):
    """AT-006 UI part: the owner pauses a Consumer in the workbench; the prediction and the actual dispatch agree."""
    from support.effect_provider import effect_provider
    c=workbench;c.configure_categories();queued=c.submit({'service':'后台退款'})['intent_id']
    with c['app']() as client:
        csrf=session(client,c['identity'])
        paused=act(client,csrf,'set_control',{'scope_kind':'consumer','scope_ref':c['consumer'],'paused':True,'reason':'客户投诉，先暂停'},key='wb-pause-consumer-0001')
        assert paused.status_code==200 and paused.json()['operation']=='set_control',paused.text
        # Same key, same body: the terminal outcome is replayed, nothing new is written.
        events=admin.execute('select count(*) from control.nexloop_control_events').fetchone()[0]
        again=act(client,csrf,'set_control',{'scope_kind':'consumer','scope_ref':c['consumer'],'paused':True,'reason':'客户投诉，先暂停'},key='wb-pause-consumer-0001')
        assert again.status_code==200 and admin.execute('select count(*) from control.nexloop_control_events').fetchone()[0]==events
    assert predict(admin,queued)['reason']=='control_paused'
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        assert c.dispatch(provider)['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0
    with c['app']() as client:
        csrf=session(client,c['identity'])
        assert act(client,csrf,'set_control',{'scope_kind':'consumer','scope_ref':c['consumer'],'paused':False,'reason':'已处理'}).status_code==200
    assert predict(admin,queued)['reason']=='control_revision_stale'  # resume is not replay


def test_contact_release_commitment_actions_and_requests_over_http(workbench,admin):
    c=workbench;plan=active_plan(c)
    c.inbound('不要再联系我了')
    commitment,_=fresh(c)
    with c['app']() as client:
        csrf=session(client,c['identity'])
        released=act(client,csrf,'release_contact_restriction',{'consumer_id':c['consumer'],'reason':'客户电话确认可以联系'})
        assert released.status_code==200 and released.json()['released'] is True
        # Nothing left to release: the handler refuses the transition.
        assert act(client,csrf,'release_contact_restriction',{'consumer_id':c['consumer'],'reason':'再次解除'}).json()['code']=='not_allowed_in_state'
        marked=act(client,csrf,'mark_commitment_communication',{'commitment_id':commitment,'reason':'沟通类承诺'})
        assert marked.status_code==200,marked.text
        due=(datetime.now(UTC)+timedelta(days=5)).replace(microsecond=0).isoformat()
        extended=act(client,csrf,'extend_commitment',{'commitment_id':commitment,'due_at':due,'reason':'客户同意延期'})
        assert extended.status_code==200 and extended.json()['new_commitment_id']!=commitment
        c.tick()
        assert c.commitment(commitment)['properties']['status']=='cancelled'
        # A cancelled commitment takes no further Action (state refused in SQL, not by the page).
        refused=act(client,csrf,'cancel_commitment',{'commitment_id':commitment,'reason':'取消'})
        assert refused.status_code==409 and refused.json()['code']=='not_allowed_in_state'
        assert act(client,csrf,'request_plan_reevaluation',{'plan_id':str(plan),'reason':'客户情况有变'}).status_code==200
        assert ('manual','human:'+admin.execute('select principal_id from runtime.nexloop_human_requests').fetchone()[0]) in [(k,r) for k,r in plan_triggers(c,plan)] or plan_triggers(c,plan)
    assert admin.execute("select array_agg(kind order by requested_at) from runtime.nexloop_human_requests").fetchone()==(['plan_reevaluation'],)


def test_a_human_without_the_grant_is_forbidden(commitments,identity,uow,admin,pg,tmp_path):
    """Logged in, business-enabled, but without the commitment Actions: 403 and nothing written."""
    from test_review_http import grant_human
    c=commitments;commitment,_=fresh(c)
    grant_human(admin,uow,identity,'eios:action:Consumer.create:1')
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing",(TENANT,))
    def private(name,content):
        path=tmp_path/name;path.write_bytes(content if isinstance(content,bytes) else content.encode());path.chmod(0o600);return path
    signer=c['signer'];(tmp_path/'app-artifacts').mkdir(mode=0o700)
    config=ApiConfiguration(private('business-dsn',make_conninfo(pg,user='nexloop_api')),private('authority-key',signer.material),
        tmp_path/'app-artifacts',signer.key_id,BrowserConfiguration(private('identity-dsn',identity[0]),private('rate-key',secrets.token_hex(32)),
        TENANT,'synthetic-browser-app',ORIGIN),execution_profile='deterministic-test')
    with TestClient(create_app(config),base_url=ORIGIN) as client:
        csrf=session(client,identity)
        response=act(client,csrf,'cancel_commitment',{'commitment_id':commitment,'reason':'无权取消'})
        assert response.status_code==403 and response.json()['code']=='forbidden',response.text
    assert c.commitment(commitment)['properties']['status']=='open'

"""NX-028 page integration over the production app with real workbench login cookies (clean catalog PostgreSQL).

The requests are the ones apps/web/src/workbench sends (the same bodies, the CSRF token from /api/v1/workbench/auth/csrf, an
Idempotency-Key per submission): staff owner and operator are workbench members configured by trusted configuration with the
grants compiled from deploy/authorization/workbench-roles; reads go through the slice-1 port (0117 + 0160) and ADR-025 for content.
The actual governed effect executor and loopback provider check that what the page shows after a pause is what dispatch does
(AT-006 interface part). Synthetic data; admin seeds configuration fixtures and probes.
"""
import secrets

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration,create_app
from commitment_fixture import TAKEOVER_ACTIONS,TENANT,commitments,publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_actions_http import publish_goal_actions
from test_workbench_read_pg import CALLER,ROLES,WORKBENCH_APP,add_human,members_file,workbench_login,write_facts

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


@pytest.fixture
def ui(commitments,identity,admin,pg,tmp_path):
    from nexloop_eios.workbench_roles import apply_members,compile_members
    c=commitments;publish_request_actions(admin);publish_request_actions(admin,names=TAKEOVER_ACTIONS);publish_goal_actions(admin)
    def private(name,content):
        path=tmp_path/name;path.write_bytes(content if isinstance(content,bytes) else content.encode());path.chmod(0o600);return path
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',(TENANT,WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)',(TENANT,WORKBENCH_APP,CALLER,'1',Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing",(TENANT,))
    c['staff']={'owner':add_human(admin,'owner'),'operator':add_human(admin,'operator')}
    admin.execute('alter role nexloop_configurator login')
    members=members_file((c['staff']['owner'],'owner'),(c['staff']['operator'],'operator'))
    apply_members(ROLES,members,database_url_file=private('configurator-dsn',make_conninfo(pg,user='nexloop_configurator')))
    write_facts(admin,compile_members(ROLES,members))
    signer=c['signer'];(tmp_path/'app-artifacts').mkdir(mode=0o700)
    config=ApiConfiguration(private('business-dsn',make_conninfo(pg,user='nexloop_api')),private('authority-key',signer.material),
        tmp_path/'app-artifacts',signer.key_id,BrowserConfiguration(private('identity-dsn',identity[0]),private('rate-key',secrets.token_hex(32)),
        TENANT,'synthetic-browser-app',ORIGIN),execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path/'identity-dsn',tmp_path/'rate-key',TENANT,WORKBENCH_APP,ORIGIN))
    c['app']=lambda:TestClient(create_app(config),base_url=ORIGIN)
    return c


class Page:
    """What the browser does: log in to the workbench realm, read, and submit a governed write as apps/web does."""
    def __init__(self,client,human):
        self.client=client
        assert workbench_login(client,human).status_code==200

    def get(self,path):
        return self.client.get('/api/v1/workbench/'+path)

    def act(self,operation,body,key=None):
        csrf=self.client.post('/api/v1/workbench/auth/csrf',headers={'Origin':ORIGIN})
        assert csrf.status_code==200
        return self.client.post('/api/v1/workbench/actions/'+operation,json=body,headers={'Origin':ORIGIN,'X-CSRF-Token':csrf.json()['csrf_token'],
            'Idempotency-Key':key or 'wb-'+secrets.token_hex(16)})


def test_role_read_and_at006_interface_matches_dispatch(ui,admin,tmp_path):
    """The page's role read (D2), then AT-006: pause a customer from the workbench; the next read of Action / exceptions shows
    the refusal the actual dispatch applies (provider zero requests); resume shows "control changed", not a replay."""
    from support.effect_provider import effect_provider
    c=ui;c.configure_categories();queued=c.submit({'service':'后台退款'})['intent_id']
    with c['app']() as client:
        operator=Page(client,c['staff']['operator'])
        me=operator.get('me').json()
        assert me['role']=='operator' and 'eios:action:Control.set:1' not in me['actions'] and 'eios:action:nexloop.message.staff_send:1' in me['actions']
        assert operator.act('set_control',{'scope_kind':'consumer','scope_ref':c['consumer'],'paused':True,'reason':'x'}).status_code==403
    with c['app']() as client:
        owner=Page(client,c['staff']['owner'])
        assert owner.get('me').json()['role']=='owner'
        row=lambda:next(i for i in owner.get('actions').json()['intents'] if i['intent_id']==queued)
        assert row()['dispatch']=={'status':'ok','prediction':{'dispatchable':True,'reason':None,'detail':row()['dispatch']['prediction']['detail']}}
        paused=owner.act('set_control',{'scope_kind':'consumer','scope_ref':c['consumer'],'paused':True,'reason':'客户投诉，先暂停'})
        assert paused.status_code==200,paused.text
        assert row()['dispatch']['prediction']['reason']=='control_paused' and owner.get('consumers/'+c['consumer']).json()['paused'] is True
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        assert c.dispatch(provider)['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0
    with c['app']() as client:
        owner=Page(client,c['staff']['owner'])
        assert owner.act('set_control',{'scope_kind':'consumer','scope_ref':c['consumer'],'paused':False,'reason':'已处理'}).status_code==200
        intent=next(i for i in owner.get('actions').json()['intents'] if i['intent_id']==queued)
        assert intent['dispatch']['prediction']['reason']=='control_revision_stale' and owner.get('consumers/'+c['consumer']).json()['paused'] is False


def test_takeover_page_reads_the_conversation_and_only_the_author_replies(ui,admin):
    """Takeover page: the operator takes the conversation over (default duration from policy), sees the original text through
    ADR-025, replies as staff; the owner sees the takeover as someone else's and cannot reply; hand-back clears it."""
    c=ui;conversation=c.conversation(c['consumer']);inbound=c.inbound('付款页面一直报错')
    # The 0046 write also creates the Message object (the fixture seeds only the stream entry): the page reads it via ADR-025.
    record=admin.execute('select record from runtime.nexloop_conversation_messages where message_id=%s',(inbound,)).fetchone()[0]
    admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
        values(%s,'real','Message',%s,1,%s,'nexloop-action',%s,clock_timestamp(),clock_timestamp())''',(TENANT,inbound,Jsonb(record),'inbound-'+inbound[:16]))
    with c['app']() as client:
        operator=Page(client,c['staff']['operator'])
        state=operator.get('takeovers').json()
        assert state=={'status':'ok','policy':{'default_seconds':7200,'max_seconds':86400},'takeovers':[]}
        started=operator.act('take_over_conversation',{'scope_kind':'conversation','scope_ref':conversation,'duration_seconds':7200,'reason':'人工处理'})
        assert started.status_code==200,started.text
        [held]=operator.get('takeovers').json()['takeovers']
        assert held['mine'] is True and held['scope_ref']==conversation and held['taken_by']==c['staff']['operator']['principal_id']
        view=operator.get('conversations/'+conversation).json()
        assert [(m['direction'],m['content']) for m in view['messages'] if m['id']==inbound][0][1]['body']=='付款页面一直报错'
        sent=operator.act('send_staff_reply',{'conversation_id':conversation,'reply_to':inbound,'text':'您好，我是人工客服，正在处理。'})
        assert sent.status_code==200 and sent.json()['message']['sender_kind']=='human_takeover',sent.text
        staff=[m for m in operator.get('conversations/'+conversation).json()['messages'] if m['sender_kind']=='human_takeover']
        assert len(staff)==1 and staff[0]['content']=={'status':'ok','actor':c['staff']['operator']['principal_id'],'body':'您好，我是人工客服，正在处理。'}
    with c['app']() as client:
        owner=Page(client,c['staff']['owner'])
        [seen]=owner.get('takeovers').json()['takeovers'];assert seen['mine'] is False
        assert owner.get('overview').json()['takeovers']['status']=='ok'
        refused=owner.act('send_staff_reply',{'conversation_id':conversation,'reply_to':inbound,'text':'我也回复'})
        assert refused.status_code==403 and refused.json()['code']=='forbidden'
        assert owner.act('hand_back_conversation',{'takeover_id':held['takeover_id'],'reason':'处理完毕'}).status_code==200
        assert owner.get('takeovers').json()['takeovers']==[]
    assert admin.execute("select count(*) from runtime.nexloop_conversation_messages where conversation_id=%s and record->>'sender_kind'='human_takeover'",(conversation,)).fetchone()==(1,)

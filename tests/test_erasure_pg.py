"""NX-029 slice 2 actual PostgreSQL: Consumer erasure, Message erasure, retention holds, Run file and Artifact purge.

On the NX-026 commitment fixture (real Consumer, conversation stream, Message objects, Claims, Commitments, plans)
plus the NX-027 commercial configuration. The owner acts through the governed entry (real browser HUMAN session); the
retention keeper runs as its own service through the signed ports only, with a fake loopback Host purge and a real
local Artifact store. Admin seeds fixtures and, where a test says so, injects a ledger state. Synthetic data only.
"""
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import retention
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.local_artifacts import LocalBlobStore
from commercial_fixture import TENANT,commercial_setup,publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from multi_authority_fixture import seed_multi_authority
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import active_plan,fresh

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
ACTIONS=('nexloop.consumer.erase','nexloop.message.erase','nexloop.retention.hold','nexloop.retention.release_hold')
KEEPER=[('eios:action:nexloop.retention.execute:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:nexloop.erasure.execute:1',ResourceType.ACTION,Operation.EXECUTE)]


@pytest.fixture
def erasing(commitments,admin,pg,tmp_path,identity,uow):
    c=commitments
    with open_core(make_conninfo(pg,user='nexloop_api')) as api,commercial_setup(admin,pg,tmp_path,api_pool=api,signer=c['signer']) as env:
        env.connector();env.link('cust-1',c['consumer'])
        publish_registry_actions(admin,ACTIONS);c['extra_human_actions']=ACTIONS
        _,token=seed_multi_authority(admin,env['worker'],KEEPER,identity_suffix='-retention-keeper',tenant=TENANT)
        service=c.service_actions('-erasure-service');c.owner(identity,uow)
        owner,human=c.owner(identity,uow)
        c.update(env=env,owner_actions=owner,human=human,service_actions=c.service_actions_session(service),
            keeper_session=lambda:authenticate_service(env['worker'],token,world='real'))
        yield c


def keeper(c,**kw):return retention.RetentionKeeper(c['env']['worker'],c['keeper_session'](),c['env']['signer'],**kw)
def erasure_port(c):return retention.ErasurePort(c['env']['worker'],c['keeper_session'](),c['env']['signer'])
def act(actions,name,request_id,**kw):return getattr(actions,name)(action_name='nexloop.'+{'erase_consumer':'consumer.erase','erase_message':'message.erase',
    'hold_retention':'retention.hold','release_retention_hold':'retention.release_hold'}[name],action_version=1,request_id=request_id,**kw)
def request(admin,consumer):
    return admin.execute('select request_id,state,phase,counts from control.nexloop_consumer_erasures where consumer_id=%s',(consumer,)).fetchone()
def fault(admin,sql,params=()):
    with admin.transaction():admin.execute('set local session_replication_role=replica');admin.execute(sql,params)


def test_owner_erases_a_consumer_end_to_end_and_services_cannot(erasing,admin):
    c=erasing;env=c['env'];consumer=c['consumer']
    plan=active_plan(c)
    commitment,source=fresh(c)
    inbound=c.inbound('我的手机号是 13800000000，请删除我的资料')
    reply=c.outbound('好的，已为您登记');claim=c.claim(reply,'已为您登记')
    env.post(env.event('payment.succeeded','pay-1'));env.tick()
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into runtime.nexloop_cost_entries(tenant_id,world,entry_id,data_mode,cost_kind,units,amount,currency,amount_unit,basis,source_ref,consumer_ref,occurred_at,recorded_by)
            values(%s,'real','discount:c1','real','discount',1,100,'CNY','minor','budget_reservation','incentive:x',%s,clock_timestamp(),'synthetic')""",(TENANT,consumer))
    # A service holding the very same grant is refused by the entry (human only); nothing is requested.
    with pytest.raises(Exception,match='human goal authority required'):act(c['service_actions'],'erase_consumer','erase-svc',consumer_id=consumer,reason='service tries')
    assert request(admin,consumer) is None
    # An unknown external result of this Consumer: erasure waits for reconciliation and never resends.
    intent=c.submit({'service':'后台退款'})['intent_id']
    fault(admin,"update runtime.nexloop_effect_intents set state='unknown' where intent_id=%s",(intent,))
    out=act(c['owner_actions'],'erase_consumer','erase-1',consumer_id=consumer,reason='顾客要求删除')
    assert out['state']=='settling' and out['plans_closed']==1 and out['replayed'] is False
    assert act(c['owner_actions'],'erase_consumer','erase-1',consumer_id=consumer,reason='顾客要求删除')['replayed'] is True
    # Blocking, same transaction: contact restricted (release refused while erasing), plan closed.
    assert admin.execute('select active,rule_id from control.nexloop_contact_restrictions where consumer_id=%s',(consumer,)).fetchone()==(True,'consumer_erasure')
    assert admin.execute("select status,reason from runtime.nexloop_plan_events where plan_id=%s order by event_id desc limit 1",(plan,)).fetchone()==('closed','consumer_erasure')
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='stays'),admin.transaction():
        admin.execute("select control.nexloop_governed_contact_release(%s,'real','p','human','r',%s)",(TENANT,Jsonb({'operation':'release_contact_restriction','consumer_id':consumer,'reason':'x'})))
    calls=[]
    host=lambda run_id:(calls.append(run_id),'purged')[1]
    keeper(c,host=host).run_once()
    assert request(admin,consumer)[1]=='waiting_unknown_effects'
    assert admin.execute("select record->>'body' from runtime.nexloop_conversation_messages where message_id=%s",(reply['message_id'],)).fetchone()[0]=='好的，已为您登记'
    fault(admin,"update runtime.nexloop_effect_intents set state='failed' where intent_id=%s",(intent,))  # reconciled: the provider never ran it
    for _ in range(10):
        keeper(c,host=host).run_once()
        if request(admin,consumer)[1]=='completed':break
    request_id,state,phase,counts=request(admin,consumer)
    assert state=='completed' and phase is None and counts['messages']>=3 and counts['claims']>=2 and counts['commitments']==1
    # Text and identity are gone everywhere they were kept.
    rows=admin.execute("""select m.record->>'body',m.record->>'erased' from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations v
        on v.tenant_id=m.tenant_id and v.conversation_id=m.conversation_id where v.consumer_id=%s""",(consumer,)).fetchall()
    assert rows and all(r==('','true') for r in rows)
    assert admin.execute("select count(*) from ontology.objects where type_name in ('Message','Consumer') and (object_id=%s or object_id=any(%s))",
        (consumer,[reply['message_id'],inbound,source['message_id']])).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.nexloop_claims where consumer_id=%s and (quote<>'' or not value ? 'erased')",(consumer,)).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.nexloop_recall_entries where source_key like %s",('%'+consumer+'%',)).fetchone()==(0,)
    made_to,status=admin.execute("select properties->>'made_to',properties->>'status' from ontology.objects where object_id=%s",(commitment,)).fetchone()
    assert made_to=='erasure:'+request_id and status  # D4: kept until its own expiry, no longer pointing at the person
    assert admin.execute("select count(*) from control.nexloop_commercial_customer_links where consumer_id=%s",(consumer,)).fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_commercial_events where customer_ref='cust-1'").fetchone()==(0,)
    assert admin.execute("select consumer_ref from runtime.nexloop_cost_entries where entry_id='discount:c1'").fetchone()==(None,)
    # The receipt is a tombstone without content; the contact restriction stays.
    receipt=admin.execute("select action,counts from runtime.nexloop_erasure_tombstones where subject_kind='consumer' and subject_key=%s",(consumer,)).fetchone()
    assert receipt[0]=='deleted' and receipt[1]['consumer_object']==1
    text=json.dumps(admin.execute('select jsonb_agg(t) from runtime.nexloop_erasure_tombstones t').fetchone()[0],ensure_ascii=False)
    assert '13800000000' not in text and '已为您登记' not in text and 'cust-1' not in text
    assert admin.execute('select active from control.nexloop_contact_restrictions where consumer_id=%s',(consumer,)).fetchone()==(True,)
    assert erasure_port(c).status()['requests'][0]['state']=='completed'


def test_message_erasure_holds_and_the_consumers_own_request(erasing,admin):
    c=erasing;consumer=c['consumer'];owner=c['owner_actions']
    commitment,source=fresh(c)
    out=act(owner,'erase_message','erase-msg-1',message_id=source['message_id'],reason='顾客要求删除这条消息')
    assert out['stream']==1 and out['object']==1
    assert admin.execute("select record->>'body' from runtime.nexloop_conversation_messages where message_id=%s",(source['message_id'],)).fetchone()==('',)
    assert admin.execute("select count(*) from ontology.objects where type_name='Message' and object_id=%s",(source['message_id'],)).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.nexloop_claims where source_message_id=%s and quote<>''",(source['message_id'],)).fetchone()==(0,)
    assert admin.execute("select reason from runtime.nexloop_commitment_exceptions where subject_ref=%s and reason='source_deleted'",('commitment:'+commitment,)).fetchone()==('source_deleted',)
    # A Consumer hold (legal basis registered by a person) keeps that Consumer's data.
    hold=act(owner,'hold_retention','hold-1',scope_kind='consumer',scope_ref=consumer,basis='合同约定保留至争议结束')
    other=c.outbound('另一条回复')
    with pytest.raises(Exception):act(owner,'erase_message','erase-msg-2',message_id=other['message_id'],reason='删除')
    assert admin.execute("select record->>'body' from runtime.nexloop_conversation_messages where message_id=%s",(other['message_id'],)).fetchone()==('另一条回复',)
    # D1: the Consumer's own request only waits; it is not erasing until the owner confirms.
    with admin.transaction():
        assert admin.execute('select runtime.nexloop_consumer_erasure_requested(%s,%s,%s,%s)',(TENANT,'real',consumer,'chat-1')).fetchone()[0]['state']=='pending_confirmation'
    assert admin.execute("select runtime.nexloop_consumer_erasing(%s,'real',%s)",(TENANT,consumer)).fetchone()==(False,)
    act(owner,'erase_consumer','erase-confirm',consumer_id=consumer,reason='负责人确认顾客请求')
    assert request(admin,consumer)[0]=='consumer-request:chat-1' and request(admin,consumer)[1]=='settling'
    for _ in range(3):keeper(c).run_once()
    assert request(admin,consumer)[1]=='completed_with_holds'
    assert admin.execute("select count(*) from ontology.objects where type_name='Consumer' and object_id=%s",(consumer,)).fetchone()==(1,)
    assert admin.execute("select action from runtime.nexloop_erasure_tombstones where subject_kind='consumer'").fetchone()==('skipped_hold',)
    act(owner,'release_retention_hold','hold-release',hold_id=hold['hold_id'],reason='争议结束')
    with pytest.raises(Exception):act(owner,'release_retention_hold','hold-release-2',hold_id=hold['hold_id'],reason='again')
    # A class hold skips that class in the expiry sweeps.
    act(owner,'hold_retention','hold-2',scope_kind='item_class',scope_ref='message_text',basis='审计期间暂停清除')
    swept=retention.RetentionPort(c['env']['worker'],c['keeper_session'](),c['env']['signer']).sweep('message_text','sweep:44444444-4444-4444-4444-444444444444')
    assert swept['held'] is True and swept['processed']==0


def test_run_files_and_artifacts_leave_only_when_their_deletion_is_confirmed(erasing,admin,tmp_path):
    c=erasing;port=erasure_port(c)
    run=str(uuid.uuid4())
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason) values(%s,'real',%s,'erasure:test','consumer_erasure')",(TENANT,run))
    # The Host fails once: the item stays queued, nothing is claimed.
    summary=keeper(c,host=lambda r:'failed').run_once()
    assert summary['runs_failed']==1 and admin.execute('select state,attempts from runtime.nexloop_run_purge_items where run_id=%s',(run,)).fetchone()==('pending',1)
    assert admin.execute("select count(*) from runtime.nexloop_erasure_tombstones where subject_key=%s",(run,)).fetchone()==(0,)
    assert keeper(c,host=lambda r:'purged').run_once()['runs_purged']==1
    assert admin.execute('select state from runtime.nexloop_run_purge_items where run_id=%s',(run,)).fetchone()==('purged',)
    assert admin.execute("select action from runtime.nexloop_erasure_tombstones where subject_key=%s",(run,)).fetchone()==('deleted',)
    assert admin.execute("select action from runtime.audit_events where event_id=%s",('run-purge:'+run,)).fetchone()==('nexloop.erasure.run_purged',)
    # Artifacts of another principal: expired → deleted with tombstone and audit; corrupted file → refused, nothing written;
    # not yet expired → never a candidate; a claim of it is refused in SQL whatever the keeper sends.
    with LocalBlobStore(tmp_path/'artifacts') as store:
        def artifact(payload,*,expired=True,corrupt=False):
            ref=store.put(tenant_id=TENANT,world='real',payload=payload,media_type='application/json')
            fault(admin,"""insert into runtime.nexloop_local_artifacts(tenant_id,world,artifact_id,principal_id,sha256,size_bytes,media_type,object_key,retention_until,status,
                payload_digest,upload_worker,upload_token,upload_fence,lease_until,permit_id) values(%s,'real',%s,'synthetic-other-principal',%s,%s,'application/json','k',%s,
                'available',%s,%s,'t',1,clock_timestamp(),'p')""",(TENANT,ref.artifact_id,('0'*64 if corrupt else ref.sha256),ref.size_bytes,
                datetime.now(UTC)-timedelta(days=1) if expired else datetime.now(UTC)+timedelta(days=30),'0'*64,secrets.token_hex(16)))
            return ref.artifact_id
        gone,broken,young=artifact(b'{"a":1}'),artifact(b'{"b":2}',corrupt=True),artifact(b'{"c":3}',expired=False)
        assert set(port.artifact_candidates())=={gone,broken}
        summary=keeper(c,artifact_store=store).run_once()
        assert summary['artifacts_deleted']==1 and summary['artifacts_failed']==1
        status=dict(admin.execute('select artifact_id,status from runtime.nexloop_local_artifacts where artifact_id=any(%s)',([gone,broken,young],)).fetchall())
        assert status=={gone:'deleted',broken:'deleting',young:'available'}
        assert admin.execute("select count(*) from runtime.nexloop_erasure_tombstones where subject_key=any(%s)",([broken,young],)).fetchone()==(0,)
        assert admin.execute("select count(*) from runtime.audit_events where event_id=%s",('artifact-purge:'+gone,)).fetchone()==(1,)
        from nexloop_eios.plan_reevaluation import PlanUnavailable
        with pytest.raises(PlanUnavailable):port.artifact_claim(young,secrets.token_hex(16))   # 42501 in SQL
    # Only the keeper's own grant reaches the port: a service without it is refused.
    with pytest.raises(Exception):retention.ErasurePort(c['env']['worker'],c['env'].session(),c['env']['signer']).requests()

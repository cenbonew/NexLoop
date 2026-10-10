"""NX-029 slice 1 actual PostgreSQL: retention settings, governed erasure of append-only rows, tombstones, expiry sweeps.

On the NX-026 commitment fixture (real Consumer, conversation stream, Message objects, Claims) plus the NX-027 commercial
configuration. The retention keeper runs as its own service principal through the signed port only. Time passes by
explicit injection: admin back-dates rows with triggers suspended for that one statement (session_replication_role,
disposable test database only), so that rows are older than their class. Synthetic data only.
"""
from datetime import UTC,datetime,timedelta
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import retention
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.commercial import CostReadPort
from commercial_fixture import TENANT,commercial_setup
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from multi_authority_fixture import seed_multi_authority

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
ROOT=Path(__file__).resolve().parents[1]
SETTINGS=ROOT/'deploy/configuration/retention.v1.json'
OLD=datetime.now(UTC)-timedelta(days=400)


@pytest.fixture
def kept(commitments,admin,pg,tmp_path):
    c=commitments
    with open_core(make_conninfo(pg,user='nexloop_api')) as api,commercial_setup(admin,pg,tmp_path,api_pool=api,signer=c['signer']) as env:
        _,token=seed_multi_authority(admin,env['worker'],[('eios:action:nexloop.retention.execute:1',ResourceType.ACTION,Operation.EXECUTE)],
            identity_suffix='-retention-keeper',tenant=TENANT)
        env.connector();env.link('cust-1',c['consumer'])
        c['keeper_session']=lambda:authenticate_service(env['worker'],token,world='real')
        yield c,env


def keeper(c,env):return retention.RetentionKeeper(env['worker'],c['keeper_session'](),env['signer'])


def backdate(admin,sql,params=()):
    """Explicit time injection (test only): one statement with triggers suspended."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute(sql,params)


def tombstones(admin,item_class):
    return admin.execute('select subject_kind,subject_key,action,counts from runtime.nexloop_erasure_tombstones where item_class=%s order by executed_at,subject_key',
        (item_class,)).fetchall()


def test_settings_are_seeded_canonically_and_bounded(tmp_path,admin,commitments):
    value=retention.load_settings(SETTINGS);text,digest=retention.canonical_settings(SETTINGS)
    stored,stored_digest=admin.execute('select definition,definition_digest from control.nexloop_retention_settings where version=1').fetchone()
    assert stored==json.loads(text)==value and stored_digest==digest
    assert value['classes']['prompt_text']['days']<=value['classes']['message_text']['days']
    for change in ({'prompt_text':{'days':120,'source':'message_text'}},{'job_input':{'days':0,'source':None}},{'unknown':{'days':1,'source':None}},
                   {'claim_quote':{'days':10,'source':'nothing'}}):
        bad=json.loads(SETTINGS.read_text());bad['classes'].update(change);path=tmp_path/'bad.json';path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):retention.load_settings(path)
        bad['version']=2
        with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
            admin.execute("insert into control.nexloop_retention_settings(version,definition,definition_digest,published_by) values(2,%s,%s,'synthetic')",(Jsonb(bad),'0'*64))
    with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute('delete from control.nexloop_retention_settings')


def test_append_only_rows_open_only_inside_a_registered_owner_transaction(kept,admin):
    c,env=kept
    receipt=env.post(env.event('payment.succeeded','pay-1'));assert receipt['status']==202
    owner=lambda:(admin.execute('set local role nexloop_owner'),admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,)))
    # Without a registration: still append-only, for the owner too.
    with admin.transaction():
        owner()
        with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute('delete from runtime.nexloop_commercial_receipts')
    # A registration of another table, or of columns only, does not open a delete.
    with admin.transaction():
        owner();admin.execute("select runtime.nexloop_erasure_open('runtime.nexloop_commercial_records',null,true,'test')")
        with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute('delete from runtime.nexloop_commercial_receipts')
    with admin.transaction():
        owner();admin.execute("select runtime.nexloop_erasure_open('runtime.nexloop_commercial_receipts',array['reason'],false,'test')")
        with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute('delete from runtime.nexloop_commercial_receipts')
        # Only the registered column, and only to an erasure value.
        with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute("update runtime.nexloop_commercial_receipts set reason='x'")
        with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute("update runtime.nexloop_commercial_receipts set outcome='duplicate'")
        admin.execute('update runtime.nexloop_commercial_receipts set reason=null')
        admin.execute('select runtime.nexloop_erasure_close()')
        with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():admin.execute('update runtime.nexloop_commercial_receipts set reason=null')
    # Application roles can neither register nor reach the scope table.
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_configurator'):
        for f in ('runtime.nexloop_erasure_open(text,text[],boolean,text)','runtime.nexloop_erasure_close()','runtime.nexloop_retention_sweep(text,text,text,integer,text,text)'):
            assert admin.execute("select has_function_privilege(%s,%s,'execute')",(role,f)).fetchone()==(False,)
        assert admin.execute("select has_table_privilege(%s,'control.nexloop_erasure_scope','insert')",(role,)).fetchone()==(False,)
    # A CommercialRecord still leaves only by retention.
    env.tick()
    with pytest.raises(psycopg.Error,match='leaves only by retention'),admin.transaction():
        admin.execute("delete from ontology.objects where type_name='CommercialRecord'")


def test_keeper_redacts_expired_text_deletes_expired_records_and_leaves_tombstones(kept,admin):
    c,env=kept
    old_msg=c.outbound('三个月前的回复：已经为您登记');new_msg=c.outbound('今天的回复：处理中')
    old_claim=c.claim(old_msg,'已经为您登记');new_claim=c.claim(new_msg,'处理中')
    backdate(admin,"update runtime.nexloop_conversation_messages set record=record||jsonb_build_object('accepted_at',%s::text) where message_id=%s",(OLD.isoformat(),old_msg['message_id']))
    # Financial: one record whose last event is old, one current; one old and one current cost entry.
    env.post(env.event('payment.succeeded','pay-old'));env.post(env.event('payment.succeeded','pay-new'));env.tick()
    old_record=env.record('payment','pay-old');new_record=env.record('payment','pay-new')
    backdate(admin,"update runtime.nexloop_commercial_events set occurred_at=%s,received_at=%s where record_key=%s",(OLD,OLD,env.record_key('payment','pay-old')))
    backdate(admin,"update runtime.nexloop_commercial_receipts set received_at=%s where source_event_id in (select source_event_id from runtime.nexloop_commercial_events where record_key=%s)",
        (OLD,env.record_key('payment','pay-old')))
    for entry,at in (('discount:old',OLD),('discount:new',datetime.now(UTC))):
        with admin.transaction():
            admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
            admin.execute("""insert into runtime.nexloop_cost_entries(tenant_id,world,entry_id,data_mode,cost_kind,units,amount,currency,amount_unit,basis,source_ref,occurred_at,recorded_by)
                values(%s,'real',%s,'real','discount',1,100,'CNY','minor','budget_reservation','incentive:x',%s,'synthetic')""",(TENANT,entry,at))
    # A terminal job with an old input.
    # A terminal runtime task whose input carried message text (seeded as the queue writes it; triggers suspended for the FK to a synthetic invocation).
    job='retention-job-1'
    backdate(admin,'''insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
        request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue) values(%s,'inv-1','nexloop.run','1','runtime','async',%s,'a','t','r',%s,'{}',
        'succeeded',%s,%s,'real','operations')''',(job,TENANT,Jsonb({'input':'三个月前的输入','run_command':{'run_id':'00000000-0000-0000-0000-000000000001'}}),OLD,OLD))
    first=keeper(c,env).run_once()
    assert first['classes']==7 and first['incomplete']==0 and first['processed']>=4
    # Message text: the old body is gone everywhere it was kept; order, ids and digests stay; the new one is untouched.
    rec=lambda m:admin.execute('select record,payload_digest from runtime.nexloop_conversation_messages where message_id=%s',(m['message_id'],)).fetchone()
    old_rec,old_digest=rec(old_msg)
    assert old_rec['body']=='' and old_rec['erased'] is True and old_rec['sequence'] and len(old_digest)==64
    assert rec(new_msg)[0]['body']=='今天的回复：处理中' and 'erased' not in rec(new_msg)[0]
    assert admin.execute("select properties->>'body' from ontology.objects where type_name='Message' and object_id=%s",(old_msg['message_id'],)).fetchone()==('',)
    assert admin.execute("select properties->>'body' from ontology.objects where type_name='Message' and object_id=%s",(new_msg['message_id'],)).fetchone()==('今天的回复：处理中',)
    # Claim quotes follow their source message (D2); the Claim itself and its structured value stay.
    quotes=dict(admin.execute('select claim_id,quote from ontology.nexloop_claims where claim_id=any(%s)',([old_claim,new_claim],)).fetchall())
    assert quotes=={old_claim:'',new_claim:'处理中'}
    # Financial (NX-027 D6): the old record leaves with its events and links; the new one stays.
    assert env.record('payment','pay-old') is None and env.record('payment','pay-new') is not None
    assert admin.execute('select count(*) from runtime.nexloop_commercial_events where record_key=%s',(env.record_key('payment','pay-old'),)).fetchone()==(0,)
    assert admin.execute("select count(*) from ontology.events where object_id=%s",(old_record['object_id'],)).fetchone()==(0,)
    assert [r[0] for r in admin.execute("select entry_id from runtime.nexloop_cost_entries order by 1").fetchall()]==['discount:new']
    assert admin.execute("select count(*) from runtime.nexloop_commercial_receipts where received_at<%s",(datetime.now(UTC)-timedelta(days=100),)).fetchone()==(0,)
    assert admin.execute('select normalized_input->>%s,normalized_input->>%s from runtime.jobs where job_id=%s',('input','input_erased',job)).fetchone()==('','true')
    # Tombstones carry no content, amount or external id.
    financial=tombstones(admin,'financial')
    assert ('commercial_record',old_record['object_id'],'deleted',{'events':1}) in financial
    assert any(k=='batch' and a=='deleted' and n['cost_entries']==1 for k,_,a,n in financial)
    assert tombstones(admin,'message_text')[0][2:]==('redacted',{'messages':1,'outbox':0,'message_objects':1})
    text=json.dumps(admin.execute('select jsonb_agg(t) from runtime.nexloop_erasure_tombstones t').fetchone()[0],ensure_ascii=False)
    assert '三个月前' not in text and 'pay-old' not in text and 'cust-1' not in text and '10000' not in text
    # Idempotent: nothing is due again within the interval; a forced second pass finds nothing.
    assert keeper(c,env).run_once()=={'classes':0,'passes':0,'processed':0,'incomplete':0}
    again=retention.RetentionPort(env['worker'],c['keeper_session'](),env['signer']).sweep('message_text','sweep:00000000-0000-0000-0000-000000000000')
    assert again['processed']==0 and again['more'] is False
    status=retention.RetentionPort(env['worker'],c['keeper_session'](),env['signer']).status()
    assert status['world']=='real' and {t['item_class'] for t in status['tombstones']}>={'financial','message_text','claim_quote'} and status['warnings']==[]
    # The current record and its metric projections are untouched; the cost read port sees the remaining entry only.
    assert [e['entry_id'] for e in CostReadPort(env['worker'],env.session(),env['signer']).entries()]==['discount:new']


def test_prompt_text_and_observations_expire_and_doctor_warns(kept,admin):
    c,env=kept
    # A metric whose maturity outlives financial_retention_days is reported (NX-027 open item).
    env.metric('late-maturity',maturity=366*86400)
    status=retention.RetentionPort(env['worker'],c['keeper_session'](),env['signer']).status()
    assert [w['metric_id'] for w in status['warnings']]==['late-maturity'] and status['warnings'][0]['financial_retention_days']==365
    # Observations older than their class are deleted; current ones stay.
    env.metric('revenue');env.post(env.event('payment.succeeded','pay-1'));env.post(env.event('payment.succeeded','pay-2'));env.tick()
    (first_obs,)=admin.execute("select observation_id from control.nexloop_metric_observations where metric_id='revenue' order by observation_id limit 1").fetchone()
    backdate(admin,"update control.nexloop_metric_observations set occurred_at=%s where metric_id='revenue' and observation_id=%s",(OLD,first_obs))
    port=retention.RetentionPort(env['worker'],c['keeper_session'](),env['signer'])
    assert port.sweep('metric_observation','sweep:11111111-1111-1111-1111-111111111111')['counts']=={'observations':1}
    assert admin.execute("select count(*) from control.nexloop_metric_observations where metric_id='revenue'").fetchone()==(1,)
    # Invalid sweeps are refused.
    with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
        port.sweep('nothing','sweep:11111111-1111-1111-1111-111111111111')
    with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
        port.sweep('message_text','not-a-ref')
    # A service without the retention grant is refused by the port.
    with pytest.raises(Exception):retention.RetentionPort(env['worker'],env.session(),env['signer']).due()

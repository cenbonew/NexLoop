"""NX-029 final (AT-026): the expiry classes run_files, commitment_closed and claim_superseded, driven by the retention
keeper. Time is advanced explicitly: the clock each class counts from (task terminal time, last status event,
supersession time) is moved past or short of the class window with triggers suspended for that one statement - the
same injection the slice 1 sweeps use. Synthetic data only."""
from datetime import UTC,datetime,timedelta
import hashlib,secrets,uuid

import pytest
from psycopg.types.json import Jsonb
from commercial_fixture import TENANT
from test_erasure_pg import ACTIONS,KEEPER,erasing,keeper,act  # noqa: F401
from test_erasure_pg import pytestmark  # noqa: F401  (execution_plan parametrization)
from test_commitments_pg import fresh
from support.journal_export import record_journal_export
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

NOW=lambda:datetime.now(UTC)


def backdate(admin,sql,params=()):
    """Explicit time injection (test only): one statement with triggers suspended."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute(sql,params)


def run(admin,status,ended_at,*,role_expires=None):
    """A Pi Run as the activation registry leaves it: credential, binding and its task (status and terminal time)."""
    run_id=uuid.uuid4();job='expiry-job-'+secrets.token_hex(4);digest=secrets.token_hex(32)
    backdate(admin,'''insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
        request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue) values(%s,'inv-1','nexloop.run','1','runtime','async',%s,'a','t','r','{}','{}',
        %s,%s,%s,'real','operations')''',(job,TENANT,status,ended_at,ended_at))
    backdate(admin,'''insert into authz.nexloop_runtime_run_bindings(run_id,tenant_id,world,queue,task_id,run_digest,command_digest,input_digest,owner_epoch,enrolled_by,created_at)
        values(%s,%s,'real','operations',%s,%s,%s,%s,1,'synthetic',%s)''',(run_id,TENANT,job,digest,'a'*64,'b'*64,ended_at))
    return str(run_id)


def ledger(admin,item_class):
    return admin.execute("select count(*) from control.nexloop_deletion_ledger where kind='item_class' and subject_ref=%s",(item_class,)).fetchone()[0]


def test_run_directories_expire_through_the_host_purge(erasing,admin):
    c=erasing;now=NOW()
    expired=run(admin,'succeeded',now-timedelta(days=31))
    young=run(admin,'failed',now-timedelta(days=29))
    live=run(admin,'running',now-timedelta(days=60))  # an old Run whose task is still live is never purged
    calls=[];outcome={'value':'failed'}
    host=lambda run_id:(calls.append(run_id),outcome['value'])[1]
    keeper(c,host=host).run_once()
    # Queued and handed to the Host; a failed purge stays pending and writes nothing.
    assert calls==[expired]
    assert admin.execute('select state,reason,attempts from runtime.nexloop_run_purge_items where run_id=%s',(expired,)).fetchone()==('pending','retention_expiry',1)
    assert admin.execute("select count(*) from runtime.nexloop_erasure_tombstones where subject_key=%s",(expired,)).fetchone()==(0,)
    outcome['value']='purged'
    keeper(c,host=host).run_once()
    assert admin.execute('select state from runtime.nexloop_run_purge_items where run_id=%s',(expired,)).fetchone()==('purged',)
    assert admin.execute("select reason,item_class,action from runtime.nexloop_erasure_tombstones where subject_key=%s",(expired,)).fetchone()==('retention_expiry','run_files','deleted')
    assert ledger(admin,'run_files')==1
    assert admin.execute('select count(*) from runtime.nexloop_run_purge_items where run_id=any(%s)',([uuid.UUID(young),uuid.UUID(live)],)).fetchone()==(0,)
    # Two days later the younger Run is past its window too; the live one still is not.
    backdate(admin,"update runtime.jobs set updated_at=updated_at-interval '2 days' where job_id in (select task_id from authz.nexloop_runtime_run_bindings where run_id=%s)",(young,))
    admin.execute('delete from runtime.nexloop_retention_sweeps')  # due again now (interval elapsed)
    keeper(c,host=host).run_once()
    assert calls[-1]==young and live not in calls


def test_closed_commitments_leave_a_year_after_closing(erasing,admin):
    c=erasing;now=NOW()
    # Distinct predicates: one correlation key would reaffirm the first Commitment instead of registering another.
    expired,_=fresh(c,predicate='发货进展');recent,_=fresh(c,predicate='退款进展');still_open,_=fresh(c,predicate='维修进展')
    for commitment,status,closed_at in ((expired,'fulfilled',now-timedelta(days=366)),(recent,'cancelled',now-timedelta(days=364)),(still_open,'open',now-timedelta(days=400))):
        backdate(admin,"update ontology.objects set properties=properties||jsonb_build_object('status',%s::text),updated_at=%s where type_name='Commitment' and object_id=%s",(status,closed_at,commitment))
        backdate(admin,"insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail,recorded_at) values(%s,'real',%s,'status',%s,%s)",
            (TENANT,'commitment:'+commitment,Jsonb({'status':status}),closed_at))
    keeper(c).run_once()
    gone=lambda cid:admin.execute('''select (select count(*) from ontology.objects where type_name='Commitment' and object_id=%s),
        (select count(*) from runtime.nexloop_commitments where commitment_id=%s),(select count(*) from runtime.nexloop_commitment_events where subject_ref=%s),
        (select count(*) from runtime.nexloop_commitment_evidence where commitment_id=%s)''',(cid,cid,'commitment:'+cid,cid)).fetchone()
    assert gone(expired)==(0,0,0,0)
    assert gone(recent)[:2]==(1,1) and gone(still_open)[:2]==(1,1)
    assert admin.execute("select reason,item_class,action from runtime.nexloop_erasure_tombstones where subject_key=%s",(expired,)).fetchone()==('retention_expiry','commitment_closed','deleted')
    assert ledger(admin,'commitment_closed')==1
    # Outside a registered erasure the guards still refuse, for the owner too.
    with pytest.raises(Exception,match='never deleted'),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("delete from ontology.objects where type_name='Commitment' and object_id=%s",(recent,))
    with pytest.raises(Exception,match='immutable'),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute('delete from runtime.nexloop_commitments where commitment_id=%s',(recent,))
    # A class hold stops it: the next year passes for the recent one, and the held class does nothing.
    hold=act(c['owner_actions'],'hold_retention','hold-commitments',scope_kind='item_class',scope_ref='commitment_closed',basis='审计期间暂停清除')
    backdate(admin,"update runtime.nexloop_commitment_events set recorded_at=recorded_at-interval '2 days' where subject_ref=%s",('commitment:'+recent,))
    admin.execute('delete from runtime.nexloop_retention_sweeps')  # due again now (interval elapsed)
    keeper(c).run_once()
    assert gone(recent)[:2]==(1,1)
    act(c['owner_actions'],'release_retention_hold','hold-commitments-release',hold_id=hold['hold_id'],reason='审计结束')
    admin.execute('delete from runtime.nexloop_retention_sweeps')
    keeper(c).run_once()
    assert gone(recent)==(0,0,0,0) and gone(still_open)[:2]==(1,1)


def test_superseded_claim_values_leave_a_year_after_supersession(erasing,admin):
    c=erasing;message=c.outbound('您的订单下周三发货，届时短信通知您')
    old,newer,live=(c.claim(message,q) for q in ('下周三发货','届时短信通知您','您的订单'))
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("update ontology.nexloop_claims set resolution_state='superseded' where claim_id=any(%s)",([old,newer],))
    for claim,value in ((old,'下周三发货'),(newer,'届时短信通知您')):
        proposal=str(uuid.uuid4())
        admin.execute('''insert into ontology.nexloop_mutation_proposals(tenant_id,world,proposal_id,business_intent_ref,claim_id,outcome,status,op,type_name,target_ref,property_name,
            effective_at,proposal,apply_args) values(%s,'real',%s,%s,%s,'partial_match','applied','set_property','Consumer',%s,'note',now(),%s,%s)''',
            (TENANT,proposal,'nx020:'+secrets.token_hex(32),claim,'eios:object:Consumer/'+c['consumer'],Jsonb({'value':value}),Jsonb({'value':value})))
        admin.execute('''insert into ontology.nexloop_claim_matches(tenant_id,world,claim_id,matcher_version,outcome,decision,recall,proposal_id)
            values(%s,'real',%s,'m1','partial_match',%s,%s,%s)''',(TENANT,claim,Jsonb({'value':value}),Jsonb([value]),proposal))
    # Supersession time is stamped by the database, not by the caller.
    assert admin.execute('select count(*) from ontology.nexloop_claims where claim_id=any(%s) and superseded_at>now()-interval \'1 minute\'',([old,newer],)).fetchone()==(2,)
    backdate(admin,"update ontology.nexloop_claims set superseded_at=now()-interval '366 days' where claim_id=%s",(old,))
    backdate(admin,"update ontology.nexloop_claims set superseded_at=now()-interval '364 days' where claim_id=%s",(newer,))
    backdate(admin,"update ontology.nexloop_claims set recorded_at=now()-interval '900 days' where claim_id=%s",(live,))
    keeper(c).run_once()
    row=lambda cid:admin.execute('select value,quote,subject_text,resolution_state from ontology.nexloop_claims where claim_id=%s',(cid,)).fetchone()
    assert row(old)==({'type':'erased','value':None,'erased':True},'','','superseded')  # row, id and chain stay
    assert row(newer)[0]=={'type':'string','value':'届时短信通知您'} and row(newer)[1]=='届时短信通知您'
    assert row(live)[0]=={'type':'string','value':'您的订单'}  # not superseded: knowledge kept (D2)
    assert ledger(admin,'claim_superseded')==1
    # NX-020's copies of the erased value leave with it; the superseding Claim's own copies stay.
    assert admin.execute('select proposal,apply_args,receipt from ontology.nexloop_mutation_proposals where claim_id=%s',(old,)).fetchone()==({'erased':True},{'erased':True},None)
    assert admin.execute('select decision,recall from ontology.nexloop_claim_matches where claim_id=%s',(old,)).fetchone()==({'erased':True},[])
    assert admin.execute('select proposal from ontology.nexloop_mutation_proposals where claim_id=%s',(newer,)).fetchone()[0]=={'value':'届时短信通知您'}
    # A Claim that leaves 'superseded' loses the stamp; superseding it again starts a new year.
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("update ontology.nexloop_claims set resolution_state='resolved' where claim_id=%s",(newer,))
    assert admin.execute('select superseded_at from ontology.nexloop_claims where claim_id=%s',(newer,)).fetchone()==(None,)


# -- NX-029 completion (0142): the remaining classes of the design table, ledger/tombstone expiry, Claim value copies --------

def fresh_tick(c,**kw):
    """The next keeper tick with every class due again (as after the sweep interval elapsed)."""
    c['admin'].execute('delete from runtime.nexloop_retention_sweeps')
    return keeper(c,**kw).run_once()


def test_closed_plans_leave_a_year_after_their_last_status(erasing,admin):
    from test_commitments_pg import active_plan
    c=erasing;now=NOW()
    old,young,live=(active_plan(c) for _ in range(3))
    strategy=uuid.uuid4()
    admin.execute("""insert into runtime.nexloop_strategies(tenant_id,world,strategy_id,version,consumer_id,goal_version_ref,content,created_by)
        values(%s,'real',%s,1,%s,'goal:synthetic-goal@1','先确认库存再联系','synthetic')""",(TENANT,strategy,c['consumer']))
    backdate(admin,'update runtime.nexloop_plans set strategy_ref=%s where plan_id=%s',('strategy:%s@1'%strategy,old))
    admin.execute("insert into runtime.nexloop_plan_steps(tenant_id,world,plan_id,version,step_key,prerequisites,expected_result,stop_if,budget) values(%s,'real',%s,1,'s','[]','完成','[]','{}')",(TENANT,old))
    run_id=uuid.uuid4()
    admin.execute("insert into runtime.nexloop_plan_runs(tenant_id,world,run_id,plan_id,version,triggers) values(%s,'real',%s,%s,1,'[]')",(TENANT,run_id,old))
    admin.execute("insert into runtime.nexloop_plan_outcomes(tenant_id,world,run_id,plan_id,version,kind,outcome,outcome_digest) values(%s,'real',%s,%s,1,'no_action','{}',%s)",(TENANT,run_id,old,'f'*64))
    for plan,status,at in ((old,'closed',now-timedelta(days=366)),(young,'invalidated',now-timedelta(days=364)),(live,'active',now-timedelta(days=500))):
        backdate(admin,"update runtime.nexloop_plan_events set recorded_at=%s where plan_id=%s",(at,plan))
        if status!='active':
            backdate(admin,"insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason,recorded_at) values(%s,'real',%s,1,%s,'done',%s)",(TENANT,plan,status,at))
    fresh_tick(c)
    rows=lambda p:admin.execute('''select (select count(*) from runtime.nexloop_plans where plan_id=%s),(select count(*) from runtime.nexloop_plan_events where plan_id=%s),
        (select count(*) from runtime.nexloop_plan_steps where plan_id=%s),(select count(*) from runtime.nexloop_plan_outcomes where plan_id=%s)''',(p,p,p,p)).fetchone()
    assert rows(old)==(0,0,0,0) and admin.execute('select count(*) from runtime.nexloop_strategies where strategy_id=%s',(strategy,)).fetchone()==(0,)
    assert rows(young)[:2]==(1,2) and rows(live)[:2]==(1,1)
    assert admin.execute("select item_class,action from runtime.nexloop_erasure_tombstones where subject_key=%s",(str(old),)).fetchone()==('plan_closed','deleted')
    assert ledger(admin,'plan_closed')==1
    backdate(admin,"update runtime.nexloop_plan_events set recorded_at=recorded_at-interval '2 days' where plan_id=%s",(young,))
    fresh_tick(c)
    assert rows(young)==(0,0,0,0) and rows(live)[:2]==(1,1)


def test_effect_text_leaves_ninety_days_after_the_intent(erasing,admin):
    c=erasing;now=NOW()
    old,young,inflight=(c.outbound(t) for t in ('九十天前发出的回复','八十九天前发出的回复','尚未完成的请求'))
    for m,days in ((old,91),(young,89),(inflight,200)):
        backdate(admin,'update runtime.nexloop_effect_intents set created_at=%s where intent_id=%s',(now-timedelta(days=days),m['intent_id']))
    backdate(admin,"update runtime.nexloop_effect_intents set state='accepted',governed_claim_finalized=false where intent_id=%s",(inflight['intent_id'],))
    fresh_tick(c)
    req=lambda m:admin.execute('select state,frozen_request from runtime.nexloop_effect_intents where intent_id=%s',(m['intent_id'],)).fetchone()
    assert req(old)==('fulfilled',{'erased':True})
    assert req(young)[1]=={'parameters':{'message':'八十九天前发出的回复'}} and req(inflight)[1]=={'parameters':{'message':'尚未完成的请求'}}
    assert ledger(admin,'effect_text')==1


def test_job_records_leave_a_year_after_the_task_ends(erasing,admin):
    c=erasing;now=NOW()
    bound=run(admin,'succeeded',now-timedelta(days=366))
    bound_job=admin.execute('select task_id from authz.nexloop_runtime_run_bindings where run_id=%s',(bound,)).fetchone()[0]
    jobs={}
    for name,days in (('free-old',366),('free-young',364)):
        jobs[name]='expiry-free-'+secrets.token_hex(4)
        backdate(admin,'''insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
            request_id,normalized_input,plan_snapshot,status,result,created_at,updated_at,world,queue) values(%s,'inv-1','nexloop.run','1','runtime','async',%s,'a','t','r',%s,'{}',
            'succeeded',%s,%s,%s,'real','operations')''',(jobs[name],TENANT,Jsonb({'input':'一年前的输入'}),Jsonb({'text':'一年前的结果'}),now-timedelta(days=days),now-timedelta(days=days)))
    for job in (bound_job,jobs['free-old'],jobs['free-young']):
        admin.execute("insert into runtime.job_events(tenant_id,job_id,sequence,status,message,data,created_at) values(%s,%s,1,'succeeded','完成','{}',now())",(TENANT,job))
    for key,days in (('idem-old',366),('idem-young',364)):
        admin.execute("insert into runtime.idempotency_records(tenant_id,operation,idempotency_key,request_digest,result_type,result,created_at) values(%s,'op',%s,%s,'r','{}',%s)",
            (TENANT,key,'a'*64,now-timedelta(days=days)))
    fresh_tick(c)
    count=lambda sql,p:admin.execute(sql,p).fetchone()[0]
    assert count('select count(*) from runtime.jobs where job_id=%s',(jobs['free-old'],))==0
    assert count('select count(*) from runtime.jobs where job_id=%s',(jobs['free-young'],))==1
    assert admin.execute('select normalized_input,plan_snapshot from runtime.jobs where job_id=%s',(bound_job,)).fetchone()==({'erased':True},{'erased':True})
    assert count('select count(*) from runtime.job_events where job_id=any(%s)',([bound_job,jobs['free-old']],))==0
    assert count('select count(*) from runtime.job_events where job_id=%s',(jobs['free-young'],))==1
    assert [k for (k,) in admin.execute("select idempotency_key from runtime.idempotency_records where operation='op'").fetchall()]==['idem-young']
    assert ledger(admin,'job_records')==1


def test_model_request_manifest_leaves_a_year_after_the_request(erasing,admin):
    c=erasing;now=NOW();runs={}
    for name,days in (('old',366),('young',364)):
        runs[name]=uuid.uuid4()
        backdate(admin,'''insert into runtime.nexloop_model_requests(tenant_id,world,run_id,call_sequence,context_id,model_provider,model_id,tool_manifest_digest,request_digest,
            prompt_artifact_id,input_token_budget,output_token_budget,settings_digest,requested_at) values(%s,'real',%s,1,%s,'synthetic','m',%s,%s,%s,1024,256,%s,%s)''',
            (TENANT,runs[name],uuid.uuid4(),'a'*64,'b'*64,'c'*32,'d'*64,now-timedelta(days=days)))
        backdate(admin,"insert into runtime.nexloop_model_request_prompts(tenant_id,world,run_id,call_sequence,prompt_artifact_id,request_text) values(%s,'real',%s,1,%s,'')",(TENANT,runs[name],'c'*32))
    fresh_tick(c)
    n=lambda r:admin.execute('select (select count(*) from runtime.nexloop_model_requests where run_id=%s),(select count(*) from runtime.nexloop_model_request_prompts where run_id=%s)',(r,r)).fetchone()
    assert n(runs['old'])==(0,0) and n(runs['young'])==(1,1)
    assert ledger(admin,'model_request_manifest')==1
    with pytest.raises(Exception,match='append-only'),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute('delete from runtime.nexloop_model_request_prompts where run_id=%s',(runs['young'],))
        admin.execute('delete from runtime.nexloop_model_requests where run_id=%s',(runs['young'],))


def test_read_audit_rows_fold_into_daily_counts_after_a_year(erasing,admin):
    c=erasing;now=NOW();day=(now-timedelta(days=370)).replace(hour=12)
    for i,(purpose,at) in enumerate((('review',day),('review',day),('export',day),('review',now-timedelta(days=364)))):
        backdate(admin,'''insert into runtime.nexloop_workbench_read_audit(tenant_id,world,principal_id,role,session_id,object_kind,target_resource,read_purpose,read_txid,read_at)
            values(%s,'real','synthetic-reader','owner','s',%s,%s,%s,%s,%s)''',(TENANT,'consumer','eios:object:Consumer/'+c['consumer'],purpose,9000+i,at))
    fresh_tick(c)
    assert admin.execute("select read_purpose,count(*) from runtime.nexloop_workbench_read_audit where principal_id='synthetic-reader' group by 1").fetchall()==[('review',1)]
    assert sorted(admin.execute('select day,read_purpose,object_kind,reads from runtime.nexloop_read_audit_daily').fetchall())==[
        (day.date(),'export','consumer',1),(day.date(),'review','consumer',2)]
    assert ledger(admin,'read_audit')==1


def test_governance_audit_leaves_a_year_after_the_event(erasing,admin):
    c=erasing;now=NOW();old=now-timedelta(days=366)
    for event,at in (('audit-old',old),('audit-young',now-timedelta(days=364))):
        admin.execute("insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at) values(%s,'synthetic',%s,'a','t','r','{}',%s)",(event,TENANT,at))
    base=admin.execute('select coalesce(max(revision),0) from control.nexloop_control_events').fetchone()[0]
    for rev,ref in ((base+1,'scope-a'),(base+2,'scope-a'),(base+3,'scope-b')):
        backdate(admin,"""insert into control.nexloop_control_events(tenant_id,world,revision,event_kind,scope_kind,scope_ref,detail,principal_id,intent_id,recorded_at)
            values(%s,'real',%s,'pause','consumer',%s,'{}','p','i',%s)""",(TENANT,rev,ref,old))
    backdate(admin,"insert into control.nexloop_data_exports(tenant_id,world,export_id,consumer_id,requested_by,reason,created_at,expires_at) values(%s,'real','export-old',%s,'p','r',%s,%s)",(TENANT,c['consumer'],old,old))
    backdate(admin,"""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark,created_at,updated_at,completed_at)
        values(%s,'real','erase-old',%s,'owner','o','r','completed',0,%s,%s,%s)""",(TENANT,'e'*64,old,old,old))
    backdate(admin,"insert into control.nexloop_deletion_ledger(seq,tenant_id,world,kind,subject_ref,request_ref,recorded_at) values(nextval('control.nexloop_deletion_watermark'),%s,'real','consumer',%s,'erase-old',%s)",
        (TENANT,'e'*64,now-timedelta(days=200)))
    fresh_tick(c)
    assert [e for (e,) in admin.execute("select event_id from runtime.audit_events where action='synthetic'").fetchall()]==['audit-young']
    assert [r for (r,) in admin.execute('select revision from control.nexloop_control_events where revision>%s order by 1',(base,)).fetchall()]==[base+2,base+3]
    assert admin.execute("select count(*) from control.nexloop_data_exports where export_id='export-old'").fetchone()==(0,)
    # The deletion request stays while its ledger entry does (the ledger outlives every backup).
    assert admin.execute("select count(*) from control.nexloop_consumer_erasures where request_id='erase-old'").fetchone()==(1,)
    backdate(admin,"update control.nexloop_deletion_ledger set recorded_at=%s where request_ref='erase-old'",(now-timedelta(days=366),))
    record_journal_export(admin)  # 0156: the ledger entry leaves only once the restore journal carrying it is exported
    fresh_tick(c)  # records removes the entry, governance_audit (earlier in the same tick) still saw it
    fresh_tick(c)
    assert admin.execute("select count(*) from control.nexloop_consumer_erasures where request_id='erase-old'").fetchone()==(0,)
    assert ledger(admin,'governance_audit')>=1


def test_ledger_and_tombstones_outlive_backups_and_never_leave_during_a_replay(erasing,admin):
    c=erasing;now=NOW()
    settings=admin.execute('select definition->\'records\' from control.nexloop_retention_settings order by version desc limit 1').fetchone()[0]
    assert settings['backup_max_retention_days']<min(settings['deletion_ledger_days'],settings['tombstone_days'])
    for ref,days in (('rec-old',366),('rec-young',364)):
        backdate(admin,"insert into control.nexloop_deletion_ledger(seq,tenant_id,world,kind,subject_ref,request_ref,recorded_at) values(nextval('control.nexloop_deletion_watermark'),%s,'real','message',%s,%s,%s)",
            (TENANT,'f'*64,ref,now-timedelta(days=days)))
        backdate(admin,"""insert into runtime.nexloop_erasure_tombstones(tenant_id,world,tombstone_id,reason,request_ref,item_class,subject_kind,subject_key,action,counts,executed_at,executed_by)
            values(%s,'real',%s,'message_erasure',%s,'message','message',%s,'redacted','{}',%s,'synthetic')""",(TENANT,secrets.token_hex(32),ref,ref,now-timedelta(days=days)))
    refs=lambda:sorted(r for (r,) in admin.execute("select request_ref from control.nexloop_deletion_ledger where request_ref like 'rec-%' union all select request_ref from runtime.nexloop_erasure_tombstones where request_ref like 'rec-%'").fetchall())
    # A restore replay is open: nothing leaves.
    backdate(admin,"insert into control.nexloop_deletion_replays(restore_ref,from_seq,state) values('restore-1',0,'replaying')")
    fresh_tick(c)
    assert refs()==['rec-old','rec-old','rec-young','rec-young']
    backdate(admin,"update control.nexloop_deletion_replays set state='replayed',finished_at=now() where restore_ref='restore-1'")
    fresh_tick(c)
    # 0156: the old tombstone leaves on its days; the old ledger entry waits for the export of its journal entry.
    assert refs()==['rec-old','rec-young','rec-young']
    record_journal_export(admin)
    fresh_tick(c)
    assert refs()==['rec-young','rec-young']

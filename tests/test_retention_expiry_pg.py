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
    # A Claim that leaves 'superseded' loses the stamp; superseding it again starts a new year.
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("update ontology.nexloop_claims set resolution_state='resolved' where claim_id=%s",(newer,))
    assert admin.execute('select superseded_at from ontology.nexloop_claims where claim_id=%s',(newer,)).fetchone()==(None,)

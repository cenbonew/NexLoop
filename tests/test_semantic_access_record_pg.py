"""NX-038 E3 (design D2, §2.2, §12.5): the Context Engine's per-access semantic record, actual PostgreSQL.

Append only; term hashes, refs and counts, never the query text; tenant and world from the authenticated service identity;
a failed write never fails the access; NX-029 retention class semantic_access and Consumer erasure delete it; the evo reader
reads it only through its function. Synthetic only: the admin connection seeds Runs and erasure requests and probes."""
import json
import secrets
import time
import uuid
from datetime import UTC,datetime,timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine.semantic import SemanticAccessRecorder,SemanticAdapter,term_hash
from nexloop_eios.postgres_artifacts import AuthoritySigner
from test_recall_pg import recall_db,recall_for,A,B  # noqa: F401  (fixture)

CONSUMER='c'*64
OTHER_CONSUMER='d'*64


def backdate(admin,sql,params=()):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute(sql,params)


def bound_run(admin,consumer,tenant=A):
    """A Run as the activation registry leaves it, bound to a Consumer through its run command."""
    run_id=uuid.uuid4();job='semantic-job-'+secrets.token_hex(4);now=datetime.now(UTC)
    backdate(admin,'''insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
        request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue) values(%s,'inv-1','nexloop.run','1','runtime','async',%s,'a','t','r',%s,'{}',
        'running',%s,%s,'real','operations')''',(job,tenant,json.dumps({'run_command':{'consumer_ref':'consumer:'+consumer}}),now,now))
    backdate(admin,'''insert into authz.nexloop_runtime_run_bindings(run_id,tenant_id,world,queue,task_id,run_digest,command_digest,input_digest,owner_epoch,enrolled_by,created_at)
        values(%s,%s,'real','operations',%s,%s,%s,%s,1,'synthetic',%s)''',(run_id,tenant,job,secrets.token_hex(32),'a'*64,'b'*64,now))
    return str(run_id)


def rows(admin,run_id=None):
    sql='select access_id::text,kind,run_id::text,context_id,accessed_id::text,operation,query_kind,term_hashes,coverage,ambiguity,result_refs,result_count,unresolved_count from runtime.nexloop_semantic_accesses'
    if run_id is None:return admin.execute(sql+' order by recorded_at').fetchall()
    return admin.execute(sql+' where run_id=%s order by recorded_at',(run_id,)).fetchall()


@pytest.fixture
def semantic(recall_db):  # noqa: F811
    f=recall_db;signer=AuthoritySigner('semantic-access-key',secrets.token_bytes(32))
    f['admin'].execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(signer.key_id,signer.material))
    session=authenticate_service(f['worker'],f['reader_token'],world='real')
    return f,session,signer


def adapter(f,session,signer,recorder=None):
    return SemanticAdapter(f['worker'],session,signer,recall_for(f,session),recorder=recorder)


def test_each_access_appends_hashes_refs_and_counts_never_the_text(semantic):
    f,session,signer=semantic;admin=f['admin'];run=bound_run(admin,CONSUMER)
    recorder=SemanticAccessRecorder(f['worker'],session,signer,run_id=run,context_id='ctx:semantic-1')
    a=adapter(f,session,signer,recorder)
    browsed=a.browse('关注防水功能')
    resolved=a.resolve(['关注防水','完全无关的火星词汇xyz'],type_hint='Consumer')
    assert recorder.written==2 and recorder.failures==0 and browsed['access_id'] and resolved['access_id']
    first,second=rows(admin,run)
    assert first[:7]==(browsed['access_id'],'access',run,'ctx:semantic-1',None,'browse','any')
    assert first[7]==[term_hash('关注防水功能')] and float(first[8])==browsed['coverage'] and first[9]==browsed['ambiguity']
    assert first[10]==[{'ref':i['ref'],'revision':str(i['schema_version'])} for i in browsed['items']] and first[11]==len(browsed['items'])
    assert second[5:8]==('resolve','object_type',[term_hash('关注防水'),term_hash('完全无关的火星词汇xyz')])
    assert second[12]==sum(1 for r in resolved['resolutions'] if r['status']=='unresolved')
    dump=json.dumps(rows(admin),ensure_ascii=False,default=str)
    assert '关注防水功能' not in dump and '火星词汇' not in dump and 'xyz' not in dump and 'score' not in dump  # hashes and refs only
    # A use is a second row; only refs the access returned, only within the same Run.
    used=[first[10][0]]
    use_id=recorder.used(browsed['access_id'],used)
    assert use_id and rows(admin,run)[2][1:5]==('use',run,'ctx:semantic-1',browsed['access_id']) and rows(admin,run)[2][10]==used
    stranger=SemanticAccessRecorder(f['worker'],session,signer,run_id=run)
    assert stranger.used(browsed['access_id'],[{'ref':'eios:object_type:Unreturned','revision':'1'}]) is None and stranger.failures==1
    other_run=SemanticAccessRecorder(f['worker'],session,signer,run_id=bound_run(admin,CONSUMER))
    assert other_run.used(browsed['access_id'],used) is None and other_run.failures==1
    assert len(rows(admin,run))==3


def test_records_are_append_only_even_for_the_owner(semantic):
    f,session,signer=semantic;admin=f['admin']
    recorder=SemanticAccessRecorder(f['worker'],session,signer)
    adapter(f,session,signer,recorder).browse('防水')
    access=rows(admin)[0][0]
    for statement in ("update runtime.nexloop_semantic_accesses set coverage=0 where access_id=%s",
                      "delete from runtime.nexloop_semantic_accesses where access_id=%s"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():
            admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(A,))
            admin.execute(statement,(access,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():admin.execute('truncate runtime.nexloop_semantic_accesses')
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_evo_reader','nexloop_metrics'):
        for privilege in ('select','insert','update','delete'):
            assert admin.execute('select has_table_privilege(%s,%s,%s)',(role,'runtime.nexloop_semantic_accesses',privilege)).fetchone()==(False,)
    assert admin.execute("select has_function_privilege('nexloop_evo_reader','authz.nexloop_semantic_access_record(text,text,text,text,text)','execute')").fetchone()==(False,)


def test_a_failed_write_never_fails_the_access(semantic):
    f,session,signer=semantic;admin=f['admin']
    plain=adapter(f,session,signer).browse('关注防水功能')
    wrong_key=SemanticAccessRecorder(f['worker'],session,AuthoritySigner(signer.key_id,secrets.token_bytes(32)))
    unknown_run=SemanticAccessRecorder(f['worker'],session,signer,run_id=uuid.uuid4())
    for recorder in (wrong_key,unknown_run):
        out=adapter(f,session,signer,recorder).browse('关注防水功能')
        assert out['items']==plain['items'] and out['access_id'] is None and recorder.failures==1 and recorder.written==0
    # A Run of another tenant is unknown to this service identity (tenant from the session, never from the payload).
    foreign=SemanticAccessRecorder(f['worker'],session,signer,run_id=bound_run(admin,CONSUMER,tenant=B))
    assert adapter(f,session,signer,foreign).browse('防水')['access_id'] is None and foreign.failures==1
    assert rows(admin)==[]


def test_consumer_erasure_deletes_the_records_of_its_runs(semantic):
    f,session,signer=semantic;admin=f['admin']
    erased,kept=bound_run(admin,CONSUMER),bound_run(admin,OTHER_CONSUMER)
    for run in (erased,kept):
        recorder=SemanticAccessRecorder(f['worker'],session,signer,run_id=run)
        out=adapter(f,session,signer,recorder).browse('防水');recorder.used(out['access_id'],out and [{'ref':i['ref'],'revision':str(i['schema_version'])} for i in out['items'][:1]])
    assert len(rows(admin,erased))==2 and len(rows(admin,kept))==2
    admin.execute('''insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','req-semantic',%s,'owner','synthetic','synthetic','blocking',1)''',(A,CONSUMER))
    admin.execute("update control.nexloop_consumer_erasures set state='purging',phase='messages' where request_id='req-semantic'")
    assert rows(admin,erased)==[] and len(rows(admin,kept))==2
    stone=admin.execute("select reason,item_class,subject_key,action,counts from runtime.nexloop_erasure_tombstones where item_class='semantic_access'").fetchall()
    assert stone==[('consumer_erasure','semantic_access',CONSUMER,'deleted',{'semantic_accesses':2})]
    # A record written while purging (a straggler) leaves when the request completes.
    recorder=SemanticAccessRecorder(f['worker'],session,signer,run_id=erased)
    adapter(f,session,signer,recorder).browse('防水');assert len(rows(admin,erased))==1
    admin.execute("update control.nexloop_consumer_erasures set state='completed',phase=null where request_id='req-semantic'")
    assert rows(admin,erased)==[] and len(rows(admin,kept))==2


def test_retention_class_semantic_access_deletes_expired_records(semantic):
    f,session,signer=semantic;admin=f['admin']
    settings=admin.execute('select definition from control.nexloop_retention_settings order by version desc limit 1').fetchone()[0]
    assert settings['classes']['semantic_access']=={'days':90,'source':None}
    assert 'semantic_access' in admin.execute('select runtime.nexloop_retention_classes()').fetchone()[0]
    recorder=SemanticAccessRecorder(f['worker'],session,signer)
    a=adapter(f,session,signer,recorder);a.browse('防水');a.browse('预算')
    old=rows(admin)[0][0]
    backdate(admin,"update runtime.nexloop_semantic_accesses set recorded_at=now()-interval '91 days' where access_id=%s",(old,))
    out=admin.execute("select runtime.nexloop_retention_sweep(%s,'real','semantic_access',200,'sweep:semantic','service:synthetic')",(A,)).fetchone()[0]
    assert out['processed']==1 and out['counts']=={'semantic_accesses':1} and [r[0] for r in rows(admin)]!=[old] and len(rows(admin))==1
    # A hold on the class keeps everything.
    backdate(admin,"update runtime.nexloop_semantic_accesses set recorded_at=now()-interval '91 days'")
    admin.execute('''insert into control.nexloop_retention_holds(tenant_id,world,hold_id,scope_kind,scope_ref,basis,held_by)
        values(%s,'real','hold-semantic','item_class','semantic_access','synthetic','synthetic')''',(A,))
    admin.execute("select set_config('eios.tenant_id',%s,false)",(A,))  # as the keeper's signed port sets it
    held=admin.execute("select runtime.nexloop_retention_sweep(%s,'real','semantic_access',200,'sweep:held','service:synthetic')",(A,)).fetchone()[0]
    assert held['held'] is True and len(rows(admin))==1


def test_the_evo_reader_reads_only_its_tenant_through_the_function(semantic,pg):
    f,session,signer=semantic;admin=f['admin']
    recorder=SemanticAccessRecorder(f['worker'],session,signer,run_id=bound_run(admin,CONSUMER))
    adapter(f,session,signer,recorder).browse('防水')
    since=datetime.now(UTC)-timedelta(hours=1)
    with psycopg.connect(make_conninfo(pg,user='nexloop_evo_reader'),autocommit=True) as reader:
        out=reader.execute('select authz.nexloop_semantic_access_read(%s,%s,%s,%s)',(session.token_digest,'real',since,100)).fetchone()[0]
        assert [r['access_id'] for r in out]==[rows(admin)[0][0]] and set(out[0])>={'term_hashes','result_refs','result_count','run_id'}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):reader.execute('select * from runtime.nexloop_semantic_accesses')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute('select authz.nexloop_semantic_access_read(%s,%s,%s,%s)',(session.token_digest,'test',since,100))
    with pytest.raises(psycopg.errors.InsufficientPrivilege),f['worker'].connection() as db:
        db.execute('select authz.nexloop_semantic_access_read(%s,%s,%s,%s)',(session.token_digest,'real',since,100))


def test_record_overhead_per_access(semantic,capsys):
    """Measured, not a threshold: the added cost of one record on top of one browse (reported in the task evidence)."""
    f,session,signer=semantic
    plain=adapter(f,session,signer);recorded=adapter(f,session,signer,SemanticAccessRecorder(f['worker'],session,signer))
    for a in (plain,recorded):a.browse('防水')
    def mean(a,n=20):
        start=time.perf_counter()
        for _ in range(n):a.browse('关注防水功能')
        return (time.perf_counter()-start)/n*1000
    base,with_record=mean(plain),mean(recorded)
    refs=[{'ref':'eios:object_type:Consumer','revision':'1'}]*5;start=time.perf_counter()
    for _ in range(50):recorded.recorder.access('browse','any',['关注防水功能'],coverage=1.0,ambiguity=False,refs=refs)
    write=(time.perf_counter()-start)/50*1000
    with capsys.disabled():print(f'\nsemantic browse {base:.2f} ms, with record {with_record:.2f} ms; one record write {write:.2f} ms')
    assert recorded.recorder.failures==0

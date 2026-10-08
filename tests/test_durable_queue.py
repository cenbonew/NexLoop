from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import secrets,time
import psycopg,pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios.durable_queue import PostgresDurableQueue,QueueConflict,StaleQueueLease
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.errors import AuthorizationUnavailable
from authority_fixture import seed_authority

TARGET='eios:action:NexLoop.queue.operations:1'
@pytest.fixture
def queues(admin,pg):
    bootstrap(admin);s=AuthoritySigner('synthetic-queue',secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(s.key_id,s.material))
    with ExitStack() as stack:
        existing=[]
        def port(role,tenant='synthetic-a',suffix='',world='real',target=TARGET):
            token,_=seed_authority(admin,tenant,target,world=world,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-'+role+suffix)
            pool=stack.enter_context(open_core(make_conninfo(pg,user=role)))
            result=PostgresDurableQueue(pool,authenticate_service(pool,token,world=world),s,queue='operations')
            existing.append((result,token))
            # Synthetic authority provisioning advances realm epoch. Refresh all
            # configured contexts before operating, never bypass the live check.
            for configured,raw in existing:configured.session=authenticate_service(configured.pool,raw,world=configured.session.world)
            return result
        yield port('nexloop_api'),port('nexloop_scheduler'),port

def event(api,id='event-1',**kwargs):return api.accept(source_id='synthetic-channel',event_id=id,payload={'text':'synthetic'},**kwargs)

def test_accept_atomic_ledger_and_replay_after_reopen(queues,admin,pg):
    api,scheduler,_=queues;accepted=event(api)
    assert accepted['accepted'] and accepted['created']
    assert event(api)=={**accepted,'created':False}
    assert admin.execute('select count(*) from runtime.invocations').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.jobs').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_inbox').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_outbox').fetchone()[0]==1
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0
    with open_core(make_conninfo(pg,user='nexloop_scheduler')) as pool:
        reopened=PostgresDurableQueue(pool,scheduler.session,scheduler.signer,queue='operations')
        job=reopened.claim();assert job['task_id']==accepted['task_id'] and job['fence']==1
        assert reopened.finish(task_id=job['task_id'],fence=1,status='succeeded',result={'ok':True})['status']=='succeeded'
    assert admin.execute('select status from runtime.invocations').fetchone()[0]=='succeeded'

@pytest.mark.parametrize('change',['payload','contract','max_attempts'])
def test_same_event_different_contract_conflicts(queues,admin,change):
    api,_,_=queues;event(api)
    if change=='payload':call=lambda:api.accept(source_id='synthetic-channel',event_id='event-1',payload={'text':'different'})
    elif change=='max_attempts':call=lambda:event(api,max_attempts=4)
    else:
        # Same granted queue resource with changed configured contract cannot alias
        # another event key; queue mismatch is tested as authorization denial below.
        call=lambda:api.accept(source_id='synthetic-channel',event_id='event-1',payload={'queue':'other'})
    with pytest.raises(QueueConflict):call()
    assert admin.execute('select count(*) from runtime.jobs').fetchone()[0]==1

def test_outbox_insert_failure_rolls_back_acceptance(queues,admin):
    api,_,_=queues
    admin.execute("create function public.synthetic_outbox_failure() returns trigger language plpgsql as $$begin raise exception 'synthetic fault';end$$")
    admin.execute('create trigger synthetic_fault before insert on runtime.nexloop_outbox for each row execute function public.synthetic_outbox_failure()')
    with pytest.raises(RuntimeError):event(api)
    for table in ['invocations','jobs','nexloop_inbox','nexloop_outbox','job_events']:
        assert admin.execute('select count(*) from runtime.'+table).fetchone()[0]==0
    admin.execute('drop trigger synthetic_fault on runtime.nexloop_outbox')
    assert event(api)['created']

def test_concurrent_event_deduplication(queues,admin):
    api,_,_=queues
    with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(lambda _:event(api),range(2)))
    assert sum(r['created'] for r in results)==1 and len({r['task_id'] for r in results})==1
    assert admin.execute('select count(*) from runtime.jobs').fetchone()[0]==1

def test_concurrent_claims_are_distinct_and_pg_polling_needs_no_cache(queues):
    api,scheduler,_=queues
    for i in range(4):event(api,str(i))
    with ThreadPoolExecutor(max_workers=4) as executor:jobs=list(executor.map(lambda _:scheduler.claim(),range(4)))
    assert len({j['task_id'] for j in jobs})==4 and all(j['fence']==1 for j in jobs)
    assert scheduler.claim() is None

def test_expired_lease_reclaims_with_new_fence_and_old_worker_denied(queues):
    api,scheduler,_=queues;event(api)
    old=scheduler.claim(lease_seconds=1);time.sleep(1.1)
    with pytest.raises(StaleQueueLease):scheduler.finish(task_id=old['task_id'],fence=old['fence'],status='succeeded')
    new=scheduler.claim();assert new['task_id']==old['task_id'] and new['fence']==old['fence']+1
    with pytest.raises(StaleQueueLease):scheduler.finish(task_id=old['task_id'],fence=old['fence'],status='succeeded')
    assert scheduler.finish(task_id=new['task_id'],fence=new['fence'],status='succeeded')['status']=='succeeded'
    with pytest.raises(StaleQueueLease):scheduler.finish(task_id=old['task_id'],fence=old['fence'],status='failed')
    assert scheduler.inspect(task_id=new['task_id'])['status']=='succeeded'

def test_other_worker_cannot_finish_lease(queues):
    api,scheduler,port=queues;event(api);job=scheduler.claim()
    other=port('nexloop_scheduler',suffix='other')
    with pytest.raises(StaleQueueLease):other.finish(task_id=job['task_id'],fence=job['fence'],status='succeeded')

def test_due_retry_budget_renew_and_deadletter(queues,admin):
    api,scheduler,_=queues;event(api,max_attempts=2)
    job=scheduler.claim(lease_seconds=1)
    assert scheduler.renew(task_id=job['task_id'],fence=job['fence'],lease_seconds=30)['fence']==1
    assert scheduler.finish(task_id=job['task_id'],fence=1,status='retry_wait',retry_seconds=1)['status']=='retry_wait'
    assert scheduler.claim() is None;time.sleep(1.1);new=scheduler.claim();assert new['fence']==2
    assert scheduler.finish(task_id=new['task_id'],fence=2,status='retry_wait')['status']=='dead_lettered'
    assert scheduler.claim() is None
    assert admin.execute('select status from runtime.invocations').fetchone()[0]=='dead_lettered'

def test_expired_final_attempt_is_deadlettered(queues,admin):
    api,scheduler,_=queues;event(api,max_attempts=1);scheduler.claim(lease_seconds=1);time.sleep(1.1)
    assert scheduler.claim() is None
    assert admin.execute('select status from runtime.jobs').fetchone()[0]=='dead_lettered'

def test_outbox_reclaim_and_old_fence_ack_denied(queues):
    api,scheduler,_=queues;event(api)
    old=scheduler.claim_outbox(lease_seconds=1);time.sleep(1.1)
    new=scheduler.claim_outbox();assert new['outbox_id']==old['outbox_id'] and new['fence']==2
    with pytest.raises(StaleQueueLease):scheduler.acknowledge_outbox(outbox_id=old['outbox_id'],fence=1)
    assert scheduler.acknowledge_outbox(outbox_id=new['outbox_id'],fence=2)['delivered']
    assert scheduler.claim_outbox() is None

@pytest.mark.parametrize('mutation',['tenant','world','resource'])
def test_queue_boundaries_and_live_grant_denial(queues,mutation):
    api,scheduler,port=queues;event(api)
    if mutation=='tenant':other=port('nexloop_scheduler',tenant='synthetic-b');assert other.claim() is None
    elif mutation=='world':other=port('nexloop_scheduler',suffix='test',world='test');assert other.claim() is None
    else:
        other=port('nexloop_scheduler',suffix='other',target='eios:action:NexLoop.queue.other:1')
        with pytest.raises(AuthorizationUnavailable):other.claim()

@pytest.mark.parametrize('verb',['claim','accept'])
def test_database_roles_cannot_cross_ports(queues,verb):
    api,scheduler,_=queues
    with pytest.raises(psycopg.errors.InsufficientPrivilege):api.claim() if verb=='claim' else event(scheduler)

def test_revoked_scheduler_cannot_finish_and_raw_tables_denied(queues,admin):
    api,scheduler,_=queues;event(api);job=scheduler.claim()
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(scheduler.session.token_digest,))
    with pytest.raises(AuthorizationUnavailable):scheduler.finish(task_id=job['task_id'],fence=job['fence'],status='succeeded')
    with api.pool.connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from runtime.jobs')
    assert admin.execute('select status from runtime.jobs').fetchone()[0]=='running'


def test_independent_domain_worker_claims_and_finishes_own_lease(queues):
    api,_,port=queues
    worker=port('nexloop_domain_worker');event(api)
    job=worker.claim()
    assert worker.finish(task_id=job['task_id'],fence=job['fence'],status='succeeded')['status']=='succeeded'


def test_completion_receipt_replay_and_conflicting_result(queues):
    api,scheduler,_=queues;accepted=event(api);job=scheduler.claim()
    args=dict(task_id=job['task_id'],fence=job['fence'],status='succeeded',result={'ok':True})
    receipt=scheduler.finish(**args);assert scheduler.finish(**args)==receipt
    with pytest.raises(QueueConflict):scheduler.finish(**{**args,'result':{'ok':False}})
    assert api.inspect(task_id=accepted['task_id'])['result']=={'ok':True}

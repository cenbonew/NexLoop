"""NX-019 background extraction over actual PG: Message→feed→durable queue→worker.

Real governed Human Messages; scheduler is a nexloop_api service, worker a
nexloop_domain_worker service. Admin only asserts and probes. Synthetic data only.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.claim_extraction_jobs import QUEUE,ClaimExtractionScheduler,ClaimExtractionWorker,ClaimFeed
from nexloop_eios.claim_store import ClaimExtractionDenied,ConversationClaimExtractor
from nexloop_eios.conversation_extraction import DeterministicExtractionProvider,build_user_payload
from nexloop_eios.durable_queue import PostgresDurableQueue
from multi_authority_fixture import seed_multi_authority
from test_claim_store_pg import source_targets
from test_conversation_messages import conversations
from test_browser_business_authorization import browser_business
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow

QUEUE_TARGET=('eios:action:NexLoop.queue.'+QUEUE+':1',ResourceType.ACTION,Operation.EXECUTE)
EXTRACT_TARGET=('eios:action:nexloop.claim.extract:1',ResourceType.ACTION,Operation.EXECUTE)


def make_window(fixture,bodies,key):
    port=fixture['port'];created=port.create_conversation(idempotency_key='nx019-job-conversation-'+key)
    ids=[port.accept_message(conversation_id=created['id'],idempotency_key=f'nx019-claim-job-message-{key}-{index}',body=body)['message']['id'] for index,body in enumerate(bodies)]
    return created['id'],ids


@pytest.fixture
def services(conversations,admin):
    fixture=conversations
    conversation_id,ids=make_window(fixture,['付款页面一直报错。','解决后我再考虑续费。','好的'],'a')
    api=fixture['reader'].pool;signer=fixture['reader'].signer
    with open_core(make_conninfo(fixture['pg'],user='nexloop_domain_worker')) as worker_pool:
        _,scheduler_token=seed_multi_authority(admin,api,[EXTRACT_TARGET,QUEUE_TARGET],identity_suffix='-claim-scheduler')
        _,worker_token=seed_multi_authority(admin,worker_pool,[QUEUE_TARGET]+source_targets(conversation_id,ids),identity_suffix='-claim-worker')
        # Provisioning advances the realm epoch; authenticate both after all seeding.
        scheduler=authenticate_service(api,scheduler_token,world='real');worker=authenticate_service(worker_pool,worker_token,world='real')
        yield dict(fixture=fixture,conversation_id=conversation_id,ids=ids,api=api,worker_pool=worker_pool,signer=signer,
                   scheduler=scheduler,worker=worker,worker_token=worker_token)


def response():
    return {'topics':[{'topic':'付款与续费','conversation_summary':'付款报错，续费有条件','user_valid_reply':True,'message_refs':[1,2,3]}],
        'claims':[{'topic_index':0,'message_ref':1,'quote':'付款页面一直报错','kind':'need_problem','predicate':'付款页面故障','value':{'type':'string','value':'报错'}},
                  {'topic_index':0,'message_ref':2,'quote':'解决后我再考虑续费','kind':'intent','predicate':'续费意向','value':{'type':'boolean','value':True}}]}


def registered_provider(s,message_ids,body=None):
    extractor=ConversationClaimExtractor(s['worker_pool'],s['worker'],s['signer'],None,timezone='Asia/Shanghai')
    context,messages=extractor.load_window(s['conversation_id'],message_ids)
    return DeterministicExtractionProvider({hashlib.sha256(build_user_payload(messages,context).encode()).hexdigest():body or response()})


def jobs(admin):
    return admin.execute("select status,attempts,result from runtime.jobs where queue=%s order by created_at,job_id",(QUEUE,)).fetchall()


def test_message_commit_feeds_background_queue_and_worker_records_claims(services,admin):
    s=services
    # Interactive commit only persisted feed rows; no extraction ran synchronously.
    assert admin.execute('select count(*),count(task_id) from runtime.nexloop_claim_extraction_feed').fetchone()==(3,0)
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone()==(0,)
    scheduler=ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=0)
    backlog=scheduler.feed.backlog()
    assert backlog['feed_pending']==3 and backlog['oldest_pending_seconds']>=0 and backlog['jobs']=={}
    tasks=scheduler.run_once()
    assert len(tasks)==1 and scheduler.run_once()==[]
    assert admin.execute('select count(*) from runtime.nexloop_claim_extraction_feed where task_id=%s',(tasks[0],)).fetchone()==(3,)
    payload=admin.execute('select normalized_input from runtime.jobs where job_id=%s',(tasks[0],)).fetchone()[0]
    assert payload=={'conversation_id':s['conversation_id'],'through_sequence':3,'message_ids':s['ids']}
    provider=registered_provider(s,s['ids'])
    worker=ClaimExtractionWorker(s['worker_pool'],s['worker'],s['signer'],provider,timezone='Asia/Shanghai')
    assert worker.run_once()=='succeeded' and worker.run_once()=='idle'
    (status,attempts,result),=jobs(admin)
    assert status=='succeeded' and attempts==1 and result['code']=='recorded' and result['claims']==2 and result['replay'] is False
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone()==(2,)
    after=scheduler.feed.backlog()
    assert after['feed_pending']==0 and after['jobs']=={'succeeded':1} and after['dead_lettered']==[]


def test_new_message_enqueues_new_window_and_missing_read_is_failed_not_retried(services,admin):
    s=services;fixture=s['fixture']
    scheduler=ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=0)
    worker=ClaimExtractionWorker(s['worker_pool'],s['worker'],s['signer'],registered_provider(s,s['ids']),timezone='Asia/Shanghai')
    scheduler.run_once();assert worker.run_once()=='succeeded'
    human=authenticate_browser_business(fixture['reader'].pool,fixture['base']['issued'].session,world='real')
    from nexloop_eios.conversation_messages import ConversationMessagePort
    ConversationMessagePort(fixture['reader'].pool,human,fixture['reader'].signer).accept_message(
        conversation_id=s['conversation_id'],idempotency_key='nx019-claim-job-message-a-late',body='付款页面还是报错。')
    second=scheduler.run_once()
    assert len(second)==1 and admin.execute('select normalized_input->>%s from runtime.jobs where job_id=%s',('through_sequence',second[0])).fetchone()==('4',)
    # Worker holds no READ on the new Message: authority denial is terminal 'failed'.
    assert worker.run_once()=='failed' and worker.run_once()=='idle'
    assert [row[0] for row in jobs(admin)]==['succeeded','failed'] and jobs(admin)[1][2]=={'code':'denied'}


def test_provider_outage_retries_then_dead_letters_with_visible_backlog(services,admin):
    s=services
    scheduler=ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=0,max_attempts=2)
    scheduler.run_once()
    worker=ClaimExtractionWorker(s['worker_pool'],s['worker'],s['signer'],DeterministicExtractionProvider({}),retry_base_seconds=0)
    assert worker.run_once()=='retry_wait'
    assert jobs(admin)[0][:2]==('retry_wait',1) and jobs(admin)[0][2]=={'code':'provider_not_registered'}
    assert worker.run_once()=='dead_lettered' and worker.run_once()=='idle'
    backlog=scheduler.feed.backlog()
    assert backlog['jobs']=={'dead_lettered':1}
    assert backlog['dead_lettered'][0]['code']=='provider_not_registered' and backlog['dead_lettered'][0]['attempts']==2
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone()==(0,)


def test_debounce_and_crash_between_accept_and_mark_is_idempotent(services,admin):
    s=services
    quiet=ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=3600)
    assert quiet.run_once()==[] and quiet.feed.backlog()['feed_pending']==3
    scheduler=ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=0)
    due=scheduler.feed.due(quiet_seconds=0,limit=10,window=50)[0]
    # Simulated crash: accepted but never marked.
    accepted=scheduler.queue.accept(source_id='nexloop.claim-extraction',event_id=due['conversation_id']+':3',
        payload={'conversation_id':due['conversation_id'],'through_sequence':3,'message_ids':due['message_ids']},max_attempts=3)
    assert scheduler.run_once()==[accepted['task_id']]
    assert admin.execute('select count(*) from runtime.jobs where queue=%s',(QUEUE,)).fetchone()==(1,)
    with pytest.raises(Exception):scheduler.feed.mark(conversation_id=due['conversation_id'],through_sequence=2,task_id=accepted['task_id'])


def test_concurrent_workers_extract_one_task_once(services,admin):
    s=services
    ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer'],quiet_seconds=0).run_once()
    provider=registered_provider(s,s['ids'])
    workers=[ClaimExtractionWorker(s['worker_pool'],authenticate_service(s['worker_pool'],s['worker_token'],world='real'),s['signer'],provider,timezone='Asia/Shanghai') for _ in range(3)]
    with ThreadPoolExecutor(3) as pool:outcomes=sorted(pool.map(lambda w:w.run_once(),workers))
    assert outcomes==['idle','idle','succeeded'],jobs(admin)
    assert admin.execute('select count(*) from ontology.nexloop_extraction_runs').fetchone()==(1,)


def test_feed_and_queue_tables_are_not_directly_accessible(services):
    s=services
    for pool in (s['api'],s['worker_pool']):
        with pool.connection() as db:
            import psycopg
            for statement in ('select count(*) from runtime.nexloop_claim_extraction_feed','delete from runtime.nexloop_claim_extraction_feed'):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute(statement)
                db.rollback()


def test_feed_requires_service_extract_authority(services,admin):
    s=services
    _,token=seed_multi_authority(admin,s['api'],[QUEUE_TARGET],identity_suffix='-claim-no-extract')
    session=authenticate_service(s['api'],token,world='real')
    with pytest.raises(ClaimExtractionDenied):ClaimFeed(s['api'],session,s['signer']).backlog()

"""Deployment plan B (0099): purpose-bound Message READ from the service manifest only.

The NX-019 scheduler/worker and the NX-020 matcher authenticate with credentials and
authority applied by the production service-grant entry point (trusted configuration,
``message_read_rules``). No per-Message, per-Conversation or per-Consumer grant exists
for them: every Message/Conversation READ is derived in SQL from the extraction task
the worker currently leases, or from a Claim the matcher still has to match. Admin
connections only build synthetic data, inject drift and probe. Synthetic data only.
"""
from datetime import UTC,datetime,timedelta
import copy,hashlib,json,secrets

import psycopg,pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import service_grants as G
from nexloop_eios.assembly import open_core
from eios.authz.grants import GrantSubjectKind
from nexloop_eios.authorization import WITNESS,authenticate_service,authority_request_scope
from nexloop_eios.claim_extraction_jobs import QUEUE,ClaimExtractionScheduler,ClaimExtractionWorker
from nexloop_eios.claim_store import ClaimExtractionDenied,ConversationClaimExtractor
from nexloop_eios.conversation_extraction import canonical
from nexloop_eios.durable_queue import PostgresDurableQueue
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import AuthoritySigner
from test_claim_extraction_jobs_pg import make_window,response
from test_service_grants_pg import MANIFEST,OWNER,TENANT,Private,private
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

DENIED=(ActionAuthorizationDenied,AuthorizationUnavailable,F.AuthorizationFactDenied,psycopg.errors.InsufficientPrivilege,PermissionError)
FIELDS=('accepted_at','actor','body','conversation_id','sequence')


class FixedProvider:
    """Synthetic extraction model: one frozen answer for the 3-message window."""
    provider='test';model_id='fixed-purpose-test'
    def __init__(self):self.calls=0
    def complete(self,system_prompt,user_payload):
        self.calls+=1;return canonical(response())


@pytest.fixture
def deployed(conversations,admin,pg,tmp_path):
    fixture=conversations
    conversation_id,ids=make_window(fixture,['付款页面一直报错。','解决后我再考虑续费。','好的'],'purpose')
    other_conversation,other_ids=make_window(fixture,['另一段对话。'],'purpose-other')
    admin.execute('alter role nexloop_configurator login')
    manifest=G.load(MANIFEST)
    tokens={p['credential_reference']:secrets.token_urlsafe(48) for p in manifest['principals']}
    key=secrets.token_bytes(32)
    paths=Private(dsn=private(tmp_path,'configurator-dsn',make_conninfo(pg,user='nexloop_configurator')),
        signing=private(tmp_path,'signing-key',key.hex()),secrets=private(tmp_path,'service-secrets',json.dumps(tokens)))
    def apply(body=manifest):
        return G.apply(body,TENANT,database_url_file=paths['dsn'],signing_key_file=paths['signing'],signing_key_id='nx048-purpose',
            service_secrets_file=paths['secrets'],credential_expires_at=datetime.now(UTC)+timedelta(hours=2),owner_restrictions=G.load_owner_restrictions(OWNER))
    report=apply()
    assert report['changed'] is True
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as worker_pool:
        pools={'nexloop_api':fixture['reader'].pool,'nexloop_domain_worker':worker_pool}
        def session(role):
            principal=next(p for p in manifest['principals'] if p['role']==role)
            pool=pools[principal['database_role']]
            return pool,authenticate_service(pool,tokens[principal['credential_reference']],world='real')
        yield Private(fixture=fixture,manifest=manifest,apply=apply,session=session,signer=AuthoritySigner('nx048-purpose',key),
            conversation_id=conversation_id,ids=ids,other_conversation=other_conversation,other_ids=other_ids,
            principal=lambda role:next(p for p in manifest['principals'] if p['role']==role)['principal_id'],
            report=report,pg=pg,dsn=paths['dsn'])


def enqueue(d):
    pool,scheduler=d['session']('claim_extraction_scheduler')
    tasks=ClaimExtractionScheduler(pool,scheduler,d['signer'],quiet_seconds=0).run_once()
    assert len(tasks)==2  # one window per conversation
    return tasks


def lease(d,conversation_id):
    """Lease tasks until the one for this conversation is held by the worker credential."""
    pool,worker=d['session']('claim_extraction_worker')
    queue=PostgresDurableQueue(pool,worker,d['signer'],queue=QUEUE);held=[]
    while True:
        item=queue.claim(lease_seconds=120)
        assert item is not None
        if item['payload']['conversation_id']==conversation_id:return pool,worker,queue,item,held
        held.append(item)


def reader(d,role):
    pool,s=d['session'](role)
    return AuthorizedObjectReader(pool,s,d['signer'])


def read(r,type_name,object_id,fields=()):
    with authority_request_scope():
        return r.get(type_name,object_id,fields=fields)['properties']


def refused(r,type_name,object_id,fields=()):
    with pytest.raises(DENIED):read(r,type_name,object_id,fields)


def grant_count(admin,principal):
    return admin.execute("""select count(*) from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[1]=%s
        and (entity_key[2] like 'eios:%%:Message/%%' or entity_key[2] like 'eios:%%:Conversation/%%' or entity_key[2] like 'eios:%%:Consumer/%%')""",(principal,)).fetchone()[0]


# --------------------------------------------------------------------------- positive end to end

def test_manifest_only_worker_extracts_and_matcher_reads_claim_evidence(deployed,admin):
    """Deployed path with manifest authority only: Message → feed → queue → worker → Claims → matcher reads."""
    d=deployed
    worker_principal,matcher_principal=d['principal']('claim_extraction_worker'),d['principal']('claim_matcher')
    rules=dict(admin.execute("select entity_key[2],payload from authz.nexloop_authority_facts where fact_kind='message_purpose_rule' order by 1").fetchall())
    assert rules['claim_extraction']['principal_id']==worker_principal and rules['claim_extraction']['fields']==list(FIELDS)
    assert rules['claim_matching']['principal_id']==matcher_principal and rules['claim_matching']['fields']==['body','conversation_id']
    revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(TENANT,)).fetchone()[0]
    enqueue(d)
    pool,worker=d['session']('claim_extraction_worker');provider=FixedProvider()
    statuses=[ClaimExtractionWorker(pool,worker,d['signer'],provider,timezone='Asia/Shanghai').run_once() for _ in range(2)]
    # The single-message window yields no valid Claim for the frozen answer: only the 3-message task succeeds.
    assert 'succeeded' in statuses,statuses
    claims=admin.execute('select claim_id,source_message_id,resolution_state from ontology.nexloop_claims where conversation_id=%s order by source_sequence',
        (d['conversation_id'],)).fetchall()
    assert [c[1] for c in claims]==d['ids'][:2] and {c[2] for c in claims}=={'unresolved'}
    # No per-object authority was written for either service, and no epoch advance for Messages.
    assert grant_count(admin,worker_principal)==0 and grant_count(admin,matcher_principal)==0
    assert admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(TENANT,)).fetchone()[0]==revision
    # Matcher: the Claims of the Conversation and the cited evidence Message body; nothing else.
    pool,matcher=d['session']('claim_matcher')
    with authority_request_scope():
        view=ConversationClaimExtractor(pool,matcher,d['signer'],None).read(conversation_id=d['conversation_id'])
    assert sorted(c['claim_id'] for c in view['statements'])==sorted(c[0] for c in claims)
    m=reader(d,'claim_matcher')
    assert read(m,'Message',d['ids'][0],('body',))=={'body':'付款页面一直报错。'}
    refused(m,'Message',d['ids'][2],('body',))          # not cited by any Claim
    refused(m,'Message',d['ids'][0],('actor',))         # field outside the matcher's rule
    refused(m,'Conversation',d['conversation_id'],('consumer_id',))  # Conversation properties only while extracting
    refused(m,'Conversation',d['other_conversation'])  # no Claim to match there
    # Outside a leased task the worker reads nothing at all.
    w=reader(d,'claim_extraction_worker')
    refused(w,'Message',d['ids'][0],('body',))
    refused(w,'Conversation',d['conversation_id'])


# --------------------------------------------------------------------------- extraction lease

def test_worker_reads_exactly_the_leased_task_and_nothing_after_it_ends(deployed,admin):
    d=deployed;enqueue(d)
    pool,worker,queue,item,_=lease(d,d['conversation_id'])
    w=AuthorizedObjectReader(pool,worker,d['signer'])
    assert read(w,'Message',d['ids'][1],FIELDS)['body']=='解决后我再考虑续费。'
    assert read(w,'Conversation',d['conversation_id'],('consumer_id','owner_principal'))['consumer_id']==d['fixture']['consumer']
    refused(w,'Message',d['other_ids'][0],('body',))  # another conversation, not in this task
    # A proof signed under the lease is refused by SQL once the task is finished.
    with authority_request_scope():
        stale=w.authorities([(ResourceType.OBJECT,'Message/'+d['ids'][0]),(ResourceType.PROPERTY,'Message/'+d['ids'][0]+'/body')])
    assert {c['derivation'] for c in stale}=={'purpose-message-v1'} and stale[0]['derivation_basis']['job_id']==item['task_id']
    queue.finish(task_id=item['task_id'],fence=item['fence'],status='succeeded',result={'code':'synthetic'})
    class Replay(AuthorizedObjectReader):
        def authorities(self,targets,operation=Operation.READ):return [dict(c) for c in stale]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        Replay(pool,worker,d['signer']).get('Message',d['ids'][0],fields=('body',))
    refused(w,'Message',d['ids'][0],('body',))


def test_tampered_basis_and_other_credential_are_refused(deployed,admin):
    d=deployed;enqueue(d)
    pool,worker,queue,item,_=lease(d,d['conversation_id'])
    w=AuthorizedObjectReader(pool,worker,d['signer'])
    with authority_request_scope():
        good=w.authorities([(ResourceType.OBJECT,'Message/'+d['ids'][0])])
    for change in ({'fence':item['fence']+1},{'job_id':'task_00000000-0000-4000-8000-000000000000'},{'purpose':'claim_matching'},
                   {'conversation_id':d['other_conversation']}):
        forged=[{**good[0],'derivation_basis':{**good[0]['derivation_basis'],**change}}]
        class Forged(AuthorizedObjectReader):
            def authorities(self,targets,operation=Operation.READ):return [dict(c) for c in forged]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):Forged(pool,worker,d['signer']).get('Message',d['ids'][0])
    # Another service credential never rides on this credential's lease.
    refused(reader(d,'claim_matcher'),'Message',d['ids'][0],('body',))
    refused(reader(d,'claim_extraction_scheduler'),'Message',d['ids'][0],('body',))
    assert read(w,'Message',d['ids'][0],('body',))  # the leasing credential still reads


# --------------------------------------------------------------------------- revocation / deletion / precedence

def test_rule_removal_deactivates_and_denies_immediately(deployed,admin):
    d=deployed;enqueue(d)
    pool,worker,queue,item,_=lease(d,d['conversation_id'])
    assert read(AuthorizedObjectReader(pool,worker,d['signer']),'Message',d['ids'][0],('body',))
    reduced=copy.deepcopy(d['manifest']);reduced.pop('message_read_rules')
    report=d['apply'](G.validate(reduced))
    assert {'kind':'message_purpose_rule','key':[d['principal']('claim_extraction_worker'),'claim_extraction']} in report['facts_written']
    assert admin.execute("select bool_or((payload->'active')::boolean) from authz.nexloop_authority_facts where fact_kind='message_purpose_rule'").fetchone()==(False,)
    # The task lease is still valid; a fresh session after the publication reads nothing.
    _,fresh=d['session']('claim_extraction_worker')
    refused(AuthorizedObjectReader(pool,fresh,d['signer']),'Message',d['ids'][0],('body',))
    # (This fixture also seeds non-manifest test services, so doctor lists extra grants; the rules themselves are in sync.)
    report=G.doctor(G.validate(reduced),TENANT,database_url_file=d['dsn'])
    assert report['missing_grants']==[] and not [f for f in report['fact_drift'] if f['kind']=='message_purpose_rule']


def test_purpose_action_revoked_or_configured_empty_grant_denies(deployed,admin):
    d=deployed;enqueue(d)
    pool,worker,queue,item,_=lease(d,d['conversation_id'])
    w=AuthorizedObjectReader(pool,worker,d['signer'])
    principal=d['principal']('claim_extraction_worker')
    # Configured authority wins: an explicit empty grant on the Message is never widened by derivation.
    empty=F.GrantFacts(tenant_id=TENANT,repository_witness=WITNESS,subject_kind=GrantSubjectKind.SERVICE,principal_id=principal,
        grants=(),valid_until=None,complete=True,next_cursor=None,revision=1).model_dump(mode='json')
    admin.execute("insert into authz.nexloop_authority_facts values(%s,'grants',%s,%s)",(TENANT,[principal,'eios:object:Message/'+d['ids'][0]],Jsonb(empty)))
    _,worker=d['session']('claim_extraction_worker');w=AuthorizedObjectReader(pool,worker,d['signer'])
    refused(w,'Message',d['ids'][0],('body',))
    assert read(w,'Message',d['ids'][1],('body',))
    # Revoking the purpose Action (empty grant set, as the manifest apply writes) ends every purpose READ.
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]') where fact_kind='grants' and entity_key=%s",
        ([principal,'eios:action:nexloop.claim.extract:1'],))
    _,worker=d['session']('claim_extraction_worker')
    refused(AuthorizedObjectReader(pool,worker,d['signer']),'Message',d['ids'][1],('body',))


def test_deleted_message_and_resolved_claim_are_denied(deployed,admin):
    d=deployed;enqueue(d)
    pool,worker=d['session']('claim_extraction_worker')
    for _ in range(2):ClaimExtractionWorker(pool,worker,d['signer'],FixedProvider(),timezone='Asia/Shanghai').run_once()
    m=reader(d,'claim_matcher')
    assert read(m,'Message',d['ids'][1],('body',))
    # Resolving a Claim ends READ on its evidence; the Conversation stays readable while another Claim is pending.
    admin.execute("update ontology.nexloop_claims set resolution_state='resolved' where source_message_id=%s",(d['ids'][1],))
    refused(m,'Message',d['ids'][1],('body',))
    assert read(m,'Message',d['ids'][0],('body',))
    admin.execute("update ontology.nexloop_claims set resolution_state='resolved' where conversation_id=%s",(d['conversation_id'],))
    refused(m,'Conversation',d['conversation_id'])
    admin.execute("update ontology.nexloop_claims set resolution_state='needs_resolution' where source_message_id=%s",(d['ids'][0],))
    assert read(m,'Message',d['ids'][0],('body',))
    # Deleting the Message ends READ immediately, even though its Claim is still pending.
    admin.execute("delete from ontology.objects where type_name='Message' and object_id=%s",(d['ids'][0],))
    refused(m,'Message',d['ids'][0],('body',))


def test_message_deleted_during_lease_is_denied_and_the_task_fails_closed(deployed,admin):
    d=deployed;enqueue(d)
    pool,worker,queue,item,held=lease(d,d['conversation_id'])
    admin.execute("delete from ontology.objects where type_name='Message' and object_id=%s",(d['ids'][2],))
    w=AuthorizedObjectReader(pool,worker,d['signer'])
    refused(w,'Message',d['ids'][2],('body',))
    assert read(w,'Message',d['ids'][0],('body',))
    with pytest.raises(ClaimExtractionDenied):
        ConversationClaimExtractor(pool,worker,d['signer'],FixedProvider()).extract(conversation_id=d['conversation_id'],message_ids=d['ids'])
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone()==(0,)


# --------------------------------------------------------------------------- configuration

def test_manifest_rule_validation():
    base=G.load(MANIFEST)
    def bad(change,reason):
        body=copy.deepcopy(base);change(body)
        with pytest.raises(G.ServiceGrantsRejected,match=reason):G.validate(body)
    bad(lambda b:b['message_read_rules'][0].update(read_purpose='context_assembly'),'message_read_rule_purpose')
    bad(lambda b:b['message_read_rules'][0].update(fields=['actor']),'message_read_rule_shape')
    bad(lambda b:b['message_read_rules'][0].update(fields=['body','actor']),'message_read_rule_shape')
    bad(lambda b:b['message_read_rules'][0].update(valid_until='2027-10-09T00:00:00'),'message_read_rule_shape')
    bad(lambda b:b['message_read_rules'].append(dict(b['message_read_rules'][0])),'message_read_rule_duplicate')
    bad(lambda b:b['message_read_rules'][1].update(principal='recall_indexer'),'requires_purpose_grant')
    bad(lambda b:b['message_read_rules'][0].update(extra=True),'message_read_rule_shape')
    bad(lambda b:b['message_read_rules'][0].update(purpose='turn on REAL_DISPATCH_ENABLED'),'excluded_by_policy')


def test_apply_is_idempotent_doctor_in_sync_and_sql_refuses_malformed_rules(deployed,admin):
    from test_service_grants_pg import _raw_configure
    d=deployed
    assert d['apply']()['changed'] is False
    principal=d['principal']('claim_matcher')
    rule={'tenant_id':TENANT,'principal_id':principal,'purpose':'claim_matching','type_name':'Message','fields':['body'],'active':True,
        'valid_until':'2027-01-01T00:00:00+00:00'}
    raw=Private(pg=d['pg'],signer=d['signer'])
    for payload,key in ((rule|{'extra':1},[principal,'claim_matching']),(rule|{'purpose':'anything'},[principal,'anything']),
                        (rule|{'fields':['actor']},[principal,'claim_matching']),(rule|{'fields':['sequence','body']},[principal,'claim_matching']),
                        (rule|{'type_name':'Conversation'},[principal,'claim_matching']),(rule,[principal,'claim_extraction']),
                        (rule|{'principal_id':'synthetic-a-unknown-principal'},['synthetic-a-unknown-principal','claim_matching'])):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _raw_configure(raw,admin,[{'kind':'message_purpose_rule','key':key,'payload':payload}])
    assert _raw_configure(raw,admin,[{'kind':'message_purpose_rule','key':[principal,'claim_matching'],'payload':rule}])['configured'] is True
    # The exact shape is accepted; doctor then reports the drift from the manifest and apply restores it.
    report=G.doctor(d['manifest'],TENANT,database_url_file=d['dsn'])
    assert report['in_sync'] is False and {'kind':'message_purpose_rule','key':[principal,'claim_matching']} in report['fact_drift']
    assert {'kind':'message_purpose_rule','key':[principal,'claim_matching']} in d['apply']()['facts_written']
    report=G.doctor(d['manifest'],TENANT,database_url_file=d['dsn'])
    assert report['missing_grants']==[] and report['fact_drift']==[]

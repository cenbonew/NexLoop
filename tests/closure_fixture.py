"""NX-025 closure fixture: one Consumer through the knowledge loop on clean catalog PostgreSQL.

Composes the existing fixtures without new business write paths:
- real browser Human, governed Conversation / Message (test_conversation_messages.conversations) on the
  NX-020 Consumer schema (published_action 'closure');
- NX-019 background extraction (scheduler service → durable queue → worker, deterministic provider);
- NX-021 recall index (configuration definitions + the recall-instance feed worker);
- NX-020 / NX-045 claim-match worker with candidate glue (published merge configuration);
- NX-044 human review decisions and the service reflow.
Every worker is driven by an explicit run_once() (no resident loop), and every service re-authenticates
from its token per step (authority changes make earlier sessions stale, as in the deployed processes).
Synthetic data only; admin seeds fixtures and probes.
"""
import hashlib

import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.candidate_merge import CandidateGluer
from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler,ClaimExtractionWorker
from nexloop_eios.claim_match_worker import ClaimMatchWorker
from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.claim_store import ConversationClaimExtractor
from nexloop_eios.conversation_extraction import DeterministicExtractionProvider,build_user_payload
from nexloop_eios.conversation_messages import ConversationMessagePort
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallIndexer
from nexloop_eios.recall_index_worker import RecallInstanceIndexWorker
from nexloop_eios.review_actions import ReviewDecisionPort,ReviewReflowWorker
from multi_authority_fixture import seed_multi_authority
from test_candidate_merge_pg import publish_config
from test_claim_extraction_jobs_pg import EXTRACT_TARGET,QUEUE_TARGET
from test_claim_matching_pg import CONSUMER_PROPS,consumer_schema,publish_action
from test_claim_store_pg import source_targets
from test_review_http import REVIEW,grant_human
from test_browser_session_reads import authenticate,create
from test_work_feeds_pg import INDEX_FEED,MATCH_FEED,drain

TENANT='synthetic-a'
READ,EDIT,EXECUTE=Operation.READ,Operation.EDIT,Operation.EXECUTE


class Closure(dict):
    """One synthetic Consumer's knowledge loop; services are re-authenticated per step."""
    def __repr__(self):return '<synthetic NX-025 closure configuration>'
    __str__=__repr__

    def service(self,pool,token):return authenticate_service(pool,token,world='real')

    def seed(self,targets,suffix,*,pool=None):
        _,token=seed_multi_authority(self['admin'],pool or self['worker'],list(targets),identity_suffix=suffix,tenant=TENANT)
        return token

    # -- conversation ---------------------------------------------------------------------------
    def human_port(self):
        human=authenticate_browser_business(self['api'],self['base']['issued'].session,world='real')
        return ConversationMessagePort(self['api'],human,self['signer'])

    def conversation(self,key,bodies):
        port=self.human_port();created=port.create_conversation(idempotency_key='nx025-conversation-'+key)
        ids=[port.accept_message(conversation_id=created['id'],idempotency_key=f'nx025-message-{key}-{i}',body=b)['message']['id'] for i,b in enumerate(bodies)]
        return created['id'],ids

    def say(self,conversation_id,key,body):
        return self.human_port().accept_message(conversation_id=conversation_id,idempotency_key='nx025-message-'+key,body=body)['message']['id']

    # -- extraction ------------------------------------------------------------------------------
    def extract(self,conversation_id,message_ids,body,*,suffix):
        """Scheduler window → durable queue → worker (deterministic provider keyed by the exact window input)."""
        token=self.seed([QUEUE_TARGET]+source_targets(conversation_id,message_ids),'-nx025-extractor'+suffix)
        scheduler=ClaimExtractionScheduler(self['api'],self.service(self['api'],self['scheduler_token']),self['signer'],quiet_seconds=0)
        tasks=scheduler.run_once()
        worker=self.service(self['worker'],token)
        context,messages=ConversationClaimExtractor(self['worker'],worker,self['signer'],None,timezone='Asia/Shanghai').load_window(conversation_id,message_ids)
        provider=DeterministicExtractionProvider({hashlib.sha256(build_user_payload(messages,context).encode()).hexdigest():body})
        results=[ClaimExtractionWorker(self['worker'],worker,self['signer'],provider,timezone='Asia/Shanghai').run_once() for _ in tasks]
        return tasks,results

    def claims(self,conversation_id):
        rows=self['admin'].execute('select quote,claim_id,source_message_id,epistemic_kind,resolution_state from ontology.nexloop_claims where conversation_id=%s order by quote',(conversation_id,)).fetchall()
        return {r[0]:r for r in rows}

    # -- recall index ---------------------------------------------------------------------------
    def reindex(self):
        drain(lambda:RecallInstanceIndexWorker(self['worker'],self.service(self['worker'],self['indexer_token']),self['signer'],
            types={'Consumer':None},provider=self['provider'],expected_dimension=64))

    # -- matching --------------------------------------------------------------------------------
    def matcher_targets(self,conversations,*,edit_version=1,extra_properties=()):
        consumer=self['consumer'];props=tuple(CONSUMER_PROPS)+tuple(extra_properties)
        targets=[MATCH_FEED,('eios:action:nexloop.claim.match:1',ResourceType.ACTION,EXECUTE),(f'eios:action:Consumer.edit:{edit_version}',ResourceType.ACTION,EXECUTE),
            ('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ),('eios:object:Consumer/'+consumer,ResourceType.OBJECT,READ),('eios:object:Consumer/'+consumer,ResourceType.OBJECT,EDIT)]
        targets+=[('eios:object:Conversation/'+c,ResourceType.OBJECT,READ) for c in conversations]
        targets+=[(f'eios:property:Consumer/{p}',ResourceType.PROPERTY,READ) for p in props]
        targets+=[(f'eios:property:Consumer/{consumer}/{p}',ResourceType.PROPERTY,op) for p in props for op in (READ,EDIT)]
        return targets

    def match_worker(self,decisions,conversations,*,suffix,edit_version=1,extra_properties=(),token=None):
        """A claim-match worker; pass the token of an earlier one to act again as that principal (re-authenticated)."""
        token=token or self.seed(self.matcher_targets(conversations,edit_version=edit_version,extra_properties=extra_properties),'-nx025-matcher'+suffix)
        session=self.service(self['worker'],token)
        recall=OntologyRecall(self['worker'],session,authorizer=EiosRecallAuthorizer(self['worker'],session),provider=self['provider'],expected_dimension=64)
        provider=ScriptedMatchProvider(decisions)
        m=ClaimMatcher(self['worker'],session,self['signer'],recall=recall,provider=provider,
            configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',edit_version)},create_actions={}))
        gluer=CandidateGluer(self['worker'],session,self['signer'],indexer=self['indexer_for'](session),matcher=m,provider=self['provider'])
        m.token=token
        return ClaimMatchWorker(self['worker'],session,self['signer'],matcher=m,gluer=gluer),m,provider

    # -- review ----------------------------------------------------------------------------------
    def reviewer(self):
        """A fresh, current Human reviewer session (the same synthetic browser identity, granted ontology.schema.review)."""
        grant_human(self['admin'],self['uow'],self['identity'],REVIEW)
        issued=create(self['uow'],self['identity'],authenticate(self['uow'],self['identity']).evidence)
        return ReviewDecisionPort(self['api'],authenticate_browser_business(self['api'],issued.session,world='real'),self['signer'])

    def reflow(self,conversations,*,suffix,edit_version=1,extra_properties=()):
        _,m,_=self.match_worker({},conversations,suffix=suffix,edit_version=edit_version,extra_properties=extra_properties)
        gluer=CandidateGluer(self['worker'],m.session,self['signer'],indexer=self['indexer_for'](m.session),matcher=m,provider=self['provider'])
        return ReviewReflowWorker(self['worker'],m.session,self['signer'],gluer=gluer)

    # -- probes ----------------------------------------------------------------------------------
    def consumer_row(self):
        return self['admin'].execute("select properties,nexloop_revision from ontology.objects where tenant_id=%s and type_name='Consumer' and object_id=%s",
            (TENANT,self['consumer'])).fetchone()

    def counts(self):
        q=lambda sql:self['admin'].execute(sql).fetchone()[0]
        return {'claims':q('select count(*) from ontology.nexloop_claims'),'proposals':q('select count(*) from ontology.nexloop_mutation_proposals'),
            'candidates':q('select count(*) from ontology.nexloop_candidate_definitions'),
            'edits':q("select count(*) from runtime.nexloop_action_claims where action_name like 'Consumer.edit%'"),'revision':self.consumer_row()[1]}


@pytest.fixture
def closure(conversations,identity,uow,admin):
    """Requires @pytest.mark.parametrize('published_action',['closure'],indirect=True) on the test."""
    fixture=conversations;base=fixture['base']
    consumer=fixture['consumer'];api=fixture['reader'].pool;signer=fixture['reader'].signer
    stored=admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1",(TENANT,)).fetchone()[0]
    assert [p['property_name'] for p in stored['properties']]==list(CONSUMER_PROPS)
    publish_action(admin,TENANT,'Consumer.edit',consumer_schema(),'ontology.object.edit')
    with open_core(make_conninfo(fixture['pg'],user='nexloop_domain_worker')) as worker:
        provider=DeterministicTestEmbeddingProvider(64)
        _,scheduler_token=seed_multi_authority(admin,api,[EXTRACT_TARGET,QUEUE_TARGET],identity_suffix='-nx025-scheduler',tenant=TENANT)
        _,indexer_token=seed_multi_authority(admin,worker,[INDEX_FEED,('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ)],identity_suffix='-nx025-indexer',tenant=TENANT)
        c=Closure(admin=admin,pg=fixture['pg'],tenant=TENANT,api=api,worker=worker,signer=signer,consumer=consumer,base=base,identity=identity,uow=uow,
            provider=provider,scheduler_token=scheduler_token,indexer_token=indexer_token)
        c['indexer_for']=lambda session:RecallIndexer(worker,c.service(worker,indexer_token),provider=provider,expected_dimension=64)
        # Definitions are indexed by the configuration identity's indexer; instances by the feed worker.
        definitions=c['indexer_for'](None);definitions.activate_profile();definitions.index_object_type('Consumer')
        publish_config(c)
        yield c

"""NX-023-A actual PostgreSQL: governed strategy publication/read, Context storage
invariants, semantic read-only adapter and open-work projection. Synthetic only; the
admin connection seeds authority/definitions and probes invariants, nothing else."""
import copy
import json
import secrets
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine import strategy as S
from nexloop_eios.context_engine.authority import ContextDenied,signed_read
from nexloop_eios.context_engine.semantic import OpenWorkReader,SemanticAdapter
from nexloop_eios.context_engine.sections import open_work_items
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import AuthoritySigner
from goal_fixture import TENANT,authenticate_human,seed_agent_author,seed_human_owner,seed_service
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow
from test_recall_pg import recall_db,recall_for,A

ROOT=Path(__file__).resolve().parents[1]
BUILTINS=S.load_builtins(ROOT/'deploy/configuration/context-strategies.v1.json')


class Env(dict):
    def __getattr__(self,name):return self[name]


@pytest.fixture
def strategies(identity,uow,published_action,admin):
    reader,base,capability=published_action
    for name,cap in ((S.PUBLISH_ACTION,S.PUBLISH_CAPABILITY),):
        binding=base.capability_binding.model_copy(update={'capability_name':cap})
        definition=base.model_copy(update={'stable_name':name,'capability_binding':binding,'contract_digest':None})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (TENANT,'real',f'eios:action:{name}:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_copy(update={'capability_name':cap}).model_dump(mode='json'))))
    browser=seed_human_owner(admin,reader.pool,[S.PUBLISH_ACTION],identity,uow)
    agent=seed_agent_author(admin,reader.pool,[S.PUBLISH_ACTION],suffix='-context-agent')
    service=seed_service(admin,reader.pool,[S.PUBLISH_ACTION],suffix='-context-service')
    assembler=seed_service(admin,reader.pool,[S.ASSEMBLE_ACTION],suffix='-context-assembler')
    e=Env(reader=reader,signer=reader.signer)
    e.human=authenticate_human(reader.pool,browser)
    e.agent=authenticate_service(reader.pool,agent,world='real');e.service=authenticate_service(reader.pool,service,world='real')
    e.assembler=authenticate_service(reader.pool,assembler,world='real')
    return e


def publisher(e,session):return S.StrategyPublisher(e.reader.pool,session,e.signer)


def test_human_owner_publishes_builtins_and_assembler_reads_current(strategies,admin):
    e=strategies
    for item in BUILTINS:
        result=publisher(e,e.human).publish(item,request_id='publish-'+item['strategy_id']+'-v1')
        assert result['strategy_ref']==S.strategy_ref(item)
    registry=S.StrategyRegistry(e.reader.pool,e.assembler,e.signer)
    current=registry.get('ontology_hybrid')
    assert current.ref=='context-strategy:ontology_hybrid@1' and current.definition==BUILTINS[1]
    v2=copy.deepcopy(BUILTINS[1]);v2['version']=2;v2['sections']['evidence']['max_items']=30
    publisher(e,e.human).publish(v2,request_id='publish-ontology-hybrid-v2')
    assert registry.get('ontology_hybrid').ref.endswith('@2') and registry.get('ontology_hybrid',1).definition==BUILTINS[1]
    assert registry.get('missing_strategy') is None
    # Replaying the same request returns the original outcome; nothing new is written.
    assert publisher(e,e.human).publish(v2,request_id='publish-ontology-hybrid-v2')['replayed'] is True
    assert admin.execute('select count(*) from control.nexloop_context_strategies').fetchone()==(3,)
    # Versions are dense and immutable.
    v4=copy.deepcopy(v2);v4['version']=4
    with pytest.raises(psycopg.errors.SerializationFailure):publisher(e,e.human).publish(v4,request_id='publish-ontology-hybrid-v4')
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
            admin.execute("update control.nexloop_context_strategies set definition='{}'::jsonb")
        with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
            admin.execute('delete from control.nexloop_context_strategies')


@pytest.mark.parametrize('who',['service','agent'])
def test_only_human_owner_may_publish_even_with_grant(strategies,admin,who):
    e=strategies
    with pytest.raises((psycopg.errors.InsufficientPrivilege,ActionAuthorizationDenied)):
        publisher(e,getattr(e,who)).publish(BUILTINS[0],request_id='publish-by-'+who)
    assert admin.execute('select count(*) from control.nexloop_context_strategies').fetchone()==(0,)


def test_sql_rejects_real_world_hypotheses_even_if_client_validation_is_bypassed(strategies,admin,monkeypatch):
    e=strategies;bad=copy.deepcopy(BUILTINS[0]);bad['include_hypotheses']=True
    monkeypatch.setattr(S,'validate',lambda definition,world:definition)
    with pytest.raises(psycopg.errors.InvalidParameterValue):publisher(e,e.human).publish(bad,request_id='publish-hypotheses-real')
    assert admin.execute('select count(*) from control.nexloop_context_strategies').fetchone()==(0,)


def test_strategy_read_requires_assemble_authority_and_tables_are_private(strategies,admin):
    e=strategies;publisher(e,e.human).publish(BUILTINS[0],request_id='publish-recent-v1')
    with pytest.raises(ContextDenied):S.StrategyRegistry(e.reader.pool,e.service,e.signer).get('recent_plus_required')
    with e.reader.pool.connection() as db:
        for table in ('control.nexloop_context_strategies','runtime.nexloop_context_packs','runtime.nexloop_context_sources','runtime.nexloop_model_requests'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select count(*) from '+table)
            db.rollback()
    # A forged strategy read signed with a wrong key is refused before any row is read.
    forged=AuthoritySigner('forged-context-key',secrets.token_bytes(32))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        signed_read(e.reader.pool,e.assembler,forged,'strategy',{'strategy_id':'recent_plus_required'})


def test_storage_invariants_dense_call_sequence_single_result_and_formal_zone(admin,pg):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    run=str(uuid.uuid4());context=str(uuid.uuid4());h='a'*64
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id','synthetic-a',true)")
        admin.execute("insert into runtime.nexloop_context_packs values('synthetic-a','real',%s,%s,'nexloop.context-pack.v6','context-strategy:recent_plus_required@1',%s,%s,'{}','[]',default)",(context,run,h,'b'*32))
        def source(ordinal,section,kind):
            admin.execute("insert into runtime.nexloop_context_sources values('synthetic-a','real',%s,%s,%s,'claim:x','1',%s,%s,%s,'included',null)",(context,ordinal,section,h,kind,'decision:'+h))
        source(0,'evidence','user_statement');source(1,'evidence','hypothesis');source(2,'consumer_state','formal_object')
        for ordinal,(section,kind) in enumerate((('consumer_state','user_statement'),('constraints','hypothesis'),('open_work','conversation'),('consumer_state','hypothesis')),start=10):
            with pytest.raises(psycopg.errors.CheckViolation),admin.transaction():source(ordinal,section,kind)
        def request(seq):
            admin.execute("insert into runtime.nexloop_model_requests(tenant_id,world,run_id,call_sequence,context_id,model_provider,model_id,tool_manifest_digest,request_digest,prompt_artifact_id,input_token_budget,output_token_budget,settings_digest) values('synthetic-a','real',%s,%s,%s,'test','deterministic-test',%s,%s,%s,16000,4000,%s)",(run,seq,context,h,h,'c'*32,h))
        request(1)
        for bad in (1,3):
            with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():request(bad)
        request(2)
        admin.execute("update runtime.nexloop_model_requests set result_status='succeeded',completed_at=now(),response_digest=%s where run_id=%s and call_sequence=1",(h,run))
        for statement in ("update runtime.nexloop_model_requests set result_status='failed' where run_id=%s and call_sequence=1",
                          "update runtime.nexloop_model_requests set request_digest=repeat('d',64),result_status='failed',completed_at=now() where run_id=%s and call_sequence=2",
                          'delete from runtime.nexloop_model_requests where run_id=%s','update runtime.nexloop_context_packs set pack_digest=pack_digest where run_id=%s'):
            with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():admin.execute(statement,(run,))
        admin.execute("select set_config('eios.tenant_id','synthetic-b',true)")
        assert admin.execute('select count(*) from runtime.nexloop_model_requests').fetchone()==(0,)


@pytest.fixture
def semantic(recall_db):
    f=recall_db;signer=AuthoritySigner('context-semantic-key',secrets.token_bytes(32))
    f['admin'].execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(signer.key_id,signer.material))
    session=authenticate_service(f['worker'],f['reader_token'],world='real')
    return f,SemanticAdapter(f['worker'],session,signer,recall_for(f,session)),session,signer


def test_semantic_browse_returns_gated_definitions_with_version_and_coverage(semantic):
    f,adapter,session,signer=semantic
    out=adapter.browse('关注防水功能')
    top=out['items'][0]
    assert top['ref']=='eios:property:Consumer/waterproof_concern' and top['display_name']=='关注防水' and top['kind']=='property'
    assert top['value_type']=='boolean' and top['group']=='needs_intent' and top['schema_version']==1 and top['source_refs']==[top['ref']]
    assert out['coverage']==1.0 and out['version'].startswith('semantic:') and out['version'].endswith('@nx021-recall-v1@nexloop-test-ngram-v1@64')
    assert all(r.startswith('decision:') for r in out['access_decision_refs'])
    vocab=adapter.browse('预算两千元以内',kind='vocabulary_value')['items']
    assert vocab and vocab[0]['kind']=='vocabulary_value' and '两千元以内' in vocab[0]['closed_vocabulary']
    typed=adapter.browse('消费者',kind='object_type')['items'][0]
    assert typed['ref']=='eios:object_type:Consumer' and 'monthly_income' not in typed['readable_properties'] and 'nickname' not in typed['readable_properties']
    # Sensitive property (no READ) is invisible through every route.
    assert all('monthly_income' not in i['ref'] for i in adapter.browse('月收入')['items'])


def test_semantic_resolve_reports_ambiguity_without_choosing(semantic):
    f,adapter,session,signer=semantic
    out=adapter.resolve(['关注防水','完全无关的火星词汇xyz'],type_hint='Consumer')
    assert out['resolutions'][0]['status'] in ('resolved','ambiguous') and out['resolutions'][0]['candidates']
    assert out['resolutions'][1]['status'] in ('unresolved','ambiguous','resolved')
    assert 0<=out['coverage']<=1 and out['version'].startswith('semantic:')
    tight=SemanticAdapter(adapter.pool,session,signer,adapter.recall,config=type(adapter.config)(ambiguity_margin=1.0))
    first=tight.resolve(['防水'])['resolutions'][0]
    if len(first['candidates'])>1:assert first['status']=='ambiguous' and tight.resolve(['防水'])['ambiguity'] is True


def test_definition_read_requires_gate_proofs_in_sql(semantic):
    f,adapter,session,signer=semantic
    from nexloop_eios.context_engine.authority import read_proof
    type_proof=read_proof(f['worker'],session,ResourceType.OBJECT_TYPE,'Consumer')
    # Caller asks for a property it has no proof for (sensitive): SQL refuses, it does not filter silently.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        signed_read(f['worker'],session,signer,'definitions',{'refs':['eios:property:Consumer/monthly_income']},proofs=[type_proof])
    with pytest.raises(ContextDenied):read_proof(f['worker'],session,ResourceType.PROPERTY,'Consumer/monthly_income')
    # Another tenant's reader cannot see tenant A definitions through tenant A refs.
    other,_=seed_multi_authority(f['admin'],f['worker'],[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)],identity_suffix='-ctx-b',tenant='synthetic-b')
    rows=signed_read(f['worker'],other,signer,'definitions',{'refs':['eios:object_type:Consumer']},proofs=[read_proof(f['worker'],other,ResourceType.OBJECT_TYPE,'Consumer')])['items']
    assert rows[0]['display_name']=='客户'  # tenant B's own schema, never tenant A's


def test_open_work_projection_is_consumer_read_gated_and_pins_unconfirmed(assembled_message,admin,tmp_path):
    from test_outbound_messages_pg import chain
    f=assembled_message
    with chain(f,admin,tmp_path) as c:
        consumer=f['recipe']['consumer_id']
        source=c['backend'].authenticate(c['tokens']['assembly-source'],world='real')
        work,decision=OpenWorkReader(source._backend._pool,source._session,source._backend._signer).read(consumer)
        intent=c['receipts'][0]['intent_id']
        assert [i['intent_id'] for i in work['intents']]==[intent] and work['intents'][0]['state']=='accepted'
        assert [(o['intent_id'],o['delivery_state']) for o in work['outbound']]==[(intent,'persisted')]
        items=open_work_items(work,decision=decision)
        assert {i.ref for i in items}=={'nexloop:intent:'+intent,'nexloop:outbound:'+intent} and all(i.pinned for i in items)
        # The outbound recorder has no Consumer READ: no projection at all.
        with pytest.raises(ContextDenied):OpenWorkReader(c['recorder'].pool,c['recorder'].session,c['recorder'].signer).read(consumer)


from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: E402,F401

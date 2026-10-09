"""NX-020 four-layer matching and governed automatic apply on clean catalog PostgreSQL.

Synthetic data only. Fixture rows (schemas, Action definitions, the existing
Consumer/Product objects, the Conversation and its Claims in the NX-019 table
shape) are written by the bootstrap identity as setup. Matching, recall,
proposal recording and every formal write use the restricted domain-worker
role, a service credential with real EIOS grants and the existing governed
object create/edit Actions. Model decisions are scripted (CI); the real model
is only in tests/verification_real_matching.py (explicit opt-in).
"""
from datetime import UTC,datetime,timedelta
import hashlib
import json
from pathlib import Path
import secrets
import uuid

import psycopg
import pytest
from jsonschema import Draft202012Validator,FormatChecker
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyGroup,PropertyTypeDescriptor,PropertyTypeKind,PropertyValueType
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.assembly import open_core
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.claim_matching import MATCHER_VERSION,ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.embedding_provider import DeterministicTestEmbeddingProvider
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall,RecallIndexer
from multi_authority_fixture import seed_multi_authority
from test_postgres_action_claims import governance_inputs

ROOT=Path(__file__).resolve().parents[1]
CHECK=FormatChecker()
MUTATION=Draft202012Validator(json.loads((ROOT/'packages/contracts/ontology-mutation.schema.json').read_text()),format_checker=CHECK)
CANDIDATE=Draft202012Validator(json.loads((ROOT/'packages/contracts/candidate-definition.schema.json').read_text()),format_checker=CHECK)
READ,EDIT,EXECUTE=Operation.READ,Operation.EDIT,Operation.EXECUTE
CONSUMER_PROPS=('display_name','budget_level','budget_amount','waterproof_concern','favorite_sport')
PRODUCT_PROPS=('sku','name')
T0=datetime(2026,10,1,2,0,tzinfo=UTC)


def oid(label):return hashlib.sha256(label.encode()).hexdigest()


def consumer_schema():
    p=lambda name,display,kind=PropertyValueType.STRING,description='',enum=():PropertyDefinition(property_name=name,value_type=kind,display_name=display,
        description=description,type_descriptor=PropertyTypeDescriptor(kind=PropertyTypeKind(kind.value),enum=enum) if enum else None)
    return ObjectTypeDefinition(type_name='Consumer',display_name='消费者',version=1,title_property='display_name',only_edit_via_actions=True,
        properties=(p('display_name','姓名'),p('budget_level','预算区间',description='顾客可接受的价格档位',enum=('两千元以内','两千到五千元','五千元以上')),
            p('budget_amount','预算金额',PropertyValueType.NUMBER,'顾客说出的具体预算金额'),p('waterproof_concern','关注防水',PropertyValueType.BOOLEAN,'顾客是否在意防水功能'),
            p('favorite_sport','喜欢的运动',description='顾客自述的运动偏好')),
        property_groups=(PropertyGroup(group_name='spending_power',property_names=('budget_level','budget_amount')),))


def product_schema():
    return ObjectTypeDefinition(type_name='Product',display_name='商品',version=1,title_property='name',primary_key=('sku',),only_edit_via_actions=True,
        properties=(PropertyDefinition(property_name='sku',value_type=PropertyValueType.STRING,display_name='商品编号',required=True),
            PropertyDefinition(property_name='name',value_type=PropertyValueType.STRING,display_name='商品名称')))


def publish_action(admin,tenant,name,schema,capability_name=None):
    inputs=governance_inputs();original=inputs['action_definition'];capability=inputs['capability_snapshot']
    declaration=json.loads(json.dumps(original.model_dump(mode='json')).replace('synthetic-a',tenant));declaration.pop('contract_digest',None)
    ref=original.object_types[0].model_copy(update={'tenant_id':tenant,'stable_name':schema.type_name,'schema_digest':schema_contract_digest(schema)})
    declaration['stable_name']=name;declaration['object_types']=[ref.model_dump(mode='json')]
    declaration['governance']['change_scope']['object_types']=declaration['object_types']
    if capability_name:
        declaration['capability_binding']['capability_name']=capability_name;capability=capability.model_copy(update={'capability_name':capability_name})
    definition=type(original).model_validate_json(json.dumps(declaration))
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
        (tenant,'real',f'eios:action:{name}:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))


def put_object(admin,tenant,type_name,label,properties):
    admin.execute("""insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at,world,nexloop_revision)
        values(%s,%s,%s,1,%s,now(),now(),'real',1)""",(tenant,type_name,oid(label),Jsonb(properties)))
    return oid(label)


class Claims:
    """Claims in the NX-019 table shape (candidate knowledge, not formal facts)."""
    def __init__(self,admin,tenant,conversation,consumer):self.admin,self.tenant,self.conversation,self.consumer=admin,tenant,conversation,consumer;self.n=0

    def add(self,label,predicate,value,*,kind='preference',quote=None,subject='consumer',subject_text='',modality='asserted',polarity='affirmed',
            at=None,value_type=None,derived=()):
        self.n+=1;claim_id=oid('claim-'+label);at=at or T0+timedelta(minutes=self.n)
        quote=quote if quote is not None else f'{predicate}{value}'
        body=quote;source=None if kind=='hypothesis' else oid('message-'+label)
        vtype=value_type or {bool:'boolean',int:'number',float:'number'}.get(type(value),'string')
        self.admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
            predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
            extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
            values(%s,'real',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'consumer',%s,%s,%s,'',%s,%s,%s,%s,%s,%s,%s,'nx019-extractor/1',0.9,%s,%s,%s,%s,'[]')""",
            (self.tenant,claim_id,self.conversation,self.consumer,'0'*64,'1'*64,subject,self.consumer if subject=='consumer' else '',subject_text,predicate,
             Jsonb({'type':vtype,'value':value}),polarity,modality,'如果' if modality=='conditional' else '',
             Jsonb({'kind':'none','status':'absent','start':None,'end':None,'anchor':at.isoformat(),'timezone':'Asia/Shanghai','expression':''}),
             source,None if source is None else self.n,None if source is None else 0,None if source is None else len(body),
             None if source is None else hashlib.sha256(body.encode()).hexdigest(),'' if kind=='hypothesis' else quote,
             kind,'hypothesis_only' if kind=='hypothesis' else 'unresolved',Jsonb(list(derived)),oid('corr-'+label)))
        return claim_id


def decision(prop=None,value=None,*,type_ref=None,strong=None,name=None,new_prop=None,new_type=None,rationale='synthetic'):
    return {'type':{'ref':type_ref,'new':new_type},'instance':{'strong_id':strong,'name':name},'property':{'ref':prop,'new':new_prop},'value':value,'rationale':rationale}


@pytest.fixture
def env(admin,pg):
    bootstrap(admin);tenant=str(uuid.uuid4())
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(tenant,))
    for schema in (consumer_schema(),product_schema()):
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(tenant,schema.type_name,Jsonb(schema.model_dump(mode='json'))))
    publish_action(admin,tenant,'Consumer.edit',consumer_schema(),'ontology.object.edit')
    publish_action(admin,tenant,'Product.create',product_schema())
    signer=AuthoritySigner('nx020-signer',secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(signer.key_id,signer.material))
    consumer=put_object(admin,tenant,'Consumer','consumer-1',{'display_name':'张伟'})
    product=put_object(admin,tenant,'Product','product-1',{'sku':'SKU-1001','name':'防水登山鞋'})
    conversation=oid('conversation-1')
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'synthetic-human','nx020')",
        (tenant,conversation,consumer))
    with open_core(make_conninfo(pg,user='nexloop_domain_worker')) as worker:
        provider=DeterministicTestEmbeddingProvider(64)
        indexer_session,_=seed_multi_authority(admin,worker,[('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ)],identity_suffix='-indexer',tenant=tenant)
        indexer=RecallIndexer(worker,indexer_session,provider=provider,expected_dimension=64)
        indexer.activate_profile();indexer.index_object_type('Consumer');indexer.index_object_type('Product')
        indexer.index_instance('Consumer',consumer);indexer.index_instance('Product',product)
        yield dict(admin=admin,pg=pg,worker=worker,tenant=tenant,signer=signer,consumer=consumer,product=product,conversation=conversation,
            provider=provider,claims=Claims(admin,tenant,conversation,consumer),indexer=indexer)


def matcher_targets(f,*,match=True,new_products=()):
    targets=[('eios:action:nexloop.claim.match:1',ResourceType.ACTION,EXECUTE)] if match else []
    targets+=[('eios:action:Consumer.edit:1',ResourceType.ACTION,EXECUTE),('eios:action:Product.create:1',ResourceType.ACTION,EXECUTE),
        ('eios:object:Conversation/'+f['conversation'],ResourceType.OBJECT,READ),
        ('eios:object_type:Consumer',ResourceType.OBJECT_TYPE,READ),('eios:object_type:Product',ResourceType.OBJECT_TYPE,READ)]
    targets+=[(f'eios:property:Consumer/{p}',ResourceType.PROPERTY,READ) for p in CONSUMER_PROPS]
    targets+=[(f'eios:property:Product/{p}',ResourceType.PROPERTY,READ) for p in PRODUCT_PROPS]
    targets+=[('eios:object:Consumer/'+f['consumer'],ResourceType.OBJECT,op) for op in (READ,EDIT)]
    targets+=[(f"eios:property:Consumer/{f['consumer']}/{p}",ResourceType.PROPERTY,op) for p in CONSUMER_PROPS for op in (READ,EDIT)]
    for product in (f['product'],*new_products):
        targets+=[('eios:object:Product/'+product,ResourceType.OBJECT,READ)]+[(f'eios:property:Product/{product}/{p}',ResourceType.PROPERTY,READ) for p in PRODUCT_PROPS]
    return targets


def matcher(f,decisions,*,suffix='-matcher',**options):
    session,token=seed_multi_authority(f['admin'],f['worker'],matcher_targets(f,**options),identity_suffix=suffix,tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    provider=ScriptedMatchProvider(decisions)
    configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',1)},create_actions={'Product':('Product.create',1)})
    return ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=provider,configuration=configuration),provider


def obj(admin,label_or_id,type_name='Consumer'):
    return admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s and type_name=%s',(label_or_id,type_name)).fetchone()


def table(admin,name,**where):
    clause=' and '.join(f'{k}=%s' for k in where) or 'true'
    return admin.execute(f'select * from ontology.{name} where {clause}',tuple(where.values())).fetchall()


def resolution(admin,claim_id):
    return admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(claim_id,)).fetchone()[0]


def proposals(admin):
    return {r[0]:r for r in admin.execute('select claim_id,status,outcome,op,reason,attempts,proposal,receipt,supersedes_claim from ontology.nexloop_mutation_proposals').fetchall()}


def test_full_match_closed_vocabulary_auto_applies_with_receipt(env):
    """AT-061: type/property/value in Schema, instance uniquely located → governed write, no review, no candidate."""
    f=env;claim=f['claims'].add('budget-level','预算区间','两千元以内',quote='预算在两千元以内')
    m,provider=matcher(f,{claim:decision('eios:property:Consumer/budget_level','两千元以内')})
    result=m.process_conversation(f['conversation'])
    assert result['matches'][claim]['outcome']=='full_match'
    assert list(result['applied'].values())==['applied']
    assert obj(f['admin'],f['consumer'])==({'display_name':'张伟','budget_level':'两千元以内'},2)
    row=proposals(f['admin'])[claim]
    assert row[1:4]==('applied','full_match','set_property') and row[7]['revision']==2
    assert not list(MUTATION.iter_errors(row[6])) and row[6]['operations'][0]['expected_revision']==1
    assert row[6]['operations'][0]['evidence_refs']==['claim:'+claim,'message:'+oid('message-budget-level')]
    assert table(f['admin'],'nexloop_candidate_definitions')==[] and resolution(f['admin'],claim)=='resolved'
    # The governed Action claim exists: the write went through EIOS, not through matching SQL.
    assert f['admin'].execute("select count(*) from runtime.nexloop_action_claims where action_name='Consumer.edit'").fetchone()[0]==1


def test_closed_vocabulary_new_value_becomes_candidate_and_waits(env):
    """AT-062: closed vocabulary value outside the Schema → vocabulary_value candidate; nothing written; awaiting_definition."""
    f=env;claim=f['claims'].add('budget-new','预算区间','一万元以上',quote='预算一万元以上')
    same=f['claims'].add('budget-new-2','预算区间','一万元以上',quote='预算大概一万元以上吧')
    m,_=matcher(f,{claim:decision('eios:property:Consumer/budget_level','一万元以上'),same:decision('eios:property:Consumer/budget_level','一万元以上')})
    result=m.process_conversation(f['conversation'])
    assert result['matches'][claim]['outcome']=='no_match' and result['applied']=={}
    assert obj(f['admin'],f['consumer'])==({'display_name':'张伟'},1)
    rows=table(f['admin'],'nexloop_candidate_definitions')
    # Same text, same property: one staged candidate, both Claims depend on it.
    assert result['matches'][same]['candidate_id']==result['matches'][claim]['candidate_id']
    assert len(rows)==1 and rows[0][3]=='vocabulary_value' and rows[0][5]=='staged' and rows[0][7]==sorted(['claim:'+claim,'claim:'+same])
    candidate=rows[0][6]
    assert not list(CANDIDATE.iter_errors(candidate))
    assert candidate['proposed']=={'display_name':'一万元以上','property_ref':'eios:property:Consumer/budget_level','value':'一万元以上'}
    assert candidate['recall'] and resolution(f['admin'],claim)=='awaiting_definition'
    assert proposals(f['admin'])=={}


def test_open_property_new_value_auto_applies_with_evidence_time(env):
    """AT-063: open numeric property new value (预算2000) → automatic write, no review, evidence and valid time kept."""
    f=env;claim=f['claims'].add('budget-amount','预算金额',2000,quote='预算2000',at=T0)
    m,_=matcher(f,{claim:decision('eios:property:Consumer/budget_amount',2000)})
    result=m.process_conversation(f['conversation'])
    assert result['matches'][claim]['outcome']=='partial_match' and list(result['applied'].values())==['applied']
    assert obj(f['admin'],f['consumer'])[0]['budget_amount']==2000
    contract=proposals(f['admin'])[claim][6]
    assert contract['operations'][0]['valid_from']==T0.isoformat() and contract['source_content_hash']==hashlib.sha256('预算2000'.encode()).hexdigest()
    assert table(f['admin'],'nexloop_candidate_definitions')==[]


def test_new_instance_strong_id_created_name_only_staged(env):
    """AT-065: strong identifier → instance created automatically; name only → object_instance candidate."""
    f=env;claims=f['claims']
    strong=claims.add('sku-new','咨询商品','SKU-2002',kind='user_statement',subject='entity',subject_text='SKU-2002',quote='我想问下SKU-2002这款')
    again=claims.add('sku-new-again','咨询商品','SKU-2002',kind='user_statement',subject='entity',subject_text='SKU-2002',quote='SKU-2002还有货吗')
    existing=claims.add('sku-old','咨询商品','SKU-1001',kind='user_statement',subject='entity',subject_text='SKU-1001',quote='SKU-1001怎么样')
    named=claims.add('name-only','咨询商品','蓝色冲锋衣',kind='user_statement',subject='entity',subject_text='蓝色冲锋衣',quote='那款蓝色冲锋衣怎么样')
    product='eios:object_type:Product'
    # The second Claim also names a property of the new instance; both share one create intent.
    m,_=matcher(f,{strong:decision(type_ref=product,strong={'key':'sku','value':'SKU-2002'}),
        again:decision('eios:property:Product/sku','SKU-2002',type_ref=product,strong={'key':'sku','value':'SKU-2002'}),
        existing:decision(type_ref=product,strong={'key':'sku','value':'SKU-1001'}),named:decision(type_ref=product,name='蓝色冲锋衣')})
    result=m.process_conversation(f['conversation'])
    assert result['matches'][strong]['outcome']=='partial_match' and result['matches'][again]['proposal_id']==result['matches'][strong]['proposal_id']
    assert list(result['applied'].values())==['applied']
    created=f['admin'].execute("select properties from ontology.objects where type_name='Product' and properties->>'sku'='SKU-2002'").fetchall()
    assert created==[({'sku':'SKU-2002'},)]
    assert result['matches'][existing]['outcome']=='needs_resolution'
    assert f['admin'].execute("select count(*) from ontology.objects where type_name='Product'").fetchone()[0]==2
    assert result['matches'][named]['outcome']=='no_match' and resolution(f['admin'],named)=='awaiting_definition'
    candidate=table(f['admin'],'nexloop_candidate_definitions',kind='object_instance')[0][6]
    assert candidate['proposed']=={'display_name':'蓝色冲锋衣','type_ref':product,'identifying_properties':{'name':'蓝色冲锋衣'},'strong_identifier':False}
    assert not list(CANDIDATE.iter_errors(candidate))
    # A strong identifier the model did not read from the evidence is never trusted.
    forged=claims.add('sku-forged','咨询商品','某商品',kind='user_statement',subject='entity',subject_text='某商品',quote='那个商品怎么样')
    m2,_=matcher(f,{forged:decision(type_ref=product,strong={'key':'sku','value':'SKU-9999'})},suffix='-matcher-2')
    assert m2.match_claim(next(c for c in m2.claims.read(conversation_id=f['conversation'])['statements'] if c['claim_id']==forged))['outcome']=='needs_resolution'


def test_hypothesis_and_non_assertions_never_written_or_queued(env):
    """ADR-019 decision 2 and docs/04 §4: hypothesis layer only; conditional/tentative/negated/intent not formal."""
    f=env;c=f['claims']
    conditional=c.add('cond','喜欢的运动','滑雪',modality='conditional',quote='如果有空就去滑雪')
    hypothesis=c.add('hyp','流失风险','较高',kind='hypothesis',derived=(conditional,))
    tentative=c.add('tent','喜欢的运动','跑步',modality='tentative',quote='可能会去跑步')
    negated=c.add('neg','关注防水',True,polarity='negated',quote='不太在意防水')
    intent=c.add('intent','续费意向',True,kind='intent',quote='想续费')
    every={k:decision('eios:property:Consumer/favorite_sport','滑雪') for k in (conditional,tentative,negated,intent)}
    m,provider=matcher(f,every)
    result=m.process_conversation(f['conversation'])
    assert result['matches'][hypothesis]['outcome']=='hypothesis' and resolution(f['admin'],hypothesis)=='hypothesis_only'
    for claim in (conditional,tentative,negated,intent):assert result['matches'][claim]['outcome']=='needs_resolution'
    assert provider.calls==0 and proposals(f['admin'])=={} and table(f['admin'],'nexloop_candidate_definitions')==[]
    assert obj(f['admin'],f['consumer'])==({'display_name':'张伟'},1)


def test_unmatched_property_candidate_and_model_cannot_pick_unrecalled_definition(env):
    f=env;c=f['claims']
    new=c.add('new-prop','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    outside=c.add('outside','喜欢的运动','爬山',quote='喜欢爬山')
    m,_=matcher(f,{new:decision(new_prop={'name':'payment_method','display_name':'常用付款方式','description':'顾客常用的付款方式',
        'value_type':'string','closed_vocabulary':False,'property_group':'支付偏好'},value='花呗'),
        outside:decision('eios:property:Consumer/no_such_property','爬山')})
    result=m.process_conversation(f['conversation'])
    assert result['matches'][new]['outcome']=='no_match'
    candidate=table(f['admin'],'nexloop_candidate_definitions',kind='property')[0][6]
    assert candidate['proposed']['owner_type_ref']=='eios:object_type:Consumer' and not list(CANDIDATE.iter_errors(candidate))
    assert candidate['proposed']['property_group']=='other'  # outside the nine groups → other
    assert result['matches'][outside]['outcome']=='needs_resolution'
    assert obj(f['admin'],f['consumer'])==({'display_name':'张伟'},1)


def test_rerun_same_claims_is_idempotent(env):
    """AT-020 proposal layer: same Claims matched again → no duplicate proposal, candidate or write."""
    f=env;c=f['claims']
    a=c.add('sport','喜欢的运动','徒步',quote='喜欢徒步');b=c.add('vocab','预算区间','一万元以上',quote='预算一万元以上')
    decisions={a:decision('eios:property:Consumer/favorite_sport','徒步'),b:decision('eios:property:Consumer/budget_level','一万元以上')}
    m,provider=matcher(f,decisions)
    first=m.process_conversation(f['conversation'])
    snapshot=(obj(f['admin'],f['consumer']),len(proposals(f['admin'])),len(table(f['admin'],'nexloop_candidate_definitions')),
        f['admin'].execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0])
    calls=provider.calls
    second=m.process_conversation(f['conversation'])
    assert all(v['replay'] for v in second['matches'].values()) and provider.calls==calls
    assert {k:v['proposal_id'] for k,v in second['matches'].items()}=={k:v['proposal_id'] for k,v in first['matches'].items()}
    assert snapshot==(obj(f['admin'],f['consumer']),len(proposals(f['admin'])),len(table(f['admin'],'nexloop_candidate_definitions')),
        f['admin'].execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0])
    # A fresh matcher process (new credential) replays too.
    m2,provider2=matcher(f,decisions,suffix='-matcher-again')
    assert all(v['replay'] for v in m2.process_conversation(f['conversation'])['matches'].values()) and provider2.calls==0


def test_same_revision_conflict_one_wins_other_reassessed(env):
    """AT-021: two proposals from the same object revision → one applies, the other gets 40001 and is reassessed, never blind-written."""
    f=env;c=f['claims']
    older=c.add('sport-a','喜欢的运动','游泳',quote='喜欢游泳',at=T0)
    newer=c.add('sport-b','喜欢的运动','攀岩',quote='最近喜欢攀岩',at=T0+timedelta(hours=1))
    m,_=matcher(f,{older:decision('eios:property:Consumer/favorite_sport','游泳'),newer:decision('eios:property:Consumer/favorite_sport','攀岩')})
    matches=m.match_conversation(f['conversation'])
    p_old,p_new=matches[older]['proposal_id'],matches[newer]['proposal_id']
    assert {r[6]['operations'][0]['expected_revision'] for r in proposals(f['admin']).values()}=={1}
    assert m.apply(p_old)=='applied' and obj(f['admin'],f['consumer'])[1]==2
    assert m.apply(p_new)=='applied'
    row=proposals(f['admin'])[newer]
    assert row[5]==2 and row[7]['revision']==3 and obj(f['admin'],f['consumer'])[0]['favorite_sport']=='攀岩'


def test_late_older_evidence_does_not_overwrite_newer(env):
    """AT-022: the older preference arriving after the newer one is superseded, not applied."""
    f=env;c=f['claims']
    newer=c.add('late-new','喜欢的运动','骑行',quote='现在喜欢骑行',at=T0+timedelta(days=2))
    older=c.add('late-old','喜欢的运动','篮球',quote='以前喜欢篮球',at=T0)
    m,_=matcher(f,{newer:decision('eios:property:Consumer/favorite_sport','骑行'),older:decision('eios:property:Consumer/favorite_sport','篮球')})
    matches=m.match_conversation(f['conversation'])
    assert m.apply(matches[newer]['proposal_id'])=='applied'
    assert m.apply(matches[older]['proposal_id'])=='superseded'
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='骑行'
    assert proposals(f['admin'])[older][4]=='late_evidence' and resolution(f['admin'],older)=='superseded'


def test_correction_chain_supersedes_previous_claim(env):
    f=env;c=f['claims']
    first=c.add('corr-1','喜欢的运动','网球',quote='喜欢网球',at=T0+timedelta(days=1))
    m,_=matcher(f,{first:decision('eios:property:Consumer/favorite_sport','网球')})
    m.process_conversation(f['conversation'])
    fix=c.add('corr-2','喜欢的运动','羽毛球',kind='correction',quote='说错了，是羽毛球',at=T0)
    m.provider.decisions[fix]=decision('eios:property:Consumer/favorite_sport','羽毛球')
    result=m.process_conversation(f['conversation'])
    assert result['matches'][first]['replay'] and result['applied'][result['matches'][fix]['proposal_id']]=='applied'
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='羽毛球'
    row=proposals(f['admin'])[fix]
    assert row[8]==first and [o['op'] for o in row[6]['operations']]==['set_property','supersede_claim'] and not list(MUTATION.iter_errors(row[6]))
    assert resolution(f['admin'],first)=='superseded' and resolution(f['admin'],fix)=='resolved'


def test_interrupted_apply_resumes_with_same_intent_without_double_write(env,monkeypatch):
    f=env;claim=f['claims'].add('resume','喜欢的运动','冲浪',quote='喜欢冲浪')
    m,_=matcher(f,{claim:decision('eios:property:Consumer/favorite_sport','冲浪')})
    proposal=m.match_conversation(f['conversation'])[claim]['proposal_id']
    original=m._transition
    def crash_before_recording_applied(row,from_status,to_status,**extra):
        if to_status=='applied':raise RuntimeError('synthetic crash after governed commit')
        return original(row,from_status,to_status,**extra)
    monkeypatch.setattr(m,'_transition',crash_before_recording_applied)
    with pytest.raises(RuntimeError):m.apply(proposal)
    assert proposals(f['admin'])[claim][1]=='applying' and obj(f['admin'],f['consumer'])==({'display_name':'张伟','favorite_sport':'冲浪'},2)
    monkeypatch.setattr(m,'_transition',original)
    # Resume: same recorded arguments and intent → governed replay, no second revision.
    assert m.apply(proposal)=='applied' and obj(f['admin'],f['consumer'])[1]==2
    assert f['admin'].execute("select count(*) from runtime.nexloop_action_claims where action_name='Consumer.edit'").fetchone()[0]==1


def test_match_requires_execute_grant_and_records_nothing(env):
    f=env;claim=f['claims'].add('noexec','喜欢的运动','跳伞',quote='喜欢跳伞')
    m,_=matcher(f,{claim:decision('eios:property:Consumer/favorite_sport','跳伞')},match=False,suffix='-no-match-grant')
    with pytest.raises(Exception):m.process_conversation(f['conversation'])
    assert table(f['admin'],'nexloop_claim_matches')==[] and resolution(f['admin'],claim)=='unresolved'
    with f['worker'].connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute('select * from ontology.nexloop_mutation_proposals')

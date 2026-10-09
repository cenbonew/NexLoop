"""NX-044 closing (0093): approving a new object type publishes its governed create/edit Actions.

Same transaction as Schema v1; the tenant's existing ontology.object.create / ontology.object.edit
Capability snapshots; canonical shape checked exactly in SQL; no authority fact written.
A real browser Human decides; synthetic data only.
"""
import copy
import json

import psycopg
import pytest
from eios.ontology.definitions import ActionDefinition
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.review_actions import PROTOCOL,build_publication
from test_candidate_merge_pg import candidates,setup
from test_claim_matching_pg import decision,env,product_schema,publish_action,resolution  # noqa: F401
from test_review_decisions_pg import TENANT,decisions,human,review  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)
PET={'name':'Pet','display_name':'宠物','description':'顾客饲养的宠物'}


def type_pending(f,label='pet'):
    claim=f['claims'].add(label,'养的宠物','布丁',kind='user_statement',subject='entity',subject_text='布丁',quote='我家养了一只叫布丁的猫')
    m,gluer=setup(f,{claim:decision(new_type=PET)})
    cid=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    row=candidates(f['admin'])[cid]
    assert row[1]=='object_type' and row[2]=='pending_review'
    return claim,cid


def create_capability(f):
    """The tenant publishes an ontology.object.create Action (the env only has consumer.create and ontology.object.edit)."""
    publish_action(f['admin'],TENANT,'Product.register',product_schema(),'ontology.object.create')


def fingerprint(admin):return admin.execute('select authz.nexloop_tenant_authority_fingerprint(%s)',(TENANT,)).fetchone()[0]


def actions(admin,type_name):
    return {r[0]:r[1:] for r in admin.execute("select resource_id,definition,capability,active from control.nexloop_action_definitions where tenant_id=%s and resource_id like %s",
        (TENANT,f'eios:action:{type_name}.%')).fetchall()}


def test_new_type_publishes_schema_and_canonical_create_edit_actions_without_authority(review):
    f=review;admin=f['admin'];claim,cid=type_pending(f);create_capability(f)
    before=fingerprint(admin)
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=candidates(admin)[cid][8],rationale='新增宠物类型',idempotency_key='synthetic-type-approve-01')
    assert result['outcome']=='published',result['publication']
    assert set(result['publication']['published_refs'])=={'eios:object_type:Pet','eios:object_type:Pet:1','eios:action:Pet.create:1','eios:action:Pet.edit:1'}
    assert fingerprint(admin)==before  # still no authority fact (ADR-020 §3)
    schema=ObjectTypeDefinition.model_validate(admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Pet' and version=1",(TENANT,)).fetchone()[0])
    published=actions(admin,'Pet')
    assert set(published)=={'eios:action:Pet.create:1','eios:action:Pet.edit:1'}
    capabilities={r[0]:r[1] for r in admin.execute("select capability->>'capability_name',capability from control.nexloop_action_definitions where tenant_id=%s and resource_id in ('eios:action:Product.register:1','eios:action:Consumer.edit:1')",(TENANT,)).fetchall()}
    for resource,(definition,capability,active) in published.items():
        action=ActionDefinition.model_validate_json(json.dumps(definition))
        suffix=action.stable_name.split('.')[1]
        assert active and capability==capabilities['ontology.object.'+suffix] and action.capability_binding.capability_name=='ontology.object.'+suffix
        assert [(r.stable_name,r.version,r.schema_digest) for r in action.object_types]==[('Pet',1,schema_contract_digest(schema))]
        assert action.governance.risk_level.value=='low' and action.governance.approval_mode.value=='none' and action.governance.policy_refs==()
    # No grant exists for the new Actions: they wait for trusted configuration.
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where tenant_id=%s and array_to_string(entity_key,'|') like '%%Pet.%%'",(TENANT,)).fetchone()==(0,)
    assert resolution(admin,claim)=='awaiting_definition'


def test_new_type_without_tenant_create_capability_stays_pending_with_reason(review):
    f=review;admin=f['admin'];claim,cid=type_pending(f,'pet-nocap')
    revision=candidates(admin)[cid][8]
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=revision,rationale='新增宠物类型',idempotency_key='synthetic-type-approve-02')
    assert result['outcome']=='publication_failed'
    assert result['publication']['gate_failures']==['eios_type_action_capability_unavailable:ontology.object.create']
    assert candidates(admin)[cid][2]=='pending_review' and actions(admin,'Pet')=={}
    assert admin.execute("select count(*) from ontology.object_type_versions where tenant_id=%s and type_name='Pet'",(TENANT,)).fetchone()==(0,)


@pytest.mark.parametrize('tamper,reason',[
    ('risk','new_type_action_not_canonical:create'),('scope','new_type_action_not_canonical:edit'),('capability','new_type_action_not_canonical:create'),
    ('extra','new_type_actions_must_be_create_and_edit'),('missing','new_type_actions_must_be_create_and_edit'),('rename','new_type_actions_must_be_create_and_edit')])
def test_non_canonical_new_type_actions_are_refused_by_sql(review,tamper,reason):
    f=review;admin=f['admin'];claim,cid=type_pending(f,'pet-'+tamper);create_capability(f)
    port=human(f);basis=port.basis(cid);revision=basis['revision']
    publication=build_publication(basis);assert 'builder_failures' not in publication
    acts=copy.deepcopy(publication['actions'])
    if tamper=='risk':acts[0]['definition']['governance']['risk_level']='high'
    if tamper=='scope':acts[1]['definition']['required_scopes']=sorted(acts[1]['definition']['required_scopes']+['ontology.schema.review'])
    if tamper=='capability':acts[0]['capability']=dict(acts[0]['capability'],has_side_effects=False)
    if tamper=='extra':acts.append(dict(copy.deepcopy(acts[0]),definition=dict(acts[0]['definition'],stable_name='Pet.delete')))
    if tamper=='missing':acts=acts[:1]
    if tamper=='rename':acts[0]['definition']['stable_name']='Consumer.create'
    payload={'decision_id':'00000000-0000-4000-8000-00000000a093','candidate_id':cid,'decision':'approve','expected_revision':revision,
        'rationale':'篡改的 Action','publication':dict(publication,actions=acts)}
    result=port._call('nexloop_review_decide',payload,PROTOCOL)
    assert result['outcome']=='publication_failed' and reason in result['publication']['gate_failures'],result['publication']
    assert actions(admin,'Pet')=={} and candidates(admin)[cid][2]=='pending_review'
    assert admin.execute("select count(*) from ontology.object_type_versions where tenant_id=%s and type_name='Pet'",(TENANT,)).fetchone()==(0,)

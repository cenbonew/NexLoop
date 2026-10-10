"""Versioned business Action declarations: profiles, human-owner-only Actions, compilation."""
import copy
from datetime import UTC,datetime
import json
from pathlib import Path

import pytest
from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios import business_actions as BA
from nexloop_eios.context_engine.audit import AUDIT_ACTION,AUDIT_CAPABILITY,context_manifest_object_type
from nexloop_eios.context_engine.strategy import PUBLISH_ACTION,PUBLISH_CAPABILITY,context_strategy_object_type
from nexloop_eios.conversation_messages import conversation_schemas

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=BA.load(ROOT/'deploy/configuration/business-actions.v1.json')
GRANTS=json.loads((ROOT/'deploy/authorization/service-grants.v1.json').read_text())


def capability(name):
    return CapabilityContractSnapshot.model_validate_json(json.dumps({'capability_name':name,'capability_version':'1.0.0','schema_hash':'c'*64,'kind':'atomic',
        'has_side_effects':True,'idempotent':True,'required_scopes':['action.execute'],'risk_level':'low'}))


def consumer_type():
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    return ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True,
        properties=(PropertyDefinition(property_name='display_name',value_type=PropertyValueType.STRING),))


REQUESTS={'nexloop.plan.request_reevaluation':'plan.request_reevaluation','nexloop.service.query_request':'service.query_request',
    'nexloop.conversation.takeover':'conversation.takeover','nexloop.conversation.handback':'conversation.handback',
    'nexloop.message.staff_send':'message.staff_send'}
COMMITMENT_HUMAN={'Commitment.cancel':'commitment.cancel','Commitment.extend':'commitment.extend','Commitment.attest':'commitment.attest',
    'Commitment.condition_met':'commitment.condition_met','Commitment.mark_communication':'commitment.mark_communication'}


def object_types():
    from nexloop_eios import business_object_types
    return ([s.model_dump(mode='json') for s in conversation_schemas()]+[context_strategy_object_type().model_dump(mode='json'),
        context_manifest_object_type().model_dump(mode='json'),consumer_type().model_dump(mode='json')]
        +business_object_types.trusted_object_types(ROOT/'deploy/ontology/business-object-types.v1.json'))


def test_manifest_declares_service_create_and_human_owner_strategy_publication():
    by={a['stable_name']:a for a in MANIFEST['actions']}
    assert by['Message.agent_create']['authority']=='service' and by['Message.agent_create']['executor_role']=='outbound_message_recorder'
    p=by[PUBLISH_ACTION]
    assert p['authority']=='human_owner' and p['executor_role']=='human_owner' and p['capability_name']==PUBLISH_CAPABILITY
    # Owner decision: Manifest reads are human-only (owner / audit role).
    audit=by[AUDIT_ACTION]
    assert audit['authority']=='human_owner' and audit['executor_role']=='human_owner' and audit['capability_name']==AUDIT_CAPABILITY
    assert audit['object_type']=={'stable_name':'ContextManifest','version':1}


def test_compiles_both_profiles_with_explicit_snapshots():
    rows=BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
        capabilities={'ontology.object.create':capability('ontology.object.create'),'ontology.object.edit':capability('ontology.object.edit'),
            PUBLISH_CAPABILITY:capability(PUBLISH_CAPABILITY),AUDIT_CAPABILITY:capability(AUDIT_CAPABILITY),
            **{cap:capability(cap) for cap in COMMITMENT_HUMAN.values()},**{cap:capability(cap) for cap in REQUESTS.values()},'commercial.observe':capability('commercial.observe')})
    names={r['definition']['stable_name']:r for r in rows}
    assert set(names)=={'Message.agent_create','Consumer.edit',PUBLISH_ACTION,AUDIT_ACTION,'Commitment.create','Commitment.edit',*COMMITMENT_HUMAN,*REQUESTS,
        'CommercialRecord.create','CommercialRecord.edit','CommercialRecord.observe'}
    # NX-028 slice 2: two human requests, typed on Consumer v1, never service-executed.
    assert all(by_name['authority']=='human_owner' for by_name in MANIFEST['actions'] if by_name['stable_name'] in REQUESTS)
    observe=next(a for a in MANIFEST['actions'] if a['stable_name']=='CommercialRecord.observe')
    assert observe['authority']=='human_owner' and observe['executor_role']=='human_owner' and observe['capability_name']=='commercial.observe'
    # NX-027: the commercial recorder's create/edit on CommercialRecord v1 (service authority only).
    assert all(names[n]['definition']['object_types'][0]['stable_name']=='CommercialRecord' for n in ('CommercialRecord.create','CommercialRecord.edit'))
    # NX-026: the keeper's create/edit and five human-owner commitment Actions, all on Commitment v1.
    by={a['stable_name']:a for a in MANIFEST['actions']}
    assert by['Commitment.create']['executor_role']==by['Commitment.edit']['executor_role']=='commitment_keeper'
    assert all(by[n]['authority']=='human_owner' and by[n]['capability_name']==c for n,c in COMMITMENT_HUMAN.items())
    assert all([t['stable_name'] for t in names[n]['definition']['object_types']]==['Commitment'] for n in ('Commitment.create','Commitment.edit',*COMMITMENT_HUMAN))
    edit=names['Consumer.edit']
    assert edit['capability']['capability_name']=='ontology.object.edit' and edit['definition']['governance']['risk_level']=='low'
    assert [t['stable_name'] for t in edit['definition']['object_types']]==['Consumer']
    assert [t['stable_name'] for t in names[AUDIT_ACTION]['definition']['object_types']]==['ContextManifest']
    publish=names[PUBLISH_ACTION]['definition']
    assert publish['capability_binding']['capability_name']==PUBLISH_CAPABILITY and publish['governance']['approval_mode']=='none'
    assert [t['stable_name'] for t in publish['object_types']]==['ContextStrategy']
    with pytest.raises(BA.BusinessActionsRejected):  # every declared Action needs its own snapshot
        BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),capability=capability('ontology.object.create'))
    only=BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
        capability=capability('ontology.object.create'),select=('Message.agent_create',))
    assert [r['definition']['stable_name'] for r in only]==['Message.agent_create']
    with pytest.raises(BA.BusinessActionsRejected):
        BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
            capability=capability('ontology.object.create'),select=('Undeclared.action',))


@pytest.mark.parametrize('change',['human_publish_by_service','service_create_by_human','unknown_capability','approval','policy','risk','audit_by_service'])
def test_profiles_are_closed(change):
    m=copy.deepcopy(MANIFEST);by={a['stable_name']:a for a in m['actions']}
    if change=='human_publish_by_service':by[PUBLISH_ACTION]['executor_role']='context_assembler'
    if change=='service_create_by_human':by['Message.agent_create'].update(authority='human_owner')
    if change=='unknown_capability':by[PUBLISH_ACTION]['capability_name']='context.strategy.delete'
    if change=='approval':by[PUBLISH_ACTION]['approval_mode']='required'
    if change=='policy':by[PUBLISH_ACTION]['policy_refs']=['policy:x']
    if change=='risk':by['Message.agent_create']['risk_level']='high'
    if change=='audit_by_service':by[AUDIT_ACTION].update(authority='service',executor_role='context_assembler')
    with pytest.raises(BA.BusinessActionsRejected):BA.validate(m)


def test_human_owner_actions_are_never_granted_to_service_principals():
    human={'eios:action:%s:%s'%(a['stable_name'],a['version']) for a in MANIFEST['actions'] if a['authority']=='human_owner'}
    assert human and not human&{g['resource_id'] for g in GRANTS['grants']}
    services={a['executor_role'] for a in MANIFEST['actions'] if a['authority']=='service'}
    assert services<={p['role'] for p in GRANTS['principals']}
    # nexloop.context.assemble:1 is issued with each Run (0104): no standing service principal holds it.
    assert not any(g['resource_id']=='eios:action:nexloop.context.assemble:1' for g in GRANTS['grants'])
    assert [r['role'] for r in GRANTS['retired_principals']]==['context_assembler']


def test_edit_profile_requires_a_side_effecting_service_snapshot():
    m=copy.deepcopy(MANIFEST);edit=next(a for a in m['actions'] if a['stable_name']=='Consumer.edit')
    assert edit['authority']=='service' and edit['executor_role']=='claim_matcher'
    grants={(g['principal'],g['resource_id']) for g in GRANTS['grants']}
    assert ('claim_matcher','eios:action:Consumer.edit:1') in grants  # the executor already holds it (no new grant here)
    edit['authority']='human_owner'
    with pytest.raises(BA.BusinessActionsRejected):BA.validate(m)
    quiet=capability('ontology.object.edit').model_copy(update={'has_side_effects':False})
    with pytest.raises(BA.BusinessActionsRejected):
        BA.compile_actions(MANIFEST,tenant='synthetic-a',created_by='owner',created_at=datetime.now(UTC),object_types=object_types(),
            capabilities={'ontology.object.edit':quiet},select=('Consumer.edit',))


def test_deployment_actions_provide_both_type_action_capabilities(admin):
    """Ruling 2: with only the business-actions manifest compiled into a tenant, a review-approved new type finds both
    Capability snapshots (no type_action_capability_unavailable) and the SQL gates accept its canonical Actions."""
    import uuid
    from psycopg.types.json import Jsonb
    from nexloop_eios.bootstrap import bootstrap
    from nexloop_eios.review_actions import build_publication
    bootstrap(admin);tenant='synthetic-a'
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(tenant,))
    types=object_types()
    for t in types:
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s)',(tenant,t['type_name'],t['version'],Jsonb(t)))
    rows=BA.compile_actions(MANIFEST,tenant=tenant,created_by='owner',created_at=datetime.now(UTC),object_types=types,
        capabilities={'ontology.object.create':capability('ontology.object.create'),'ontology.object.edit':capability('ontology.object.edit')},
        select=('Message.agent_create','Consumer.edit'))
    for row in rows:
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (tenant,'real',f"eios:action:{row['definition']['stable_name']}:{row['definition']['version']}",Jsonb(row['definition']),Jsonb(row['capability'])))
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
        caps=admin.execute('select ontology.nexloop_review_type_action_capabilities(%s)',(tenant,)).fetchone()[0]
        assert set(caps)=={'ontology.object.create','ontology.object.edit'}
        cid=str(uuid.uuid4())
        candidate={'tenant_id':tenant,'kind':'object_type','proposed':{'name':'pet','display_name':'宠物','description':''}}
        admin.execute("""insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,candidate,dependent_claims,status,merge_scores,config_version)
            values(%s,'real',%s,'object_type',%s,%s,%s,'pending_review',%s,'test')""",(tenant,cid,'e'*64,Jsonb(candidate),['claim:'+'a'*64],
            Jsonb({'lexical_similarity':0,'core_term_containment':0,'vector_cluster':0,'rule_whitelist':0,'weighted_total':0,'threshold':1,'config_version':'test'})))
        publication=build_publication({'kind':'object_type','candidate':candidate,'type_action_capabilities':caps})
        assert 'builder_failures' not in publication and len(publication['actions'])==2
        gates=admin.execute("""select ontology.nexloop_review_publication_gates(%s,'real',d,%s) from ontology.nexloop_candidate_definitions d where candidate_id=%s""",
            (tenant,Jsonb(publication),cid)).fetchone()[0]
        assert gates==[]


def test_doctor_reports_an_already_published_declared_action_and_capability_conflicts():
    """A deployment that already published Consumer.edit:1: same Capability → exclude it; another Capability → conflict."""
    lineage=lambda cap:[{'stable_name':'Consumer.edit','version':1,'world':'real','active':True,
        'definition':{'capability_binding':{'capability_name':cap}},'capability':{'capability_name':cap}}]
    fresh=BA.doctor(MANIFEST,[])
    assert fresh['ok'] and 'Consumer.edit' in fresh['publish'] and fresh['findings']==[]
    same=BA.doctor(MANIFEST,lineage('ontology.object.edit'))
    assert same['ok'] and 'Consumer.edit' not in same['publish'] and [f['state'] for f in same['findings']]==['already_published']
    other=BA.doctor(MANIFEST,lineage('consumer.edit'))
    assert other['ok'] is False and other['findings'][0]=={**other['findings'][0],'state':'conflict_different_capability',
        'resource_id':'eios:action:Consumer.edit:1','declared':'ontology.object.edit','published':'consumer.edit'}


def test_doctor_reads_the_tenant_through_the_configurator(admin,pg,tmp_path):
    from psycopg.conninfo import make_conninfo
    from psycopg.types.json import Jsonb
    from nexloop_eios.bootstrap import bootstrap
    from test_postgres_action_claims import governance_inputs
    bootstrap(admin);tenant='synthetic-a'
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(tenant,))
    inputs=governance_inputs();definition=inputs['action_definition'].model_copy(update={'stable_name':'Consumer.edit','contract_digest':None})
    admin.execute("insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,'real','eios:action:Consumer.edit:1',%s,%s)",
        (tenant,Jsonb(definition.model_dump(mode='json')),Jsonb(inputs['capability_snapshot'].model_dump(mode='json'))))
    admin.execute('alter role nexloop_configurator login')
    dsn=tmp_path/'configurator-dsn';dsn.write_text(make_conninfo(pg,user='nexloop_configurator'));dsn.chmod(0o600)
    assert BA.main(['doctor','--manifest',str(ROOT/'deploy/configuration/business-actions.v1.json'),'--tenant',tenant,'--database-url-file',str(dsn)])==1

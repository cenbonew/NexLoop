"""NX-044 human review decisions and the human-approved Schema publication on clean catalog PostgreSQL.

A real browser Human (canonical identity, session, EIOS browser authority facts
for ontology.schema.review) decides on candidates produced by the actual NX-020
matching and NX-045 glue. Reflow runs as a service credential. Synthetic data.
"""
import json
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.candidate_merge import CandidateGluer
from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
from nexloop_eios.review_actions import PROTOCOL,ReviewDecisionPort,ReviewReflowWorker
from multi_authority_fixture import seed_multi_authority
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_browser_session_reads import authenticate,create
from test_candidate_merge_pg import candidates,new_property,setup
from test_claim_matching_pg import CONSUMER_PROPS,decision,env,matcher_targets,obj,resolution  # noqa: F401
from test_review_http import REVIEW,grant_human
from test_review_workbench_pg import to_review

TENANT='synthetic-a'
pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)
ROOT=Path(__file__).resolve().parents[1]
RECORD=Draft202012Validator(json.loads((ROOT/'packages/contracts/review-decision.schema.json').read_text()))


@pytest.fixture
def review(env,identity,uow,admin,pg,tmp_path):
    f=env
    with open_core(make_conninfo(pg,user='nexloop_api')) as api:
        f.update(api=api,identity=identity,uow=uow,tmp=tmp_path)
        yield f


def human(f,*,grant=True):
    """A fresh, current Human browser session (authority changes make earlier ones stale)."""
    if grant:grant_human(f['admin'],f['uow'],f['identity'],REVIEW)
    issued=create(f['uow'],f['identity'],authenticate(f['uow'],f['identity']).evidence)
    return ReviewDecisionPort(f['api'],authenticate_browser_business(f['api'],issued.session,world='real'),f['signer'])


def pending(f,label,predicate,value,quote,prop,group='purchase_behavior'):
    claim,d=to_review(f,label,predicate,value,quote,prop)
    m,gluer=setup(f,{claim:d})
    cid=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    assert candidates(f['admin'])[cid][2]=='pending_review'
    return claim,cid,m,gluer


def worker(f,*,edit_version=1,extra_targets=(),suffix='-reflow'):
    session,_=seed_multi_authority(f['admin'],f['worker'],matcher_targets(f)+list(extra_targets),identity_suffix=suffix,tenant=f['tenant'])
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    m=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=ScriptedMatchProvider({}),
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',edit_version)},create_actions={'Product':('Product.create',1)}))
    gluer=CandidateGluer(f['worker'],session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    return ReviewReflowWorker(f['worker'],session,f['signer'],gluer=gluer)


def decisions(admin):
    return admin.execute('select decision,outcome,reviewer_ref,record,publication,reflow_status,reflow_report from ontology.nexloop_review_decisions order by decided_at').fetchall()


def test_human_reject_records_decision_cooldown_and_replays(review):
    """AT-068 decision: rejected by a governed human decision; Claims kept as evidence; cooldown; audit; idempotent."""
    f=review;claim,cid,_,_=pending(f,'r1','常用付款方式','花呗','我一般用花呗付款','payment_method')
    port=human(f);revision=candidates(f['admin'])[cid][8]
    result=port.decide(candidate_id=cid,decision='reject',expected_revision=revision,rationale='不是业务概念',idempotency_key='synthetic-reject-0001')
    assert result['outcome']=='rejected' and result['replay'] is False and result['reflow_status']=='none'
    assert candidates(f['admin'])[cid][2]=='rejected' and resolution(f['admin'],claim)=='rejected_definition'
    assert f['admin'].execute('select cooldown_until=rejected_at+make_interval(secs=>cooldown_seconds) from ontology.nexloop_candidate_rejections').fetchone()==(True,)
    row=decisions(f['admin'])[0]
    principal=f['identity'][2].principal_id
    assert row[:3]==('reject','rejected','human:'+principal) and not list(RECORD.iter_errors(row[3]))
    assert f['admin'].execute("select actor_kind,actor_ref from ontology.nexloop_candidate_events where to_status='rejected'").fetchone()==('human','human:'+principal)
    again=port.decide(candidate_id=cid,decision='reject',expected_revision=revision,rationale='不是业务概念',idempotency_key='synthetic-reject-0001')
    assert again['replay'] is True and len(decisions(f['admin']))==1
    with pytest.raises(psycopg.errors.SerializationFailure):
        port.decide(candidate_id=cid,decision='reject',expected_revision=revision,rationale='again',idempotency_key='synthetic-reject-0002')


def test_service_agent_and_ungranted_principals_cannot_decide(review):
    """AT-067 negative: no review decision without a granted Human; nothing changes."""
    f=review;claim,cid,_,_=pending(f,'n1','常用付款方式','花呗','我一般用花呗付款','payment_method')
    revision=candidates(f['admin'])[cid][8]
    # A service credential holding the very same review EXECUTE grant.
    service,_=seed_multi_authority(f['admin'],f['api'],[(REVIEW,ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-svc-reviewer',tenant=f['tenant'])
    with pytest.raises(PermissionError):ReviewDecisionPort(f['api'],service,f['signer'])
    forged=object.__new__(ReviewDecisionPort);forged.pool,forged.session,forged.signer=f['api'],service,f['signer']
    payload={'decision_id':'00000000-0000-4000-8000-000000000044','candidate_id':cid,'decision':'reject','expected_revision':revision,'rationale':'service'}
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='human session'):forged._call('nexloop_review_decide',payload,PROTOCOL)
    # A business-enabled Human without ontology.schema.review.
    grant_human(f['admin'],f['uow'],f['identity'],'eios:action:Product.create:1')
    with pytest.raises(PermissionError):human(f,grant=False).decide(candidate_id=cid,decision='reject',expected_revision=revision,rationale='x',idempotency_key='synthetic-reject-ungranted')
    assert candidates(f['admin'])[cid][2]=='pending_review' and decisions(f['admin'])==[] and resolution(f['admin'],claim)=='awaiting_definition'


def test_human_merge_into_reflows_and_applies(review):
    """AT-069 (merge path via review): alias + re-point by the human decision, service reflow applies the Claim."""
    f=review;claim,cid,_,_=pending(f,'m1','平时的消遣','钓鱼','平时的消遣是钓鱼','pastime')
    port=human(f);revision=candidates(f['admin'])[cid][8]
    with pytest.raises(psycopg.errors.InvalidParameterValue,match='same kind'):
        port.decide(candidate_id=cid,decision='merge_into',expected_revision=revision,rationale='x',idempotency_key='synthetic-merge-bad-01',merge_target_ref='eios:object_type:Consumer')
    result=port.decide(candidate_id=cid,decision='merge_into',expected_revision=revision,rationale='就是运动偏好',idempotency_key='synthetic-merge-0001',
        merge_target_ref='eios:property:Consumer/favorite_sport')
    assert result['outcome']=='merged' and result['reflow_status']=='pending' and not list(RECORD.iter_errors(result['record']))
    assert resolution(f['admin'],claim)=='unresolved'
    report=worker(f).run_pending()
    assert report[result['decision_id']]['status']=='done' and report[result['decision_id']]['applied']==1
    assert obj(f['admin'],f['consumer'])[0]['favorite_sport']=='钓鱼' and resolution(f['admin'],claim)=='resolved'
    assert decisions(f['admin'])[0][5]=='done'


def test_human_approve_publishes_additively_and_waiting_claims_apply_after_grant(review):
    """AT-067 / AT-069 publish path: gates, Schema v2 + successor Action, no authority change; Claims wait for grants, then apply."""
    f=review;admin=f['admin'];claim,cid,_,_=pending(f,'a1','常用付款方式','花呗','我一般用花呗付款','payment_method')
    port=human(f);revision=candidates(admin)[cid][8]
    facts_before=admin.execute("select authz.nexloop_tenant_authority_fingerprint(%s)",(TENANT,)).fetchone()[0]
    result=port.decide(candidate_id=cid,decision='approve',expected_revision=revision,rationale='新增付款方式属性',idempotency_key='synthetic-approve-0001')
    assert result['outcome']=='published',result['publication']
    pub=result['publication']
    assert pub['schema_revision_before']=='Consumer@1' and pub['schema_revision_after']=='Consumer@2' and pub['gate_failures']==[]
    assert set(pub['published_refs'])>={'eios:property:Consumer/payment_method','eios:object_type:Consumer:2','eios:action:Consumer.edit:2'}
    assert not list(RECORD.iter_errors(result['record']))
    assert admin.execute("select authz.nexloop_tenant_authority_fingerprint(%s)",(TENANT,)).fetchone()[0]==facts_before
    schema=admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2",(TENANT,)).fetchone()[0]
    assert [p['property_name'] for p in schema['properties']][-1]=='payment_method' and schema['properties'][-1]['required'] is False
    assert admin.execute("select schema_version from ontology.objects where object_id=%s",(f['consumer'],)).fetchone()==(2,)
    assert admin.execute("select count(*) from control.nexloop_action_definitions where resource_id='eios:action:Consumer.edit:2' and active").fetchone()==(1,)
    # Two steps (ADR-020 §3): published now, applied only after trusted configuration grants.
    assert candidates(admin)[cid][2]=='published' and resolution(admin,claim)=='awaiting_definition'
    first=worker(f,edit_version=2,suffix='-reflow-nogrant').run_pending()
    assert first[result['decision_id']]['status']=='waiting' and first[result['decision_id']]['reason']=='awaiting_grants'
    assert 'payment_method' not in obj(admin,f['consumer'])[0] and resolution(admin,claim)=='awaiting_definition'
    shown=human(f).awaiting()
    assert [(i['decision_id'],i['reflow_status'],i['reason']) for i in shown]==[(result['decision_id'],'waiting','awaiting_grants')]
    # Read-only doctor for the scheduler: the successor Action is not in the service grant manifest yet.
    from nexloop_eios.review_actions import uncovered_actions
    admin.execute('alter role nexloop_configurator login')
    dsn=f['tmp']/'configurator-dsn';dsn.write_text(make_conninfo(f['pg'],user='nexloop_configurator'));dsn.chmod(0o600)
    manifest=json.loads((ROOT/'deploy/authorization/service-grants.v1.json').read_text())
    report=uncovered_actions(manifest,TENANT,database_url_file=dsn)
    assert not report['covered'] and [u['resource_id'] for u in report['uncovered']]==['eios:action:Consumer.edit:2']
    assert report['uncovered'][0]['predecessor']=='eios:action:Consumer.edit:1' and report['uncovered'][0]['predecessor_grants']
    manifest['grants'].append({**next(g for g in manifest['grants'] if g['resource_id']=='eios:action:Consumer.edit:1'),'resource_id':'eios:action:Consumer.edit:2'})
    assert uncovered_actions(manifest,TENANT,database_url_file=dsn)['covered'] is True
    # Trusted configuration grants the successor Action and the new property (ADR-020 §3), then reflow applies.
    grants=[('eios:action:Consumer.edit:2',ResourceType.ACTION,Operation.EXECUTE),('eios:property:Consumer/payment_method',ResourceType.PROPERTY,Operation.READ)]
    grants+=[(f"eios:property:Consumer/{f['consumer']}/payment_method",ResourceType.PROPERTY,op) for op in (Operation.READ,Operation.EDIT)]
    second=worker(f,edit_version=2,extra_targets=grants,suffix='-reflow-granted').run_pending()
    assert second[result['decision_id']]=={'status':'done','applied':1,'claims':{claim:{'outcome':'partial_match','applied':'applied'}},
        'published_refs':pub['published_refs']}
    assert obj(admin,f['consumer'])[0]['payment_method']=='花呗' and resolution(admin,claim)=='resolved'
    stored=decisions(admin)[0]
    assert stored[5]=='done' and stored[4]['applied_claim_count']==1 and stored[3]['publication']['applied_claim_count']==1
    assert human(f).awaiting()==[]


def test_gate_failure_keeps_candidate_pending_with_reason_and_no_schema_change(review):
    f=review;admin=f['admin']
    claim,d=to_review(f,'g1','会员等级','金卡','我是金卡会员等级','member_tier')
    d['property']['new']['closed_vocabulary']=True
    m,gluer=setup(f,{claim:d})
    cid=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    port=human(f);revision=candidates(admin)[cid][8]
    result=port.decide(candidate_id=cid,decision='approve',expected_revision=revision,rationale='试图发布',idempotency_key='synthetic-approve-gate-01')
    assert result['outcome']=='publication_failed' and result['reflow_status']=='none'
    assert result['publication']['gate_failures']==['eios_closed_or_enum_property_requires_values']
    row=admin.execute('select status,status_reason,revision from ontology.nexloop_candidate_definitions where candidate_id=%s',(cid,)).fetchone()
    assert row[0]=='pending_review' and 'closed_or_enum_property_requires_values' in row[1] and row[2]==revision
    assert admin.execute("select max(version) from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'",(TENANT,)).fetchone()==(1,)
    # A publication that changes more than the reviewed property is refused by the SQL gates too.
    basis=port.basis(cid);from nexloop_eios.review_actions import build_publication
    candidate=dict(basis['candidate']);candidate['proposed']=dict(candidate['proposed'],closed_vocabulary=False)
    tampered=build_publication(dict(basis,candidate=candidate))
    tampered['schema']['description']='悄悄修改了类型描述'
    payload={'decision_id':'00000000-0000-4000-8000-0000000000aa','candidate_id':cid,'decision':'approve','expected_revision':revision,'rationale':'x','publication':tampered}
    refused=port._call('nexloop_review_decide',payload,PROTOCOL)
    assert refused['outcome']=='publication_failed' and 'schema_change_not_exactly_the_reviewed_additive_property' in refused['publication']['gate_failures']
    assert admin.execute("select max(version) from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'",(TENANT,)).fetchone()==(1,)


def test_human_approve_vocabulary_value_extends_closed_enum(review):
    f=review;admin=f['admin']
    claim,d=to_review(f,'v1','预算区间','一万元以上','预算一万元以上','x')
    d=decision('eios:property:Consumer/budget_level','一万元以上')
    m,gluer=setup(f,{claim:d})
    cid=m.process_conversation(f['conversation'])['matches'][claim]['candidate_id'];gluer.process_staged()
    port=human(f)
    result=port.decide(candidate_id=cid,decision='approve',expected_revision=candidates(admin)[cid][8],rationale='新增预算档位',idempotency_key='synthetic-approve-vocab-1')
    assert result['outcome']=='published','%s'%result['publication']
    schema=admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2",(TENANT,)).fetchone()[0]
    enum=next(p for p in schema['properties'] if p['property_name']=='budget_level')['type_descriptor']['enum']
    assert enum==['两千元以内','两千到五千元','五千元以上','一万元以上']
    assert 'nexloop:vocabulary:Consumer/budget_level/一万元以上' in result['publication']['published_refs']

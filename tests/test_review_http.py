"""NX-046 review workbench HTTP through the production app wiring and a real Human browser session.

Real PG identity login (cookie), the production create_app review router and
Backend.authenticate_browser_reviewer, EIOS browser authority facts for
ontology.schema.review EXECUTE. Candidate rows are synthetic fixture rows in the
browser realm's tenant. No decision endpoint exists (NX-044).
"""
import json
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration,create_app
from authority_fixture import authority_records
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_browser_http import ORIGIN,login
from test_claim_matching_pg import Claims,oid

TENANT='synthetic-a'
REVIEW='eios:action:ontology.schema.review:1'


def grant_human(admin,uow,identity,resource):
    """Browser Human authority facts for one Action (same shape as the NX-012 browser_business fixture)."""
    binding,expiry,records=authority_records(TENANT,resource,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    subject=identity[1].subject_id;principal=identity[2].principal_id
    def convert(value):
        if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
        if isinstance(value,list):return [convert(v) for v in value]
        if value==binding.subject_id:return subject
        if value==binding.subject_principal_id:return principal
        if value=='service':return 'human'
        return value
    for name,key,fact in records:
        if name in ('subject','membership','authentication'):continue
        body=convert(fact.model_dump(mode='json'));body.pop('snapshot_digest',None)
        model=type(fact).model_validate_json(json.dumps(body))
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
            (TENANT,name,convert(key),Jsonb(model.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s) on conflict do nothing',
        (TENANT,uow.application_id,binding.caller_application_id,'1',Jsonb(['action.execute'])))


def put_candidate(admin,world,label,claims,*,status='pending_review',superseded_by=None):
    candidate_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'nx046-'+label))
    candidate={'candidate_id':candidate_id,'kind':'property','world_id':world,'status':'staged',
        'proposed':{'name':'payment_method','display_name':label,'owner_type_ref':'eios:object_type:Consumer','value_type':'string','closed_vocabulary':False,'property_group':'purchase_behavior'},
        'recall':[{'ref':'eios:property:Consumer/favorite_sport','method':'fts','score':0.21}]}
    scores={'lexical_similarity':0.1,'core_term_containment':0.0,'vector_cluster':0.2,'rule_whitelist':0.0,'weighted_total':0.06,
        'best_match_ref':'eios:property:Consumer/favorite_sport','threshold':0.375,'config_version':'nx045-glue-v1-test64'}
    admin.execute("""insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,status,candidate,dependent_claims,merge_scores,config_version,superseded_by)
        values(%s,%s,%s,'property',%s,%s,%s,%s,%s,'nx045-glue-v1-test64',%s)""",
        (TENANT,world,candidate_id,oid('key-'+label),status,Jsonb(candidate),['claim:'+c for c in claims],Jsonb(scores),superseded_by))
    return candidate_id


@pytest.fixture
def review_app(identity,uow,published_action,admin,pg,tmp_path):
    reader=published_action[0]
    def private(name,content):
        path=tmp_path/name;path.write_bytes(content if isinstance(content,bytes) else content.encode());path.chmod(0o600);return path
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',10,60,true) on conflict do nothing",(TENANT,))
    conversation=oid('nx046-conversation')
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'synthetic-human','nx046')",
        (TENANT,conversation,'c'*64))
    claims=Claims(admin,TENANT,conversation,'c'*64)
    first=claims.add('http-1','常用付款方式','花呗',kind='user_statement',quote='我一般用花呗付款')
    folded=claims.add('http-2','付款方式','花呗',kind='user_statement',quote='付款方式是花呗')
    # Claims depending on an open candidate wait for its definition (as NX-020 records them).
    admin.execute("update ontology.nexloop_claims set resolution_state='awaiting_definition' where tenant_id=%s",(TENANT,))
    pending=put_candidate(admin,'real','常用付款方式',[first,folded])
    similar=put_candidate(admin,'real','付款方式',[folded],status='superseded',superseded_by=pending)
    hidden=put_candidate(admin,'simulation','模拟世界候选',[first])
    config=ApiConfiguration(private('business-dsn',make_conninfo(pg,user='nexloop_api')),private('authority-key',reader.signer.material),
        tmp_path/'artifacts',reader.signer.key_id,BrowserConfiguration(private('identity-dsn',identity[0]),private('rate-key',secrets.token_hex(32)),
        TENANT,'synthetic-browser-app',ORIGIN),execution_profile='deterministic-test')
    (tmp_path/'artifacts').mkdir(mode=0o700)
    def client():return TestClient(create_app(config),base_url=ORIGIN)
    return dict(client=client,identity=identity,uow=uow,admin=admin,pending=pending,similar=similar,hidden=hidden,first=first,folded=folded)


def test_without_review_permission_the_queue_is_invisible(review_app):
    """AT-070 negative: an authenticated Human without ontology.schema.review gets 403 and no queue content."""
    f=review_app
    with f['client']() as client:
        assert client.get('/api/v1/review/queue').status_code==401
        assert login(client,f['identity']).status_code==200
        # No browser business application bound at all: identity cannot be verified for business → 503, no content.
        unverified=client.get('/api/v1/review/queue')
        assert unverified.status_code==503 and '常用付款方式' not in unverified.text
    # A business-enabled Human (may create Consumers) without ontology.schema.review → 403.
    grant_human(f['admin'],f['uow'],f['identity'],'eios:action:Consumer.create:1')
    with f['client']() as client:
        assert login(client,f['identity']).status_code==200
        denied=client.get('/api/v1/review/queue')
        assert denied.status_code==403 and denied.json()['code']=='forbidden' and '常用付款方式' not in denied.text
        detail=client.get('/api/v1/review/candidates/'+f['pending'])
        assert detail.status_code==403 and f['pending'] not in detail.text


def test_reviewer_sees_queue_detail_and_disabled_decisions(review_app):
    """AT-070: span, recall, scores + config version, dependent Claims, similar; decisions are the governed human Action (NX-044)."""
    f=review_app;grant_human(f['admin'],f['uow'],f['identity'],REVIEW)
    with f['client']() as client:
        assert login(client,f['identity']).status_code==200
        queue=client.get('/api/v1/review/queue')
        assert queue.status_code==200 and queue.headers['cache-control']=='no-store'
        body=queue.json()
        assert [i['candidate_id'] for i in body['items']]==[f['pending']]  # simulation and superseded are not in the real queue
        assert body['items'][0]['dependent_claim_count']==2 and body['items'][0]['config_version']=='nx045-glue-v1-test64'
        assert body['decisions']=={'enabled':True,'reason':'governed_human_review_action','actions':['approve','merge_into','reject']}
        detail=client.get('/api/v1/review/candidates/'+f['pending']).json()
        assert {e['quote'] for e in detail['evidence']}=={'我一般用花呗付款','付款方式是花呗'}
        assert detail['candidate']['recall'][0]['ref']=='eios:property:Consumer/favorite_sport' and detail['merge_scores']['weighted_total']==0.06
        assert [s['candidate_id'] for s in detail['similar']]==[f['similar']] and detail['decisions']['enabled'] is True
        # 0084 approve warning data: no owner restriction recorded yet, so no service principal would derive access.
        impact=detail['derivation_impact']
        assert impact['applies'] is True and impact['type_name']=='Consumer' and impact['restriction_configured'] is False and impact['auto_access']==[]
        assert client.get('/api/v1/review/candidates/'+f['hidden']).status_code==404
        assert client.get('/api/v1/review/candidates/not-a-uuid').status_code==422
        assert client.get('/api/v1/review/queue?limit=500').status_code==422
        assert client.get('/api/v1/review/queue',headers={'Host':'other.example'}).status_code==403


def test_reviewer_decides_through_http_with_csrf_idempotency_and_cas(review_app):
    """AT-070 decision part + AT-068: a real reject through the HTTP boundary; audit record; replay; 409 on stale revision."""
    f=review_app;grant_human(f['admin'],f['uow'],f['identity'],REVIEW)
    path=f"/api/v1/review/candidates/{f['pending']}/decisions"
    body={'decision':'reject','expected_revision':1,'rationale':'不是业务概念'}
    with f['client']() as client:
        assert login(client,f['identity']).status_code==200
        csrf=client.post('/api/v1/auth/csrf',headers={'Origin':ORIGIN}).json()['csrf_token']
        headers={'Origin':ORIGIN,'X-CSRF-Token':csrf,'Idempotency-Key':'synthetic-http-reject-0001'}
        assert client.post(path,headers={**headers,'X-CSRF-Token':'wrong'},json=body).status_code==401
        assert client.post(path,headers={k:v for k,v in headers.items() if k!='Origin'},json=body).status_code==403
        for bad in ({**body,'tenant_id':'other'},{**body,'decision':'publish'},{**body,'expected_revision':'1'},{'decision':'reject'}):
            assert client.post(path,headers=headers,json=bad).status_code==422
        assert client.post(path,headers={**headers,'Idempotency-Key':'short'},json=body).status_code==422
        decided=client.post(path,headers=headers,json=body)
        assert decided.status_code==200 and decided.json()['outcome']=='rejected' and decided.json()['record']['reviewer_ref']=='human:'+f['identity'][2].principal_id
        assert client.post(path,headers=headers,json=body).json()['replay'] is True
        assert client.post(path,headers={**headers,'Idempotency-Key':'synthetic-http-reject-0002'},json=body).status_code==409
        assert client.get('/api/v1/review/queue').json()['items']==[]
    assert f['admin'].execute('select status from ontology.nexloop_candidate_definitions where candidate_id=%s',(f['pending'],)).fetchone()[0]=='rejected'
    assert f['admin'].execute('select count(*) from ontology.nexloop_review_decisions').fetchone()[0]==1
    assert f['admin'].execute("select resolution_state from ontology.nexloop_claims where claim_id=%s",(f['first'],)).fetchone()[0]=='rejected_definition'


def test_revoked_review_grant_hides_queue_immediately(review_app):
    f=review_app;grant_human(f['admin'],f['uow'],f['identity'],REVIEW)
    with f['client']() as client:
        assert login(client,f['identity']).status_code==200
        assert client.get('/api/v1/review/queue').status_code==200
        f['admin'].execute("delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[2]=%s",(TENANT,REVIEW))
        assert client.get('/api/v1/review/queue').status_code==403


def test_reviewer_approves_through_http_and_sees_the_wait_for_grants(review_app):
    """AT-067 step 1 over HTTP: published; the dependent Claims are shown as waiting, never silent."""
    f=review_app;grant_human(f['admin'],f['uow'],f['identity'],REVIEW)
    path=f"/api/v1/review/candidates/{f['pending']}/decisions"
    with f['client']() as client:
        assert login(client,f['identity']).status_code==200
        csrf=client.post('/api/v1/auth/csrf',headers={'Origin':ORIGIN}).json()['csrf_token']
        decided=client.post(path,headers={'Origin':ORIGIN,'X-CSRF-Token':csrf,'Idempotency-Key':'synthetic-http-approve-0001'},
            json={'decision':'approve','expected_revision':1,'rationale':'新增付款方式属性'})
        assert decided.status_code==200,decided.text
        body=decided.json()
        assert body['outcome']=='published' and body['reflow_status']=='pending' and 'eios:property:Consumer/payment_method' in body['publication']['published_refs']
        waiting=client.get('/api/v1/review/awaiting')
        assert waiting.status_code==200
        assert [(i['candidate_id'],i['reflow_status'],i['waiting_claim_count']) for i in waiting.json()['items']]==[(f['pending'],'pending',2)]
    assert f['admin'].execute("select resolution_state from ontology.nexloop_claims where claim_id=%s",(f['first'],)).fetchone()[0]=='awaiting_definition'

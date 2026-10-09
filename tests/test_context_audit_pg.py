"""NX-023-C actual PostgreSQL: human-only Manifest read (owner decision; nexloop.context.audit:1).

The Action comes from the versioned business-Action manifest (trusted configuration) and
is granted to a human principal only. SQL refuses services, Agents and Run credentials
before any grant is evaluated, so even a granted service or Agent is refused. Synthetic
data only; admin seeds definitions/authority (as the other NX-023 tests do) and probes.
"""
from datetime import UTC,datetime,timedelta
import copy,json,uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz.resources import ResourceType
from nexloop_eios import business_actions
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine.audit import AUDIT_ACTION,AUDIT_CAPABILITY,ContextAuditReader,context_manifest_object_type
from nexloop_eios.context_engine.authority import ContextDenied,action_claims
from goal_fixture import TENANT,authenticate_human,seed_agent_author,seed_human_owner,seed_service
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow
from test_context_v6_pg import v6  # noqa: F401  (v6 message Run world)
from test_context_artifacts import context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=business_actions.load(ROOT/'deploy/configuration/business-actions.v1.json')
RESOURCE='eios:action:'+AUDIT_ACTION+':1'
REFUSED='human context audit authority required'


def audit_rows(tenant,capability):
    schema=context_manifest_object_type()
    rows=business_actions.compile_actions(MANIFEST,tenant=tenant,created_by='synthetic-owner',created_at=datetime.now(UTC),
        object_types=[schema.model_dump(mode='json')],select=(AUDIT_ACTION,),
        capabilities={AUDIT_CAPABILITY:capability.model_copy(update={'capability_name':AUDIT_CAPABILITY,'has_side_effects':True})})
    return schema,rows


def forged_claims(session,definition):
    """Claims any credential can sign for itself; only SQL decides whether they count."""
    auth=session.authentication
    return {'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,'credential_id':auth.credential_id,'directory_hash':session.directory_hash,
        'world':session.world,'resource_id':RESOURCE,'action_resource':RESOURCE,'operation':'execute',
        'expires_at':(datetime.now(UTC)+timedelta(seconds=20)).isoformat(),'facts':[],'definition':definition}


def refused(call):
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:call()
    return error.value.diag.message_primary


@pytest.fixture
def audit(identity,uow,published_action,admin):
    reader,base,capability=published_action
    schema,rows=audit_rows(TENANT,capability)
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s)',(TENANT,schema.type_name,1,Jsonb(schema.model_dump(mode='json'))))
    for row in rows:
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (TENANT,'real',RESOURCE,Jsonb(row['definition']),Jsonb(row['capability'])))
    browser=seed_human_owner(admin,reader.pool,[AUDIT_ACTION],identity,uow)
    agent=seed_agent_author(admin,reader.pool,[AUDIT_ACTION],suffix='-audit-agent')
    service=seed_service(admin,reader.pool,[AUDIT_ACTION],suffix='-audit-service')
    return dict(pool=reader.pool,signer=reader.signer,definition=rows[0]['definition'],human=authenticate_human(reader.pool,browser),
        agent=authenticate_service(reader.pool,agent,world='real'),service=authenticate_service(reader.pool,service,world='real'))


def test_granted_service_and_agent_are_refused_and_human_passes_every_gate(audit):
    a=audit
    for who in ('service','agent'):
        reader=ContextAuditReader(a['pool'],a[who],a['signer'])
        # Even with a current EXECUTE grant the SQL identity gate refuses first.
        assert refused(lambda:reader.manifest(uuid.uuid4(),1))==REFUSED,who
        assert refused(lambda:reader._call(forged_claims(a[who],a['definition']),uuid.uuid4(),1))==REFUSED,who
    human=ContextAuditReader(a['pool'],a['human'],a['signer'])
    # Unknown or other-tenant call: nothing to return, nothing leaked.
    assert human.manifest(uuid.uuid4(),1) is None
    # A human still needs the actual current grant: forged claims fail the authority check.
    assert refused(lambda:human._call(forged_claims(a['human'],a['definition']),uuid.uuid4(),1))!=REFUSED
    # A definition other than the published one is refused.
    tampered=copy.deepcopy(a['definition']);tampered['version']=2
    with pytest.raises(psycopg.Error):human._call({**action_claims(a['pool'],a['human'],AUDIT_ACTION),'definition':tampered},uuid.uuid4(),1)


def test_audit_action_is_human_only_in_configuration():
    action=next(x for x in MANIFEST['actions'] if x['stable_name']==AUDIT_ACTION)
    assert (action['authority'],action['executor_role'])==('human_owner','human_owner')
    grants=json.loads((ROOT/'deploy/authorization/service-grants.v1.json').read_text())
    assert not any(RESOURCE in g['resource_id'] for g in grants['grants'])


def test_human_reads_actual_v6_manifest_run_and_source_credentials_are_refused(v6,admin,tmp_path):
    """Positive path on a real v6 message Run: the human owner (granted by trusted configuration)
    receives exactly the owner-only projection; the Run's own credential and the Source are refused."""
    from fastapi.testclient import TestClient
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.browser_http import COOKIE,BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.run_credentials import AUDIENCE
    from local_message_assembly_fixture import human_declarations
    from test_context_artifacts import active_worker,reconfigure
    from test_context_v6_pg import manifest,request_snapshot
    from test_trusted_configuration_pg import private
    f=v6;o=f['original'];tenant=o['tenant']
    assert f['v6_relay']().run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model',request_snapshot=request_snapshot(1,text))
    run_id=command['run_id'];expected=manifest(admin,tenant,run_id,1);assert expected['call_sequence']==1
    pool,signer=f['backend']._pool,f['backend']._signer
    # The Run's own credential (valid only until the authority directory moves) is refused by identity.
    record=f['vault'].read(f['vault'].message_key(tenant,'real',f['message']['id']))
    run=authenticate_service(pool,record.token,world='real',run_id=run_id,audience=AUDIENCE)
    assert run.run_context is not None
    assert refused(lambda:ContextAuditReader(pool,run,signer)._call(forged_claims(run,{'stable_name':AUDIT_ACTION}),run_id,1))==REFUSED
    # Trusted configuration: ContextManifest type, the audit Action and the human's grant.
    base=next(a for a in f['manifest']['actions'] if a['definition']['stable_name']=='Consumer.create')
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    schema,rows=audit_rows(tenant,CapabilityContractSnapshot.model_validate_json(json.dumps(base['capability'])))
    human=f['f']['base']['human']
    facts,_=human_declarations(tenant,human,extra=[(RESOURCE,ResourceType.ACTION)])
    def publish(m):
        m['object_types']=m['object_types']+[schema.model_dump(mode='json')];m['actions']=m['actions']+rows
        merged={(r['kind'],tuple(r['key'])):r for r in m['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in facts})
        m['authority_facts']=list(merged.values())
    reconfigure(f,admin,publish)
    origin='https://context.invalid'
    config=ApiConfiguration(private(tmp_path,'audit-api-dsn',make_conninfo(o['pg'],user='nexloop_api')),o['paths']['backend_signing'],tmp_path/'audit-api-artifacts',
        'explicit-configuration',BrowserConfiguration(o['paths']['identity'],private(tmp_path,'audit-rate','c'*64),tenant,o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(config),base_url=origin) as client:
        assert client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()}).status_code==200
        store=client.app.state.browser_store
        operator=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id=str(uuid.uuid4()),trace_id=str(uuid.uuid4()))
        browser=BrowserSessionService(store,store,application_id=o['application'],operator=operator).inspect(client.cookies.get(COOKIE))
        owner=authenticate_browser_business(pool,browser,world='real')
        # Same bytes as the owner-only projection, which did not move when a new type was published later.
        assert ContextAuditReader(pool,owner,signer).manifest(run_id,1)==expected==manifest(admin,tenant,run_id,1)
        assert ContextAuditReader(pool,owner,signer).manifest(run_id,2) is None
    definition=rows[0]['definition']
    # The Source service (now with the audit Action published) is refused too, grant or not.
    source=f['source']._session
    assert refused(lambda:ContextAuditReader(pool,source,signer)._call(forged_claims(source,definition),run_id,1))==REFUSED
    with pytest.raises(ContextDenied):ContextAuditReader(pool,source,signer).manifest(run_id,1)

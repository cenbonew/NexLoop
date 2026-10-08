"""Actual password/browser HUMAN facts and governed Action, candidate-only."""
import json
import uuid
import pytest
import psycopg
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service,authorization_service
from nexloop_eios.browser_authorization import authenticate_browser_business,BrowserBusinessSession
from nexloop_eios.object_actions import GovernedObjectCreator
from authority_fixture import authority_records
from test_action_definitions import published_action
from test_browser_identity_reads import identity
from test_browser_session_creation import uow,create
from test_browser_evidence import authenticate


class PrivateBrowserPlan(dict):
    def __repr__(self):return "<isolated actual browser authority configuration>"
    __str__=__repr__


@pytest.fixture
def browser_business(identity,uow,published_action,admin):
    reader,definition,capability=published_action
    binding,expiry,records=authority_records('synthetic-a','eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
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
            ('synthetic-a',name,convert(key),Jsonb(model.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)',
        ('synthetic-a',uow.application_id,binding.caller_application_id,'1',Jsonb(['action.execute'])))
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    human=authenticate_browser_business(reader.pool,issued.session,world='real')
    return PrivateBrowserPlan(reader=reader,human=human,issued=issued,identity=identity,uow=uow,definition=definition,capability=capability)


def test_actual_browser_human_full_chain_creates_governed_object(browser_business,admin):
    fixture=browser_business;human=fixture['human'];reader=fixture['reader']
    assert type(human) is BrowserBusinessSession and type(human.authentication) is F.BrowserAuthenticationBinding
    assert human.authentication.subject_kind.value=='human' and human.agent_invocation is None
    decision=authorization_service(reader.pool,human).decide(human.query(resource_id='eios:action:Consumer.create:1',resource_type=ResourceType.ACTION,operation=Operation.EXECUTE))
    assert decision.allowed and decision.authoritative
    receipt=GovernedObjectCreator(reader.pool,human,reader.signer).create(action_name='Consumer.create',action_version=1,
        intent_id='browser-human-'+str(uuid.uuid4()),type_name='Consumer',properties={})
    assert admin.execute('select count(*) from ontology.objects where object_id=%s',(receipt['object_id'],)).fetchone()==(1,)
    assert admin.execute('select principal_id from runtime.nexloop_action_claims').fetchone()==(fixture['identity'][2].principal_id,)
    assert not admin.execute('select 1 from authz.nexloop_service_credentials where token_digest=%s',(human.token_digest,)).fetchone()


def test_service_authentication_cannot_swallow_browser_and_crossrealm_fails(browser_business):
    fixture=browser_business;pool=fixture['reader'].pool;session=fixture['issued'].session
    with pytest.raises(AuthorizationUnavailable):authenticate_service(pool,fixture['issued'].session_token.get_secret_value(),world='real')
    with pytest.raises(AuthorizationUnavailable):authenticate_browser_business(pool,session.model_copy(update={'tenant_id':'synthetic-b'}),world='real')
    with pytest.raises(AuthorizationUnavailable):authenticate_browser_business(pool,session,world='shadow')


@pytest.mark.parametrize('table,change',[
    ('sessions',{'revoked_at':'2026-01-01T00:00:00+00:00'}),('sessions',{'idle_expires_at':'2026-01-01T00:00:00+00:00'}),
    ('accounts',{'session_epoch':2}),('accounts',{'must_change_password':True}),('memberships',{'status':'revoked'}),('subjects',{'status':'disabled'})])
def test_browser_current_binding_revocation_denies_business(browser_business,admin,table,change):
    fixture=browser_business
    row=admin.execute('select payload from control.nexloop_browser_'+table).fetchone()[0]
    admin.execute('update control.nexloop_browser_'+table+' set payload=%s',(Jsonb({**row,**change}),))
    with pytest.raises(AuthorizationUnavailable):authenticate_browser_business(fixture['reader'].pool,fixture['issued'].session,world='real')
    with pytest.raises(AuthorizationUnavailable):
        authorization_service(fixture['reader'].pool,fixture['human']).decide(fixture['human'].query(resource_id='eios:action:Consumer.create:1',resource_type=ResourceType.ACTION,operation=Operation.EXECUTE))
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(0,)


def test_business_commit_tail_rechecks_real_browser_deadline(browser_business,admin):
    from datetime import UTC,datetime,timedelta
    fixture=browser_business;reader=fixture['reader'];deadline=datetime.now(UTC)+timedelta(seconds=2)
    session=fixture['issued'].session.model_copy(update={'idle_expires_at':deadline})
    body=admin.execute('select payload from control.nexloop_browser_sessions').fetchone()[0]
    admin.execute('update control.nexloop_browser_sessions set payload=%s',(Jsonb({**body,'idle_expires_at':deadline.isoformat()}),))
    human=authenticate_browser_business(reader.pool,session,world='real')
    admin.execute('create function public.synthetic_browser_write_delay() returns trigger language plpgsql as $$begin perform pg_sleep(3);return new;end$$')
    admin.execute('grant execute on function public.synthetic_browser_write_delay() to nexloop_owner')
    admin.execute('create trigger synthetic_browser_write_delay after insert on ontology.objects for each row execute function public.synthetic_browser_write_delay()')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        GovernedObjectCreator(reader.pool,human,reader.signer).create(action_name='Consumer.create',action_version=1,
            intent_id='browser-tail-'+str(uuid.uuid4()),type_name='Consumer',properties={})
    assert admin.execute('select count(*) from ontology.objects').fetchone()==(0,)
    assert not admin.execute("select 1 from runtime.nexloop_action_claims where claim->>'state'='terminal'").fetchone()

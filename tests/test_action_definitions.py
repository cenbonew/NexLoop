import secrets
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.errors import AuthorizationUnavailable
from authority_fixture import seed_authority
from test_postgres_action_claims import governance_inputs
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from eios.ontology.semantics import schema_contract_digest

@pytest.fixture
def published_action(admin,pg,request):
    bootstrap(admin)
    token,_=seed_authority(admin,'synthetic-a','eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    inputs=governance_inputs();definition=inputs['action_definition'];capability=inputs['capability_snapshot']
    schema=ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True,
      properties=(PropertyDefinition(property_name='preference',value_type=PropertyValueType.STRING),) if getattr(request,'param',None)=='with-preference' else ())
    ref=definition.object_types[0].model_copy(update={'schema_digest':'f'*64 if getattr(request,'param',None)=='wrong-schema-digest' else schema_contract_digest(schema)})
    governance=definition.governance.model_copy(update={'change_scope':definition.governance.change_scope.model_copy(update={'object_types':(ref,)})})
    definition=definition.model_copy(update={'object_types':(ref,),'governance':governance})
    if getattr(request,'param',None)!='missing-schema':
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s)',
          ('synthetic-a','Consumer',1,Jsonb(schema.model_dump(mode='json'))))
    if getattr(request,'param',None)=='wrong-capability':capability=capability.model_copy(update={'schema_hash':'f'*64})
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
      ('synthetic-a','real','eios:action:Consumer.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    signer=AuthoritySigner('synthetic-reader',secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(signer.key_id,signer.material))
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        session=authenticate_service(pool,token,world='real')
        yield PostgresActionDefinitionReader(pool,session,signer),definition,capability


def test_real_pg_published_contract_read(published_action):
    reader,definition,capability=published_action
    assert reader.get('Consumer.create',1)==(definition,capability)
    with reader.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from control.nexloop_action_definitions')


def test_publication_change_invalidates_old_session(published_action,admin):
    reader,_,_=published_action
    admin.execute("update control.nexloop_action_definitions set active=false")
    with pytest.raises(AuthorizationUnavailable):reader.get('Consumer.create',1)


def test_missing_published_contract_fails_closed(admin,pg):
    bootstrap(admin)
    token,_=seed_authority(admin,'synthetic-a','eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    signer=AuthoritySigner('synthetic-missing',secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(signer.key_id,signer.material))
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        reader=PostgresActionDefinitionReader(pool,authenticate_service(pool,token,world='real'),signer)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):reader.get('Consumer.create',1)


def test_wrong_signer_cannot_read_published_definition(published_action):
    reader,_,_=published_action
    forged=PostgresActionDefinitionReader(reader.pool,reader.session,AuthoritySigner(reader.signer.key_id,secrets.token_bytes(32)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):forged.get('Consumer.create',1)


@pytest.mark.parametrize('published_action',['wrong-capability'],indirect=True)
def test_mismatched_capability_snapshot_fails_closed(published_action):
    reader,_,_=published_action
    with pytest.raises(ActionAuthorizationDenied):reader.get('Consumer.create',1)


def test_published_reader_and_governor_create_actual_pg_permit(published_action):
    from nexloop_eios.action_governor import govern_published_action
    from eios.actions.models import ActionExecutionPermit
    reader,definition,_=published_action;inputs=governance_inputs()
    command=inputs['claim_request'];binding=command.binding.model_copy(update={'action_reference':definition.reference()})
    command=command.model_copy(update={'binding':binding})
    permit=govern_published_action(reader.pool,reader.session,reader.signer,claim_request=command,request=inputs['request'])
    assert type(permit) is ActionExecutionPermit
    assert permit.claim.binding.action_reference==definition.reference()


def test_forged_contract_digest_cannot_override_published_definition(published_action,admin):
    from nexloop_eios.action_governor import govern_published_action
    from eios.actions.governance import ActionGovernanceError
    reader,definition,_=published_action;inputs=governance_inputs();command=inputs['claim_request']
    ref=definition.reference().model_copy(update={'contract_digest':'f'*64})
    command=command.model_copy(update={'binding':command.binding.model_copy(update={'action_reference':ref})})
    with pytest.raises(ActionGovernanceError):
        govern_published_action(reader.pool,reader.session,reader.signer,claim_request=command,request=inputs['request'])
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==0


def test_published_version_content_is_immutable(published_action,admin):
    with pytest.raises(psycopg.errors.DataException):
        admin.execute("update control.nexloop_action_definitions set definition=definition||jsonb_build_object('created_by','synthetic-other-owner')")
    reader,definition,capability=published_action
    assert reader.get('Consumer.create',1)==(definition,capability)


@pytest.mark.parametrize('published_action',['missing-schema'],indirect=True)
def test_missing_exact_schema_blocks_bundle(published_action):
    reader,_,_=published_action
    with pytest.raises(psycopg.errors.InsufficientPrivilege):reader.get('Consumer.create',1)


def test_schema_version_is_immutable(published_action,admin):
    with pytest.raises(psycopg.errors.DataException):
        admin.execute("update ontology.object_type_versions set definition=definition||jsonb_build_object('description','synthetic-change')")
    reader,definition,capability=published_action
    assert reader.get('Consumer.create',1)==(definition,capability)


@pytest.mark.parametrize('published_action',['wrong-schema-digest'],indirect=True)
def test_exact_schema_digest_mismatch_fails_closed(published_action):
    reader,_,_=published_action
    with pytest.raises(ActionAuthorizationDenied):reader.get('Consumer.create',1)


def test_governed_consumer_create_persists_before_receipt_and_replays(published_action,admin):
    from nexloop_eios.object_actions import GovernedObjectCreator
    reader,_,_=published_action;creator=GovernedObjectCreator(reader.pool,reader.session,reader.signer)
    arguments=dict(action_name='Consumer.create',action_version=1,intent_id='synthetic-create-intent-001',type_name='Consumer',properties={})
    receipt=creator.create(**arguments)
    row=admin.execute('select world,type_name,object_id,properties from ontology.objects').fetchone()
    assert row==('real','Consumer',receipt['object_id'],{})
    assert creator.create(**arguments)==receipt
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==1
    with reader.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute("delete from ontology.objects")


def test_committed_create_survives_sigkill_before_receipt(published_action,admin,pg):
    """Actual process death, then a new restricted backend replays the intent."""
    import json
    import select
    import signal
    import subprocess
    import sys

    reader,_,_=published_action
    arguments=dict(action_name='Consumer.create',action_version=1,
      intent_id='synthetic-kill-before-receipt-001',type_name='Consumer',properties={})
    # Only synthetic fixture material travels through anonymous stdin, never argv,
    # a file, environment, logs or a production credential source.
    payload={'dsn':make_conninfo(pg,user='nexloop_api'),
      'digest':reader.session.token_digest,'world':'real',
      'key_id':reader.signer.key_id,'material':reader.signer.material.hex(),
      'arguments':arguments}
    program='''
import json,signal,sys
from nexloop_eios.assembly import open_core,verify_application_role
from nexloop_eios.authorization import _identity
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios.object_actions import GovernedObjectCreator
p=json.loads(sys.stdin.readline())
with open_core(p['dsn']) as pool:
    with pool.connection() as c,c.transaction():
        verify_application_role(c)
        session=_identity(c,p['digest'],p['world'])
    receipt=GovernedObjectCreator(pool,session,AuthoritySigner(p['key_id'],bytes.fromhex(p['material']))).create(**p['arguments'])
    if sys.argv[1]=='await-kill':
        print('committed',flush=True)
        signal.pause()
        raise SystemExit(99)
    print(json.dumps(receipt),flush=True)
'''
    child=subprocess.Popen([sys.executable,'-c',program,'await-kill'],
      stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    try:
        child.stdin.write(json.dumps(payload)+'\n');child.stdin.flush();child.stdin.close()
        assert select.select([child.stdout],[],[],20)[0], 'create did not reach post-commit boundary'
        assert child.stdout.readline()=='committed\n'
        child.kill()
        assert child.wait(timeout=10)==-signal.SIGKILL
        assert child.stdout.read()==''  # No business receipt escaped the killed process.
    finally:
        if child.poll() is None:
            child.kill();child.wait(timeout=10)
        child.stdout.close()
    row=admin.execute('select object_id,schema_version from ontology.objects').fetchone()
    assert row is not None and row[1]==1
    replay=subprocess.run([sys.executable,'-c',program,'replay'],
      input=json.dumps(payload)+'\n',capture_output=True,text=True,timeout=20)
    assert replay.returncode==0, 'fresh restricted backend replay failed'
    assert json.loads(replay.stdout)=={'object_id':row[0],'type_name':'Consumer','world':'real'}
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==1
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==1


def test_unknown_property_rejected_before_claim_or_business_write(published_action,admin):
    from nexloop_eios.object_actions import GovernedObjectCreator
    from eios.ontology.models import OntologyValidationError
    reader,_,_=published_action;creator=GovernedObjectCreator(reader.pool,reader.session,reader.signer)
    with pytest.raises(OntologyValidationError):
        creator.create(action_name='Consumer.create',action_version=1,intent_id='synthetic-invalid-001',type_name='Consumer',properties={'undeclared':'synthetic'})
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==0
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_revoke_after_governance_before_business_sql_denies_write(published_action,admin,monkeypatch):
    import nexloop_eios.object_actions as module
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    reader,_,_=published_action;original=module.hmac.new;revoked=False
    def revoke_at_signature(key,msg,*args,**kwargs):
        nonlocal revoked
        if msg.startswith(b'nexloop-object-create-v1:'):
            replace_fact(admin,'synthetic-a','grants',['synthetic-a-principal','eios:action:Consumer.create:1'],F.GrantFacts,grants=[])
            revoked=True
        return original(key,msg,*args,**kwargs)
    monkeypatch.setattr(module.hmac,'new',revoke_at_signature)
    creator=module.GovernedObjectCreator(reader.pool,reader.session,reader.signer)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        creator.create(action_name='Consumer.create',action_version=1,intent_id='synthetic-revoked-001',type_name='Consumer',properties={})
    assert revoked
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_authenticated_object_metadata_read_without_raw_select(published_action,admin):
    from nexloop_eios.object_actions import GovernedObjectCreator
    from nexloop_eios.object_reads import AuthorizedObjectReader
    reader,_,_=published_action
    receipt=GovernedObjectCreator(reader.pool,reader.session,reader.signer).create(action_name='Consumer.create',action_version=1,intent_id='synthetic-read-create-001',type_name='Consumer',properties={})
    target='eios:object:Consumer/'+receipt['object_id']
    token,_=seed_authority(admin,'synthetic-a',target,operation=Operation.READ,resource_type=ResourceType.OBJECT,identity_suffix='-reader')
    session=authenticate_service(reader.pool,token,world='real')
    projector=AuthorizedObjectReader(reader.pool,session,reader.signer)
    result=projector.get('Consumer',receipt['object_id'])
    assert result==dict(object_id=receipt['object_id'],type_name='Consumer',world='real',schema_version=1,properties={},revision=1)
    with reader.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from ontology.objects')
    with pytest.raises(AuthorizationUnavailable):projector.get('Consumer',receipt['object_id'],fields=('preference',))


def test_object_read_rechecks_revoke_before_sql(published_action,admin,monkeypatch):
    from nexloop_eios.object_actions import GovernedObjectCreator
    import nexloop_eios.object_reads as module
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    reader,_,_=published_action
    receipt=GovernedObjectCreator(reader.pool,reader.session,reader.signer).create(action_name='Consumer.create',action_version=1,intent_id='synthetic-revoke-read-001',type_name='Consumer',properties={})
    target='eios:object:Consumer/'+receipt['object_id']
    token,_=seed_authority(admin,'synthetic-a',target,operation=Operation.READ,resource_type=ResourceType.OBJECT,identity_suffix='-reader')
    projector=module.AuthorizedObjectReader(reader.pool,authenticate_service(reader.pool,token,world='real'),reader.signer)
    original=module.hmac.new
    def revoke(key,msg,*args,**kwargs):
        if msg.startswith(b'nexloop-object-read-v1:'):
            replace_fact(admin,'synthetic-a','grants',['synthetic-a-reader-principal',target],F.GrantFacts,grants=[])
        return original(key,msg,*args,**kwargs)
    monkeypatch.setattr(module.hmac,'new',revoke)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):projector.get('Consumer',receipt['object_id'])


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_property_requires_its_own_live_eios_authority(published_action,admin):
    import json,hashlib
    from authority_fixture import authority_records,replace_fact
    from eios.authz import facts as F
    from eios.authz.applications import ResourceRestriction
    from nexloop_eios.object_actions import GovernedObjectCreator
    from nexloop_eios.object_reads import AuthorizedObjectReader
    reader,_,_=published_action
    receipt=GovernedObjectCreator(reader.pool,reader.session,reader.signer).create(action_name='Consumer.create',action_version=1,intent_id='synthetic-property-create-001',type_name='Consumer',properties={'preference':'blue'})
    targets=[(ResourceType.OBJECT,'eios:object:Consumer/'+receipt['object_id']),
             (ResourceType.PROPERTY,'eios:property:Consumer/'+receipt['object_id']+'/preference')]
    scopes=frozenset({'object.read','property.read'});digest=F.canonical_authority_digest({'resources':[t for _,t in targets]})
    rows={};binding=None;expiry=None
    for kind,target in targets:
        base,expiry,records=authority_records('synthetic-a',target,operation=Operation.READ,resource_type=kind,identity_suffix='-property-reader')
        binding=base.model_copy(update={'requested_scopes':scopes,'caller_application_digest':digest})
        for name,key,fact in records:
            payload=fact.model_dump(mode='json');payload.pop('snapshot_digest',None)
            if name=='authentication':payload.update(binding.model_dump(mode='json'))
            if name=='application':
                payload.update(version_digest=digest,record_digest=digest,resources=[ResourceRestriction(tenant_id='synthetic-a',resource_type=k.value,resource_id=t).model_dump(mode='json') for k,t in targets])
            if name=='scope':payload.update(catalog_scopes=list(scopes),authorized_scopes=list(scopes))
            fact=type(fact).model_validate_json(json.dumps(payload))
            rows[(name,tuple(key))]=(name,key,fact)
    token=secrets.token_urlsafe(48)
    admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
      (hashlib.sha256(token.encode()).hexdigest(),'synthetic-a',binding.credential_id,Jsonb(binding.model_dump(mode='json')),['real'],'nexloop-core','active',expiry))
    for name,key,fact in rows.values():
        admin.execute('insert into authz.nexloop_authority_facts values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
          ('synthetic-a',name,key,Jsonb(fact.model_dump(mode='json'))))
    projector=AuthorizedObjectReader(reader.pool,authenticate_service(reader.pool,token,world='real'),reader.signer)
    assert projector.get('Consumer',receipt['object_id'])['properties']=={}
    assert projector.get('Consumer',receipt['object_id'],fields=('preference',))['properties']=={'preference':'blue'}
    replace_fact(admin,'synthetic-a','grants',[binding.subject_principal_id,targets[1][1]],F.GrantFacts,grants=[])
    refreshed=AuthorizedObjectReader(reader.pool,authenticate_service(reader.pool,token,world='real'),reader.signer)
    assert refreshed.get('Consumer',receipt['object_id'])['properties']=={}
    with pytest.raises(ActionAuthorizationDenied):refreshed.get('Consumer',receipt['object_id'],fields=('preference',))


def test_tenant_b_cannot_read_tenant_a_object_even_with_same_target_grant(published_action,admin):
    from nexloop_eios.object_actions import GovernedObjectCreator
    from nexloop_eios.object_reads import AuthorizedObjectReader
    reader,_,_=published_action
    receipt=GovernedObjectCreator(reader.pool,reader.session,reader.signer).create(action_name='Consumer.create',action_version=1,intent_id='synthetic-tenant-read-001',type_name='Consumer',properties={})
    target='eios:object:Consumer/'+receipt['object_id']
    token,_=seed_authority(admin,'synthetic-b',target,operation=Operation.READ,resource_type=ResourceType.OBJECT)
    projector=AuthorizedObjectReader(reader.pool,authenticate_service(reader.pool,token,world='real'),reader.signer)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):projector.get('Consumer',receipt['object_id'])

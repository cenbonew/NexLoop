"""Actual two Human identities, two same-name governed fresh-profile Consumer v1 objects."""
from contextlib import contextmanager
from datetime import UTC,datetime,timedelta
import json,secrets,uuid
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.applications import ResourceRestriction,OperationRestriction
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from eios.ontology.definitions import ActionDefinition
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.backend import open_backend
from nexloop_eios.trusted_configuration import apply_manifest,validate_manifest
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.browser_identity import open_browser_identity
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from nexloop_eios.conversation_messages import ConversationOwnershipRegistrar,ConversationMessagePort,CREATE,MESSAGE,READ
from test_trusted_configuration_pg import configured,identity_manifest,create_human
from test_business_setup_pg import business_plan
from local_message_assembly_fixture import human_declarations
from authority_fixture import authority_records
from test_browser_session_reads import authenticate,create
from nexloop_eios.consumer_resolution import ConsumerNameResolver
from nexloop_eios.consumer_profile import consumer_profile_schema,consumer_creation_schema,validated_profile
from test_business_setup_pg import declared_service,governance_inputs,PrivateConfiguration,private
from nexloop_eios.effect_contexts import effect_plan_schemas,CONFIGURE,registrar_schema
from nexloop_eios.conversation_messages import conversation_schemas
from nexloop_eios.bootstrap import bootstrap
from pathlib import Path
import hashlib,psycopg


@pytest.fixture
def fresh_configured(pg,admin,tmp_path):
    bootstrap(admin)
    admin.execute('set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length_on_error=0')
    admin.execute('select pg_reload_conf()')
    with psycopg.connect(pg) as clean:
        assert clean.execute('show log_parameter_max_length').fetchone()[0]=='0'
        assert clean.execute('show log_parameter_max_length_on_error').fetchone()[0]=='0'
    admin.execute('alter role nexloop_configurator login')
    tenant=str(uuid.uuid4());target='eios:artifact:explicit-configuration-source'
    binding,_,rows=authority_records(tenant,target,operation=Operation.READ,resource_type=ResourceType.ARTIFACT)
    token=secrets.token_urlsafe(48);key=secrets.token_hex(32);seal=secrets.token_hex(32)
    identityapp='explicit-local-login';expiry=(datetime.now(UTC)+timedelta(hours=1)).isoformat()
    manifest={'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':tenant,'expected_revision':0,'tenant_status':'active',
        'object_types':[],'actions':[],'functions':[],
        'authority_facts':[{'kind':kind,'key':key,'payload':fact.model_dump(mode='json')} for kind,key,fact in rows],
        'service_credentials':[{'reference':'source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry,'status':'active'}],
        'browser_applications':[{'application_id':identityapp,'active':True}],'browser_business_applications':[],
        'browser_rate_policies':[{'action':'local_login','operator_principal_id':'nexloop_identity','maximum_attempts':10,'window_seconds':60,'active':True}],
        'identity_allowances':[{'application_id':identityapp,'enabled':True,'maximum_accounts':2,'operator_label':'explicit-technical-owner','idempotency_key_digest':hashlib.sha256(bytes.fromhex(seal)).hexdigest()}]}
    paths=PrivateConfiguration(dsn=private(tmp_path,'configuration-dsn',make_conninfo(pg,user='nexloop_configurator')),signing=private(tmp_path,'signing-key',key),
        secrets=private(tmp_path,'service-secrets',json.dumps({'source':token})),identity=private(tmp_path,'identity-dsn',make_conninfo(pg,user='nexloop_identity')),
        seal=private(tmp_path,'identity-idempotency-key',seal),password=private(tmp_path,'human-password',secrets.token_urlsafe(24)))
    raw_signing=tmp_path/'backend-signing-key';raw_signing.write_bytes(bytes.fromhex(key));raw_signing.chmod(0o600);paths['backend_signing']=raw_signing
    result=apply_manifest(manifest,database_url_file=paths['dsn'],signing_key_file=paths['signing'],signing_key_id='explicit-configuration',service_secrets_file=paths['secrets'])
    yield PrivateConfiguration(pg=pg,tenant=tenant,manifest=manifest,paths=paths,result=result,token=token,application=identityapp,target=target,binding=binding)


@contextmanager
def fresh_consumer_profile_plan(configured,admin,tmp_path):
    f=configured;tenant=f['tenant'];body=identity_manifest(f);human=create_human(f,body)
    schemas={s.type_name:s for s in effect_plan_schemas()}
    schemas.update({s.type_name:s for s in conversation_schemas()})
    schemas['Consumer']=consumer_profile_schema()
    base=governance_inputs();original=base['action_definition'];original_cap=base['capability_snapshot']
    refs={name:original.object_types[0].model_copy(update={'tenant_id':tenant,'stable_name':name,'schema_digest':schema_contract_digest(schema)}) for name,schema in schemas.items()}
    actions=[]
    for name,types in {'Consumer.create':['Consumer'],'EffectControl.create':['EffectControl'],'ConsumerOwnership.create':['ConsumerOwnership'],
        CONFIGURE:['EffectControl'],'nexloop.service.request':['Consumer']}.items():
        declaration=json.loads(json.dumps(original.model_dump(mode='json')).replace('synthetic-a',tenant));declaration.pop('contract_digest',None)
        declaration['stable_name']=name;declaration['object_types']=[refs[t].model_dump(mode='json') for t in types]
        declaration['governance']['change_scope']['object_types']=declaration['object_types']
        capability_name='ontology.object.create' if name.endswith('.create') else name
        declaration['capability_binding']['capability_name']=capability_name
        if name==CONFIGURE:declaration['input_schema']=registrar_schema('configure')
        if name=='nexloop.service.request':
            declaration['governance']['change_scope']['target_systems']=['service']
            declaration['input_schema']={'type':'object','properties':{'message':{'type':'string','minLength':1}},'required':['message'],'additionalProperties':False}
        if name=='Consumer.create':
            # Deliberately permissive published nested schema tests SQL guard
            # independently of Root's complete JSONSchema admission gate.
            declaration['input_schema']={'type':'object'}
        definition=type(original).model_validate_json(json.dumps(declaration));capability=original_cap.model_copy(update={'capability_name':capability_name,'has_side_effects':True})
        actions.append({'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')})
    owner,owner_expiry,owner_facts=declared_service(tenant,['Consumer.create','EffectControl.create','ConsumerOwnership.create',CONFIGURE],'-business-owner')
    executor,executor_expiry,executor_facts=declared_service(tenant,['nexloop.service.request'],'-business-executor')
    owner_token=secrets.token_urlsafe(48);executor_token=secrets.token_urlsafe(48)
    facts={(r['kind'],tuple(r['key'])):r for r in f['manifest']['authority_facts']+owner_facts+executor_facts}
    manifest={**f['manifest'],'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0],
        'object_types':[s.model_dump(mode='json') for s in schemas.values()],'actions':actions,'authority_facts':list(facts.values()),
        'identity_allowances':[],
        'service_credentials':f['manifest']['service_credentials']+[
            {'reference':'business-owner','binding':owner.model_dump(mode='json'),'worlds':['real'],'expires_at':owner_expiry.isoformat(),'status':'active'},
            {'reference':'business-executor','binding':executor.model_dump(mode='json'),'worlds':['real'],'expires_at':executor_expiry.isoformat(),'status':'active'}]}
    f['paths']['secrets'].write_text(json.dumps({'source':f['token'],'business-owner':owner_token,'business-executor':executor_token}))
    apply_manifest(manifest,database_url_file=f['paths']['dsn'],signing_key_file=f['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=f['paths']['secrets'])
    recipe={'schema_version':'1.0','request_id':str(uuid.uuid4()),'human_principal_id':human['principal_id'],
        'control':{'budget_units':1,'valid_until':(datetime.now(UTC)+timedelta(seconds=180)).isoformat()}}
    material={'database-url-file':make_conninfo(f['pg'],user='nexloop_api'),'owner-credential-file':owner_token,
        'executor-credential-file':executor_token,'recipe-file':json.dumps(recipe)}
    argv=[];paths={}
    for name,value in material.items():
        paths[name]=private(tmp_path,name,value);argv+=['--'+name,str(paths[name])]
    argv+=['--signing-key-file',str(f['paths']['backend_signing']),'--signing-key-id','explicit-configuration','--artifact-root',str(tmp_path/'setup-artifacts')]
    yield PrivateConfiguration(f=f,argv=argv,recipe=recipe,paths=paths,human=human,owner_token=owner_token,executor_token=executor_token,manifest=manifest)


def declaration(tenant,specs,suffix):
    records={};scopes={kind.value+'.'+operation.value for target,kind,operation in specs}
    for target,kind,operation in specs:
        binding,expiry,rows=authority_records(tenant,target,operation=operation,resource_type=kind,identity_suffix=suffix)
        for name,key,fact in rows:records[name,tuple(key)]=(name,key,fact)
    app=next(row[2] for row in records.values() if row[0]=='application')
    resources=tuple(ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=target) for target,kind,op in specs)
    digest=F.canonical_authority_digest({'resources':[r.model_dump(mode='json') for r in resources]})
    app=app.model_copy(update={'resources':resources,'operations':tuple(OperationRestriction(operation=op) for op in set(op for target,kind,op in specs)),'version_digest':digest,'record_digest':digest})
    binding=binding.model_copy(update={'caller_application_digest':digest,'requested_scopes':frozenset(scopes)})
    role=next(row[2].roles[0] for row in records.values() if row[0]=='subject_authority')
    rows=[]
    for name,key,fact in records.values():
        if name=='application':fact=app
        if name=='authentication':fact=F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        if name=='scope':fact=fact.model_copy(update={'catalog_scopes':frozenset(scopes),'authorized_scopes':frozenset(scopes)})
        if name=='grants':fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role.digest}) for g in fact.grants)})
        body=fact.model_dump(mode='json');body.pop('snapshot_digest',None);fact=type(fact).model_validate_json(json.dumps(body));rows.append({'kind':name,'key':key,'payload':fact.model_dump(mode='json')})
    return binding,expiry,rows


def publish(f,admin,manifest):
    p=f['paths'];manifest={**manifest,'manifest_id':str(uuid.uuid4()),'expected_revision':admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()[0]}
    assert validate_manifest(manifest), 'typed manifest preflight failed'
    apply_manifest(manifest,database_url_file=p['dsn'],signing_key_file=p['signing'],signing_key_id='explicit-configuration',service_secrets_file=p['secrets']);return manifest


def session(f,human):
    with open_browser_identity(make_conninfo(f['pg'],user='nexloop_identity')) as pool:
        uow=PostgresBrowserSessionUnitOfWork(pool,tenant_id=f['tenant'],application_id=f['application'])
        identity=(make_conninfo(f['pg'],user='nexloop_identity'),uow.get_subject(human['subject_id']),uow.get_membership(f['tenant'],human['principal_id']),uow.get_local_account(f['tenant'],human['account_id']),f['paths']['password'].read_text())
        return create(uow,identity,authenticate(uow,identity).evidence).session


@pytest.mark.parametrize('revoke_kind',['object','display_name','locale','timezone','lifecycle_status','external_id_refs','contact_preferences'])
def test_two_real_same_name_consumers_preserve_ambiguous_candidates_and_session_ownership(fresh_configured,admin,tmp_path,revoke_kind):
    f=fresh_configured;t=f['tenant']
    with fresh_consumer_profile_plan(f,admin,tmp_path) as plan:
        original=plan['manifest'];base=next(a for a in original['actions'] if a['definition']['stable_name']=='Consumer.create')
        schema=consumer_profile_schema()
        schemas={s['type_name']:s for s in original['object_types']}
        actions=list(original['actions'])
        for name,version,type_name,object_version in [(CREATE,1,'Conversation',1),(MESSAGE,1,'Message',1),(READ,1,'Conversation',1)]:
            body=json.loads(json.dumps(base['definition']));body.pop('contract_digest',None);body.update(stable_name=name,version=version)
            ref={**body['object_types'][0],'stable_name':type_name,'version':object_version,'schema_digest':schema_contract_digest(schema if type_name=='Consumer' else ObjectTypeDefinition.model_validate_json(json.dumps(schemas[type_name])))}
            body['object_types']=[ref];body['governance']['change_scope']['object_types']=[ref]
            capability_name='ontology.object.create' if name.endswith('.create') else name;body['capability_binding']['capability_name']=capability_name
            definition=ActionDefinition.model_validate_json(json.dumps(body));actions.append({'definition':definition.model_dump(mode='json'),'capability':{**base['capability'],'capability_name':capability_name,'has_side_effects':True}})
        specs=[('eios:action:Consumer.create:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:ConsumerOwnership.create:1',ResourceType.ACTION,Operation.EXECUTE)]
        binding,expiry,rows=declaration(t,specs,'-identity-registrar');token=secrets.token_urlsafe(48)
        secrets_map=json.loads(f['paths']['secrets'].read_text());secrets_map['identity-registrar']=token;f['paths']['secrets'].write_text(json.dumps(secrets_map))
        manifest={**original,'object_types':original['object_types'],'actions':actions,'authority_facts':list({(r['kind'],tuple(r['key'])):r for r in original['authority_facts']+rows}.values()),'service_credentials':original['service_credentials']+[{'reference':'identity-registrar','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry.isoformat(),'status':'active'}],'identity_allowances':[{**f['manifest']['identity_allowances'][0],'maximum_accounts':2}]}
        manifest=publish(f,admin,manifest)
        second_input=identity_manifest(f);second_input['account']['username']='explicit-second-user';second=create_human(f,second_input);humans=[plan['human'],second]
        merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']}
        for human in humans:
            rows,app=human_declarations(t,human);merged.update({(r['kind'],tuple(r['key'])):r for r in rows})
        manifest=publish(f,admin,{**manifest,'authority_facts':list(merged.values()),'identity_allowances':[],'browser_business_applications':[{'application_id':f['application'],'caller_application_id':app,'application_version':'1','requested_scopes':['action.execute','function.execute']}]})
        with open_backend(database_url=make_conninfo(f['pg'],user='nexloop_api'),artifact_root=tmp_path/'artifacts',signing_key_file=f['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
            creator_session=authenticate_service(backend._pool,token,world='real');creator=GovernedObjectCreator(backend._pool,creator_session,backend._signer)
            ids=[creator.create(action_name='Consumer.create',action_version=1,intent_id='same-name-consumer-'+str(i),type_name='Consumer',properties=validated_profile({'display_name':'同名测试用户','locale':'zh-CN','timezone':'Asia/Shanghai','lifecycle_status':'display-only','external_id_refs':[],'contact_preferences':[]}))['object_id'] for i in range(2)]
            registrar=ConversationOwnershipRegistrar(backend._pool,creator_session,backend._signer)
            for i,human in enumerate(humans):registrar.register_consumer_owner(consumer_id=ids[i],principal_id=human['principal_id'],idempotency_key='own-distinct-consumer-'+str(i))
            readerspecs=[(target,kind,Operation.READ) for cid in ids for target,kind in [(f'eios:object:Consumer/{cid}',ResourceType.OBJECT)]+[(f'eios:property:Consumer/{cid}/{field}',ResourceType.PROPERTY) for field in ['display_name','locale','timezone','lifecycle_status','external_id_refs','contact_preferences']]]
            rb,re,recs=declaration(t,readerspecs,'-identity-reader');rtoken=secrets.token_urlsafe(48);secrets_map['identity-reader']=rtoken;f['paths']['secrets'].write_text(json.dumps(secrets_map))
            manifest=publish(f,admin,{**manifest,'authority_facts':list({(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']+recs}.values()),'service_credentials':manifest['service_credentials']+[{'reference':'identity-reader','binding':rb.model_dump(mode='json'),'worlds':['real'],'expires_at':re.isoformat(),'status':'active'}]})
            edit_body=json.loads(json.dumps(base['definition']));edit_body.pop('contract_digest',None)
            edit_body['stable_name']='Consumer.edit';edit_body['capability_binding']['capability_name']='ontology.object.edit';edit_body['input_schema']={'type':'object'}
            edit_definition=ActionDefinition.model_validate_json(json.dumps(edit_body))
            edit_specs=[('eios:action:Consumer.edit:1',ResourceType.ACTION,Operation.EXECUTE)]+[(target,kind,Operation.EDIT) for target,kind,op in readerspecs]
            eb,ee,ef=declaration(t,edit_specs,'-profile-editor');etoken=secrets.token_urlsafe(48);secrets_map['profile-editor']=etoken;f['paths']['secrets'].write_text(json.dumps(secrets_map))
            manifest=publish(f,admin,{**manifest,'actions':manifest['actions']+[{'definition':edit_definition.model_dump(mode='json'),'capability':{**base['capability'],'capability_name':'ontology.object.edit'}}],'authority_facts':list({(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']+ef}.values()),'service_credentials':manifest['service_credentials']+[{'reference':'profile-editor','binding':eb.model_dump(mode='json'),'worlds':['real'],'expires_at':ee.isoformat(),'status':'active'}]})
            from nexloop_eios.object_edits import GovernedObjectEditor
            editor=GovernedObjectEditor(backend._pool,authenticate_service(backend._pool,etoken,world='real'),backend._signer)
            creator=GovernedObjectCreator(backend._pool,authenticate_service(backend._pool,token,world='real'),backend._signer)
            reader=AuthorizedObjectReader(backend._pool,authenticate_service(backend._pool,rtoken,world='real'),backend._signer)
            ports=[ConversationMessagePort(backend._pool,authenticate_browser_business(backend._pool,session(f,h),world='real'),backend._signer) for h in humans]
            conversations=[port.create_conversation(idempotency_key='same-name-real-conversation-'+str(i)) for i,port in enumerate(ports)]
            assert reader.get('Consumer',ids[0])['properties']=={}
            projected=reader.get('Consumer',ids[0],fields=('locale','timezone','lifecycle_status','external_id_refs','contact_preferences'))
            assert projected['properties']=={'locale':'zh-CN','timezone':'Asia/Shanghai','lifecycle_status':'display-only','external_id_refs':[],'contact_preferences':[]}
            protected_before=admin.execute('select object_id,nexloop_revision,properties from ontology.objects where type_name=%s order by object_id',('Consumer',)).fetchall()
            assert len(protected_before)==2
            for invalid in ['+08:00','/etc/passwd','../UTC','UTC+8','Mars/Olympus']:
                with pytest.raises(psycopg.errors.CheckViolation) as rejected:creator.create(action_name='Consumer.create',action_version=1,intent_id='invalid-timezone-'+hashlib.sha256(invalid.encode()).hexdigest()[:12],type_name='Consumer',properties={'timezone':invalid})
                assert rejected.value.diag.message_primary=='consumer_profile_timezone_invalid'
                with pytest.raises(psycopg.errors.CheckViolation) as edited:editor.edit(action_name='Consumer.edit',action_version=1,intent_id='invalid-edit-zone-'+hashlib.sha256(invalid.encode()).hexdigest()[:12],type_name='Consumer',object_id=ids[0],expected_revision=1,properties={'timezone':invalid})
                assert edited.value.diag.message_primary=='consumer_profile_timezone_invalid'
            for field in ['external_id_refs','contact_preferences']:
                with pytest.raises(psycopg.errors.CheckViolation) as rejected:creator.create(action_name='Consumer.create',action_version=1,intent_id='unverified-'+field,type_name='Consumer',properties={field:['caller-asserted-verified-ref']})
                assert rejected.value.diag.message_primary=='consumer_profile_verified_refs_unavailable'
                with pytest.raises(psycopg.errors.CheckViolation) as edited:editor.edit(action_name='Consumer.edit',action_version=1,intent_id='unverified-edit-'+field,type_name='Consumer',object_id=ids[0],expected_revision=1,properties={field:['caller-asserted-verified-ref']})
                assert edited.value.diag.message_primary=='consumer_profile_verified_refs_unavailable'
            assert admin.execute('select object_id,nexloop_revision,properties from ontology.objects where type_name=%s order by object_id',('Consumer',)).fetchall()==protected_before
            before=admin.execute('select object_id,nexloop_revision,properties from ontology.objects where type_name=%s order by object_id',('Consumer',)).fetchall()
            for i,port in enumerate(ports):
                result=ConsumerNameResolver(reader,port).resolve('同名测试用户',tuple(ids))
                assert result.status=='ambiguous' and result.candidates==tuple(ids) and result.verified_session_consumer==ids[i] and result.automatic_merge is False
                assert port.list_conversations()['items'][0]['consumer_id']==ids[i]
            assert len(set(ids))==2 and len({c['id'] for c in conversations})==2
            assert admin.execute('select object_id,nexloop_revision,properties from ontology.objects where type_name=%s order by object_id',('Consumer',)).fetchall()==before

            from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
            from eios.authz.errors import AuthorizationUnavailable
            resolver=ConsumerNameResolver(reader,ports[0])
            no_match=resolver.resolve('同名测试用户 ',tuple(ids))
            assert no_match.status=='no_match' and no_match.candidates==() and no_match.verified_session_consumer==ids[0]
            with pytest.raises(TypeError):resolver.resolve('同名测试用户',tuple(ids),bound_consumer=ids[1])
            target=f'eios:object:Consumer/{ids[1]}' if revoke_kind=='object' else f'eios:property:Consumer/{ids[1]}/{revoke_kind}'
            changed=json.loads(json.dumps(manifest));affected=0
            for record in changed['authority_facts']:
                if record['kind']=='grants' and record['key']==[rb.subject_principal_id,target]:
                    record['payload']['grants']=[];record['payload'].pop('snapshot_digest',None);affected+=1
            assert affected==1
            publish(f,admin,changed)
            with pytest.raises((ActionAuthorizationDenied,AuthorizationUnavailable)):
                reader.get('Consumer',ids[1],fields=('display_name','locale','timezone','lifecycle_status','external_id_refs','contact_preferences'))
            assert admin.execute('select object_id,nexloop_revision,properties from ontology.objects where type_name=%s order by object_id',('Consumer',)).fetchall()==before

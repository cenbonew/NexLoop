"""Actual technical publisher→Human Message→governed Plan/Run→Context Artifact.
No business SQL seed or successful authorization callback. Generated authority
records are typed explicit configuration input, not runtime authorization.
"""
from datetime import UTC,datetime,timedelta
import copy,hashlib,json,uuid
from pathlib import Path
import pytest,psycopg
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.applications import ResourceRestriction,OperationRestriction
from eios.ontology.definitions import ActionDefinition
from authority_fixture import authority_records
from local_message_assembly_fixture import assembled_message,business_plan,configured
from test_trusted_configuration_pg import private,PrivateConfiguration
from nexloop_eios.trusted_configuration import apply_manifest
from nexloop_eios.backend import open_backend
from nexloop_eios.http_api import ApiConfiguration,create_app
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.message_relay import MessageRelay,MessageRelayUnavailable
from nexloop_eios.run_credential_vault import RunCredentialVault
from nexloop_eios.context_artifacts import ACTION,context_binding_schema,ContextArtifactProducer,ContextArtifactUnavailable
from nexloop_eios.runtime_dispatch import RuntimeDispatcher,HostControlConfiguration


def source_declarations(tenant,catalog_targets=(),*,custom_specs=None,identity_suffix="-assembly-source"):
    specs=[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:'+ACTION+':1',ResourceType.ACTION,Operation.EXECUTE),
     ('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.CREATE),('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.READ)]
    if custom_specs is not None:specs=list(custom_specs)
    specs.extend((target,kind,Operation.READ) for target,kind in catalog_targets)
    records={};scopes={kind.value+'.'+op.value for unused,kind,op in specs}
    for target,kind,op in specs:
        binding,expiry,rows=authority_records(tenant,target,operation=op,resource_type=kind,operations=(Operation.CREATE,Operation.READ) if kind is ResourceType.ARTIFACT else None,identity_suffix=identity_suffix)
        for name,key,fact in rows:records[name,tuple(key)]=(name,key,fact)
    if catalog_targets:scopes.update(('object.read','property.read'))
    app=next(row[2] for row in records.values() if row[0]=='application')
    resources=[ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=target) for target,kind in sorted(set((t,k) for t,k,_ in specs),key=lambda p:p[0])]
    digest=F.canonical_authority_digest({'resources':[r.model_dump(mode='json') for r in resources]})
    app=app.model_copy(update={'application_id':app.application_id+':context','resources':tuple(resources),'operations':tuple(OperationRestriction(operation=op) for op in sorted({op for unused,kind,op in specs},key=lambda value:value.value)),'version_digest':digest,'record_digest':digest})
    binding=binding.model_copy(update={'credential_id':binding.credential_id+':context','caller_application_id':app.application_id,'caller_application_digest':digest,'requested_scopes':frozenset(scopes)})
    role=next(row[2].roles[0] for row in records.values() if row[0]=='subject_authority')
    for index,(name,key,fact) in list(records.items()):
        if name=='application':fact=app
        if name=='authentication':fact=F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=expiry,repository_witness=fact.repository_witness)
        if name=='scope':fact=fact.model_copy(update={'catalog_scopes':frozenset(scopes),'authorized_scopes':frozenset(scopes)})
        if name=='grants':fact=fact.model_copy(update={'grants':tuple(g.model_copy(update={'role_digest':role.digest}) for g in fact.grants)})
        body=fact.model_dump(mode='json');body.pop('snapshot_digest',None)
        if name=='application':key=[app.application_id,'1']
        if name=='authentication':key=[binding.credential_id]
        records[index]=(name,key,type(fact).model_validate_json(json.dumps(body)))
    return binding,[{'kind':name,'key':key,'payload':fact.model_dump(mode='json')} for name,key,fact in records.values()]

@pytest.fixture
def context_message(assembled_message,admin,tmp_path):
    f=assembled_message;o=f['original'];tenant=o['tenant'];manifest=copy.deepcopy(f['manifest'])
    base=next(a for a in manifest['actions'] if a['definition']['stable_name']=='nexloop.plan.bind_effect_context')
    body=copy.deepcopy(base['definition']);body.pop('contract_digest',None);body['stable_name']=ACTION
    body['input_schema']=context_binding_schema();body['capability_binding']['capability_name']=ACTION
    definition=ActionDefinition.model_validate_json(json.dumps(body));manifest['actions'].append({'definition':definition.model_dump(mode='json'),'capability':{**base['capability'],'capability_name':ACTION}})
    # Candidate catalog maintenance uses its own real governed service identity.
    from nexloop_eios.service_offerings import offering_schemas,json_export_example,OFFERING_FIELDS,BINDING_FIELDS
    from test_business_setup_pg import declared_service
    from test_postgres_action_claims import governance_inputs
    from eios.ontology.semantics import schema_contract_digest
    from nexloop_eios.trusted_configuration import apply_manifest
    import secrets
    inputs=governance_inputs();original=inputs['action_definition'];cap=inputs['capability_snapshot']
    schemas=offering_schemas()
    existing_types={(row['type_name'],row['version']):row for row in manifest['object_types']}
    for schema in schemas:
        assert existing_types[schema.type_name,schema.version]==schema.model_dump(mode='json')
    existing_actions={(row['definition']['stable_name'],row['definition']['version']) for row in manifest['actions']}
    for schema in schemas:
        for operation in ('edit',):
            name=schema.type_name+'.'+operation
            if (name,1) in existing_actions:continue
            d=json.loads(json.dumps(original.model_dump(mode='json')).replace('synthetic-a',tenant));d.pop('contract_digest',None)
            ref={**d['object_types'][0],'stable_name':schema.type_name,'schema_digest':schema_contract_digest(schema)}
            d['stable_name']=name;d['object_types']=[ref];d['governance']['change_scope']['object_types']=[ref]
            capability=cap.model_dump(mode='json')
            if operation=='edit':d['capability_binding']['capability_name']='ontology.object.edit';capability['capability_name']='ontology.object.edit'
            manifest['actions'].append({'definition':type(original).model_validate_json(json.dumps(d)).model_dump(mode='json'),'capability':capability})
    # Reuse the real governed catalog created by assembled_message.
    offering={'type_name':'ServiceOffering','object_id':f['recipe']['offering_id']}
    link={'type_name':'ConsumerServiceOffering','object_id':f['recipe']['offering_binding_id']}
    catalog_targets=[]
    for obj,fields in ((offering,OFFERING_FIELDS),(link,BINDING_FIELDS)):
        target=obj['type_name']+'/'+obj['object_id'];catalog_targets.append(('eios:object:'+target,ResourceType.OBJECT))
        catalog_targets.extend(('eios:property:'+target+'/'+field,ResourceType.PROPERTY) for field in fields)
    edit_specs=[('eios:action:'+schema.type_name+'.edit:1',ResourceType.ACTION,Operation.EXECUTE) for schema in schemas]
    edit_specs.extend((target,kind,Operation.EDIT) for target,kind in catalog_targets)
    edit_binding,edit_rows=source_declarations(tenant,custom_specs=edit_specs,identity_suffix='-catalog-maintainer')
    edit_token=secrets.token_urlsafe(48)
    expiry=next(row['payload']['expires_at'] for row in edit_rows if row['kind']=='authentication')
    merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in edit_rows});manifest['authority_facts']=list(merged.values())
    manifest['service_credentials'].append({'reference':'catalog-editor','binding':edit_binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry,'status':'active'})
    secret_map=json.loads(o['paths']['secrets'].read_text());secret_map['catalog-editor']=edit_token;o['paths']['secrets'].write_text(json.dumps(secret_map))
    binding,rows=source_declarations(tenant,catalog_targets)
    f['recipe'].update(offering_id=offering['object_id'],offering_binding_id=link['object_id'])
    merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in rows});manifest['authority_facts']=list(merged.values())
    import secrets
    new_source=secrets.token_urlsafe(48)
    manifest['service_credentials'].append({'reference':'context-source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':next(c['expires_at'] for c in manifest['service_credentials'] if c['reference']=='assembly-source'),'status':'active'})
    secret_map=json.loads(o['paths']['secrets'].read_text());secret_map['context-source']=new_source;o['paths']['secrets'].write_text(json.dumps(secret_map))
    manifest.update(manifest_id=str(uuid.uuid4()),expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0])
    from nexloop_eios.trusted_configuration import validate_manifest
    validate_manifest(manifest)
    apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=o['paths']['secrets'])
    origin='https://context.invalid';dsn=private(tmp_path,'context-api-dsn',make_conninfo(o['pg'],user='nexloop_api'))
    rate=private(tmp_path,'context-rate-key','a'*64)
    config=ApiConfiguration(dsn,o['paths']['backend_signing'],tmp_path/'context-api-artifacts','explicit-configuration',
     BrowserConfiguration(o['paths']['identity'],rate,tenant,o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(config),base_url=origin) as client:
        logged=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()});assert logged.status_code==200
        headers={'Origin':origin,'X-CSRF-Token':logged.json()['csrf_token'],'Idempotency-Key':'context-conversation-key'}
        conversation=client.post('/api/v1/conversations',headers=headers,json={});assert conversation.status_code==200
        headers['Idempotency-Key']='context-message-key'
        response=client.post('/api/v1/conversations/'+conversation.json()['id']+'/messages',headers=headers,json={'body':'用户原文不是正式事实。'});assert response.status_code==202
        message=response.json()['message']
    root=tmp_path/'context-artifacts';vault_root=tmp_path/'context-vault';vault_root.mkdir(mode=0o700)
    with open_backend(database_url=dsn.read_text(),artifact_root=root,signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend,RunCredentialVault(vault_root) as vault:
        route=backend.authenticate(f['tokens']['assembly-route'],world='real');source=backend.authenticate(new_source,world='real');planner=backend.authenticate(f['tokens']['assembly-planner'],world='real')
        relay=MessageRelay(route=route,source=source,planner=planner,executor_token=f['tokens']['assembly-executor'],vault=vault,recipe=f['recipe'])
        candidate=PrivateConfiguration(f=f,original=o,manifest=manifest,message=message,relay=relay,backend=backend,source=source,route=route,planner=planner,root=root,vault=vault,dsn=dsn,source_token=new_source,editor=backend.authenticate(edit_token,world='real'))
        from context_copy_authority_fixture import configure_message_read
        configure_message_read(candidate,admin,allow=True)
        yield candidate


def test_real_pack_artifact_and_queue_input_persist_before_ack(context_message,admin):
    f=context_message
    assert f['relay'].run_once()=='queued'
    row=admin.execute('select artifact_id,namespace,pack_text,pack_digest,run_id from runtime.nexloop_context_artifact_bindings').fetchone()
    body=json.loads(row[2]);assert body['user_statement']['body']==f['message']['body'] and body['user_statement']['message_id']==f['message']['id']
    assert len(body['formal_facts'])==4 and {x['type'] for x in body['formal_facts']}=={'Consumer','Goal','PlanStep','EffectControl'}
    assert body['bindings']['namespace']==row[1] and body['bindings']['artifact_id']==row[0]
    assert f['source'].read_artifact(row[0])==row[2].encode()
    job=admin.execute('select normalized_input from runtime.jobs').fetchone()[0]
    assert job['input']==row[2] and job['run_command']['context_manifest_ref']=='artifact:'+row[0]
    assert hashlib.sha256(job['input'].encode()).hexdigest()==row[3]
    assert admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('delivered',)
    assert f['relay'].run_once()=='idle'
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(1,)

from contextlib import contextmanager
from nexloop_eios.authorization import AuthorizationUnavailable

@contextmanager
def active_worker(f,tmp_path):
    with open_backend(database_url=make_conninfo(f['original']['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'worker-artifacts',
     signing_key_file=f['original']['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        worker=backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
        job=worker.claim_task(queue='operations',lease_seconds=60);assert job is not None
        command=job['payload']['run_command'];text=job['payload']['input']
        activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=command['run_id'],command=command,input=text,owner_epoch=1)
        yield worker,activation,command,text


def reconfigure(f,admin,transform):
    manifest=copy.deepcopy(f['manifest']);transform(manifest)
    manifest.update(manifest_id=str(uuid.uuid4()),expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['original']['tenant'],)).fetchone()[0])
    o=f['original'];apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=o['paths']['secrets'])
    f['manifest']=manifest
    backend=f['backend'];f['source']=backend.authenticate(f['source_token'],world='real');f['route']=backend.authenticate(f['f']['tokens']['assembly-route'],world='real');f['planner']=backend.authenticate(f['f']['tokens']['assembly-planner'],world='real')
    f['relay']=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],recipe=f['f']['recipe'])


def revoke_scope(f,admin,scope):
    target='eios:action:'+ACTION+':1' if scope=='action.execute' else 'eios:artifact:local_real'
    operation=scope.split('.')[1]
    def change(manifest):
        for row in manifest['authority_facts']:
            if row['kind']=='grants' and row['key']==[f['source']._session.authentication.subject_principal_id,target]:
                if scope=='action.execute':row['payload']['grants']=[]
                else:
                    for grant in row['payload']['grants']:grant['operations']=[op for op in grant['operations'] if op!=operation]
                row['payload'].pop('snapshot_digest',None)
    reconfigure(f,admin,change)


def test_actual_model_tool_guard_checks_artifact_read_revocation(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        result=worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model')
        assert result['authorized'] is True and result['ever_execution_authorized'] is True
        receipt=worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'actual guarded service'})
        assert receipt['receipt']['state']=='accepted'
        revoke_scope(f,admin,'artifact.read')
        worker=worker._backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
        for operation in ('model','tool'):
            with pytest.raises(AuthorizationUnavailable):worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation=operation)
        from nexloop_eios.effect_intents import EffectIntentUnavailable
        with pytest.raises(EffectIntentUnavailable):worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'forbidden after revocation'})
        assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)

@pytest.mark.parametrize('scope',['artifact.create','artifact.read','action.execute'])
def test_missing_current_permission_never_acknowledges(context_message,admin,scope):
    f=context_message;revoke_scope(f,admin,scope)
    with pytest.raises(MessageRelayUnavailable):f['relay'].run_once()
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
    assert admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('pending',)


def test_runtime_pack_and_reference_tampering_rejected(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        for changed in ({**command,'context_manifest_ref':'artifact:'+'0'*32},{**command,'tenant_id':str(uuid.uuid4())},{**command,'world_id':'simulation'}):
            with pytest.raises(AuthorizationUnavailable):worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=changed,operation='model')
        with pytest.raises(AuthorizationUnavailable):worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='resume',input=text+' ')
        assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()==(0,)


def test_available_artifact_corruption_detected_not_memory_fallback(context_message,admin):
    f=context_message;assert f['relay'].run_once()=='queued'
    artifact,namespace=admin.execute('select artifact_id,namespace from runtime.nexloop_context_artifact_bindings').fetchone()
    path=f['root']/namespace/artifact;path.write_bytes(b'corrupt')
    from nexloop_eios.local_artifacts import ArtifactIntegrityError
    with pytest.raises(ArtifactIntegrityError):f['source'].read_artifact(artifact)


def test_direct_binding_tables_and_old_activation_inner_are_denied(context_message):
    f=context_message
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_scheduler','nexloop_action_worker','nexloop_identity'):
        with psycopg.connect(make_conninfo(f['original']['pg'],user=role),autocommit=True) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from runtime.nexloop_context_artifact_bindings')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select authz.nexloop_runtime_activation_command_v0051(null,null,null,null,null)')

@pytest.mark.parametrize('fault',['status','expiry','sha256'])
def test_current_artifact_metadata_denies_model_tool_and_rolls_back_marker(context_message,admin,tmp_path,fault):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        # Infrastructure fault in the owned disposable fixture only. No business
        # authority/object is seeded or mutated to authorize a positive path.
        statements={'status':"update runtime.nexloop_local_artifacts set status='deleted'",'expiry':"update runtime.nexloop_local_artifacts set retention_until=clock_timestamp()-interval '1 second'",'sha256':"update runtime.nexloop_local_artifacts set sha256=repeat('0',64)"}
        admin.execute(statements[fault])
        for operation in ('model','tool'):
            with pytest.raises(AuthorizationUnavailable):worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation=operation)
        assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()==(0,)


def test_opaque_guard_projection_has_only_actual_artifact_attestation(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        result=worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model')
        assert {'authorized','run_id','ever_execution_authorized','context_artifact'}<=set(result)
        assert not any(key.startswith('_') for key in result)
        assert set(result['context_artifact'])=={'artifact_ref','sha256','command_binding_digest'}
        artifact=result['context_artifact'];assert artifact['artifact_ref']==command['context_manifest_ref']
        assert artifact['sha256']==hashlib.sha256(text.encode()).hexdigest()
        assert artifact['command_binding_digest']==json.loads(text)['bindings']['command_digest']
        assert f['source_token'] not in json.dumps(result) and 'source_digest' not in json.dumps(result)
        from test_agent_host import files
        from nexloop_eios.runtime_control import create_runtime_guard_server
        import threading,httpx,secrets
        tls=tmp_path/'guard-tls';tls.mkdir(mode=0o700);files(tls)
        key=private(tmp_path,'guard-transport-key',secrets.token_hex(32))
        server=create_runtime_guard_server(worker,port=0,key_file=key,certificate_file=tls/'host-cert.pem',tls_key_file=tls/'host-key.pem')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with httpx.Client(verify=str(tls/'host-cert.pem'),trust_env=False,timeout=5) as client:
                response=client.post('https://127.0.0.1:'+str(server.server_port)+'/internal/v1/runtime/authorize',headers={'Authorization':'Bearer '+key.read_text()},
                 json={'activation_ref':activation['activation_ref'],'command':command,'operation':'model'})
                assert response.status_code==200
                projected=response.json();assert set(projected)=={'authorized','run_id','ever_execution_authorized','context_artifact'}
                assert projected['context_artifact']==artifact
                assert key.read_text() not in response.text and f['source_token'] not in response.text
        finally:
            server.shutdown();server.server_close();thread.join(5);assert not thread.is_alive()



def test_foreign_real_authenticated_worker_cannot_authorize_original_context(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    foreign=str(uuid.uuid4());original=f['original']['tenant']
    from test_business_setup_pg import declared_service
    from nexloop_eios.postgres_artifacts import canonical_payload
    binding,expires,rows=declared_service(foreign,['NexLoop.queue.operations'],'-foreign-context-worker')
    import secrets
    token=secrets.token_urlsafe(48)
    base=next(a for a in f['manifest']['actions'] if a['definition']['stable_name']=='NexLoop.queue.operations')
    action=json.loads(json.dumps(base).replace(original,foreign))
    types=json.loads(json.dumps(f['manifest']['object_types']).replace(original,foreign))
    manifest={'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':foreign,'expected_revision':0,'tenant_status':'active',
     'object_types':types,'actions':[action],'functions':[],'authority_facts':rows,'service_credentials':[{'reference':'foreign-worker','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expires.isoformat(),'status':'active'}],
     'browser_applications':[],'browser_business_applications':[],'browser_rate_policies':[],'identity_allowances':[]}
    secret_file=private(tmp_path,'foreign-worker-secrets',json.dumps({'foreign-worker':token}))
    o=f['original'];apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=secret_file)
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        foreign_worker=worker._backend.authenticate(token,world='real')
        with pytest.raises(AuthorizationUnavailable):foreign_worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model')
        assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()==(0,)


def test_real_effect_intent_tail_artifact_expiry_rolls_back_every_write(context_message,admin,tmp_path,monkeypatch):
    f=context_message;assert f['relay'].run_once()=='queued'
    from nexloop_eios.effect_intents import EffectIntentPort,EffectIntentUnavailable
    original=EffectIntentPort._execute_in_transaction;seen=[];wait_completed=[];tail_rejections=[]
    from nexloop_eios.runtime_activation import RuntimeActivationPort
    actual_execute=RuntimeActivationPort._execute
    def observe_actual_sql_tail(self,db,envelope,**kwargs):
        try:return actual_execute(self,db,envelope,**kwargs)
        except psycopg.Error as error:
            payload=json.loads(envelope[2])
            if wait_completed and payload.get('verb')=='authorize' and payload.get('operation')=='tool' and error.sqlstate=='42501' and error.diag.message_primary=='context artifact unavailable':tail_rejections.append(True)
            raise
    monkeypatch.setattr(RuntimeActivationPort,'_execute',observe_actual_sql_tail)
    def baseline(db):
        counts=[db.execute('select count(*) from '+table).fetchone()[0] for table in (
         'authz.nexloop_runtime_execution_markers','runtime.nexloop_effect_intents','runtime.nexloop_effect_outbox','runtime.nexloop_effect_submissions','runtime.nexloop_effect_control_reservations','runtime.nexloop_action_claims')]
        return counts+[db.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()[0]]
    before=baseline(admin)
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        expiry=admin.execute("update runtime.nexloop_local_artifacts set retention_until=clock_timestamp()+interval '2 seconds' returning retention_until").fetchone()[0]
        def pause_after_real_insert(self,db,*args,**kwargs):
            result=original(self,db,*args,**kwargs)
            # The actual signed port has returned the new accepted Intent on
            # this same transaction; baseline had no Intent. No restricted table
            # read bypass is installed to inspect its uncommitted rows.
            assert result['state']=='accepted' and before[1]==0
            found=original(self,db,'find',action_version=1,intent_id=result['intent_id'])
            assert found==result
            seen.append(True)
            # Actual PG clock, after real signed admission and quota reservation.
            now=db.execute('select clock_timestamp()').fetchone()[0]
            db.execute('select pg_sleep(%s)',(max(0,(expiry-now).total_seconds())+.05,))
            assert db.execute('select clock_timestamp()').fetchone()[0]>=expiry
            wait_completed.append(True)
            return result
        monkeypatch.setattr(EffectIntentPort,'_execute_in_transaction',pause_after_real_insert)
        with pytest.raises(EffectIntentUnavailable):worker.runtime_effect_tool(activation_ref=activation['activation_ref'],command=command,tool_operation='submit',parameters={'message':'must roll back after actual admission'})
        assert seen==[True] and wait_completed==[True] and tail_rejections==[True]
        assert admin.execute('select clock_timestamp()').fetchone()[0]>=expiry
        assert baseline(admin)==before


def test_actual_three_process_replay_model_tool_do_not_invert_locks(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    import os,subprocess,sys,time
    record=f['vault'].read(f['vault'].message_key(f['original']['tenant'],'real',f['message']['id']))
    start_file=tmp_path/'parallel-start';children=[]
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        sha=hashlib.sha256(text.encode()).hexdigest()
        try:
            for operation in ('producer','model','tool'):
                values={'database_url':f['dsn'].read_text() if operation=='producer' else make_conninfo(f['original']['pg'],user='nexloop_domain_worker'),
                 'artifact_root':str(f['root'] if operation=='producer' else tmp_path/('parallel-'+operation)),
                 'signing_key_file':str(f['original']['paths']['backend_signing']),
                 'service_token':f['source_token'] if operation=='producer' else f['f']['tokens']['assembly-runtime-worker'],
                 'run_token':record.token,'message_id':f['message']['id'],'command':command,'input':text,'sha256':sha,
                 'activation_ref':activation['activation_ref'],'operation':operation,'start_file':str(start_file),'offering_id':f['f']['recipe']['offering_id'],'binding_id':f['f']['recipe']['offering_binding_id']}
                config=private(tmp_path,'parallel-'+operation+'.json',json.dumps(values))
                environment={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','TMPDIR','PYTHONPATH')}
                child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('context_artifact_process.py')),str(config)],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                children.append(child)
            import select
            deadline=time.monotonic()+15
            for child in children:
                observed=b''
                while b'\n' not in observed:
                    remaining=deadline-time.monotonic();assert remaining>0 and child.poll() is None
                    readable,_,_=select.select([child.stdout],[],[],min(remaining,.2))
                    if readable:observed+=os.read(child.stdout.fileno(),128)
                    assert len(observed)<=128
                assert observed==b'ready\n'
            start_file.write_text('start')
            for child in children:
                output,error=child.communicate(timeout=20)
                assert child.returncode==0 and output=='passed\n' and error==''
            assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()==(1,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
            assert admin.execute('select sum(reserved_units) from control.nexloop_effect_control_ledger').fetchone()==(1,)
        finally:
            for child in children:
                if child.poll() is None:child.kill()
                child.communicate(timeout=5)

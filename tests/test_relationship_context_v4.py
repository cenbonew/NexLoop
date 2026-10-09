"""Protected latest relationship projection into explicit v4 Context; not full AT009 evidence."""
import json
from pathlib import Path
import pytest,psycopg
from psycopg.types.json import Jsonb
from test_assessment_candidate import assessment
from test_action_definitions import published_action
from nexloop_eios.relationship_context import RelationshipContextReader,RelationshipContextRecipe
from nexloop_eios.assessment_actions import FIELDS
from eios.authz import facts as F
from eios.authz.resources import ResourceType
from authority_fixture import replace_fact

def install(admin):
    # Real catalog owns the v4 relationship Context migration; no manual DRAFT SQL.
    pass

def project(port,recipe,consumer):
    envelopes=RelationshipContextReader(port.pool,port.session,port.signer,recipe).envelopes()
    with port.pool.connection() as connection,connection.transaction():
        return connection.execute('select authz.nexloop_relationship_context_snapshot(%s,%s,%s,%s)',(port.session.token_digest,'real',consumer,Jsonb(envelopes))).fetchone()[0]

def test_protected_current_hypothesis_and_correction(assessment,admin):
    port,creator,values,object_id,token=assessment;install(admin)
    recipe=RelationshipContextRecipe((object_id,))
    admin.execute('insert into control.nexloop_relationship_context_recipes values(%s,%s,%s,%s,%s)',('synthetic-a','real',port.session.authentication.subject_principal_id,values['source_id'],Jsonb([object_id])))
    first=project(port,recipe,values['source_id'])
    assert first['current_statements']==[] and first['evidence'][0]['epistemic_kind']=='hypothesis' and first['evidence'][0]['revision']==1
    assert first['evidence'][0]['conclusion']==values['conclusion']
    port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='relationship-context-pending',object_id=object_id,expected_revision=1,properties=values|{'corrects_revision':1,'resolution_state':'awaiting_definition','conclusion':'corrected still pending'})
    second=project(port,recipe,values['source_id'])
    assert second['current_statements']==[] and second['evidence'][0]['revision']==2 and second['evidence'][0]['resolution_state']=='awaiting_definition'
    assert first!=second and values['conclusion'] not in json.dumps(second)
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='relationship recipe unavailable'):
        project(port,recipe,'f'*64)
    principal=port.session.authentication.subject_principal_id
    replace_fact(admin,'synthetic-a','grants',[principal,'eios:property:RelationshipAssessment/'+object_id+'/conclusion'],F.GrantFacts,grants=[])
    from nexloop_eios.authorization import authenticate_service
    port.session=authenticate_service(port.pool,token,world='real')
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    with pytest.raises(ActionAuthorizationDenied,match='object/property read denied'):project(port,recipe,values['source_id'])

@pytest.mark.parametrize('ids',[(),('f'*64,'f'*64),tuple(str(i)*64 for i in range(5)),('g'*64,)])
def test_server_recipe_rejects_unbounded_or_duplicate_ids(ids):
    with pytest.raises(ValueError,match='relationship_recipe_invalid'):RelationshipContextRecipe(ids)

from test_context_artifacts import context_message,source_declarations,reconfigure
from local_message_assembly_fixture import assembled_message,business_plan,configured
from test_assessment_candidate import prepare_assessment
from eios.ontology.definitions import ActionDefinition
from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios.message_relay import MessageRelay,MessageRelayUnavailable
from types import SimpleNamespace

@pytest.mark.parametrize('mode',['complete','missing_trigger_object','missing_trigger_body','missing_trigger_actor','tampered_revision','tail_action_wait','tail_trigger_wait','tail_relation_wait','tail_statement_wait','bind_lease_null','bind_lease_expired','formal_Consumer','formal_Goal','formal_PlanStep','formal_EffectControl','formal_allow_effect','formal_budget_units','formal_executor_principal','formal_valid_until','artifact_read_Consumer','artifact_read_Goal','artifact_read_PlanStep','artifact_read_EffectControl','artifact_read_allow_effect','artifact_read_budget_units','artifact_read_executor_principal','artifact_read_valid_until'])
def test_real_human_message_v4_bound_artifact(context_message,admin,tmp_path,monkeypatch,mode):
    f=context_message;install(admin);tenant=f['original']['tenant'];consumer=f['f']['recipe']['consumer_id']
    base=next(row for row in f['manifest']['actions'] if row['definition']['stable_name']=='Consumer.create')
    definition=dict(base['definition']);definition.pop('contract_digest',None);definition['input_schema']={'type':'object'}
    template=ActionDefinition.model_validate_json(json.dumps(definition));cap=CapabilityContractSnapshot.model_validate_json(json.dumps(base['capability']))
    reader=SimpleNamespace(pool=f['backend']._pool,session=f['source']._session,signer=f['backend']._signer)
    port,creator,values,obj,token=prepare_assessment(reader,template,cap,admin,consumer,tenant)
    principal=f['source']._session.authentication.subject_principal_id
    recipe=RelationshipContextRecipe((obj,))
    admin.execute('insert into control.nexloop_relationship_context_recipes values(%s,%s,%s,%s,%s)',(tenant,'real',principal,consumer,Jsonb([obj])))
    formal_refs=precreate_formal_refs(f,f['message']['id'])
    original_binding=f['source']._session.authentication
    application=next(row['payload'] for row in f['manifest']['authority_facts'] if row['kind']=='application' and row['key']==[original_binding.caller_application_id,'1'])
    from eios.authz.resources import ResourceType
    targets=[(r['resource_id'],ResourceType(r['resource_type'])) for r in application['resources'] if r['resource_type'] in ('object','property') and not r['resource_id'].startswith(('eios:object:Message/','eios:property:Message/'))]
    targets += [('eios:object:RelationshipAssessment/'+obj,ResourceType.OBJECT),('eios:object:Consumer/'+consumer,ResourceType.OBJECT),('eios:link_type:contact',ResourceType.LINK_TYPE)]
    if mode!='missing_trigger_object':targets.append(('eios:object:Message/'+f['message']['id'],ResourceType.OBJECT))
    targets += [('eios:property:Message/'+f['message']['id']+'/'+field,ResourceType.PROPERTY) for field in ('actor','body') if mode!='missing_trigger_'+field]
    targets += [('eios:property:RelationshipAssessment/'+obj+'/'+field,ResourceType.PROPERTY) for field in FIELDS]
    formal_targets=[('eios:object:'+kind+'/'+ref,ResourceType.OBJECT) for kind,ref in formal_refs.items()]+[('eios:property:EffectControl/'+formal_refs['EffectControl']+'/'+field,ResourceType.PROPERTY) for field in ('allow_effect','budget_units','executor_principal','valid_until')]
    all_formal={target for target,kind in formal_targets}
    targets=[(target,kind) for target,kind in targets if target not in all_formal]
    missing=mode[7:] if mode.startswith('formal_') else ''
    targets += [(target,kind) for target,kind in formal_targets if target!=('eios:object:'+missing+'/'+formal_refs.get(missing,'')) and not target.endswith('/'+str(missing))]
    binding,rows=source_declarations(tenant,targets)
    import secrets
    old_app=binding.caller_application_id;old_credential=binding.credential_id
    binding=binding.model_copy(update={'caller_application_id':old_app+':relationship-v4','credential_id':old_credential+':relationship-v4'})
    for row in rows:
        if row['kind']=='application':row['key']=[binding.caller_application_id,'1'];row['payload']['application_id']=binding.caller_application_id
        if row['kind']=='authentication':row['key']=[binding.credential_id];row['payload'].update(caller_application_id=binding.caller_application_id,credential_id=binding.credential_id)
        row['payload'].pop('snapshot_digest',None)
    relationship_token=secrets.token_urlsafe(48)
    secret_map=json.loads(f['original']['paths']['secrets'].read_text());secret_map['relationship-source']=relationship_token;f['original']['paths']['secrets'].write_text(json.dumps(secret_map))
    def publish_read_permissions(manifest):
        merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in rows});manifest['authority_facts']=list(merged.values())
        expires=next(row['payload']['expires_at'] for row in rows if row['kind']=='authentication')
        manifest['service_credentials'].append({'reference':'relationship-source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expires,'status':'active'})
    reconfigure(f,admin,publish_read_permissions)
    f['source']=f['backend'].authenticate(relationship_token,world='real')
    relay=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],recipe=f['f']['recipe'],relationship_recipe=recipe)
    if mode=='tampered_revision':
        import nexloop_eios.relationship_context_artifacts as module
        original_encode=module.encode_pack
        def tamper(snapshot,command):
            pack=json.loads(original_encode(snapshot,command));pack['relationship_context']['evidence'][0]['revision']+=1
            from nexloop_eios.postgres_artifacts import canonical_payload
            return canonical_payload(pack)
        monkeypatch.setattr(module,'encode_pack',tamper)
    if mode in ('bind_lease_null','bind_lease_expired'):
        from datetime import UTC,datetime,timedelta
        lease='null' if mode=='bind_lease_null' else json.dumps((datetime.now(UTC)-timedelta(seconds=1)).isoformat())
        admin.execute("create function runtime.owned_bad_bind_lease() returns trigger language plpgsql as $$begin if new.action_name='nexloop.context.bind' then new.claim:=jsonb_set(new.claim,'{lease_expires_at}',"+"'"+lease+"'::jsonb);end if;return new;end $$")
        admin.execute('create trigger owned_bad_bind_lease before insert on runtime.nexloop_action_claims for each row execute function runtime.owned_bad_bind_lease()')
    if mode=='tail_statement_wait':
        from datetime import UTC,datetime,timedelta
        import hashlib
        from multi_authority_fixture import seed_multi_authority
        from eios.authz.operations import Operation
        from nexloop_eios.assessment_actions import GovernedAssessmentCorrector
        targets=[('eios:object:Message/'+f['message']['id'],ResourceType.OBJECT,Operation.READ)]+[('eios:property:Message/'+f['message']['id']+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in ('actor','body')]
        session,_=seed_multi_authority(admin,port.pool,port.test_targets+targets,identity_suffix='-tail-statement',tenant=tenant)
        amended=values|{'corrects_revision':1,'epistemic_kind':'user_statement','conclusion':f['message']['body'],'evidence_message_id':f['message']['id'],'evidence_content_hash':hashlib.sha256(f['message']['body'].encode()).hexdigest(),'valid_to':(datetime.now(UTC)+timedelta(seconds=5)).isoformat()}
        GovernedAssessmentCorrector(port.pool,session,port.signer).correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='owned-tail-statement',object_id=obj,expected_revision=1,properties=amended)
        # Restore Publisher-owned operational facts after the synthetic writer seed.
        reconfigure(f,admin,lambda manifest:None)
        f['source']=f['backend'].authenticate(relationship_token,world='real')
        f['route']=f['backend'].authenticate(f['f']['tokens']['assembly-route'],world='real')
        f['planner']=f['backend'].authenticate(f['f']['tokens']['assembly-planner'],world='real')
        relay=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],recipe=f['f']['recipe'],relationship_recipe=recipe)
    if mode.startswith('tail_'):
        from concurrent.futures import ThreadPoolExecutor
        from datetime import UTC,datetime,timedelta
        import threading,time
        import nexloop_eios.relationship_context_artifacts as module
        original=module.canonical_payload;ready=threading.Event()
        def short(value):
            if isinstance(value,dict) and value.get('protocol')=='nexloop-context-artifact-v1':
                value=json.loads(json.dumps(value));expiry=(datetime.now(UTC)+timedelta(seconds=1)).isoformat()
                if mode=='tail_action_wait':value['expires_at']=expiry
                elif mode in ('tail_trigger_wait','tail_relation_wait'):
                    import hmac
                    env=value['trigger_message_envelope'] if mode=='tail_trigger_wait' else value['relationship_envelopes'][0]
                    claim=json.loads(env['text']);claim['expires_at']=expiry
                    text=original(claim);env.update(text=text,signature=hmac.new(f['backend']._signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest())
                ready.set()
            return original(value)
        monkeypatch.setattr(module,'canonical_payload',short)
        admin.execute('alter function authz.nexloop_relationship_message_read(text,text,text,jsonb) rename to nexloop_relationship_message_read_owned_original')
        admin.execute("""create function authz.nexloop_relationship_message_read(d text,w text,m text,e jsonb) returns jsonb language plpgsql security definer set search_path=pg_catalog as $$declare n integer;r jsonb;begin n:=coalesce(nullif(current_setting('nexloop.owned_message_count',true),''),'0')::integer+1;perform set_config('nexloop.owned_message_count',n::text,true);r:=authz.nexloop_relationship_message_read_owned_original(d,w,m,e);if n=2 then perform pg_advisory_xact_lock(71818001);end if;return r;end $$""")
        admin.execute('alter function authz.nexloop_relationship_message_read(text,text,text,jsonb) owner to nexloop_owner')
        admin.execute('revoke all on function authz.nexloop_relationship_message_read(text,text,text,jsonb) from public')
        with ThreadPoolExecutor(max_workers=1) as executor:
            with admin.transaction():
                admin.execute('select pg_advisory_xact_lock(71818001)')
                future=executor.submit(relay.run_once)
                if not ready.wait(8):
                    error=future.exception() if future.done() else None;chain=[]
                    while error is not None:
                        import traceback
                        chain.append((type(error).__name__,getattr(getattr(error,'diag',None),'message_primary',None),[(Path(frame.filename).name,frame.lineno,frame.name) for frame in traceback.extract_tb(error.__traceback__)]));error=error.__context__
                    pytest.fail(str((getattr(relay,'_diagnostic_failure',None),chain)))
                until=time.monotonic()+8
                while time.monotonic()<until:
                    if admin.execute("select count(*) from pg_stat_activity where wait_event='advisory' and pid<>pg_backend_pid()").fetchone()[0]:break
                    if future.done():break
                    time.sleep(.02)
                assert not future.done(),'normal signed producer did not wait on owned final Message guard'
                time.sleep(5.15 if mode=='tail_statement_wait' else 1.15)
            with pytest.raises(MessageRelayUnavailable):future.result(timeout=15)
    elif mode=='complete' or mode.startswith('artifact_read_'):
        try:assert relay.run_once()=='queued'
        except MessageRelayUnavailable:pytest.fail(str(getattr(relay,'_diagnostic_failure','missing')))
    else:
        with pytest.raises(MessageRelayUnavailable):relay.run_once()
    if mode!='complete' and not mode.startswith('artifact_read_'):assert getattr(relay,'_diagnostic_failure',None) is not None
    row=admin.execute('select pack_text,pack_digest,artifact_id from runtime.nexloop_context_artifact_bindings').fetchone()
    if mode!='complete' and not mode.startswith('artifact_read_'):
        assert row is None
        assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
        expected_artifacts=1 if mode in ('tampered_revision','bind_lease_null','bind_lease_expired') else 0
        assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()==(expected_artifacts,)
        if expected_artifacts:
            artifact_id=admin.execute('select artifact_id from runtime.nexloop_local_artifacts').fetchone()[0]
            from nexloop_eios.local_artifacts import ArtifactAccessDenied
            with pytest.raises(psycopg.errors.InsufficientPrivilege,match='context artifact unavailable'):f['source'].read_artifact(artifact_id)
        return
    assert row is not None
    pack=json.loads(row[0]);assert pack['schema_version']=='nexloop.context-pack.v4'
    assert pack['user_statement']['body']==f['message']['body']
    assert pack['relationship_context']['current_statements']==[] and pack['relationship_context']['evidence'][0]['revision']==1
    assert pack['relationship_context']['evidence'][0]['epistemic_kind']=='hypothesis'
    assert len(pack['formal_facts'])==4 and all(r['type']!='RelationshipAssessment' for r in pack['formal_facts'])
    import hashlib
    assert hashlib.sha256(row[0].encode()).hexdigest()==row[1]
    assert f['source'].read_artifact(row[2])==row[0].encode()
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(1,)
    if mode.startswith('artifact_read_'):
        label=mode[len('artifact_read_'):]
        target='eios:object:'+label+'/'+formal_refs[label] if label in formal_refs else 'eios:property:EffectControl/'+formal_refs['EffectControl']+'/'+label
        principal=f['source']._session.authentication.subject_principal_id
        def withdraw(manifest):
            record=next(record for record in manifest['authority_facts'] if record['kind']=='grants' and record['key']==[principal,target])
            raw=dict(record['payload']);raw.pop('snapshot_digest',None);raw['grants']=[]
            record['payload']=F.GrantFacts.model_validate_json(json.dumps(raw)).model_dump(mode='json')
        reconfigure(f,admin,withdraw)
        f['source']=f['backend'].authenticate(relationship_token,world='real')
        from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
        with pytest.raises(ActionAuthorizationDenied):f['source'].read_artifact(row[2])
        assert admin.execute('select count(*) from runtime.jobs').fetchone()==(1,)
        assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(1,)
        from psycopg.conninfo import make_conninfo
        from nexloop_eios.backend import open_backend
        from eios.authz.errors import AuthorizationUnavailable
        o=f['original']
        with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'withdraw-runtime',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
            worker=backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
            job=worker.claim_task(queue='operations',lease_seconds=60)
            payload=admin.execute('select normalized_input from runtime.jobs where job_id=%s',(job['task_id'],)).fetchone()[0]
            with pytest.raises(AuthorizationUnavailable):
                worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=payload['run_command']['run_id'],command=payload['run_command'],input=payload['input'],owner_epoch=1)
        assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()==(0,)
        return

    first_run=dispatch_relationship_run(f,tmp_path/'relationship-first-run',admin,monkeypatch)
    assert first_run['pack']['relationship_context']['current_statements']==[]
    assert first_run['pack']['relationship_context']['evidence'][0]['epistemic_kind']=='hypothesis'
    first_human=f['message']
    f['message']=accept_new_human_message(f,tmp_path,'人类更正：双方仍保持每周联系。')
    # Real accepted Human Message is the corrected source; the writer remains
    # a genuine governed service and does not invent a Human actor.
    from multi_authority_fixture import seed_multi_authority
    from eios.authz.operations import Operation
    from nexloop_eios.assessment_actions import GovernedAssessmentCorrector
    human_targets=[('eios:object:Message/'+f['message']['id'],ResourceType.OBJECT,Operation.READ)]+[('eios:property:Message/'+f['message']['id']+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in ('actor','body')]
    corrector_session,corrector_token=seed_multi_authority(admin,port.pool,port.test_targets+human_targets,identity_suffix='-human-statement-corrector',tenant=tenant)
    corrector=GovernedAssessmentCorrector(port.pool,corrector_session,port.signer)
    corrected=values|{'corrects_revision':1,'epistemic_kind':'user_statement','conclusion':f['message']['body'],'evidence_message_id':f['message']['id'],'evidence_content_hash':hashlib.sha256(f['message']['body'].encode()).hexdigest()}
    assert corrector.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='relationship-context-human-correction',object_id=obj,expected_revision=1,properties=corrected)['revision']==2
    from context_copy_authority_fixture import configure_message_read
    configure_message_read(f,admin,allow=True)
    latest_port=SimpleNamespace(pool=f['backend']._pool,session=f['backend'].authenticate(relationship_token,world='real')._session,signer=f['backend']._signer)
    current=project(latest_port,recipe,consumer)
    assert current['evidence']==[] and current['current_statements'][0]['epistemic_kind']=='user_statement' and current['current_statements'][0]['revision']==2
    assert current['current_statements'][0]['conclusion']==f['message']['body'] and current['current_statements'][0]['source_content_hash']==corrected['evidence_content_hash']
    assert values['conclusion'] not in json.dumps(current)
    from nexloop_eios.relationship_context_artifacts import RelationshipContextArtifactProducer
    from nexloop_eios.relationship_context_artifacts import ContextArtifactUnavailable
    stored=admin.execute('select command_binding from runtime.nexloop_context_artifact_bindings').fetchone()[0]
    key=f['vault'].message_key(tenant,'real',first_human['id'])
    assignment_digest=admin.execute('select assignment_digest from authz.nexloop_message_run_issuances').fetchone()[0]
    record=f['vault'].prepare(message_key=key,assignment_digest=assignment_digest)
    command=stored|{'credential_ref':'run:'+record.run_id,'context_manifest_ref':'artifact:'+row[2]}
    with pytest.raises(ContextArtifactUnavailable):
        RelationshipContextArtifactProducer(f['backend'].authenticate(relationship_token,world='real'),recipe).prepare(message_id=first_human['id'],run_token=record.token,command=command,offering_id=f['f']['recipe']['offering_id'],binding_id=f['f']['recipe']['offering_binding_id'])
    assert admin.execute('select pack_text from runtime.nexloop_context_artifact_bindings').fetchone()==(row[0],)
    # Exact new Message READ was published once before current projection.
    f['source']=f['backend'].authenticate(relationship_token,world='real')
    grant_formal_refs(f,admin,precreate_formal_refs(f,f['message']['id']))
    relay=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],recipe=f['f']['recipe'],relationship_recipe=recipe)
    try:assert relay.run_once()=='queued'
    except MessageRelayUnavailable as error:
        chain=[]
        while error is not None:
            chain.append((type(error).__name__,getattr(getattr(error,'diag',None),'message_primary',None)));error=error.__context__
        pytest.fail(str((getattr(relay,'_diagnostic_failure',None),chain)))
    second=dispatch_relationship_run(f,tmp_path/'relationship-corrected-run',admin,monkeypatch)
    assert second['run_id']!=first_run['run_id'] and second['input_sha256']!=first_run['input_sha256']
    current_zone=second['pack']['relationship_context']
    assert current_zone['current_statements'][0]['revision']==2
    assert current_zone['current_statements'][0]['conclusion']==f['message']['body']
    assert all(call['arguments']['message']==f['message']['body'] for call in second['calls'])
    assert values['conclusion'] not in json.dumps(current_zone['current_statements'])
    assert first_run['pack']['relationship_context']['evidence'][0]['epistemic_kind']=='hypothesis'
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(2,)
    assert admin.execute("select revision,new_properties->>'epistemic_kind' from ontology.nexloop_assessment_revisions order by revision").fetchall()==[(1,'hypothesis'),(2,'user_statement')]


def dispatch_relationship_run(f,tmp_path,admin,monkeypatch):
    """Actual local TLS Host/Pi with current protected PG activation, no model key."""
    import secrets,sqlite3,hashlib
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.backend import open_backend
    from nexloop_eios.runtime_dispatch import RuntimeDispatcher,HostControlConfiguration
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,configuration
    from test_message_runtime_effect_e2e import actual_host
    from test_runtime_effect_tools import tool_evidence
    tmp_path.mkdir(mode=0o700)
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'relationship-guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    o=f['original']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'domain-artifacts',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        worker=backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
        with guard_server(worker,tmp_path,guard_key) as port:
            cfg=configuration(tmp_path,port,guard_key);body=json.loads(cfg.read_text());body.update(effect_tools=True,deterministic_message_from_input=True,deterministic_relationship_from_context=True,context_input_protocol='nexloop.context-pack.v4');cfg.write_text(json.dumps(body))
            with actual_host(runtime,key,cfg) as (_,client,headers):
                result=RuntimeDispatcher(worker,HostControlConfiguration(str(client.base_url).rstrip('/'),key,tmp_path/'host-cert.pem'),queue='operations',lease_seconds=60,total_timeout=45,request_timeout=10).run_once()
                assert result['claimed'] is True and result['status']=='succeeded',result.get('result',{}).get('code')
                task=worker.inspect_task(queue='operations',task_id=result['task_id'])
                payload=admin.execute('select normalized_input from runtime.jobs where job_id=%s',(result['task_id'],)).fetchone()[0]
                command=payload['run_command'];pack=payload['input'];run=command['run_id']
    database=runtime/run/'runtime.sqlite'
    with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as db:
        submission=db.execute('select * from submissions').fetchone();assert submission is not None
        records=[json.loads(row[0]) for row in db.execute('select record from entries order by id')]
    calls,receipts=tool_evidence(database)
    requested=[call for call in calls if call['name']=='nexloop.service.request']
    assert requested
    return {'run_id':run,'pack':json.loads(pack),'calls':requested,'records':records,'input_sha256':hashlib.sha256(pack.encode()).hexdigest()}


def accept_new_human_message(f,tmp_path,body):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import ApiConfiguration,create_app
    from nexloop_eios.browser_http import BrowserConfiguration
    from test_trusted_configuration_pg import private
    from psycopg.conninfo import make_conninfo
    o=f['original'];origin='https://context.invalid'
    dsn=private(tmp_path,'relationship-human-dsn',make_conninfo(o['pg'],user='nexloop_api'));rate=private(tmp_path,'relationship-human-rate','b'*64)
    cfg=ApiConfiguration(dsn,o['paths']['backend_signing'],tmp_path/'relationship-api-artifacts','explicit-configuration',BrowserConfiguration(o['paths']['identity'],rate,o['tenant'],o['application'],origin),execution_profile='deterministic-test')
    with TestClient(create_app(cfg),base_url=origin) as client:
        login=client.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'explicit-user','password':o['paths']['password'].read_text()});assert login.status_code==200
        headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token'],'Idempotency-Key':'relationship-correction-conversation'}
        conversation=client.post('/api/v1/conversations',headers=headers,json={});assert conversation.status_code==200
        headers['Idempotency-Key']='relationship-correction-human-message'
        response=client.post('/api/v1/conversations/'+conversation.json()['id']+'/messages',headers=headers,json={'body':body});assert response.status_code==202
        return response.json()['message']


def precreate_formal_refs(f,message_id):
    # A Publisher revision requires fresh genuine authenticated snapshots.
    f['route']=f['backend'].authenticate(f['f']['tokens']['assembly-route'],world='real')
    f['planner']=f['backend'].authenticate(f['f']['tokens']['assembly-planner'],world='real')
    from nexloop_eios.authorization import _identity
    with f['backend']._pool.connection() as db,db.transaction():session=_identity(db,f['source']._session.token_digest,'real')
    f['source']=type(f['source'])(f['backend'],session)
    relay=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],recipe=f['f']['recipe'])
    recipe=relay.recipe;principal=f['source']._session.authentication.subject_principal_id
    goal=relay._create('Goal',message_id,{'consumer_id':recipe['consumer_id'],'state':'active','valid_until':recipe['valid_until']})
    step=relay._create('PlanStep',message_id,{'consumer_id':recipe['consumer_id'],'goal_id':goal,'control_id':recipe['control_id'],'submitter_principals':[principal],'action_name':'nexloop.service.request','state':'ready'})
    return {'Consumer':recipe['consumer_id'],'Goal':goal,'PlanStep':step,'EffectControl':recipe['control_id']}

def grant_formal_refs(f,admin,refs):
    from eios.authz.applications import ResourceRestriction
    auth=f['source']._session.authentication
    additions=[('eios:object:'+kind+'/'+ref,ResourceType.OBJECT) for kind,ref in refs.items()]+[('eios:property:EffectControl/'+refs['EffectControl']+'/'+field,ResourceType.PROPERTY) for field in ('allow_effect','budget_units','executor_principal','valid_until')]
    from authority_fixture import authority_records
    from eios.authz.operations import Operation
    def publish(manifest):
        merged={(row['kind'],tuple(row['key'])):row for row in manifest['authority_facts']}
        role=merged['subject_authority',(auth.subject_principal_id,)]['payload']['roles'][0]['digest']
        for target,kind in additions:
            _,_,rows=authority_records(auth.tenant_id,target,resource_type=kind,operation=Operation.READ,identity_suffix='-assembly-source')
            for name,key,fact in rows:
                if name not in ('resource_graph','grants','scope','controls','policies'):continue
                if name=='scope':fact=fact.model_copy(update={'catalog_scopes':auth.requested_scopes,'authorized_scopes':auth.requested_scopes})
                if name=='grants':fact=fact.model_copy(update={'grants':tuple(grant.model_copy(update={'role_digest':role}) for grant in fact.grants)})
                raw=fact.model_dump(mode='json');raw.pop('snapshot_digest',None);fact=type(fact).model_validate_json(json.dumps(raw))
                merged[name,tuple(key)]={'kind':name,'key':key,'payload':fact.model_dump(mode='json')}
        row=merged['application',(auth.caller_application_id,'1')];raw=dict(row['payload']);raw.pop('snapshot_digest',None)
        resources={(r['resource_type'],r['resource_id']):r for r in raw['resources']}
        resources.update({(kind.value,target):ResourceRestriction(tenant_id=auth.tenant_id,resource_type=kind.value,resource_id=target).model_dump(mode='json') for target,kind in additions})
        raw['resources']=list(resources.values());row['payload']=F.ApplicationFacts.model_validate_json(json.dumps(raw)).model_dump(mode='json')
        manifest['authority_facts']=list(merged.values())
    token=f['source']._session.token_digest;reconfigure(f,admin,publish)
    from nexloop_eios.authorization import _identity
    with f['backend']._pool.connection() as db,db.transaction():session=_identity(db,token,'real')
    f['source']=type(f['source'])(f['backend'],session)
    f['route']=f['backend'].authenticate(f['f']['tokens']['assembly-route'],world='real')
    f['planner']=f['backend'].authenticate(f['f']['tokens']['assembly-planner'],world='real')


def test_two_assessment_later_owned_lock_rejects_earlier_signed_proof(assessment,admin):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC,datetime,timedelta
    import hashlib,hmac,time
    from multi_authority_fixture import seed_multi_authority
    from eios.authz.operations import Operation
    from nexloop_eios.assessment_actions import GovernedAssessmentCreator
    from nexloop_eios.postgres_artifacts import canonical_payload
    port,creator,values,obj,token=assessment;install(admin)
    second=GovernedAssessmentCreator(port.pool,port.session,port.signer).create(action_name='RelationshipAssessment.create',action_version=1,intent_id='owned-second-assessment',properties=values|{'conclusion':'independent second hypothesis'})['object_id']
    ids=tuple(sorted((obj,second)));targets=[('eios:link_type:contact',ResourceType.LINK_TYPE,Operation.READ),('eios:object:Consumer/'+values['source_id'],ResourceType.OBJECT,Operation.READ)]
    for ref in ids:
        targets.append(('eios:object:RelationshipAssessment/'+ref,ResourceType.OBJECT,Operation.READ))
        targets.extend(('eios:property:RelationshipAssessment/'+ref+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in FIELDS)
    session,_=seed_multi_authority(admin,port.pool,targets,identity_suffix='-owned-multi-reader',tenant='synthetic-a')
    recipe=RelationshipContextRecipe(ids)
    admin.execute('insert into control.nexloop_relationship_context_recipes values(%s,%s,%s,%s,%s)',('synthetic-a','real',session.authentication.subject_principal_id,values['source_id'],Jsonb(list(ids))))
    envelopes=RelationshipContextReader(port.pool,session,port.signer,recipe).envelopes()
    claim=json.loads(envelopes[0]['text']);claim['expires_at']=(datetime.now(UTC)+timedelta(seconds=.6)).isoformat();text=canonical_payload(claim)
    envelopes[0].update(text=text,signature=hmac.new(port.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest())
    def read():
        with port.pool.connection() as db,db.transaction():return db.execute('select authz.nexloop_relationship_context_snapshot(%s,%s,%s,%s)',(session.token_digest,'real',values['source_id'],Jsonb(envelopes))).fetchone()[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        with admin.transaction():
            admin.execute('select object_id from ontology.objects where tenant_id=%s and world=%s and object_id=%s for update',('synthetic-a','real',ids[1]))
            future=executor.submit(read);until=time.monotonic()+3
            while time.monotonic()<until:
                if admin.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and pid<>pg_backend_pid()").fetchone()[0]:break
                if future.done():break
                time.sleep(.01)
            assert not future.done(),'actual protected reader did not wait on later Assessment row'
            time.sleep(.75)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):future.result(timeout=5)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(0,)

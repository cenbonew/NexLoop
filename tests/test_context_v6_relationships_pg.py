"""NX-023-D actual PostgreSQL + Pi: v4 relationship assessments inside Context v6, and the
v6 Context copy read.

Setup follows the v4 'complete' path (test_relationship_context_v4): a governed hypothesis
RelationshipAssessment, the relationship recipe and a Source with current READ on the
assessment, the trigger Message, the formal facts and the supply; additionally the
published built-in strategy (admin seed, as in test_context_v6_pg). No standing
nexloop.context.assemble:1: the assemble authority is issued with each Run (0104).
Synthetic data only.
"""
import hashlib,json,secrets
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.definitions import ActionDefinition
from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios.assessment_actions import FIELDS
from nexloop_eios.message_relay import MessageRelay,MessageRelayUnavailable
from nexloop_eios.relationship_context import RelationshipContextRecipe
from test_assessment_candidate import prepare_assessment
from test_context_artifacts import context_message,source_declarations,reconfigure
from local_message_assembly_fixture import assembled_message,business_plan,configured
from test_relationship_context_v4 import precreate_formal_refs
from test_context_v6_pg import ASSEMBLE,STRATEGY,manifest,model_requests,owner,seed_strategy,sources,MANIFEST_SCHEMA
from nexloop_eios.context_artifacts import ACTION
from nexloop_eios.contracts import ContextManifest,ContextPackV6


@pytest.fixture
def relationship_v6(context_message,admin):
    f=context_message;tenant=f['original']['tenant'];consumer=f['f']['recipe']['consumer_id']
    base=next(row for row in f['manifest']['actions'] if row['definition']['stable_name']=='Consumer.create')
    definition=dict(base['definition']);definition.pop('contract_digest',None);definition['input_schema']={'type':'object'}
    template=ActionDefinition.model_validate_json(json.dumps(definition));cap=CapabilityContractSnapshot.model_validate_json(json.dumps(base['capability']))
    reader=SimpleNamespace(pool=f['backend']._pool,session=f['source']._session,signer=f['backend']._signer)
    port,creator,values,obj,token=prepare_assessment(reader,template,cap,admin,consumer,tenant)
    principal=f['source']._session.authentication.subject_principal_id
    recipe=RelationshipContextRecipe((obj,))
    admin.execute('insert into control.nexloop_relationship_context_recipes values(%s,%s,%s,%s,%s)',(tenant,'real',principal,consumer,Jsonb([obj])))
    formal_refs=precreate_formal_refs(f,f['message']['id'])
    application=next(row['payload'] for row in f['manifest']['authority_facts'] if row['kind']=='application' and row['key']==[f['source']._session.authentication.caller_application_id,'1'])
    targets=[(r['resource_id'],ResourceType(r['resource_type'])) for r in application['resources'] if r['resource_type'] in ('object','property') and not r['resource_id'].startswith(('eios:object:Message/','eios:property:Message/'))]
    targets+=[('eios:object:RelationshipAssessment/'+obj,ResourceType.OBJECT),('eios:object:Consumer/'+consumer,ResourceType.OBJECT),('eios:link_type:contact',ResourceType.LINK_TYPE),
        ('eios:object:Message/'+f['message']['id'],ResourceType.OBJECT),('eios:object:Conversation/'+f['message']['conversation_id'],ResourceType.OBJECT)]
    targets+=[('eios:property:Message/'+f['message']['id']+'/'+field,ResourceType.PROPERTY) for field in ('actor','body')]
    targets+=[('eios:property:RelationshipAssessment/'+obj+'/'+field,ResourceType.PROPERTY) for field in FIELDS]
    targets+=[('eios:object:'+kind+'/'+ref,ResourceType.OBJECT) for kind,ref in formal_refs.items()]
    targets+=[('eios:property:EffectControl/'+formal_refs['EffectControl']+'/'+field,ResourceType.PROPERTY) for field in ('allow_effect','budget_units','executor_principal','valid_until')]
    targets=sorted(set(targets))
    specs=[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:'+ACTION+':1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.CREATE),('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.READ)]
    binding,rows=source_declarations(tenant,targets,custom_specs=specs)
    binding=binding.model_copy(update={'caller_application_id':binding.caller_application_id+':relationship-v6','credential_id':binding.credential_id+':relationship-v6'})
    for row in rows:
        if row['kind']=='application':row['key']=[binding.caller_application_id,'1'];row['payload']['application_id']=binding.caller_application_id
        if row['kind']=='authentication':row['key']=[binding.credential_id];row['payload'].update(caller_application_id=binding.caller_application_id,credential_id=binding.credential_id)
        row['payload'].pop('snapshot_digest',None)
    source_token=secrets.token_urlsafe(48)
    secret_map=json.loads(f['original']['paths']['secrets'].read_text());secret_map['relationship-v6-source']=source_token;f['original']['paths']['secrets'].write_text(json.dumps(secret_map))
    def publish(manifest):
        merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in rows});manifest['authority_facts']=list(merged.values())
        expires=next(row['payload']['expires_at'] for row in rows if row['kind']=='authentication')
        manifest['service_credentials'].append({'reference':'relationship-v6-source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expires,'status':'active'})
    reconfigure(f,admin,publish)
    f['source_token']=source_token;f['source']=f['backend'].authenticate(source_token,world='real')
    seed_strategy(admin,tenant,STRATEGY)
    f.update(assessment=obj,assessment_values=values,recipe_v4=recipe,formal_refs=formal_refs,rows=rows)
    f['relay_v6']=lambda:MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],vault=f['vault'],
        recipe=f['f']['recipe'],relationship_recipe=recipe,context_strategy='recent_plus_required')
    return f


def dispatch_v6(f,tmp_path,admin):
    """Actual local TLS Host/Pi on the bound v6 Run (deterministic relationship mode)."""
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.backend import open_backend
    from nexloop_eios.runtime_dispatch import RuntimeDispatcher,HostControlConfiguration
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,configuration
    from test_message_runtime_effect_e2e import actual_host
    from test_runtime_effect_tools import tool_evidence
    tmp_path.mkdir(mode=0o700);runtime,key=files(tmp_path)
    guard_key=tmp_path/'v6-guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    o=f['original']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'domain-artifacts',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        worker=backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
        with guard_server(worker,tmp_path,guard_key) as port:
            cfg=configuration(tmp_path,port,guard_key);body=json.loads(cfg.read_text())
            body.update(effect_tools=True,deterministic_message_from_input=True,deterministic_relationship_from_context=True,context_input_protocol='nexloop.context-pack.v6');cfg.write_text(json.dumps(body))
            with actual_host(runtime,key,cfg) as (_,client,headers):
                result=RuntimeDispatcher(worker,HostControlConfiguration(str(client.base_url).rstrip('/'),key,tmp_path/'host-cert.pem'),queue='operations',lease_seconds=60,total_timeout=45,request_timeout=10).run_once()
                assert result['claimed'] is True and result['status']=='succeeded',result.get('result',{}).get('code')
                payload=admin.execute('select normalized_input from runtime.jobs where job_id=%s',(result['task_id'],)).fetchone()[0]
    run=payload['run_command']['run_id']
    calls,_=tool_evidence(runtime/run/'runtime.sqlite')
    return run,[c for c in calls if c['name']=='nexloop.service.request']


def test_v6_carries_v4_relationships_with_hypotheses_as_evidence_only(relationship_v6,admin,tmp_path):
    f=relationship_v6;tenant=f['original']['tenant']
    relay=f['relay_v6']()
    try:assert relay.run_once()=='queued'
    except MessageRelayUnavailable:pytest.fail(str((getattr(relay,'_diagnostic_failure',None),getattr(relay,'_context_diagnostic',None))))
    row=admin.execute('select pack_text,pack_digest,artifact_id,run_id::text,context_id::text from runtime.nexloop_context_artifact_bindings').fetchone()
    pack=json.loads(row[0]);ContextPackV6.model_validate(pack)
    assert pack['schema_version']=='nexloop.context-pack.v6' and pack['user_statement']['body']==f['message']['body']
    zone=pack['relationship_context']
    # v4 rule kept: the hypothesis assessment is evidence only, never a current statement.
    assert zone['current_statements']==[] and zone['evidence'][0]['epistemic_kind']=='hypothesis'
    assert zone['evidence'][0]['assessment_ref']=='eios:object:RelationshipAssessment/'+f['assessment']
    stored={r[2]:r for r in sources(admin,tenant,row[4])}
    assessment=stored['eios:object:RelationshipAssessment/'+f['assessment']]
    assert assessment[1]=='evidence' and assessment[5]=='hypothesis'
    # Actual Pi on v6 + relationships: the guard applies the v4 relationship and formal checks,
    # the Host reads the relationship section, and every model call is recorded.
    run,calls=dispatch_v6(f,tmp_path/'relationship-v6-run',admin)
    assert run==row[3] and calls and all(c['arguments']['message']==f['message']['body'] for c in calls)
    recorded=model_requests(admin,tenant)
    assert [r[0] for r in recorded]==list(range(1,len(recorded)+1)) and len(recorded)>=3
    body=manifest(admin,tenant,run,1);MANIFEST_SCHEMA.validate(body);ContextManifest.model_validate(body)
    assert 'eios:object:RelationshipAssessment/'+f['assessment'] in {s['ref'] for s in body['sources']}
    # Copy read: the Source holds current READ on every source object, so it gets the exact bytes.
    assert f['source'].read_artifact(row[2])==row[0].encode()
    # Withdrawing one source READ (the assessment) withdraws the copy, even of a bound pack.
    principal=f['source']._session.authentication.subject_principal_id;target='eios:object:RelationshipAssessment/'+f['assessment']
    def withdraw(m):
        record=next(r for r in m['authority_facts'] if r['kind']=='grants' and r['key']==[principal,target])
        raw=dict(record['payload']);raw.pop('snapshot_digest',None);raw['grants']=[]
        record['payload']=F.GrantFacts.model_validate_json(json.dumps(raw)).model_dump(mode='json')
    reconfigure(f,admin,withdraw)
    source=f['backend'].authenticate(f['source_token'],world='real')
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    with pytest.raises(ActionAuthorizationDenied):source.read_artifact(row[2])


def test_v6_copy_read_sql_requires_every_named_source_read(relationship_v6,admin):
    """A caller that skips or swaps one of the reads SQL names is refused by SQL itself."""
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.service_offerings import _read_envelope
    f=relationship_v6
    assert f['relay_v6']().run_once()=='queued'
    artifact=admin.execute('select artifact_id from runtime.nexloop_context_artifact_bindings').fetchone()[0]
    from nexloop_eios.postgres_artifacts import PostgresArtifactRepository
    meta=PostgresArtifactRepository(f['backend']._pool,f['source']._session,f['backend']._signer)
    params={'artifact_id':artifact};permit=meta.issue_permit(Operation.READ,params)
    with f['backend']._pool.connection() as c:
        dependency=c.execute('select authz.nexloop_context_artifact_read_dependency_v2(%s,%s,%s,%s)',(f['source']._session.token_digest,'real',permit,canonical_payload(params))).fetchone()[0]
    assert dependency['kind']=='v6' and {'message','fact_Goal','offering','relationship_1'}<=set(dependency['reads'])
    holder=SimpleNamespace(_session=f['source']._session,_backend=SimpleNamespace(_pool=f['backend']._pool,_signer=f['backend']._signer))
    reads={name:_read_envelope(holder,row['type_name'],row['object_id'],tuple(row['fields'])) for name,row in dependency['reads'].items()}
    def attempt(changed):
        body={'artifact_id':artifact,'context_dependency':{'kind':'v6','run_id':dependency['run_id'],'reads':changed}}
        permit=meta.issue_permit(Operation.READ,body)
        with f['backend']._pool.connection() as c:
            return c.execute('select authz.nexloop_read_local_artifact(%s,%s,%s,%s)',(f['source']._session.token_digest,'real',permit,canonical_payload(body))).fetchone()[0]
    missing=dict(reads);missing.pop('relationship_1')
    swapped=dict(reads);swapped['fact_Goal']=reads['fact_PlanStep']
    for changed in (missing,swapped):
        with pytest.raises(psycopg.errors.InsufficientPrivilege,match='context v6 copy READ required'):attempt(changed)
    assert attempt(reads)['artifact_id']==artifact

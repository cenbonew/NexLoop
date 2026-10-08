import copy,json,pytest
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.applications import ResourceRestriction
from authority_fixture import authority_records
from test_context_artifacts import context_message,assembled_message,business_plan,configured,reconfigure,active_worker
from nexloop_eios.authorization import AuthorizationUnavailable
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied


from context_copy_authority_fixture import configure_message_read


@pytest.mark.parametrize("withdraw",["body","actor","object"])
def test_actual_current_source_revoke_denies_old_context_copy(context_message,admin,tmp_path,withdraw):
 f=context_message
 assert f['source'].read_object(type_name='Message',object_id=f['message']['id'],fields=('body','actor'))['properties']['body']==f['message']['body']
 assert f['relay'].run_once()=='queued'
 artifact,pack=admin.execute('select artifact_id,pack_text from runtime.nexloop_context_artifact_bindings').fetchone()
 assert f['source'].read_artifact(artifact)==pack.encode()
 with active_worker(f,tmp_path) as (worker,activation,command,text):
  assert worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='start',input=text)['authorized'] is True
  configure_message_read(f,admin,allow=False,withdraw=withdraw)
  with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):
   f['source'].read_object(type_name='Message',object_id=f['message']['id'],fields=('body','actor'))
  # Corrected behavior: source-field withdrawal denies new Artifact reads; Runtime also rejects stale bindings.
  with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):
   f['source'].read_artifact(artifact)
  fresh=worker._backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
  with pytest.raises(AuthorizationUnavailable):
   fresh.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='start',input=text)


def test_unbound_context_not_publicly_readable(context_message,tmp_path):
 from datetime import UTC,datetime,timedelta
 import psycopg
 f=context_message
 ref=f['source'].put_artifact(request_id='unbound-context-test-artifact',payload=b'{"copy":"synthetic"}',media_type='application/vnd.nexloop.context+json',retention_until=datetime.now(UTC)+timedelta(minutes=5))
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  f['source'].read_artifact(ref.artifact_id)

def test_non_context_artifact_read_unchanged(context_message):
 from datetime import UTC,datetime,timedelta
 f=context_message
 ref=f['source'].put_artifact(request_id='ordinary-artifact-test-request',payload=b'ordinary synthetic',media_type='text/plain',retention_until=datetime.now(UTC)+timedelta(minutes=5))
 assert f['source'].read_artifact(ref.artifact_id)==b'ordinary synthetic'


def test_unknown_context_protocol_fails_closed(context_message,admin):
 import psycopg
 f=context_message;assert f['relay'].run_once()=='queued'
 artifact,pack=admin.execute('select artifact_id,pack_text from runtime.nexloop_context_artifact_bindings').fetchone()
 # Owned disposable metadata fault, not a business write or forged runtime authority.
 changed=json.loads(pack);changed['schema_version']='nexloop.context-pack.v3'
 admin.execute('update runtime.nexloop_context_artifact_bindings set pack_text=%s',(json.dumps(changed),))
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  f['source'].read_artifact(artifact)


def test_actual_binding_not_media_request_controls_dependency(context_message,admin):
 from context_copy_authority_fixture import configure_message_read
 f=context_message;assert f['relay'].run_once()=='queued'
 artifact=admin.execute('select artifact_id from runtime.nexloop_context_artifact_bindings').fetchone()[0]
 # Disposable metadata fault demonstrates binding dependency survives media mismatch.
 admin.execute("update runtime.nexloop_local_artifacts set media_type='text/plain' where artifact_id=%s",(artifact,))
 configure_message_read(f,admin,allow=False)
 with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):
  f['source'].read_artifact(artifact)


def test_governed_current_message_correction_denies_old_copy(context_message,admin):
 import secrets,psycopg
 from test_context_artifacts import source_declarations,reconfigure
 from eios.ontology.definitions import ActionDefinition
 from eios.ontology.semantics import schema_contract_digest
 from eios.ontology.models import ObjectTypeDefinition
 f=context_message;tenant=f['original']['tenant'];mid=f['message']['id']
 token=secrets.token_urlsafe(48)
 def add_editor(manifest):
  schema=next(row for row in manifest['object_types'] if row['type_name']=='Message')
  # The same declared generic EDIT capability used by actual catalog maintenance.
  row=copy.deepcopy(next(row for row in manifest['actions'] if row['definition']['stable_name']=='ServiceOffering.edit'))
  d=row['definition'];d.pop('contract_digest',None);d['stable_name']='Message.edit'
  ref={**d['object_types'][0],'stable_name':'Message','schema_digest':schema_contract_digest(ObjectTypeDefinition.model_validate_json(json.dumps(schema)))}
  d['object_types']=[ref];d['governance']['change_scope']['object_types']=[ref]
  row['definition']=ActionDefinition.model_validate_json(json.dumps(d)).model_dump(mode='json');manifest['actions'].append(row)
  specs=[('eios:action:Message.edit:1',ResourceType.ACTION,Operation.EXECUTE),('eios:object:Message/'+mid,ResourceType.OBJECT,Operation.EDIT),('eios:property:Message/'+mid+'/body',ResourceType.PROPERTY,Operation.EDIT)]
  binding,rows=source_declarations(tenant,custom_specs=specs,identity_suffix='-message-corrector')
  merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in rows});manifest['authority_facts']=list(merged.values())
  manifest['service_credentials'].append({'reference':'message-corrector','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':next(row['payload']['expires_at'] for row in rows if row['kind']=='authentication'),'status':'active'})
  path=f['original']['paths']['secrets'];values=json.loads(path.read_text());values['message-corrector']=token;path.write_text(json.dumps(values))
 reconfigure(f,admin,add_editor)
 assert f['relay'].run_once()=='queued'
 artifact=admin.execute('select artifact_id from runtime.nexloop_context_artifact_bindings').fetchone()[0]
 editor=f['backend'].authenticate(token,world='real')
 receipt=editor.edit_object(action_name='Message.edit',action_version=1,intent_id='actual-accepted-message-body-correction',type_name='Message',object_id=mid,expected_revision=1,properties={'body':'明确更正后的合成消息'})
 assert receipt['revision']==2
 assert f['source'].read_object(type_name='Message',object_id=mid,fields=('body',))['properties']['body']=='明确更正后的合成消息'
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  f['source'].read_artifact(artifact)


@pytest.mark.parametrize('withdraw',['body','actor','object'])
def test_source_without_message_read_cannot_produce_or_enqueue(context_message,admin,withdraw):
 from nexloop_eios.message_relay import MessageRelayUnavailable
 f=context_message;configure_message_read(f,admin,allow=False,withdraw=withdraw)
 with pytest.raises(MessageRelayUnavailable):f['relay'].run_once()
 assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()[0]==0
 assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()[0]==0
 assert admin.execute('select count(*) from runtime.jobs').fetchone()[0]==0

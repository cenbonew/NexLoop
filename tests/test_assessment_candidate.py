from datetime import UTC,datetime
import pytest,psycopg
from psycopg.types.json import Jsonb
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
from eios.ontology.semantics import schema_contract_digest
from eios.authz.resources import ResourceType
from eios.authz.operations import Operation
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.assessment_actions import GovernedAssessmentCreator,GovernedAssessmentCorrector,FIELDS
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action
@pytest.fixture
def assessment(published_action,admin):
 reader,template,cap=published_action
 endpoint=GovernedObjectCreator(reader.pool,reader.session,reader.signer).create(action_name='Consumer.create',action_version=1,intent_id='endpoint-1',type_name='Consumer',properties={})['object_id']
 return prepare_assessment(reader,template,cap,admin,endpoint,'synthetic-a')

def prepare_assessment(reader,template,cap,admin,endpoint,tenant,extra_targets=()):
 schema=ObjectTypeDefinition(type_name='RelationshipAssessment',version=1,only_edit_via_actions=True,properties=tuple(PropertyDefinition(property_name=f,value_type=PropertyValueType.INTEGER if f in ('corrects_revision','relation_version') else PropertyValueType.STRING) for f in FIELDS))
 admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s)',(tenant,schema.type_name,1,Jsonb(schema.model_dump(mode='json'))))
 ref=template.object_types[0].model_copy(update={'stable_name':schema.type_name,'schema_digest':schema_contract_digest(schema)})
 for verb in ('create','correct','genericCreate','genericEdit'):
  binding=template.capability_binding.model_copy(update={'capability_name':{'genericCreate':'ontology.object.create','genericEdit':'ontology.object.edit'}.get(verb,'ontology.relationship_assessment.'+verb)})
  scope=template.governance.change_scope.model_copy(update={'object_types':(ref,)})
  definition=template.model_copy(update={'contract_digest':None,'stable_name':'RelationshipAssessment.'+verb,'object_types':(ref,),'governance':template.governance.model_copy(update={'change_scope':scope}),'capability_binding':binding})
  capability=cap.model_copy(update={'capability_name':binding.capability_name})
  admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',(tenant,'real','eios:action:'+definition.stable_name+':1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
 from eios.ontology.models import RelationTypeDefinition
 relation=RelationTypeDefinition(relation_name='contact',source_type='Consumer',target_type='Consumer',version=1)
 admin.execute('insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,%s,%s,%s)',(tenant,'contact',1,Jsonb(relation.model_dump(mode='json'))))
 targets=[('eios:link_type:contact',ResourceType.LINK_TYPE,Operation.READ),('eios:action:RelationshipAssessment.create:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:RelationshipAssessment.correct:1',ResourceType.ACTION,Operation.EXECUTE),('eios:object:Consumer/'+endpoint,ResourceType.OBJECT,Operation.READ),('eios:object:Consumer/'+endpoint,ResourceType.OBJECT,Operation.EDIT)]
 targets += list(extra_targets)
 targets += [('eios:action:RelationshipAssessment.'+verb+':1',ResourceType.ACTION,Operation.EXECUTE) for verb in ('genericCreate','genericEdit')]
 session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-assessment',tenant=tenant)
 creator=GovernedAssessmentCreator(reader.pool,session,reader.signer)
 props=dict(relation_name='contact',relation_version=1,source_type='Consumer',source_id=endpoint,target_type='Consumer',target_id=endpoint,conclusion='unconfirmed inactivity',epistemic_kind='hypothesis',resolution_state='resolved',valid_from=datetime.now(UTC).isoformat(),valid_to='',evidence_message_id='',evidence_content_hash='',corrects_revision=0)
 obj=creator.create(action_name='RelationshipAssessment.create',action_version=1,intent_id='assessment-1',properties=props)['object_id']
 targets += [('eios:object:RelationshipAssessment/'+obj,ResourceType.OBJECT,op) for op in (Operation.READ,Operation.EDIT)]
 targets += [('eios:property:RelationshipAssessment/'+obj+'/'+f,ResourceType.PROPERTY,op) for f in FIELDS for op in (Operation.READ,Operation.EDIT)]
 session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-corrector',tenant=tenant)
 port=GovernedAssessmentCorrector(reader.pool,session,reader.signer);port.test_targets=targets
 return port,creator,props,obj,token

def test_actual_atomic_revision_replay_and_conflict(assessment,admin):
 port,creator,p,obj,token=assessment
 args=dict(action_name='RelationshipAssessment.correct',action_version=1,intent_id='correction-1',object_id=obj,expected_revision=1,properties=p|{'conclusion':'corrected but still unverified','corrects_revision':1})
 got=port.correct(**args);assert got['revision']==2;assert port.correct(**args)==got
 assert admin.execute('select revision,prior_properties,new_properties from ontology.nexloop_assessment_revisions order by revision').fetchall()==[(1,None,p),(2,p,args['properties'])]
 with pytest.raises(psycopg.errors.SerializationFailure):port.correct(**(args|{'intent_id':'stale'}))
 from eios.actions.governance import ActionGovernanceError
 with pytest.raises(ActionGovernanceError):port.correct(**(args|{'properties':args['properties']|{'conclusion':'intent conflict'}}))
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(2,)

def test_generic_create_cannot_bypass_initial_audit(assessment,admin):
 port,creator,p,obj,token=assessment
 with pytest.raises(psycopg.errors.InsufficientPrivilege):GovernedObjectCreator(creator.pool,port.session,creator.signer).create(action_name='RelationshipAssessment.genericCreate',action_version=1,intent_id='generic-create',type_name='RelationshipAssessment',properties=p)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)

def test_restricted_role_cannot_mutate_audit(assessment):
 port,*_=assessment
 with port.pool.connection() as c:
  with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('delete from ontology.nexloop_assessment_revisions')

def test_generic_edit_cannot_bypass_revision_audit(assessment,admin):
 from nexloop_eios.object_edits import GovernedObjectEditor
 port,creator,p,obj,token=assessment
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  GovernedObjectEditor(port.pool,port.session,port.signer).edit(action_name='RelationshipAssessment.genericEdit',action_version=1,intent_id='generic-edit',type_name='RelationshipAssessment',object_id=obj,expected_revision=1,properties={'conclusion':'unaudited'})
 assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(p,1)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)

def test_actual_current_projection_excludes_hypothesis_and_pending(assessment):
 from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
 port,creator,p,obj,token=assessment
 projection=AuthorizedAssessmentProjection(port.pool,port.session,port.signer)
 got=projection.current(obj);assert got['formal']==[] and got['evidence'][0]['epistemic_kind']=='hypothesis'
 port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='pending-correction',object_id=obj,expected_revision=1,properties=p|{'resolution_state':'awaiting_definition','corrects_revision':1})
 got=projection.current(obj);assert got['formal']==[] and got['evidence'][0]['revision']==2

from local_message_assembly_fixture import assembled_message,business_plan,configured
@pytest.mark.parametrize('assembled_message',[False],indirect=True)
def test_real_human_message_correction_updates_current_basis(assembled_message,admin,tmp_path,monkeypatch):
 import json,hashlib
 from psycopg.conninfo import make_conninfo
 from nexloop_eios.assembly import open_core
 from nexloop_eios.browser_identity import open_browser_identity
 from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
 from test_browser_evidence import authenticate
 from test_browser_session_creation import create
 from nexloop_eios.browser_authorization import authenticate_browser_business
 from nexloop_eios.conversation_messages import ConversationMessagePort
 from nexloop_eios.postgres_artifacts import AuthoritySigner
 from nexloop_eios.action_definitions import PostgresActionDefinitionReader
 from nexloop_eios.authorization import authenticate_service
 from eios.ontology.definitions import ActionDefinition
 from eios.ontology.version_resolution import CapabilityContractSnapshot
 from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
 f=assembled_message;o=f['original'];tenant=o['tenant'];human=f['base']['human'];signer=AuthoritySigner('explicit-configuration',o['paths']['backend_signing'].read_bytes())
 with open_browser_identity(make_conninfo(o['pg'],user='nexloop_identity')) as identity_pool,open_core(make_conninfo(o['pg'],user='nexloop_api')) as pool:
  uow=PostgresBrowserSessionUnitOfWork(identity_pool,tenant_id=tenant,application_id=o['application'])
  identity=(make_conninfo(o['pg'],user='nexloop_identity'),uow.get_subject(human['subject_id']),uow.get_membership(tenant,human['principal_id']),uow.get_local_account(tenant,human['account_id']),o['paths']['password'].read_text())
  issued=create(uow,identity,authenticate(uow,identity).evidence)
  actual=authenticate_browser_business(pool,issued.session,world='real')
  messages=ConversationMessagePort(pool,actual,signer);conversation=messages.create_conversation(idempotency_key='assessment-human-conversation')
  body='I have not ended the relationship; I am away this month.'
  accepted=messages.accept_message(conversation_id=conversation['id'],idempotency_key='assessment-human-message',body=body)['message'];mid=accepted['id']
  original=next(a for a in f['manifest']['actions'] if a['definition']['stable_name']=='Consumer.create')
  definition=ActionDefinition.model_validate_json(json.dumps(original['definition']));cap=CapabilityContractSnapshot.model_validate_json(json.dumps(original['capability']))
  reader=PostgresActionDefinitionReader(pool,actual,signer)
  targets=[('eios:object:Message/'+mid,ResourceType.OBJECT,Operation.READ)]+[('eios:property:Message/'+mid+'/'+field,ResourceType.PROPERTY,Operation.READ) for field in ('actor','body')]
  port,creator,p,obj,token=prepare_assessment(reader,definition,cap,admin,f['setup']['consumer_id'],tenant,targets)
  projection=AuthorizedAssessmentProjection(pool,port.session,signer);assert projection.current(obj)['formal']==[]
  values=p|{'conclusion':body,'epistemic_kind':'user_statement','evidence_message_id':mid,'evidence_content_hash':hashlib.sha256(body.encode()).hexdigest(),'corrects_revision':1}
  import nexloop_eios.assessment_actions as actions
  original_canonical=actions.canonical_payload
  # Legitimate governor reservation and backend HMAC after bypassing only the
  # client-side semantic validator: SQL itself must reject unknown resolved definitions.
  with monkeypatch.context() as patched:
   patched.setattr(actions,'validate_assessment',lambda value,**kw:value)
   with pytest.raises(psycopg.errors.InsufficientPrivilege):
    GovernedAssessmentCreator(pool,port.session,signer).create(action_name='RelationshipAssessment.create',action_version=1,intent_id='sql-unknown-resolved-user',properties=values|{'relation_version':0,'corrects_revision':0})

  def missing_source_expiry(value):
   if value.get('protocol')=='nexloop-assessment-correct-v1':
    value=json.loads(json.dumps(value))
    proof=next(q for q in value['assessment_authorities'] if q['target_resource']=='eios:property:Message/'+mid+'/body')
    proof['expires_at']=None
   return original_canonical(value)
  with monkeypatch.context() as patched:
   patched.setattr(actions,'canonical_payload',missing_source_expiry)
   with pytest.raises(psycopg.errors.InsufficientPrivilege):
    port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='signed-null-message-source',object_id=obj,expected_revision=1,properties=values)
  assert admin.execute('select nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(1,)
  got=port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='actual-human-correction',object_id=obj,expected_revision=1,properties=values)
  current=projection.current(obj);assert got['revision']==2 and current['formal'][0]['properties']==values and current['evidence']==[]
  assert current['formal'][0]['epistemic_kind']=='user_statement'
  with pytest.raises(psycopg.errors.InsufficientPrivilege):
   port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='bad-source-hash',object_id=obj,expected_revision=2,properties=values|{'corrects_revision':2,'evidence_content_hash':'f'*64})
  assert projection.current(obj)==current
  pending=values|{'corrects_revision':2,'resolution_state':'awaiting_definition'}
  port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='pending-human-correction',object_id=obj,expected_revision=2,properties=pending)
  assert projection.current(obj)['formal']==[] and projection.current(obj)['evidence'][0]['revision']==3

  assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions where assessment_id=%s',(obj,)).fetchone()==(3,)
  from authority_fixture import replace_fact
  from eios.authz import facts as F
  from eios.authz.errors import AuthorizationUnavailable
  from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
  replace_fact(admin,tenant,'grants',[tenant+'-corrector-principal','eios:property:Message/'+mid+'/body'],F.GrantFacts,grants=[])
  fresh=authenticate_service(pool,token,world='real')
  with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied,psycopg.errors.InsufficientPrivilege)):
   AuthorizedAssessmentProjection(pool,fresh,signer).current(obj)


def test_actual_revoke_after_proof_denies_without_revision(assessment,admin,monkeypatch):
 import nexloop_eios.assessment_actions as actions
 from authority_fixture import replace_fact
 from eios.authz import facts as F
 from eios.authz.errors import AuthorizationUnavailable
 from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
 port,creator,p,obj,token=assessment;original=actions.canonical_payload
 def race(value):
  text=original(value)
  if value.get('protocol')=='nexloop-assessment-correct-v1':
   replace_fact(admin,'synthetic-a','grants',['synthetic-a-corrector-principal','eios:property:RelationshipAssessment/'+obj+'/conclusion'],F.GrantFacts,grants=[])
  return text
 monkeypatch.setattr(actions,'canonical_payload',race)
 with pytest.raises((psycopg.errors.InsufficientPrivilege,AuthorizationUnavailable,ActionAuthorizationDenied)):
  port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='revoke-correction',object_id=obj,expected_revision=1,properties=p|{'conclusion':'revoked','corrects_revision':1})
 assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(p,1)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)

def test_actual_commit_tail_expiry_rolls_back_head_audit_and_terminal(assessment,admin,monkeypatch):
 from datetime import timedelta
 import nexloop_eios.assessment_actions as actions
 port,creator,p,obj,token=assessment;original=actions.canonical_payload
 admin.execute("create function ontology.assessment_delay_owned() returns trigger language plpgsql as $$begin perform pg_sleep(.25);return new;end $$")
 admin.execute("create trigger assessment_delay_owned after update on ontology.objects for each row when (new.type_name='RelationshipAssessment') execute function ontology.assessment_delay_owned()")
 def short(value):
  if value.get('protocol')=='nexloop-assessment-correct-v1':value=dict(value,expires_at=(datetime.now(UTC)+timedelta(seconds=.1)).isoformat())
  return original(value)
 monkeypatch.setattr(actions,'canonical_payload',short)
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='tail-correction',object_id=obj,expected_revision=1,properties=p|{'conclusion':'expired','corrects_revision':1})
 assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(p,1)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)
 assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where intent_id='tail-correction'").fetchone()==('active',)

def test_actual_lease_tail_rolls_back_revision_and_terminal(assessment,admin,monkeypatch):
 from datetime import timedelta
 import nexloop_eios.assessment_actions as actions
 port,creator,p,obj,token=assessment;govern=actions.govern_published_action
 admin.execute("create function ontology.assessment_lease_delay_owned() returns trigger language plpgsql as $$begin perform pg_sleep(1.2);return new;end $$")
 admin.execute("create trigger assessment_lease_delay_owned after update on ontology.objects for each row when (new.type_name='RelationshipAssessment') execute function ontology.assessment_lease_delay_owned()")
 def shorter(*args,**kw):
  request=kw['claim_request'];kw['claim_request']=request.model_copy(update={'lease_expires_at':request.requested_at+timedelta(seconds=1)})
  return govern(*args,**kw)
 monkeypatch.setattr(actions,'govern_published_action',shorter)
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='lease-tail-correction',object_id=obj,expected_revision=1,properties=p|{'conclusion':'lease expired','corrects_revision':1})
 assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(p,1)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)
 assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where intent_id='lease-tail-correction'").fetchone()==('active',)

@pytest.mark.parametrize('fault',['command_expiry','permit_expiry','lease_expiry','object_expiry','property_expiry','endpoint_expiry','definition_expiry'])
def test_legitimately_signed_null_fences_fail_closed(assessment,admin,monkeypatch,fault):
 import json
 import nexloop_eios.assessment_actions as actions
 port,creator,p,obj,token=assessment;original=actions.canonical_payload
 def null(value):
  if value.get('protocol')=='nexloop-assessment-correct-v1':
   value=json.loads(json.dumps(value))
   if fault=='command_expiry':value['expires_at']=None
   elif fault=='permit_expiry':value['permit']['expires_at']=None
   elif fault=='lease_expiry':value['permit']['claim']['lease_expires_at']=None
   elif fault=='object_expiry':value['object_authority']['expires_at']=None
   elif fault=='property_expiry':value['property_authorities'][0]['expires_at']=None
   elif fault=='definition_expiry':next(q for q in value['assessment_authorities'] if q['target_resource'].startswith('eios:link_type:'))['expires_at']=None
   else:next(q for q in value['assessment_authorities'] if q['target_resource'].startswith('eios:object:Consumer/'))['expires_at']=None
  return original(value)
 monkeypatch.setattr(actions,'canonical_payload',null)
 with pytest.raises((psycopg.errors.InsufficientPrivilege,psycopg.errors.SerializationFailure)):
  port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='signed-null-'+fault,object_id=obj,expected_revision=1,properties=p|{'conclusion':'NULL must not commit','corrects_revision':1})
 assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(p,1)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)
 assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where intent_id=%s",('signed-null-'+fault,)).fetchone()==('active',)

@pytest.mark.parametrize('fault',['unknown_version','wrong_registered_endpoint_type','absent_definition'])
def test_registered_binding_mismatch_cannot_mutate(assessment,admin,fault):
 port,creator,p,obj,token=assessment
 if fault=='unknown_version':values=p|{'relation_version':2}
 elif fault=='absent_definition':values=p|{'relation_name':'not_registered'}
 else:values=p|{'target_type':'Message'}
 from eios.authz.errors import AuthorizationUnavailable
 from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
 with pytest.raises((psycopg.errors.InsufficientPrivilege,AuthorizationUnavailable,ActionAuthorizationDenied)):
  port.correct(action_name='RelationshipAssessment.correct',action_version=1,intent_id='bad-binding-'+fault,object_id=obj,expected_revision=1,properties=values|{'corrects_revision':1})
 assert admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(obj,)).fetchone()==(p,1)
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)

def test_current_relation_definition_read_revocation_denies(assessment,admin):
 from authority_fixture import replace_fact
 from eios.authz import facts as F
 from nexloop_eios.authorization import authenticate_service
 from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
 from eios.authz.errors import AuthorizationUnavailable
 from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
 port,creator,p,obj,token=assessment
 replace_fact(admin,'synthetic-a','grants',['synthetic-a-corrector-principal','eios:link_type:contact'],F.GrantFacts,grants=[])
 fresh=authenticate_service(port.pool,token,world='real')
 with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied,psycopg.errors.InsufficientPrivilege)):
  AuthorizedAssessmentProjection(port.pool,fresh,port.signer).current(obj)

def test_registered_endpoint_definition_type_is_checked_in_sql(assessment,admin):
 from eios.ontology.models import RelationTypeDefinition
 port,creator,p,obj,token=assessment
 definition=RelationTypeDefinition(relation_name='contact',version=2,source_type='Consumer',target_type='Message')
 admin.execute('insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,%s,%s,%s)',('synthetic-a','contact',2,Jsonb(definition.model_dump(mode='json'))))
 from nexloop_eios.authorization import authenticate_service
 fresh=authenticate_service(port.pool,token,world='real')
 create=GovernedAssessmentCreator(port.pool,fresh,port.signer)
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  create.create(action_name='RelationshipAssessment.create',action_version=1,intent_id='bad-definition-endpoint',properties=p|{'relation_version':2})
 assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions').fetchone()==(1,)

def test_unknown_definition_can_only_remain_nonformal_evidence(assessment,admin):
 from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
 port,creator,p,obj,token=assessment
 unknown=p|{'relation_name':'UnknownCandidate','relation_version':0}
 create=GovernedAssessmentCreator(port.pool,port.session,port.signer)
 zero=create.create(action_name='RelationshipAssessment.create',action_version=1,intent_id='unknown-hypothesis',properties=unknown)['object_id']
 targets=port.test_targets+[('eios:object:RelationshipAssessment/'+zero,ResourceType.OBJECT,Operation.READ)]+[('eios:property:RelationshipAssessment/'+zero+'/'+f,ResourceType.PROPERTY,Operation.READ) for f in FIELDS]
 session,token=seed_multi_authority(admin,port.pool,targets,identity_suffix='-zero-projection')
 got=AuthorizedAssessmentProjection(port.pool,session,port.signer).current(zero)
 assert got['formal']==[] and got['evidence'][0]['properties']==unknown
 with pytest.raises(ValueError):
  create.create(action_name='RelationshipAssessment.create',action_version=1,intent_id='unknown-resolved-user',properties=unknown|{'epistemic_kind':'user_statement'})

@pytest.mark.parametrize('fault',['object','property','definition'])
def test_legitimate_hmac_read_null_expiry_rejected(assessment,monkeypatch,fault):
 import json
 import nexloop_eios.assessment_actions as actions
 from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
 port,creator,p,obj,token=assessment;original=actions.canonical_payload
 def null(value):
  if value.get('protocol')=='nexloop-object-read-v1' and value.get('type_name')=='RelationshipAssessment' and len(value.get('fields',()))==len(FIELDS):
   value=json.loads(json.dumps(value))
   if fault=='object':value['expires_at']=None
   elif fault=='property':value['property_authorities'][0]['expires_at']=None
   else:value['relation_authority']['expires_at']=None
  return original(value)
 monkeypatch.setattr(actions,'canonical_payload',null)
 with pytest.raises(psycopg.errors.InsufficientPrivilege):
  AuthorizedAssessmentProjection(port.pool,port.session,port.signer).current(obj)

def test_legitimate_hmac_definition_lock_wait_rechecks_current_expiry(assessment,admin,monkeypatch):
 import json,threading,time
 from concurrent.futures import ThreadPoolExecutor
 from datetime import timedelta
 import nexloop_eios.assessment_actions as actions
 from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
 port,creator,p,obj,token=assessment;original=actions.canonical_payload;ready=threading.Event()
 def short(value):
  if value.get('protocol')=='nexloop-object-read-v1' and value.get('type_name')=='RelationshipAssessment' and len(value.get('fields',()))==len(FIELDS):
   value=json.loads(json.dumps(value));value['relation_authority']['expires_at']=(datetime.now(UTC)+timedelta(seconds=.3)).isoformat();ready.set()
  return original(value)
 monkeypatch.setattr(actions,'canonical_payload',short)
 with ThreadPoolExecutor(max_workers=1) as executor:
  with admin.transaction():
   admin.execute("select definition from ontology.relation_type_versions where tenant_id='synthetic-a' and relation_name='contact' and version=1 for update")
   future=executor.submit(AuthorizedAssessmentProjection(port.pool,port.session,port.signer).current,obj)
   assert ready.wait(5),'actual signed command did not arrive'
   time.sleep(.5);assert not future.done(),'actual read did not wait for owned definition row lock'
  with pytest.raises(psycopg.errors.InsufficientPrivilege):future.result(timeout=5)

"""Authenticated service trigger→durable PG→real Artifact→Role Context v3.

Source body remains statement data. Snapshot/bind reconstruct it from PG,
not an invented Message/Human or a model-approved formal fact.
"""
from datetime import datetime
import hashlib,hmac
from jsonschema import Draft202012Validator
from eios.authz.operations import Operation
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.run_credentials import AUDIENCE
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.context_pack import command_binding
from nexloop_eios.role_context_pack import encode_role_pack
from nexloop_eios.context_artifacts import artifact_authority_proof,MEDIA,ContextArtifactUnavailable
from nexloop_eios.runtime_activation import RuntimeActivationPort,_command
from nexloop_eios.role_runs import role_envelope_for_run
from nexloop_eios.service_offerings import _catalog_envelope,read_catalog,_read_envelope
from nexloop_eios.action_definitions import PostgresActionDefinitionReader

ACTION='nexloop.context.bind_role'

def role_context_binding_schema():
    return {'type':'object','properties':{'run_id':{'type':'string','format':'uuid'},'trigger_event_id':{'type':'string','format':'uuid'},'body':{'type':'string','minLength':1,'maxLength':8192}},'required':['run_id','trigger_event_id','body'],'additionalProperties':False}

class RoleContextArtifactProducer:
    def __init__(self,source):
        self.source=source;self.backend=source._backend
        self.pool,self.session,self.signer=self.backend._pool,source._session,self.backend._signer
        if self.session.run_context is not None or self.session.world!='real':raise ContextArtifactUnavailable()
        self.authority=RuntimeActivationPort(self.pool,self.session,self.signer)

    def _call(self,parameters,run,definition,capability):
        payload=canonical_payload(parameters)
        claims={'protocol':'nexloop-role-context-v1','key_id':self.signer.key_id,
          **self.authority._proof(self.session,'eios:action:'+ACTION+':1'),
          'parameters_digest':hashlib.sha256(payload.encode()).hexdigest(),
          'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json'),
          'run_proofs':self.authority._run_proofs(run),
          'role_envelope':role_envelope_for_run(self.pool,self.signer,'real',run.token_digest)}
        claims['catalog_envelope']=dict(zip(('text','signature','payload'),_catalog_envelope(self.source,
          offering_id=parameters['offering_id'],binding_id=parameters['binding_id'],consumer_id=parameters['command']['consumer_ref'].removeprefix('consumer:'),
          request_scope={'offering_id':parameters['offering_id'],'offering_revision':parameters['offering_revision'],'requested_guarantees':[],'requested_discounts':[]})))
        if parameters['verb']!='stage':
            selected=claims['role_envelope']['payload']
            import json
            selected=json.loads(selected)
            refs={'Consumer':(selected['consumer_id'],()),'Goal':(parameters['command']['goal_version_ref'].split(':')[1],()),'PlanStep':(selected['step_id'],()),'EffectControl':(parameters['control_id'],('allow_effect','budget_units','executor_principal','valid_until'))}
            claims['formal_reads']={kind:_read_envelope(self.source,kind,ref,fields) for kind,(ref,fields) in refs.items()}
        if parameters['verb']=='bind':
            claims['artifact_proofs']=[artifact_authority_proof(self.pool,self.session,op) for op in (Operation.CREATE,Operation.READ)]
        text=canonical_payload(claims);sig=hmac.new(self.signer.material,('nexloop-role-context-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            return db.execute('select authz.nexloop_role_context_command(%s,%s,%s,%s,%s)',(self.session.token_digest,'real',text,sig,payload)).fetchone()[0]

    def prepare(self,*,run_token,command,body,offering_id,binding_id,control_id):
        try:
            with self.backend._lock:
                self.backend._assert_open();_command(command)
                request={'run_id':command['run_id'],'trigger_event_id':command['trigger_event_id'],'body':body}
                Draft202012Validator(role_context_binding_schema(),format_checker=Draft202012Validator.FORMAT_CHECKER).validate(request)
                run=authenticate_service(self.pool,run_token,world='real',run_id=command['run_id'],audience=AUDIENCE)
                if run_token in body or run_token in canonical_payload(command):raise ValueError()
                definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(ACTION,1)
                if definition.model_dump(mode='json')['input_schema']!=role_context_binding_schema():raise ValueError()
                catalog=read_catalog(self.source,offering_id=offering_id,binding_id=binding_id,consumer_id=command['consumer_ref'].removeprefix('consumer:'))
                binding_text=canonical_payload(command_binding(command));binding_digest=hashlib.sha256(binding_text.encode()).hexdigest()
                params={'control_id':control_id,'verb':'stage','command':command,'body':body,'offering_id':offering_id,'binding_id':binding_id,'offering_revision':catalog['revision'],
                  'command_binding_text':binding_text,'command_binding_digest':binding_digest,
                  'artifact_identity_text':canonical_payload([self.session.authentication.tenant_id,'real',self.session.authentication.subject_principal_id,'context:'+command['run_id']])}
                staged=self._call(params,run,definition,capability)
                if staged.get('persisted') is not True:raise ValueError()
                snapshot=self._call({**params,'verb':'snapshot'},run,definition,capability)
                encoded=encode_role_pack(snapshot,command)
                retention=datetime.fromisoformat(snapshot['current_constraints']['valid_until'])
                artifact=self.source.put_artifact(request_id='context:'+command['run_id'],payload=encoded.encode(),media_type=MEDIA,retention_until=retention)
                # LocalArtifactService.put already compares store.read(ref) with
                # payload inside the authorized CREATE/finalize transaction.
                # Public unbound Context reads correctly remain unavailable.
                bound=self._call({**params,'verb':'bind','artifact_id':artifact.artifact_id,'pack_text':encoded,'pack_digest':artifact.sha256},run,definition,capability)
                if encode_role_pack(bound,command)!=encoded:raise ValueError()
                return {'artifact_ref':'artifact:'+artifact.artifact_id,'sha256':artifact.sha256,'input':encoded,'command_binding_digest':binding_digest}
        except Exception:raise ContextArtifactUnavailable() from None

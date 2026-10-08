"""Server-only real governed ContextArtifact producer. No credential reaches pack."""
from datetime import UTC,datetime,timedelta
import hashlib,hmac
from eios.actions import models as M
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import authenticate_service,PostgresAuthorityProvider
from nexloop_eios.run_credentials import AUDIENCE
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.runtime_activation import RuntimeActivationPort,_command
from nexloop_eios.effect_contexts import EffectContextRegistrar
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.context_pack import command_binding,encode_pack

ACTION='nexloop.context.bind'
MEDIA='application/vnd.nexloop.context+json'

def context_binding_schema():
    return {'type':'object','properties':{'message_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},'run_id':{'type':'string','format':'uuid'}},'required':['message_id','run_id'],'additionalProperties':False}

class ContextArtifactUnavailable(RuntimeError):
    def __init__(self):super().__init__('context_artifact_unavailable')
class ContextArtifactConflict(RuntimeError):
    code='context_artifact_conflict';http_status=409
    def __init__(self):super().__init__(self.code)

def artifact_authority_proof(pool,session,operation):
    entries=[];query=session.query(resource_id='eios:artifact:local_real',resource_type=ResourceType.ARTIFACT,operation=operation)
    decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session,entries)).resolve(query))
    if not decision.allowed or not decision.authoritative or decision.obligations:raise ContextArtifactUnavailable()
    return {'tenant_id':session.authentication.tenant_id,'principal_id':session.authentication.subject_principal_id,
     'credential_id':session.authentication.credential_id,'directory_hash':session.directory_hash,'world':'real',
     'resource_id':'eios:artifact:local_real','operation':operation.value,'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
     'facts':sorted(entries,key=lambda x:(x['kind'],x['key']))}


class ContextArtifactProducer:
    def __init__(self,services):
        self.services=services;self.backend=services._backend
        self.pool,self.session,self.signer=self.backend._pool,services._session,self.backend._signer
        if self.session.run_context is not None or self.session.world!='real':raise ContextArtifactUnavailable()
        self.authority=RuntimeActivationPort(self.pool,self.session,self.signer)

    def _artifact_proof(self,operation):
        return artifact_authority_proof(self.pool,self.session,operation)

    def _call(self,db,parameters,run,definition,capability,claim_binding=None):
        proof=self.authority._proof(self.session,'eios:action:'+ACTION+':1')
        body=canonical_payload(parameters)
        claims={'protocol':'nexloop-context-artifact-v1','key_id':self.signer.key_id,**proof,
         'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'definition':definition.model_dump(mode='json'),
         'capability':capability.model_dump(mode='json'),'artifact_proofs':[self._artifact_proof(Operation.CREATE),self._artifact_proof(Operation.READ)],'run_proofs':self.authority._run_proofs(run)}
        from nexloop_eios.service_offerings import _catalog_envelope
        claims['catalog_envelope']=dict(zip(('text','signature','payload'),_catalog_envelope(self.services,offering_id=parameters['offering_id'],binding_id=parameters['binding_id'],consumer_id=parameters['command']['consumer_ref'].removeprefix('consumer:'),request_scope={'offering_id':parameters['offering_id'],'offering_revision':parameters['offering_revision'],'requested_guarantees':[],'requested_discounts':[]})))
        if parameters['verb']=='bind':
            claims.update(artifact_proofs=[self._artifact_proof(Operation.CREATE),self._artifact_proof(Operation.READ)],claim_binding=claim_binding)
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-context-artifact-v1:'+text).encode(),'sha256').hexdigest()
        return db.execute('select authz.nexloop_context_artifact_command(%s,%s,%s,%s,%s)',(self.session.token_digest,'real',text,signature,body)).fetchone()[0]

    def prepare(self,*,message_id,run_token,command,offering_id,binding_id):
        """Actual snapshot→Artifact CREATE/READ→governed bind; no queue ACK here."""
        try:
            with self.backend._lock:
                self.backend._assert_open();_command(command)
                run=authenticate_service(self.pool,run_token,world='real',run_id=command['run_id'],audience=AUDIENCE)
                definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(ACTION,1)
                if definition.model_dump(mode='json')['input_schema']!=context_binding_schema() or definition.parameters:raise ValueError()
                from jsonschema import Draft202012Validator
                Draft202012Validator(context_binding_schema(),format_checker=Draft202012Validator.FORMAT_CHECKER).validate({'message_id':message_id,'run_id':command['run_id']})
                if definition.preconditions or definition.governance.policy_refs or definition.governance.approval_mode.value!='none' or definition.governance.risk_level.value!='low':raise ValueError()
                from nexloop_eios.service_offerings import read_catalog
                catalog=read_catalog(self.services,offering_id=offering_id,binding_id=binding_id,consumer_id=command['consumer_ref'].removeprefix('consumer:'))
                binding_text=canonical_payload(command_binding(command));binding_digest=hashlib.sha256(binding_text.encode()).hexdigest()
                parameters={'offering_id':offering_id,'binding_id':binding_id,'offering_revision':catalog['revision'],'verb':'snapshot','message_id':message_id,'command':command,'run_digest':run.token_digest,
                 'artifact_identity_text':canonical_payload([self.session.authentication.tenant_id,'real',self.session.authentication.subject_principal_id,'context:'+command['run_id']]),'command_binding_text':binding_text,'command_binding_digest':binding_digest}
                with self.pool.connection() as db,db.transaction():snapshot=self._call(db,parameters,run,definition,capability)
                text=encode_pack(snapshot,command)
                if run_token in text:raise ValueError()
                # Both calls execute the real existing Artifact authorization chain.
                retention=datetime.fromisoformat(snapshot['current_constraints']['valid_until'])
                ref=self.services.put_artifact(request_id='context:'+command['run_id'],payload=text.encode(),media_type=MEDIA,retention_until=retention)
                if self.services.read_artifact(ref.artifact_id)!=text.encode():raise ValueError()
                payload={**parameters,'verb':'bind','pack_text':text,'pack_digest':ref.sha256,'artifact_id':ref.artifact_id}
                request_hash=M.canonical_request_digest({'message_id':message_id,'command_binding':command_binding(command),'pack_digest':ref.sha256})
                claim_id='context-artifact:'+request_hash;payload['claim_id']=claim_id
                now=datetime.now(UTC)
                request=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,action_stable_name=ACTION,idempotency_key=claim_id),
                 binding=M.ClaimBindingPayload(invocation_id=claim_id,action_reference=definition.reference(),request_digest=request_hash,capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),
                 requested_at=now,lease_expires_at=now+timedelta(seconds=20))
                registrar=EffectContextRegistrar(self.pool,self.session,self.signer)
                with self.pool.connection() as db,db.transaction():
                    reservation=M.ActionClaimResult.model_validate_json(canonical_payload(registrar._claim(db,request,'reserve')))
                    if reservation.disposition not in (M.ActionClaimDisposition.CLAIMED,M.ActionClaimDisposition.REPLAY):raise ValueError()
                    result=self._call(db,payload,run,definition,capability,request.binding.model_dump(mode='json'))
                    if reservation.claim is not None:
                        at=db.execute('select clock_timestamp()').fetchone()[0]
                        outcome=M.TerminalOutcomeReference(outcome_id='artifact:'+ref.artifact_id,outcome_revision=1,status=M.TerminalOutcomeStatus.SUCCEEDED,outcome_digest=ref.sha256,finalized_at=at)
                        finalize=M.ActionClaimFinalizeCommand(key=request.key,binding=request.binding,expected_claim_revision=reservation.claim.claim_revision,fencing_token=reservation.claim.fencing_token,outcome=outcome)
                        registrar._claim(db,finalize,'finalize')
                    if self._call(db,payload,run,definition,capability,request.binding.model_dump(mode='json'))!=result:raise ValueError()
                return {'artifact_ref':'artifact:'+ref.artifact_id,'sha256':ref.sha256,'input':text,'command_binding_digest':binding_digest}
        except Exception as error:
            import psycopg
            if isinstance(error,psycopg.Error) and error.diag.message_primary=='context artifact conflict':raise ContextArtifactConflict() from None
            raise ContextArtifactUnavailable() from None

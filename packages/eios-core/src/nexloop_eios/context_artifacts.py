"""Server-only real governed ContextArtifact producer. No credential reaches pack."""
import re
from datetime import UTC,datetime,timedelta
from nexloop_eios.signing import SIGNED_COMMAND_TTL
import hashlib,hmac
from eios.actions import models as M
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import authenticate_service,PostgresAuthorityProvider,resolve_authority
from nexloop_eios.run_credentials import AUDIENCE
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.runtime_activation import RuntimeActivationPort,_command
from nexloop_eios.effect_contexts import EffectContextRegistrar
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.context_pack import command_binding,encode_pack

import logging
_log=logging.getLogger(__name__)
ACTION='nexloop.context.bind'
MEDIA='application/vnd.nexloop.context+json'
# NX-038 E3 wiring (option A, dispatcher 2026-10-22): the most the semantic read may add to a Context assembly.
SEMANTIC_READ_TIMEOUT_SECONDS=0.5

def context_binding_schema():
    return {'type':'object','properties':{'message_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},'run_id':{'type':'string','format':'uuid'}},'required':['message_id','run_id'],'additionalProperties':False}

def check_code(diagnostic):
    """10/18 stage (trial e80e360c stopped): which check refused a Context, as a code fit for logs and escalation evidence:
    the refusing SQL check's own fixed text with spaces as underscores (e.g. context_v6_pinned_source_missing), else the
    Python error class. Never a value, a body or a statement."""
    if not diagnostic:return None
    name,message=diagnostic
    if type(message) is str and re.fullmatch(r'[a-z0-9][a-z0-9 _.:-]{0,79}',message):return message.replace(' ','_')
    return 'python:'+name if type(name) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',name) else 'unrecognized'


def degraded_fallback_items(items):
    """ADR-023 degraded fallback (draft): ([item lists to try, largest first], extra insufficient). Every constraint, contact
    limit and refusal is in every list (never dropped); the customer's latest turn is in the first only; the pinned open work
    is named by reference as mandatory_exceeds_budget instead of carried; everything else (not pinned) is left out."""
    safety=[i for i in items if i.section=='constraints' or i.tags&{'negation','contact_limit'}]
    latest=[i for i in items if 'latest_consumer_turn' in i.tags and i not in safety]
    deferred=[i for i in items if i.section=='open_work' and i.pinned]
    extra=[{'code':'mandatory_exceeds_budget','section':'open_work','refs':[i.ref for i in deferred]}] if deferred else []
    return [safety+latest,safety],extra


class ContextArtifactUnavailable(RuntimeError):
    def __init__(self):super().__init__('context_artifact_unavailable')
class ContextArtifactConflict(RuntimeError):
    code='context_artifact_conflict';http_status=409
    def __init__(self):super().__init__(self.code)

def artifact_authority_proof(pool,session,operation):
    entries=[];query=session.query(resource_id='eios:artifact:local_real',resource_type=ResourceType.ARTIFACT,operation=operation)
    decision=AuthorizationDecisionService().decide_resolved(resolve_authority(pool,session,query,entries))
    if not decision.allowed or not decision.authoritative or decision.obligations:raise ContextArtifactUnavailable()
    return {'tenant_id':session.authentication.tenant_id,'principal_id':session.authentication.subject_principal_id,
     'credential_id':session.authentication.credential_id,'directory_hash':session.directory_hash,'world':'real',
     'resource_id':'eios:artifact:local_real','operation':operation.value,'expires_at':min(decision.expires_at,datetime.now(UTC)+SIGNED_COMMAND_TTL).isoformat(),
     'facts':sorted(entries,key=lambda x:(x['kind'],x['key']))}


class ContextArtifactProducer:
    def __init__(self,services):
        self.services=services;self.backend=services._backend
        self.pool,self.session,self.signer=self.backend._pool,services._session,self.backend._signer
        if self.session.run_context is not None or self.session.world!='real':raise ContextArtifactUnavailable()
        self.authority=RuntimeActivationPort(self.pool,self.session,self.signer)
        self.scope=None  # ADR-029a: the proofs of the current preparation only

    def _reuse(self,key,build):
        return build() if self.scope is None else self.scope.get(key,build)

    def _artifact_proof(self,operation):
        return self._reuse(('artifact',operation.value),lambda:artifact_authority_proof(self.pool,self.session,operation))

    def _object_envelope(self,type_name,object_id,fields):
        from nexloop_eios.service_offerings import _read_envelope
        return self._reuse(('object',type_name,object_id,tuple(sorted(fields))),lambda:_read_envelope(self.services,type_name,object_id,fields))

    def _read_catalog(self,*,offering_id,binding_id,consumer_id):
        """The snapshot's catalog read through the same signed object READ envelopes as this preparation's catalog envelope."""
        from nexloop_eios.object_reads import AuthorizedObjectReader
        from nexloop_eios.service_offerings import read_catalog
        reader=AuthorizedObjectReader(self.pool,self.session,self.signer)
        def read(type_name,object_id,fields):
            envelope=self._object_envelope(type_name,object_id,fields)
            return reader.read_signed(envelope['text'],envelope['signature'])
        return read_catalog(self.services,offering_id=offering_id,binding_id=binding_id,consumer_id=consumer_id,read=read)

    def _catalog(self,*,offering_id,binding_id,consumer_id,request_scope):
        from nexloop_eios.service_offerings import _catalog_envelope
        key=('catalog',offering_id,binding_id,consumer_id,canonical_payload(request_scope))
        return self._reuse(key,lambda:dict(zip(('text','signature','payload'),_catalog_envelope(self.services,offering_id=offering_id,binding_id=binding_id,
            consumer_id=consumer_id,request_scope=request_scope,read_envelope=self._object_envelope))))

    def _scoped(self):
        """A fresh PrepareScope for one preparation; dropped when it ends, whatever the outcome."""
        from contextlib import contextmanager
        from nexloop_eios.prepare_scope import PrepareScope
        @contextmanager
        def scope():
            self.scope=PrepareScope()
            try:yield self.scope
            finally:self.scope=None
        return scope()

    def _call(self,db,parameters,run,definition,capability,claim_binding=None):
        text,signature,body=self._envelope(parameters,run,definition,capability,claim_binding)
        return db.execute('select authz.nexloop_context_artifact_command(%s,%s,%s,%s,%s)',(self.session.token_digest,'real',text,signature,body)).fetchone()[0]

    def _envelope(self,parameters,run,definition,capability,claim_binding=None):
        proof=self._reuse(('action',ACTION),lambda:self.authority._proof(self.session,'eios:action:'+ACTION+':1'))
        body=canonical_payload(parameters)
        claims={'protocol':'nexloop-context-artifact-v1','key_id':self.signer.key_id,**proof,
         'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'definition':definition.model_dump(mode='json'),
         'capability':capability.model_dump(mode='json'),'artifact_proofs':[self._artifact_proof(Operation.CREATE),self._artifact_proof(Operation.READ)],
         'run_proofs':self._reuse(('run',run.token_digest),lambda:self.authority._run_proofs(run))}
        from nexloop_eios.postgres_artifacts import context_message_read_envelope
        claims['message_read_envelope']=self._reuse(('message',parameters['message_id']),lambda:context_message_read_envelope(self.pool,self.session,self.signer,parameters['message_id']))
        claims['catalog_envelope']=self._catalog(offering_id=parameters['offering_id'],binding_id=parameters['binding_id'],consumer_id=parameters['command']['consumer_ref'].removeprefix('consumer:'),request_scope={'offering_id':parameters['offering_id'],'offering_revision':parameters['offering_revision'],'requested_guarantees':[],'requested_discounts':[]})
        if parameters['verb']=='bind':
            claims.update(artifact_proofs=[self._artifact_proof(Operation.CREATE),self._artifact_proof(Operation.READ)],claim_binding=claim_binding)
        elif claim_binding is not None:claims['claim_binding']=claim_binding  # v6 core snapshot inside its bind
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-context-artifact-v1:'+text).encode(),'sha256').hexdigest()
        return text,signature,body

    def _snapshot(self,*,message_id,run_token,command,offering_id,binding_id):
        """Governed bind Action checks and the SQL-derived v2 snapshot (verb 'snapshot')."""
        _command(command)
        run=authenticate_service(self.pool,run_token,world='real',run_id=command['run_id'],audience=AUDIENCE)
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(ACTION,1)
        if definition.model_dump(mode='json')['input_schema']!=context_binding_schema() or definition.parameters:raise ValueError()
        from jsonschema import Draft202012Validator
        Draft202012Validator(context_binding_schema(),format_checker=Draft202012Validator.FORMAT_CHECKER).validate({'message_id':message_id,'run_id':command['run_id']})
        if definition.preconditions or definition.governance.policy_refs or definition.governance.approval_mode.value!='none' or definition.governance.risk_level.value!='low':raise ValueError()
        catalog=self._read_catalog(offering_id=offering_id,binding_id=binding_id,consumer_id=command['consumer_ref'].removeprefix('consumer:'))
        binding_text=canonical_payload(command_binding(command));binding_digest=hashlib.sha256(binding_text.encode()).hexdigest()
        parameters={'offering_id':offering_id,'binding_id':binding_id,'offering_revision':catalog['revision'],'verb':'snapshot','message_id':message_id,'command':command,'run_digest':run.token_digest,
         'artifact_identity_text':canonical_payload([self.session.authentication.tenant_id,'real',self.session.authentication.subject_principal_id,'context:'+command['run_id']]),'command_binding_text':binding_text,'command_binding_digest':binding_digest}
        with self.pool.connection() as db,db.transaction():snapshot=self._call(db,parameters,run,definition,capability)
        return run,definition,capability,parameters,binding_digest,snapshot

    def _claim_request(self,message_id,command,ref,definition,capability):
        request_hash=M.canonical_request_digest({'message_id':message_id,'command_binding':command_binding(command),'pack_digest':ref.sha256})
        claim_id='context-artifact:'+request_hash
        now=datetime.now(UTC)
        request=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,action_stable_name=ACTION,idempotency_key=claim_id),
         binding=M.ClaimBindingPayload(invocation_id=claim_id,action_reference=definition.reference(),request_digest=request_hash,capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),
         requested_at=now,lease_expires_at=now+timedelta(seconds=20))
        return claim_id,request

    def _finalize(self,db,registrar,request,reservation,ref):
        if reservation.claim is None:return
        at=db.execute('select clock_timestamp()').fetchone()[0]
        outcome=M.TerminalOutcomeReference(outcome_id='artifact:'+ref.artifact_id,outcome_revision=1,status=M.TerminalOutcomeStatus.SUCCEEDED,outcome_digest=ref.sha256,finalized_at=at)
        finalize=M.ActionClaimFinalizeCommand(key=request.key,binding=request.binding,expected_claim_revision=reservation.claim.claim_revision,fencing_token=reservation.claim.fencing_token,outcome=outcome)
        registrar._claim(db,finalize,'finalize')

    def prepare(self,*,message_id,run_token,command,offering_id,binding_id):
        """Actual snapshot→Artifact CREATE/READ→governed bind; no queue ACK here."""
        try:
            with self.backend._lock,self._scoped():
                self.backend._assert_open()
                run,definition,capability,parameters,binding_digest,snapshot=self._snapshot(message_id=message_id,run_token=run_token,command=command,offering_id=offering_id,binding_id=binding_id)
                text=encode_pack(snapshot,command)
                if run_token in text:raise ValueError()
                # Both calls execute the real existing Artifact authorization chain.
                retention=datetime.fromisoformat(snapshot['current_constraints']['valid_until'])
                ref=self.services.put_artifact(request_id='context:'+command['run_id'],payload=text.encode(),media_type=MEDIA,retention_until=retention)
                # put_artifact verifies fsynced bytes within the trusted upload transaction.
                # Unbound Context copies are never readable through the generic READ API.
                payload={**parameters,'verb':'bind','pack_text':text,'pack_digest':ref.sha256,'artifact_id':ref.artifact_id}
                claim_id,request=self._claim_request(message_id,command,ref,definition,capability);payload['claim_id']=claim_id
                registrar=EffectContextRegistrar(self.pool,self.session,self.signer)
                with self.pool.connection() as db,db.transaction():
                    reservation=M.ActionClaimResult.model_validate_json(canonical_payload(registrar._claim(db,request,'reserve')))
                    if reservation.disposition not in (M.ActionClaimDisposition.CLAIMED,M.ActionClaimDisposition.REPLAY):raise ValueError()
                    result=self._call(db,payload,run,definition,capability,request.binding.model_dump(mode='json'))
                    self._finalize(db,registrar,request,reservation,ref)
                    if self._call(db,payload,run,definition,capability,request.binding.model_dump(mode='json'))!=result:raise ValueError()
                return {'artifact_ref':'artifact:'+ref.artifact_id,'sha256':ref.sha256,'input':text,'command_binding_digest':binding_digest}
        except Exception as error:
            import psycopg
            if isinstance(error,psycopg.Error) and error.diag.message_primary=='context artifact conflict':raise ContextArtifactConflict() from None
            raise ContextArtifactUnavailable() from None


V6_PROTOCOL='nexloop-context-v6-v1'
ASSEMBLE_RESOURCE='eios:action:nexloop.context.assemble:1'


def v6_call(db,authority,session,signer,function,payload,proofs,run=None):
    """Signed v6 bind (EXECUTE nexloop.context.assemble:1 + the cited READ proofs).

    With ``run`` the assemble authority is the one issued with that Run (0104), which SQL
    accepts only for the pack's own Run; without it, the Source's standing grant.
    """
    if function not in ('authz.nexloop_context_v6_command','authz.nexloop_role_context_v6_command','authz.nexloop_context_v6_fallback_command'):raise ValueError('v6 bind function')
    body=canonical_payload(payload)
    from nexloop_eios.context_engine.authority import run_assemble_claims
    assemble=authority._proof(session,ASSEMBLE_RESOURCE) if run is None else run_assemble_claims(session,run)
    claims={**assemble,'protocol':V6_PROTOCOL,'key_id':signer.key_id,
        'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'read_proofs':list(proofs)}
    text=canonical_payload(claims);signature=hmac.new(signer.material,(V6_PROTOCOL+':'+text).encode(),'sha256').hexdigest()
    return db.execute('select '+function+'(%s,%s,%s,%s,%s)',(session.token_digest,'real',text,signature,body)).fetchone()[0]


class ContextV6ArtifactProducer(ContextArtifactProducer):
    """Message Run Context v6: the frozen v2 core plus Engine sections, bound by 0091.

    The Source assembles under its own current READ authority and the EXECUTE
    nexloop.context.assemble:1 issued with this Run (0104; no standing grant needed);
    SQL re-derives the core, re-verifies each source
    against its row and refuses an omitted pinned source. The pack grants nothing.
    """
    def __init__(self,services,strategy_id,relationship_recipe=None,formal_refs=None):
        super().__init__(services);self.strategy_id=strategy_id
        self.last_diagnostic=None;self.last_check=None;self.run=None;self.last_mismatch=None;self._sent_items=None
        self.degrade=False  # only the fallback reply Run may degrade (FallbackContextV6Producer)
        # NX-038 E3 wiring: per-producer counts of the semantic read (recorded, timed out, failed) and the Run it last read for.
        self.semantic={'read':0,'timeout':0,'failed':0};self._semantic_run=None
        # 0227 operator persona slice 1: the message Run bind (0227 copy of 0187) takes the section; the fallback bind does not yet.
        self.operator_persona=True
        # With relationship assessments the core is the frozen v4 chain (0076), not v2.
        self.core=None
        if relationship_recipe is not None:
            from nexloop_eios.relationship_context_artifacts import RelationshipContextArtifactProducer
            self.core=RelationshipContextArtifactProducer(services,relationship_recipe,formal_refs)

    def _envelope(self,parameters,run,definition,capability,claim_binding=None):
        if self.core is None:return super()._envelope(parameters,run,definition,capability,claim_binding)
        return self.core._envelope(parameters,run,definition,capability,claim_binding)

    def _call(self,db,parameters,run,definition,capability,claim_binding=None):
        if self.core is None:return super()._call(db,parameters,run,definition,capability,claim_binding)
        return self.core._call(db,parameters,run,definition,capability,claim_binding)

    def _v6_call(self,db,payload,proofs):
        return v6_call(db,self.authority,self.session,self.signer,'authz.nexloop_context_v6_command',payload,proofs,run=self.run)

    def assemble(self,snapshot,command):
        """(pack body, outcome, items, proofs) from the SQL core snapshot and current sources."""
        import json
        from nexloop_eios.context_engine.pack import assemble_v6
        from nexloop_eios.context_engine.sources import collect
        from nexloop_eios.context_engine.strategy import StrategyRegistry
        if self.core is None:core=json.loads(encode_pack(snapshot,command))
        else:
            from nexloop_eios.relationship_context_pack import encode_pack as encode_v4
            core=json.loads(encode_v4(snapshot,command))
        strategy=StrategyRegistry(self.pool,self.session,self.signer,run=self.run).get(self.strategy_id)
        if strategy is None:raise ValueError('context strategy unavailable')
        consumer=command['consumer_ref'].removeprefix('consumer:')
        self._semantic_read(strategy.definition,core['user_statement'],command['run_id'])
        found=collect(self.pool,self.session,self.signer,consumer_id=consumer,conversation_ids=[core['user_statement']['conversation_id']],strategy=strategy.definition,
            current_message_id=core['user_statement']['message_id'],current_sequence=core['user_statement']['sequence'])
        goal=next(f for f in core['formal_facts'] if f['type']=='Goal')
        statement=core['user_statement']
        persona=self._operator_persona(consumer) if self.operator_persona else None
        def build(items,extra=()):
            return assemble_v6(strategy=strategy.definition,bindings=core['bindings'],role=None,
                current_event={'kind':'consumer_message','message_id':statement['message_id'],'provenance':statement['provenance']},user_statement=statement,
                goal={'goal_version_refs':[f"goal:{goal['id']}@{goal['revision']}"],'control_snapshot':found.control},
                formal_facts=core['formal_facts'],current_constraints=core['current_constraints'],supply=core['supply'],items=items,
                extra_insufficient=list(found.insufficient)+list(extra),relationship_context=core.get('relationship_context'),effect_supplies=core.get('effect_supplies'),
                operator_persona=persona)
        try:
            body,outcome=build(found.items)
            return body,outcome,found.items,found.proofs
        except ValueError as error:
            if str(error)!='context_pack_too_large' or not self.degrade:raise
        # ADR-023 degraded fallback (draft, owner 2026-10-18): the pinned sources alone exceed the bound input size. Every
        # contact limit and refusal stays (safety); the open work it cannot carry (unconfirmed intents and replies, plans,
        # commitments) is named, by reference, as mandatory_exceeds_budget (SQL accepts that for a fallback Run only); the
        # customer's latest turn stays when it fits. If the contact limits alone do not fit, no Context: the owner is escalated.
        candidates,extra=degraded_fallback_items(found.items)
        for items in candidates:
            try:
                body,outcome=build(items,extra)
                return body,outcome,items,found.proofs
            except ValueError as error:
                if str(error)!='context_pack_too_large':raise
        raise ValueError('fallback_context_unsafe')

    def _semantic_read(self,strategy,statement,run_id):
        """NX-038 E3 wiring, option A (dispatcher 2026-10-22; design §2.2): while the strategy's semantic.enabled is on, one browse
        of the current statement through SemanticAdapter with a SemanticAccessRecorder bound to this Run (tenant and world are the
        service identity's, checked again by the SQL port). Its result is not put in the pack (the bind verifies no definition item;
        option B is a follow-up), so the model sees no definition and no use record is written; context_id stays empty (the Context
        is bound after this). Best effort and bounded: at most SEMANTIC_READ_TIMEOUT_SECONDS, once per Run; a timeout or an error is
        counted here and never fails the assembly or the Run."""
        sem=strategy.get('semantic') or {}
        if sem.get('enabled') is not True or self._semantic_run==run_id or not (statement or {}).get('body'):return
        self._semantic_run=run_id
        import concurrent.futures
        from nexloop_eios.context_engine.semantic import SemanticAccessRecorder,SemanticAdapter,SemanticConfig
        from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
        pool,session,signer,body=self.pool,self.session,self.signer,statement['body']
        def read():
            recall=OntologyRecall(pool,session,authorizer=EiosRecallAuthorizer(pool,session))  # full text only: no embedding call
            recorder=SemanticAccessRecorder(pool,session,signer,run_id=run_id)
            SemanticAdapter(pool,session,signer,recall,config=SemanticConfig(top_k=sem['top_k'],ambiguity_margin=sem['ambiguity_margin']),
                recorder=recorder).browse(body,limit=sem['top_k'])
            return recorder
        worker=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            recorder=worker.submit(read).result(timeout=SEMANTIC_READ_TIMEOUT_SECONDS)
            self.semantic['read' if recorder.written else 'failed']+=1
        except concurrent.futures.TimeoutError:
            self.semantic['timeout']+=1;_log.warning('semantic read timed out')
        except Exception as error:  # never the Run's failure (design D2); the type only, never content
            self.semantic['failed']+=1;_log.warning('semantic read failed: %s',type(error).__name__)
        finally:worker.shutdown(wait=False)

    def _operator_persona(self,consumer):
        """0227: the tenant's operator persona section for this Consumer (persona@version, document, AI disclosure), or None.
        SQL re-derives it at bind and refuses a pack that differs or omits it."""
        from nexloop_eios.assembly import verify_application_role
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_context_operator_persona(%s,%s,%s)',(self.session.token_digest,'real',consumer)).fetchone()[0]

    def prepare(self,*,message_id,run_token,command,offering_id,binding_id):
        """10/18 stage (refusal-2 fallback dc7ff6f3: context source content mismatch). This Run's Artifact id is fixed by its
        run id and the stored pack cannot be replaced, so a refused pack is not bound again. Instead the sources are read once
        more and the check names what changed: context_source_changed:<section>_<subsection>... (a source moved on between the
        read and the bind) or context_source_content_mismatch_stable (the pack disagrees with unchanged sources). Codes only."""
        import psycopg
        self.last_mismatch=None
        try:return self._prepare_once(message_id=message_id,run_token=run_token,command=command,offering_id=offering_id,binding_id=binding_id)
        except ContextArtifactUnavailable:
            if self.last_check!='context_source_content_mismatch' or self._sent_items is None:raise
            sent=self._sent_items
            try:
                with self.backend._lock,self._scoped():
                    self.backend._assert_open()
                    *_,snapshot=self._snapshot(message_id=message_id,run_token=run_token,command=command,offering_id=offering_id,binding_id=binding_id)
                    _,_,items,_=self.assemble(snapshot,command)
                now={(i.section,i.ref):(i.revision,i.content) for i in items}
                changed=sorted({key[0]+'_'+sub for key,sub,seen in sent if now.get(key,_GONE)!=seen})
            except (psycopg.Error,ValueError,RuntimeError,LookupError):raise ContextArtifactUnavailable() from None
            self.last_mismatch={'changed':changed}
            self.last_check=(('context_source_changed:'+':'.join(changed))[:80] if changed else 'context_source_content_mismatch_stable')
            raise ContextArtifactUnavailable() from None

    def _prepare_once(self,*,message_id,run_token,command,offering_id,binding_id):
        from nexloop_eios.context_engine.pack import wire_item
        self._sent_items=None
        try:
            with self.backend._lock,self._scoped():
                self.backend._assert_open()
                run,definition,capability,parameters,binding_digest,snapshot=self._snapshot(message_id=message_id,run_token=run_token,command=command,offering_id=offering_id,binding_id=binding_id)
                self.run=run  # assemble authority issued with this Run
                if snapshot.get('schema_version')=='nexloop.context-pack.v6':
                    # Already bound (retry after a lost ACK): replay the exact stored pack; SQL
                    # verified its sources when it was bound and now rechecks core/Artifact/claim.
                    body,omitted,proofs=snapshot,[],{}
                else:
                    body,outcome,items,proofs=self.assemble(snapshot,command)
                    by_ref={(i.section,i.ref):i for i in items}
                    self._sent_items=[((i.section,i.ref),i.subsection,(i.revision,i.content)) for i in items]
                    omitted=[{'section':o['section'],'reason':o['reason'],'item':wire_item(by_ref[(o['section'],o['ref'])])} for o in outcome.report['omitted']]
                text=canonical_payload(body)
                if run_token in text:raise ValueError()
                retention=datetime.fromisoformat(snapshot['current_constraints']['valid_until'])
                ref=self.services.put_artifact(request_id='context:'+command['run_id'],payload=text.encode(),media_type=MEDIA,retention_until=retention)
                claim_id,request=self._claim_request(message_id,command,ref,definition,capability)
                binding=request.binding.model_dump(mode='json')
                registrar=EffectContextRegistrar(self.pool,self.session,self.signer)
                def bind(db):
                    core=dict(zip(('text','signature','payload'),self._envelope(parameters,run,definition,capability,binding)))
                    return self._v6_call(db,{'verb':'bind','core':core,'pack_text':text,'pack_digest':ref.sha256,'artifact_id':ref.artifact_id,
                        'claim_id':claim_id,'omitted_items':omitted},proofs.values())
                with self.pool.connection() as db,db.transaction():
                    reservation=M.ActionClaimResult.model_validate_json(canonical_payload(registrar._claim(db,request,'reserve')))
                    if reservation.disposition not in (M.ActionClaimDisposition.CLAIMED,M.ActionClaimDisposition.REPLAY):raise ValueError()
                    result=bind(db)
                    self._finalize(db,registrar,request,reservation,ref)
                    if bind(db)!=result:raise ValueError()
                return {'artifact_ref':'artifact:'+ref.artifact_id,'sha256':ref.sha256,'input':text,'command_binding_digest':binding_digest,
                    'context_id':result['context_id'],'strategy_ref':result['strategy_ref'],'insufficient':body['insufficient']}
        except Exception as error:
            import psycopg
            # Operator-only: which SQL check refused (never returned to the Run or Host).
            self.last_diagnostic=(type(error).__name__,getattr(getattr(error,'diag',None),'message_primary',None)
                or (str(error) if isinstance(error,ValueError) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',str(error)) else None))
            self.last_check=check_code(self.last_diagnostic)
            if isinstance(error,psycopg.Error) and error.diag.message_primary=='context artifact conflict':raise ContextArtifactConflict() from None
            raise ContextArtifactUnavailable() from None


_GONE=object()


class FallbackContextV6Producer(ContextV6ArtifactProducer):
    """ADR-023 §2.7 fallback reply Run: the same message Context v6 (this inbound message as the user statement, recent
    conversation, pinned constraints such as a contact restriction), bound over the fallback issuance (0110)."""
    def __init__(self,services,strategy_id):
        super().__init__(services,strategy_id)
        self.degrade=True
        self.operator_persona=False  # the fallback bind (0212) accepts no operator persona section yet

    def _call(self,db,parameters,run,definition,capability,claim_binding=None):
        text,signature,body=self._envelope(parameters,run,definition,capability,claim_binding)
        return db.execute('select authz.nexloop_context_artifact_fallback_command(%s,%s,%s,%s,%s)',(self.session.token_digest,'real',text,signature,body)).fetchone()[0]

    def _v6_call(self,db,payload,proofs):
        return v6_call(db,self.authority,self.session,self.signer,'authz.nexloop_context_v6_fallback_command',payload,proofs,run=self.run)

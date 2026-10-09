"""Trusted EIOS effect execution port over the protected PostgreSQL lineage.

Uses append-only0042 signed SQL boundaries. All candidate/hint data is private,
non-authorizing and revalidated under protected PG locks. No provider IO occurs
here. Source and executor are real independently authenticated EIOS identities.
No public reserve is called before an independent effect transaction.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import json

import psycopg
import uuid
import re

from eios.actions import models as M
from eios.actions.governance import ActionGovernor
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.action_governor import UnavailableApprovalAuthority,UnavailableApprovalPort
from nexloop_eios.authorization import PostgresAuthorityProvider,_identity,resolve_authority
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload

SEND='eios:action:nexloop.service.request:1'
QUERY='eios:action:nexloop.service.query:1'
RECONCILE='eios:action:nexloop.service.receipt_reconcile:1'

def receipt_reconcile_schema():
    return {'type':'object','properties':{'intent_id':{'type':'string','format':'uuid'},
        'effect_fence':{'type':'integer','minimum':1},'query_id':{'type':'string','format':'uuid'}},
        'required':['intent_id','effect_fence','query_id'],'additionalProperties':False}

PROTOCOL='nexloop-effect-execution-v1'


class EffectExecutionUnavailable(PermissionError):
    def __init__(self):super().__init__('effect_execution_unavailable')


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
    return value


def _profile(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{64}',value) is None:raise ValueError()
    return value


def _lease(value):
    if type(value) is not int or not 3<=value<=300:raise ValueError()
    return value


def _identity_arguments(intent_id,fence):
    _uuid(intent_id)
    if type(fence) is not int or not 1<=fence<=9223372036854775807:raise ValueError()
    return {'intent_id':intent_id,'effect_fence':fence}


class _AtomicReserve:
    """PRIVATE Governor port: real signed definer, same existing transaction.

    The callback returns actual ActionClaimResult from SQL. It never returns an
    invented permit, does not open a connection and cannot be used by Runtime.
    """
    def __init__(self,execute):self._execute=execute
    def reserve(self,command):return self._execute(command)


class EffectExecutionPort:
    def __init__(self,pool,session,signer):
        if session.run_context is not None or session.world!='real':raise EffectExecutionUnavailable()
        self.pool,self.session,self.signer=pool,session,signer
        with pool.connection() as db:
            if verify_application_role(db)!='nexloop_action_worker':raise EffectExecutionUnavailable()

    def _proof(self,session,target):
        entries=[]
        query=session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(
            resolve_authority(self.pool,session,query,entries))
        if not decision.allowed or not decision.authoritative or decision.obligations:
            raise AuthorizationUnavailable('effect_execution_unavailable')
        return {'tenant_id':session.authentication.tenant_id,'principal_id':session.authentication.subject_principal_id,
            'credential_id':session.authentication.credential_id,'directory_hash':session.directory_hash,
            'world':session.world,'resource_id':target,'action_resource':target,'operation':'execute',
            'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))},decision

    def _signed(self,verb,*,target,**parameters):
        catalog=parameters.pop('catalog_envelope',None)
        role=parameters.pop('role_envelope',None)
        proof,_=self._proof(self.session,target)
        name,version=target.removeprefix('eios:action:').rsplit(':',1)
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(name,int(version))
        payload=canonical_payload({'verb':verb,**parameters})
        if len(payload.encode())>1048576:raise ValueError()
        claims={'protocol':PROTOCOL,'key_id':self.signer.key_id,**proof,
                'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json'),
                'parameters_digest':hashlib.sha256(payload.encode()).hexdigest()}
        if catalog is not None:claims['catalog_envelope']=catalog
        if role is not None:
            claims['role_envelope']=role
            from nexloop_eios.role_runs import formal_reads_for_role
            claims['formal_reads']=formal_reads_for_role(self.pool,self.signer,self.session,role)
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        return text,signature,payload

    def _execute(self,db,verb,*,target,**parameters):
        verify_application_role(db)
        return db.execute('select authz.nexloop_effect_execution_command(%s,%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,*self._signed(verb,target=target,**parameters))).fetchone()[0]

    def _call(self,verb,*,target,**parameters):
        with self.pool.connection() as db,db.transaction():
            return self._execute(db,verb,target=target,**parameters)

    def _bundle(self,metadata):
        definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get('nexloop.service.request',1)
        # Published version may not be silently substituted for accepted intent.
        if (definition.model_dump(mode='json')!=metadata['action_definition']
            or capability.model_dump(mode='json')!=metadata['capability']):raise ValueError()
        contract=definition.governance
        if (contract.approval_mode.value!='none' or contract.risk_level.value!='low'
            or contract.policy_refs or definition.preconditions or definition.parameters):raise ValueError()
        return definition,capability

    def _claim_command(self,db,metadata,lease_seconds):
        definition,capability=self._bundle(metadata)
        now=db.execute('select clock_timestamp()').fetchone()[0]
        until=now+timedelta(seconds=lease_seconds)
        if metadata.get('lease_until') is not None:
            until=min(until,datetime.fromisoformat(metadata['lease_until'].replace('Z','+00:00')))
        if until<=now:raise ValueError()
        request=metadata['frozen_request'];intent=_uuid(metadata['intent_id'])
        command=M.ActionClaimRequest(
            key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,
                action_stable_name=definition.stable_name,idempotency_key=intent),
            binding=M.ClaimBindingPayload(invocation_id=intent,action_reference=definition.reference(),
                request_digest=M.canonical_request_digest(request),capability_binding=capability.binding(),
                adapter_id='nexloop.effect',target_system='service'),
            requested_at=now,lease_expires_at=until)
        return definition,capability,command

    def _action_envelope(self,command,verb):
        # Same existing frozen action-command wire protocol; preparation only.
        # No PostgresActionClaimPort.reserve/finalize (and no independent commit).
        model=M.ActionClaimRequest if verb=='reserve' else M.ActionClaimFinalizeCommand
        command=model.model_validate_json(command.model_dump_json())
        if command.key.tenant_id!=self.session.authentication.tenant_id:raise ValueError()
        target=f'eios:action:{command.binding.action_reference.stable_name}:{command.binding.action_reference.version}'
        proof,decision=self._proof(self.session,target)
        payload=canonical_payload(command.model_dump(mode='json'))
        claims={'protocol':'nexloop-action-command-v1','key_id':self.signer.key_id,**proof,
            'permit_id':decision.decision_id,'verb':verb,
            'parameters_digest':hashlib.sha256(payload.encode()).hexdigest()}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-action-command-v1:'+text).encode(),'sha256').hexdigest()
        return {'text':text,'signature':signature,'payload':payload}

    def _reserve_in_transaction(self,db,metadata,verb,*,lease_seconds,**arguments):
        definition,capability,command=self._claim_command(db,metadata,lease_seconds)
        holder={}
        def reserve(actual_command):
            # SQL locks effect and business controls, executes the real original
            # Action claim definer, persists ownership, then returns actual result.
            extra={'lease_seconds':lease_seconds} if verb=='claim' else {}
            try:
                row=self._execute(db,verb,target=SEND,**arguments,**extra,
                    action_request_text=canonical_payload(metadata['frozen_request']),
                    action_claim=self._action_envelope(actual_command,'reserve'))
            except psycopg.errors.SerializationFailure as error:
                # Frozen Governor intentionally converts unavailable claim
                # ports to a denial. Preserve only this actual SQL conflict so
                # the outer fresh-transaction retry can distinguish it.
                holder['serialization_failure']=error
                raise
            holder['row']=row
            return M.ActionClaimResult.model_validate_json(canonical_payload(row['action_claim_result']))
        def clock():return db.execute('select clock_timestamp()').fetchone()[0]
        governor=ActionGovernor(claim_port=_AtomicReserve(reserve),approval_port=UnavailableApprovalPort(),
            authority_verifier=UnavailableApprovalAuthority(),clock=clock)
        evidence=M.PolicyEvidenceSet(tenant_id=self.session.authentication.tenant_id,
            invocation_id=command.binding.invocation_id,action_reference=definition.reference(),
            required_policy_references=(),evidence=(),evaluated_at=clock())
        try:
            permit=governor.govern(tenant_id=self.session.authentication.tenant_id,
                invocation_id=command.binding.invocation_id,action_reference=definition.reference(),
                action_definition=definition,capability_snapshot=capability,request=metadata['frozen_request'],
                claim_request=command,granted_scopes=self.session.authentication.requested_scopes,
                policy_evidence=evidence,approval_evidence=None)
        except Exception:
            if 'serialization_failure' in holder:raise holder['serialization_failure']
            raise
        if 'serialization_failure' in holder:raise holder['serialization_failure']
        if type(permit) is not M.ActionExecutionPermit:raise ValueError()
        return holder['row'],permit.claim

    def _resolve(self,identity):
        metadata=self._call('resolve',target=QUERY,**identity)
        if metadata['intent_id']!=identity['intent_id'] or metadata['effect_fence']!=identity['effect_fence']:raise ValueError()
        return metadata

    def _origin_proof(self,metadata):
        # Only an owned lease's signed definer may reveal this private digest.
        # No _identity from an arbitrary Run UUID or caller-provided digest.
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db);run=_identity(db,metadata['_run_digest'],self.session.world)
        if run.run_context is None or run.run_context.run_id!=metadata['origin_run_id']:raise ValueError()
        if SEND not in run.run_context.allowed_resources:raise ValueError()
        proof,_=self._proof(run,SEND)
        return proof

    def _role_envelope(self,metadata):
        from nexloop_eios.role_runs import role_envelope_for_run
        return role_envelope_for_run(self.pool,self.signer,self.session.world,metadata['_run_digest'])

    def _catalog_envelope(self,metadata):
        # Private owned resolve, then genuine original Run and Source proofs.
        from nexloop_eios.service_offerings import catalog_envelope_from_hint
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            run=_identity(db,metadata['_run_digest'],self.session.world)
        if run.run_context is None or run.run_context.run_id!=metadata['origin_run_id']:raise ValueError()
        proof,_=self._proof(run,SEND)
        payload=canonical_payload({'verb':'find','intent_id':metadata['intent_id'],'action_version':1})
        claims={'protocol':'nexloop-effect-intent-v1','key_id':self.signer.key_id,**proof,
            'parameters_digest':hashlib.sha256(payload.encode()).hexdigest()}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-effect-intent-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            hint=db.execute('select authz.nexloop_effect_catalog_hint(%s,%s,%s,%s,%s)',
                (run.token_digest,run.world,text,signature,payload)).fetchone()[0]
        return catalog_envelope_from_hint(self.pool,self.signer,run.world,hint)

    def claim_effect(self,*,lease_seconds=30):
        try:
            _lease(lease_seconds)
            # A hint is only a candidate. Concurrent Workers may invalidate it
            # before claim; retry the whole fresh-authority transaction, never
            # an old signed claim or an external effect.
            for retry in range(3):
                try:
                    candidate=self._call('hint',target=QUERY)
                    if candidate is None:return None
                    if candidate['has_attempt'] is True:
                        row=self._call('claim',target=QUERY,intent_id=candidate['intent_id'],lease_seconds=lease_seconds)
                    elif candidate['has_attempt'] is False:
                        with self.pool.connection() as db,db.transaction():
                            row,_=self._reserve_in_transaction(db,candidate,'claim',lease_seconds=lease_seconds,
                                intent_id=candidate['intent_id'])
                    else:raise ValueError()
                    return {'intent_id':row['intent_id'],'fence':row['effect_fence'],'lease_until':row['lease_until'],
                        'stage':'reconcile' if row['has_attempt'] is True else 'fresh'}
                except psycopg.errors.SerializationFailure:
                    if retry == 2:raise
        except Exception:raise EffectExecutionUnavailable() from None

    def prepare_effect_dispatch(self,*,intent_id,fence,provider_profile_digest):
        try:
            identity=_identity_arguments(intent_id,fence);metadata=self._resolve(identity)
            proof=self._origin_proof(metadata)
            claim=M.ActionClaim.model_validate_json(canonical_payload(metadata['action_claim']))
            result=self._call('admit',target=SEND,**identity,origin_proof=proof,catalog_envelope=self._catalog_envelope(metadata),role_envelope=self._role_envelope(metadata),provider_profile_digest=_profile(provider_profile_digest),
                action_claim_revision=claim.claim_revision,action_fencing_token=claim.fencing_token)
            # Commit has completed before these trusted frozen parameters escape.
            return {key:result[key] for key in ('parameters','provider_payload_digest')}
        except Exception:raise EffectExecutionUnavailable() from None

    def record_effect_unknown(self,*,intent_id,fence):
        try:
            identity=_identity_arguments(intent_id,fence);metadata=self._resolve(identity)
            result=self._call('unknown',target=QUERY,**identity,attempt_revision=metadata['attempt_revision'])
            if type(result) is not dict or result.get('state')!='unknown' or result.get('business_action_success') is not False:raise ValueError()
            return {'state':'unknown','business_action_success':False}
        except Exception:raise EffectExecutionUnavailable() from None

    def record_effect_observation(self,*,intent_id,fence,provider_profile_digest,provider_payload_digest,provider_state,provider_reference):
        try:
            identity=_identity_arguments(intent_id,fence)
            metadata=self._resolve(identity)
            observed=self._call('observe',target=QUERY,**identity,attempt_revision=metadata['attempt_revision'],
                provider_profile_digest=_profile(provider_profile_digest),
                provider_payload_digest=provider_payload_digest,provider_state=provider_state,provider_reference=provider_reference)
            # Independent READ observation is durable evidence, not finalization.
            if provider_state!='fulfilled':return self._public_observation(observed)
            return self._finalize_known_observation(intent_id=intent_id,fence=fence,observed=observed,
                provider_profile_digest=provider_profile_digest,provider_payload_digest=provider_payload_digest,
                provider_state=provider_state,provider_reference=provider_reference)
        except Exception:raise EffectExecutionUnavailable() from None

    def _finalize_known_observation(self,*,intent_id,fence,observed,provider_profile_digest,provider_payload_digest,provider_state,provider_reference):
        try:
            identity=_identity_arguments(intent_id,fence)
            metadata=self._resolve(identity)
            try:
                origin=self._origin_proof(metadata)
                self._proof(self.session,SEND)
                catalog=self._catalog_envelope(metadata)
            except (AuthorizationUnavailable,psycopg.errors.InsufficientPrivilege):
                return self._public_observation(observed)
            with self.pool.connection() as db,db.transaction():
                # Both phases are same connection + transaction. SQL pins xid,
                # observation_id and exact new Action/effect fence between them.
                _,_,request=self._claim_command(db,metadata,30)
                renewed=self._execute(db,'finalize',target=SEND,**identity,phase='reserve',provider_profile_digest=provider_profile_digest,catalog_envelope=catalog,role_envelope=self._role_envelope(metadata),
                    origin_proof=origin,observation_id=observed['observation_id'],attempt_revision=metadata['attempt_revision'],
                    action_request_text=canonical_payload(metadata['frozen_request']),
                    action_claim=self._action_envelope(request,'reserve'))
                # No forged CLAIMED disposition: SQL may return actual
                # IN_PROGRESS plus the exact current owned active Action claim.
                disposition=M.ActionClaimResult.model_validate_json(canonical_payload(renewed['action_claim_result']))
                if disposition.disposition not in {M.ActionClaimDisposition.CLAIMED,M.ActionClaimDisposition.IN_PROGRESS}:raise ValueError()
                claim=M.ActionClaim.model_validate_json(canonical_payload(renewed['action_claim']))
                if claim.key!=request.key or claim.binding!=request.binding or claim.state is not M.ActionClaimState.ACTIVE:raise ValueError()
                now=db.execute('select clock_timestamp()').fetchone()[0]
                outcome={'intent_id':intent_id,'receipt_id':metadata['receipt_id'],'provider_payload_digest':provider_payload_digest,
                    'provider_state':provider_state,'provider_reference':provider_reference}
                command=M.ActionClaimFinalizeCommand(key=claim.key,binding=claim.binding,
                    expected_claim_revision=claim.claim_revision,fencing_token=claim.fencing_token,
                    outcome=M.TerminalOutcomeReference(outcome_id=metadata['receipt_id'],outcome_revision=1,
                        status=M.TerminalOutcomeStatus.SUCCEEDED,outcome_digest=M.canonical_request_digest(outcome),finalized_at=now))
                result=self._execute(db,'finalize',target=SEND,**identity,phase='commit',origin_proof=origin,provider_profile_digest=provider_profile_digest,catalog_envelope=catalog,role_envelope=self._role_envelope(metadata),
                    observation_id=observed['observation_id'],attempt_revision=metadata['attempt_revision'],
                    action_request_text=canonical_payload(metadata['frozen_request']),outcome_text=canonical_payload(outcome),
                    action_claim=self._action_envelope(command,'finalize'))
            return self._public_observation(result)
        except Exception:raise EffectExecutionUnavailable() from None

    def _recovery_execute(self,db,verb,target,**parameters):
        return db.execute('select authz.nexloop_receipt_reconcile_command(%s,%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,*self._signed(verb,target=target,**parameters))).fetchone()[0]

    def authorize_effect_query(self,*,intent_id,fence,provider_profile_digest):
        try:
            identity=_identity_arguments(intent_id,fence);metadata=self._resolve(identity)
            args=dict(**identity,attempt_revision=metadata['attempt_revision'],provider_profile_digest=_profile(provider_profile_digest))
            text,signature,payload=self._signed('query',target=QUERY,**args)
            with self.pool.connection() as db,db.transaction():
                result=self._recovery_execute(db,'admit_query',QUERY,**identity,
                    provider_profile_digest=provider_profile_digest,query_envelope=dict(text=text,signature=signature,payload=payload))
            return {key:result[key] for key in ('provider_payload_digest','query_id')}
        except Exception:raise EffectExecutionUnavailable() from None

    def record_effect_query_observation(self,*,intent_id,fence,query_id,provider_profile_digest,provider_payload_digest,provider_state,provider_reference):
        try:
            identity=_identity_arguments(intent_id,fence);metadata=self._resolve(identity);_uuid(query_id)
            args=dict(**identity,attempt_revision=metadata['attempt_revision'],provider_profile_digest=_profile(provider_profile_digest),
                provider_payload_digest=provider_payload_digest,provider_state=provider_state,provider_reference=provider_reference)
            text,signature,payload=self._signed('observe',target=QUERY,**args)
            with self.pool.connection() as db,db.transaction():
                observed=self._recovery_execute(db,'observe_query',QUERY,**identity,query_id=query_id,
                    observation_envelope=dict(text=text,signature=signature,payload=payload))
            # Durable GET evidence is committed even when independent recovery authority is absent.
            if provider_state!='fulfilled':return self._public_observation(observed)
            # Preserve normal execution finalization when its original current authority remains valid.
            # Failed normal finalization rolls back its transaction before the independent recovery Action.
            try:
                normal=self._finalize_known_observation(intent_id=intent_id,fence=fence,observed=observed,
                    provider_profile_digest=provider_profile_digest,provider_payload_digest=provider_payload_digest,
                    provider_state=provider_state,provider_reference=provider_reference)
                if normal['business_action_success']:return normal
            except EffectExecutionUnavailable:pass
            try:return self.reconcile_effect_receipt(intent_id=intent_id,fence=fence,query_id=query_id)
            except EffectExecutionUnavailable:return self._public_observation(observed)
        except Exception:raise EffectExecutionUnavailable() from None

    def reconcile_effect_receipt(self,*,intent_id,fence,query_id):
        try:
            target=RECONCILE
            identity=_identity_arguments(intent_id,fence);_uuid(query_id);metadata=self._resolve(identity)
            definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get('nexloop.service.receipt_reconcile',1)
            request=dict(**identity,query_id=query_id)
            with self.pool.connection() as db,db.transaction():
                now=db.execute('select clock_timestamp()').fetchone()[0]
                invocation='receipt-reconcile:'+intent_id
                command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,
                    action_stable_name=definition.stable_name,idempotency_key=invocation),
                    binding=M.ClaimBindingPayload(invocation_id=invocation,action_reference=definition.reference(),
                        request_digest=M.canonical_request_digest(request),capability_binding=capability.binding(),
                        adapter_id='nexloop.receipt.reconcile',target_system='service'),requested_at=now,
                    lease_expires_at=now+timedelta(seconds=25))
                holder={}
                def reserve(actual):
                    outcome={'intent_id':intent_id,'receipt_id':metadata['receipt_id'],'provider_state':'fulfilled',
                        'provider_payload_digest':metadata['provider_payload_digest']}
                    # Actual provider reference comes only from protected owned durable query lineage.
                    # Resolve now includes the persisted reference for recovery outcome hashing.
                    outcome['provider_reference']=metadata['provider_reference']
                    query_proof=json.loads(self._signed('query_authority',target=QUERY)[0])
                    row=self._recovery_execute(db,'recover',target,**identity,query_id=query_id,query_proof=query_proof,
                        original_request_text=canonical_payload(metadata['frozen_request']),recovery_request_text=canonical_payload(request),
                        outcome_text=canonical_payload(outcome),recovery_reserve=self._action_envelope(actual,'reserve'))
                    holder['row']=row
                    return M.ActionClaimResult.model_validate_json(canonical_payload(row['recovery_claim_result']))
                governor=ActionGovernor(claim_port=_AtomicReserve(reserve),approval_port=UnavailableApprovalPort(),authority_verifier=UnavailableApprovalAuthority(),clock=lambda:db.execute('select clock_timestamp()').fetchone()[0])
                evidence=M.PolicyEvidenceSet(tenant_id=self.session.authentication.tenant_id,invocation_id=invocation,
                    action_reference=definition.reference(),required_policy_references=(),evidence=(),evaluated_at=now)
                permit=governor.govern(tenant_id=self.session.authentication.tenant_id,invocation_id=invocation,
                    action_reference=definition.reference(),action_definition=definition,capability_snapshot=capability,
                    request=request,claim_request=command,granted_scopes=self.session.authentication.requested_scopes,
                    policy_evidence=evidence,approval_evidence=None)
                if type(permit) is not M.ActionExecutionPermit:raise ValueError()
            return self._public_observation(holder['row'])
        except Exception:raise EffectExecutionUnavailable() from None

    def read_effect_receipt(self,*,intent_id):
        try:
            _uuid(intent_id)
            # Sole historical READ branch: no owned resolve, effect fence,
            # attempt revision, source lookup, claim or provider operation.
            result=self._call('query',target=QUERY,intent_id=intent_id,terminal_only=True)
            keys={'intent_id','receipt_id','state','provider_state','governed_claim_finalized',
                  'business_action_success','receipt_replay'}
            if (type(result) is not dict or set(result)!=keys or result['intent_id']!=intent_id
                or result['state'] not in {'fulfilled','confirmed'} or result['provider_state']!='fulfilled'
                or result['governed_claim_finalized'] is not True or result['business_action_success'] is not True
                or result['receipt_replay'] is not True):raise ValueError()
            _uuid(result['receipt_id'])
            return {key:result[key] for key in keys}
        except Exception:raise EffectExecutionUnavailable() from None

    @staticmethod
    def _public_observation(result):
        if (type(result) is not dict or result.get('state') not in {'dispatching','unknown','observed_fulfilled','fulfilled'}
            or type(result.get('business_action_success')) is not bool
            or type(result.get('governed_claim_finalized')) is not bool
            or result.get('provider_state') not in {'accepted','fulfilled','not_found'}
            or result['provider_state']=='accepted' and result['state']!='dispatching'
            or result['provider_state']=='fulfilled' and result['state'] not in {'observed_fulfilled','fulfilled'}
            or result['provider_state']=='not_found' and result['state']!='unknown'
            or result['business_action_success'] and (result['state']!='fulfilled' or not result['governed_claim_finalized'])):
            raise ValueError()
        return {key:result[key] for key in ('state','provider_state','business_action_success','governed_claim_finalized')}

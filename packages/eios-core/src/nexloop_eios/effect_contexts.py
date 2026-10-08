"""DRAFT real governed registrar; not imported or executed by head40 CI.

No raw token persists. Backend assembly supplies already-authenticated sessions;
Run authentication MUST occur through Backend.authenticate_run, never lookup of
an arbitrary Run ID. Formal Goal/Step/Control writes use existing object Actions.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import re
import psycopg
from jsonschema import Draft202012Validator
from eios.actions import models as M
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload

CONFIGURE='nexloop.effect.configure_control'
BIND='nexloop.plan.bind_effect_context'
EFFECT='nexloop.service.request'

def registrar_schema(verb):
    fields={'control_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},'control_revision':{'type':'integer','minimum':1,'maximum':9223372036854775807}} if verb=='configure' else {
        'step_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},**{name:{'type':'integer','minimum':1,'maximum':9223372036854775807} for name in
            ('step_revision','goal_revision','consumer_revision','control_revision')}}
    fields={'verb':{'const':verb},**fields}
    return {'type':'object','properties':fields,'required':sorted(fields),'additionalProperties':False}

def effect_plan_schemas():
    """Canonical protected formal types for governed publication, no DB writes.

    Publication still needs its real governance authority; returning models is
    not publication. Consumer remains supplied by the actual Consumer contract.
    """
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    fields={
        'Goal':{'consumer_id':'string','state':'string','valid_until':'string'},
        'PlanStep':{'consumer_id':'string','goal_id':'string','control_id':'string','submitter_principals':'json','action_name':'string','state':'string'},
        'EffectControl':{'consumer_id':'string','owner_principal':'string','executor_principal':'string','budget_units':'integer','allow_effect':'boolean','valid_until':'string'},
    }
    return tuple(ObjectTypeDefinition(type_name=name,version=1,only_edit_via_actions=True,
        properties=tuple(PropertyDefinition(property_name=field,value_type=PropertyValueType(kind)) for field,kind in properties.items()))
        for name,properties in fields.items())


class EffectContextUnavailable(PermissionError):pass
class EffectContextConflict(RuntimeError):
    code='effect_context_conflict'
    http_status=409


class EffectContextRegistrar:
    """Private Backend-owned port: session arguments must be genuine auth output.

    Backend exposes owner.configure_effect_control(...executor_token...) and
    planner.bind_effect_context(...run_id,run_token,executor_token...) by calling
    real authenticate/authenticate_run in memory and passing their sessions here.
    Runtime/Host never receives either credential or this port.
    """
    def __init__(self,pool,session,signer):
        if session.run_context is not None:raise EffectContextUnavailable('effect context unavailable')
        self.pool,self.session,self.signer=pool,session,signer

    def _proof(self,session,target):
        entries=[]
        query=session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(
            PostgresAuthorityProvider(self.pool,session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ValueError()
        return {'tenant_id':session.authentication.tenant_id,'principal_id':session.authentication.subject_principal_id,
            'credential_id':session.authentication.credential_id,'directory_hash':session.directory_hash,'world':session.world,
            'resource_id':target,'action_resource':target,'operation':'execute',
            'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),'_decision_id':decision.decision_id}

    def _sign(self,protocol,claims):
        text=canonical_payload({'protocol':protocol,'key_id':self.signer.key_id,**claims})
        return text,hmac.new(self.signer.material,(protocol+':'+text).encode(),'sha256').hexdigest()

    def _claim(self,db,command,verb):
        target='eios:action:'+command.binding.action_reference.stable_name+':1'
        proof=self._proof(self.session,target);decision_id=proof.pop('_decision_id')
        payload=canonical_payload(command.model_dump(mode='json'))
        text,signature=self._sign('nexloop-action-command-v1',{**proof,'permit_id':decision_id,'verb':verb,
            'parameters_digest':hashlib.sha256(payload.encode()).hexdigest()})
        return db.execute('select authz.nexloop_action_claim_command(%s,%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,text,signature,payload)).fetchone()[0]

    def _execute(self,*,verb,payload,executor_session,run_session=None):
        try:
            action=CONFIGURE if verb=='configure' else BIND
            definition,capability,schemas=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get_with_schemas(action,1)
            g=definition.governance
            if g.approval_mode.value!='none' or g.risk_level.value!='low' or g.policy_refs or definition.preconditions or definition.parameters:raise ValueError()
            needed=set(definition.required_scopes)|set(capability.required_scopes)
            if not needed<=self.session.authentication.requested_scopes:raise ValueError()
            if executor_session.run_context is not None:raise ValueError()
            for session in (executor_session,run_session):
                if session is not None and (session.world!=self.session.world or session.authentication.tenant_id!=self.session.authentication.tenant_id):raise ValueError()
            # Validate current actual published effect contract, not a label.
            PostgresActionDefinitionReader(self.pool,executor_session,self.signer).get(EFFECT,1)
            if run_session is not None:
                if run_session.run_context is None:raise ValueError()
                PostgresActionDefinitionReader(self.pool,run_session,self.signer).get(EFFECT,1)
            payload={'verb':verb,**payload}
            schema=definition.model_dump(mode='json')['input_schema']
            if schema!=registrar_schema(verb):raise ValueError()
            Draft202012Validator(schema).validate(payload)
            text_payload=canonical_payload(payload)
            request_hash=M.canonical_request_digest(payload)
            # Registrar's stable retry key cannot be chosen by caller or include
            # fresh timestamp/tool call. Different principal remains claim conflict.
            claim_id='context:'+request_hash
            now=datetime.now(UTC)
            command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,
                action_stable_name=action,idempotency_key=claim_id),
                binding=M.ClaimBindingPayload(invocation_id=claim_id,action_reference=definition.reference(),request_digest=request_hash,
                    capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),
                requested_at=now,lease_expires_at=now+timedelta(seconds=20))
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                reservation=M.ActionClaimResult.model_validate_json(canonical_payload(self._claim(db,command,'reserve')))
                if reservation.disposition not in (M.ActionClaimDisposition.CLAIMED,M.ActionClaimDisposition.REPLAY):raise ValueError()
                if reservation.terminal_outcome is not None and reservation.terminal_outcome.status is not M.TerminalOutcomeStatus.SUCCEEDED:raise ValueError()
                def register():
                    proof=self._proof(self.session,'eios:action:'+action+':1');proof.pop('_decision_id')
                    executor_proof=self._proof(executor_session,'eios:action:'+EFFECT+':1');executor_proof.pop('_decision_id')
                    claims={**proof,'parameters_digest':hashlib.sha256(text_payload.encode()).hexdigest(),
                        'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json'),
                        'definition_reference':definition.reference().model_dump(mode='json'),'schemas':[schema.model_dump(mode='json') for schema in schemas],
                        'capability_binding':capability.binding().model_dump(mode='json'),'claim_id':claim_id,
                        'executor_digest':executor_session.token_digest,'executor_proof':executor_proof}
                    if run_session is not None:
                        run_proof=self._proof(run_session,'eios:action:'+EFFECT+':1');run_proof.pop('_decision_id')
                        claims.update(run_digest=run_session.token_digest,run_proof=run_proof)
                    text,signature=self._sign('nexloop-effect-context-v1',claims)
                    return db.execute('select authz.nexloop_effect_context_command(%s,%s,%s,%s,%s)',
                        (self.session.token_digest,self.session.world,text,signature,text_payload)).fetchone()[0]
                result=register()
                if reservation.claim is not None:
                    # Use PG time, never a client clock later than final SQL now.
                    finalized_at=db.execute('select clock_timestamp()').fetchone()[0]
                    outcome=M.TerminalOutcomeReference(outcome_id=result['context_ref'] or result['control_id'],outcome_revision=1,
                        status=M.TerminalOutcomeStatus.SUCCEEDED,outcome_digest=M.canonical_request_digest(result),finalized_at=finalized_at)
                    finalize=M.ActionClaimFinalizeCommand(key=command.key,binding=command.binding,
                        expected_claim_revision=reservation.claim.claim_revision,fencing_token=reservation.claim.fencing_token,outcome=outcome)
                    self._claim(db,finalize,'finalize')
                # Final full chain + validity checks after finalize, same txn;
                # no partial context or successful claim survives a failed check.
                if register()!=result:raise ValueError()
            return result
        except Exception as error:
            if isinstance(error,psycopg.Error) and error.diag.message_primary=='effect context conflict':
                raise EffectContextConflict('effect_context_conflict') from None
            raise EffectContextUnavailable('effect context unavailable') from None

    @staticmethod
    def _id(value):
        if type(value) is not str or not re.fullmatch('[a-f0-9]{64}',value):raise EffectContextUnavailable('effect context unavailable')
        return value

    @staticmethod
    def _revision(value):
        if type(value) is not int or value<1 or value>9223372036854775807:raise EffectContextUnavailable('effect context unavailable')
        return value

    def configure_control(self,*,control_id,control_revision,executor_session):
        return self._execute(verb='configure',payload={'control_id':self._id(control_id),'control_revision':self._revision(control_revision)},executor_session=executor_session)

    def bind(self,*,step_id,step_revision,goal_revision,consumer_revision,control_revision,run_session,executor_session):
        return self._execute(verb='bind',payload={'step_id':self._id(step_id),
            **{name:self._revision(value) for name,value in [('step_revision',step_revision),('goal_revision',goal_revision),
                ('consumer_revision',consumer_revision),('control_revision',control_revision)]}},executor_session=executor_session,run_session=run_session)

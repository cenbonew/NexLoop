"""Durable frozen ActionClaimPort; no ontology or external-effect write grant."""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import psycopg
from eios.actions import models as M,ports as P
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload

class ActionAuthorizationDenied(PermissionError):
    """Live EIOS authorization did not allow the Action command."""


_ERRORS={
 'action_claim_binding_conflict':P.ActionClaimBindingConflictError,
 'action_claim_revision_conflict':P.ActionClaimRevisionConflictError,
 'action_claim_stale_fence':P.ActionClaimStaleFenceError,
 'action_claim_not_found':P.ActionClaimNotFoundError,
 'action_claim_outcome_conflict':P.ActionClaimOutcomeConflictError,
 'action_claim_transition_invalid':P.ActionClaimTransitionError,
 'action_claim_lease_expired':P.ActionClaimTransitionError,
 'action_claim_time_invalid':P.ActionClaimContractError,
}

class PostgresActionClaimPort:
    def __init__(self,pool,session,signer):
        self.pool,self.session,self.signer=pool,session,signer
        with pool.connection() as c:verify_application_role(c)

    def _execute(self,command,model,verb,result_model):
        try:
            if type(command) is not model:raise ValueError()
            # Revalidate nested frozen contracts; never trust model_construct/copy.
            command=model.model_validate_json(command.model_dump_json())
        except Exception:
            raise P.ActionClaimContractError('action_claim_command_invalid','invalid Action claim command') from None
        if command.key.tenant_id!=self.session.authentication.tenant_id:
            raise P.ActionClaimContractError('action_claim_command_invalid','Action tenant differs from authenticated tenant')
        ref=command.binding.action_reference
        resource=resource_id(ResourceType.ACTION,ref.stable_name,ref.version)
        entries=[]
        query=self.session.query(resource_id=resource,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        context=F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query)
        decision=AuthorizationDecisionService().decide_resolved(context)
        if not decision.allowed or not decision.authoritative or decision.obligations:
            raise ActionAuthorizationDenied('Action authorization denied')
        payload=canonical_payload(command.model_dump(mode='json'))
        claims={'protocol':'nexloop-action-command-v1','key_id':self.signer.key_id,'permit_id':decision.decision_id,
          'tenant_id':command.key.tenant_id,'principal_id':self.session.authentication.subject_principal_id,
          'credential_id':self.session.authentication.credential_id,'world':self.session.world,
          'directory_hash':self.session.directory_hash,'resource_id':resource,'action_resource':resource,'operation':'execute',
          'verb':verb,'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'parameters_digest':hashlib.sha256(payload.encode()).hexdigest(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-action-command-v1:'+text).encode(),'sha256').hexdigest()
        try:
            with self.pool.connection() as c,c.transaction():
                verify_application_role(c)
                row=c.execute('select authz.nexloop_action_claim_command(%s,%s,%s,%s,%s)',
                   (self.session.token_digest,self.session.world,text,signature,payload)).fetchone()[0]
        except psycopg.errors.RaiseException as error:
            code=error.diag.message_primary
            cls=_ERRORS.get(code,P.ActionClaimContractError)
            raise cls(code if code in _ERRORS else 'action_claim_command_invalid','Action claim operation rejected') from None
        try:
            return result_model.model_validate_json(canonical_payload(row))
        except Exception:
            raise P.ActionClaimContractError('action_claim_result_invalid','Action claim result violated the frozen contract') from None

    def reserve(self,command):
        return self._execute(command,M.ActionClaimRequest,'reserve',M.ActionClaimResult)

    def mark_retryable(self,command):
        return self._execute(command,M.ActionClaimRetryableCommand,'retryable',M.ActionClaim)

    def finalize(self,command):
        return self._execute(command,M.ActionClaimFinalizeCommand,'finalize',M.ActionClaim)

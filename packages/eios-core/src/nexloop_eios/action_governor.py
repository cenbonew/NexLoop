"""Explicit PostgreSQL Governor composition; unsupported approvals fail closed."""
from eios.actions.governance import ActionGovernor
from eios.approvals.ports import ApprovalHumanAuthorityUnavailable
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_action_claims import PostgresActionClaimPort


class UnavailableApprovalAuthority:
    def assert_fresh(self,record):
        raise ApprovalHumanAuthorityUnavailable('authoritative approval storage is not assembled')


class UnavailableApprovalPort:
    def create(self,command):raise ApprovalHumanAuthorityUnavailable('approvals unavailable')
    def decide(self,command):raise ApprovalHumanAuthorityUnavailable('approvals unavailable')
    def get(self,*,tenant_id,approval_id):raise ApprovalHumanAuthorityUnavailable('approvals unavailable')
    def list(self,*,tenant_id):raise ApprovalHumanAuthorityUnavailable('approvals unavailable')


def create_action_governor(pool,session,signer,*,object_reader=None):
    """Trusted backend only: definitions/policies must come from published stores.

    This composition supports no-approval Actions. It never invents approval or
    uses the upstream default Memory authority verifier. No business write API
    is exposed by constructing it.
    """
    def database_clock():
        with pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select clock_timestamp()').fetchone()[0]
    return ActionGovernor(claim_port=PostgresActionClaimPort(pool,session,signer),
        approval_port=UnavailableApprovalPort(),authority_verifier=UnavailableApprovalAuthority(),
        clock=database_clock,object_reader=object_reader)


def govern_published_action(pool,session,signer,*,claim_request,request,object_reader=None):
    """Resolve published contracts and server scopes; no caller risk/definition override.

    Action-specific policy evidence storage is not assembled. Nonempty policy
    references therefore fail closed; the separate live EIOS authorization
    policy intersection is still mandatory for every supported reservation.
    """
    from eios.actions.models import PolicyEvidenceSet
    from nexloop_eios.action_definitions import PostgresActionDefinitionReader
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    reference=claim_request.binding.action_reference
    definition,capability=PostgresActionDefinitionReader(pool,session,signer).get(reference.stable_name,reference.version)
    if definition.governance.policy_refs:
        raise ActionAuthorizationDenied('authoritative Action policy evidence unavailable')
    with pool.connection() as c,c.transaction():
        verify_application_role(c)
        now=c.execute('select clock_timestamp()').fetchone()[0]
    evidence=PolicyEvidenceSet(tenant_id=session.authentication.tenant_id,
        invocation_id=claim_request.binding.invocation_id,action_reference=definition.reference(),
        required_policy_references=(),evidence=(),evaluated_at=now)
    governor=create_action_governor(pool,session,signer,object_reader=object_reader)
    return governor.govern(tenant_id=session.authentication.tenant_id,
        invocation_id=claim_request.binding.invocation_id,action_reference=definition.reference(),
        action_definition=definition,capability_snapshot=capability,request=request,
        claim_request=claim_request,granted_scopes=session.authentication.requested_scopes,
        policy_evidence=evidence,approval_evidence=None)

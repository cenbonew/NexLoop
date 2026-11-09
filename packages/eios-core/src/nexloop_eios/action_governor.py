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


# Shadow validations that could not run (L6 G1 on 0431): dispatch went on and nothing was counted; this process-local counter and
# the warning keep the failure visible.
SHADOW_FAILURES={'count':0}


def shadow_validate(pool,session,definition,request):
    """D23 option B (migration action_input_shadow): when trusted configuration set a candidate input schema for this exact Action
    version, validate the request with it as dispatch would (same validator, format checker on), count the call and whether the
    candidate would have refused it - never enforce it. Only the Action and two counts are recorded: no request, no principal. A
    shadow that cannot run never affects dispatch and counts nothing; it logs the failure's class only, counts it in SHADOW_FAILURES and records it in PostgreSQL
    (authz.nexloop_action_input_shadow_failure: world, day, error class)."""
    import logging
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource
    resource=f'eios:action:{definition.stable_name}:{definition.version}'
    def no_external_schema(uri):
        raise NoSuchResource(ref=uri)
    try:
        with pool.connection() as c,c.transaction():
            verify_application_role(c)
            candidate=c.execute('select authz.nexloop_action_input_candidate(%s,%s,%s)',(session.token_digest,session.world,resource)).fetchone()[0]
            if candidate is None:return None
            try:
                Draft202012Validator.check_schema(candidate['input_schema'])
                rejected=not Draft202012Validator(candidate['input_schema'],registry=Registry(retrieve=no_external_schema),
                    format_checker=Draft202012Validator.FORMAT_CHECKER).is_valid(request)
            except Exception:
                rejected=True  # an unusable candidate refuses everything: it must never pass the shadow gate
            c.execute('select authz.nexloop_action_input_shadow_record(%s,%s,%s,%s,%s)',
                (session.token_digest,session.world,resource,candidate['candidate_digest'],rejected))
            return rejected
    except Exception as error:
        SHADOW_FAILURES['count']+=1
        logging.getLogger(__name__).warning('action input shadow unavailable %s',{'action':resource,'error':type(error).__name__})
        try:  # L6 J1: counted in PostgreSQL too (world, day, error class), read by the metrics snapshot and alert rule v36
            with pool.connection() as c,c.transaction():
                verify_application_role(c)
                c.execute('select authz.nexloop_action_input_shadow_failure(%s,%s)',(session.world,type(error).__name__))
        except Exception as again:
            logging.getLogger(__name__).warning('action input shadow failure not recorded %s',{'action':resource,'error':type(again).__name__})
        return None


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
    # Upstream's parameter gate does not recursively validate input_schema.
    # Validate the actual published JSON contract before reserving an intent.
    # No network/file schema resolution, and no rejected payload in errors.
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource
    def no_external_schema(uri):
        raise NoSuchResource(ref=uri)
    try:
        schema=definition.model_dump(mode='json')['input_schema']
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema,registry=Registry(retrieve=no_external_schema),
            format_checker=Draft202012Validator.FORMAT_CHECKER).validate(request)
    except Exception:
        raise ActionAuthorizationDenied('published Action input rejected') from None
    shadow_validate(pool,session,definition,request)
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

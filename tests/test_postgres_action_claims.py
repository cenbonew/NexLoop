from datetime import UTC,datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
import secrets
import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from eios.actions import models as M,ports as P
from eios.ontology.definitions import DefinitionReference,DefinitionType,CapabilityBinding
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios.postgres_action_claims import PostgresActionClaimPort
from authority_fixture import seed_authority,replace_fact

@pytest.fixture
def action_port(admin,pg):
    bootstrap(admin)
    token,_=seed_authority(admin,'synthetic-a','eios:action:Consumer.create:1',operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    signer=AuthoritySigner('synthetic-action',secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(signer.key_id,signer.material))
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        session=authenticate_service(pool,token,world='real')
        yield PostgresActionClaimPort(pool,session,signer)


def request(*,intent='synthetic-intent-0001',seconds=20):
    now=datetime.now(UTC)
    ref=DefinitionReference(tenant_id='synthetic-a',definition_type=DefinitionType.ACTION,stable_name='Consumer.create',version=1,contract_digest='a'*64)
    binding=M.ClaimBindingPayload(invocation_id='synthetic-invocation',action_reference=ref,request_digest='b'*64,
      capability_binding=CapabilityBinding(capability_name='consumer.create',capability_version='1.0.0',schema_hash='c'*64),adapter_id='nexloop.core',target_system='postgres')
    return M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id='synthetic-a',action_stable_name='Consumer.create',idempotency_key=intent),binding=binding,requested_at=now,lease_expires_at=now+timedelta(seconds=seconds))


def finish(claim):
    return M.ActionClaimFinalizeCommand(key=claim.key,binding=claim.binding,expected_claim_revision=claim.claim_revision,fencing_token=claim.fencing_token,
     outcome=M.TerminalOutcomeReference(outcome_id='synthetic-outcome',outcome_revision=1,status=M.TerminalOutcomeStatus.SUCCEEDED,outcome_digest='d'*64,finalized_at=datetime.now(UTC)))


def test_atomic_claim_concurrency_and_terminal_replay(action_port):
    port=action_port;command=request()
    with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(lambda _:port.reserve(command),range(4)))
    owners=[r.claim for r in results if r.disposition is M.ActionClaimDisposition.CLAIMED]
    assert len(owners)==1
    assert sum(r.disposition is M.ActionClaimDisposition.IN_PROGRESS for r in results)==3
    final=finish(owners[0]);terminal=port.finalize(final)
    assert terminal.state is M.ActionClaimState.TERMINAL
    assert port.finalize(final)==terminal
    replay=port.reserve(command)
    assert replay.disposition is M.ActionClaimDisposition.REPLAY
    assert replay.terminal_outcome==terminal.terminal_outcome
    with port.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from runtime.nexloop_action_claims')


def test_payload_conflict_does_not_expose_claim(action_port):
    command=request();action_port.reserve(command)
    changed=command.model_copy(update={'binding':command.binding.model_copy(update={'request_digest':'e'*64})})
    result=action_port.reserve(changed)
    assert result.disposition is M.ActionClaimDisposition.CONFLICT
    assert result.claim is None


def test_lease_takeover_fences_old_owner_and_reopen(action_port):
    old=action_port.reserve(request(seconds=1)).claim
    with action_port.pool.connection() as c:c.execute('select pg_sleep(1.1)')
    reopened=PostgresActionClaimPort(action_port.pool,action_port.session,action_port.signer)
    new=reopened.reserve(request()).claim
    assert new.claim_revision==old.claim_revision+1 and new.fencing_token!=old.fencing_token
    with pytest.raises(P.ActionClaimRevisionConflictError):reopened.finalize(finish(old))
    assert reopened.finalize(finish(new)).state is M.ActionClaimState.TERMINAL


def test_retryable_preserves_intent_and_advances_fence(action_port):
    old=action_port.reserve(request()).claim
    retry=M.ActionClaimRetryableCommand(key=old.key,binding=old.binding,expected_claim_revision=old.claim_revision,fencing_token=old.fencing_token,marked_at=datetime.now(UTC))
    assert action_port.mark_retryable(retry).state is M.ActionClaimState.RETRYABLE
    new=action_port.reserve(request()).claim
    assert new.claim_revision==old.claim_revision+1
    with pytest.raises(P.ActionClaimRevisionConflictError):action_port.finalize(finish(old))


def test_revoke_after_claim_denies_finalize(action_port,admin):
    claim=action_port.reserve(request()).claim
    replace_fact(admin,'synthetic-a','grants',['synthetic-a-principal','eios:action:Consumer.create:1'],F.GrantFacts,grants=[])
    with pytest.raises(AuthorizationUnavailable):action_port.finalize(finish(claim))


def test_wrong_tenant_rejected_before_any_sql_mutation(action_port):
    command=request();wrong=command.model_copy(update={'key':command.key.model_copy(update={'tenant_id':'synthetic-b'}),'binding':command.binding.model_copy(update={'action_reference':command.binding.action_reference.model_copy(update={'tenant_id':'synthetic-b'})})})
    with pytest.raises(P.ActionClaimContractError):action_port.reserve(wrong)


def test_revoke_between_real_decision_and_sql_claim_recheck(action_port,admin,monkeypatch):
    from contextlib import contextmanager
    original=action_port.pool.connection;count=0
    @contextmanager
    def race_connection(*args,**kwargs):
        nonlocal count
        count+=1
        if count==2:
            replace_fact(admin,'synthetic-a','grants',['synthetic-a-principal','eios:action:Consumer.create:1'],F.GrantFacts,grants=[])
        with original(*args,**kwargs) as c:yield c
    monkeypatch.setattr(action_port.pool,'connection',race_connection)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):action_port.reserve(request())
    assert count==2
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==0


def test_application_sql_role_cannot_forge_signer(action_port):
    forged=PostgresActionClaimPort(action_port.pool,action_port.session,AuthoritySigner(action_port.signer.key_id,secrets.token_bytes(32)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):forged.reserve(request())


def test_stale_fence_and_changed_terminal_outcome_rejected(action_port):
    claim=action_port.reserve(request()).claim;command=finish(claim)
    with pytest.raises(P.ActionClaimStaleFenceError):action_port.finalize(command.model_copy(update={'fencing_token':'synthetic-other-fence'}))
    action_port.finalize(command)
    changed=command.model_copy(update={'outcome':command.outcome.model_copy(update={'outcome_digest':'e'*64})})
    with pytest.raises(P.ActionClaimOutcomeConflictError):action_port.finalize(changed)


def test_malformed_result_parser_cannot_disclose_native_details(action_port,monkeypatch):
    sentinel=secrets.token_urlsafe(48)
    def broken_parser(*args,**kwargs):raise ValueError(sentinel)
    monkeypatch.setattr(M.ActionClaimResult,'model_validate_json',broken_parser)
    with pytest.raises(P.ActionClaimContractError) as result:action_port.reserve(request())
    assert result.value.code=='action_claim_result_invalid'
    assert sentinel not in str(result.value) and result.value.__suppress_context__


def governance_inputs(*,approval_required=False):
    from eios.ontology.definitions import ActionDefinition,DefinitionStatus,ActionGovernanceContract,ActionChangeScope,ActionRiskLevel,ActionApprovalMode,ActionIdempotencyPolicy,OntologySchemaReference,OntologySchemaType
    from eios.ontology.version_resolution import CapabilityContractSnapshot,CapabilityContractKind
    command=request();now=datetime.now(UTC);payload={'request_id':command.key.idempotency_key}
    object_type=OntologySchemaReference(tenant_id='synthetic-a',schema_type=OntologySchemaType.OBJECT_TYPE,stable_name='Consumer',version=1,schema_digest='a'*64)
    definition=ActionDefinition(tenant_id='synthetic-a',stable_name='Consumer.create',version=1,status=DefinitionStatus.PUBLISHED,
      created_by='synthetic-owner',created_at=now-timedelta(days=1),required_scopes=('action.execute',),
      capability_binding=command.binding.capability_binding,object_types=(object_type,),
      governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=(object_type,),target_systems=('postgres',)),risk_level=ActionRiskLevel.LOW,
       approval_mode=ActionApprovalMode.REQUIRED if approval_required else ActionApprovalMode.NONE,
       policy_refs=(),idempotency=ActionIdempotencyPolicy(key_fields=('request_id',))),receipt_schema={'type':'object'})
    ref=definition.reference()
    binding=command.binding.model_copy(update={'action_reference':ref,'request_digest':M.canonical_request_digest(payload)})
    command=command.model_copy(update={'binding':binding})
    snapshot=CapabilityContractSnapshot(**command.binding.capability_binding.model_dump(),kind=CapabilityContractKind.ATOMIC,has_side_effects=True,idempotent=True,required_scopes=('action.execute',))
    evidence=M.PolicyEvidenceSet(tenant_id='synthetic-a',invocation_id=binding.invocation_id,action_reference=ref,required_policy_references=(),evidence=(),evaluated_at=now)
    return dict(tenant_id='synthetic-a',invocation_id=binding.invocation_id,action_reference=ref,action_definition=definition,
      capability_snapshot=snapshot,request=payload,claim_request=command,granted_scopes=frozenset({'action.execute'}),policy_evidence=evidence,approval_evidence=None)


def test_actual_governor_composes_with_pg_claim_and_database_clock(action_port):
    from nexloop_eios.action_governor import create_action_governor
    governor=create_action_governor(action_port.pool,action_port.session,action_port.signer)
    permit=governor.govern(**governance_inputs())
    assert type(permit) is M.ActionExecutionPermit
    assert permit.claim.claim_revision==1 and permit.claim.state is M.ActionClaimState.ACTIVE


def test_required_approval_fails_before_pg_reservation(action_port,admin):
    from nexloop_eios.action_governor import create_action_governor
    from eios.actions.governance import ActionGovernanceError
    governor=create_action_governor(action_port.pool,action_port.session,action_port.signer)
    with pytest.raises(ActionGovernanceError):governor.govern(**governance_inputs(approval_required=True))
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==0

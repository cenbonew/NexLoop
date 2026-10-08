from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import math
from threading import Barrier

from pydantic import ValidationError
import pytest

from eios.actions.models import (
    ActionClaim,
    ActionClaimDisposition,
    ActionClaimFinalizeCommand,
    ActionClaimRequest,
    ActionClaimRetryableCommand,
    ActionClaimState,
    ActionExecutionPermit,
    ActionReservationKey,
    ApprovalEvidence,
    ClaimBindingPayload,
    PolicyDecisionEvidence,
    PolicyEvidenceBinding,
    PolicyEvidenceSet,
    TerminalOutcomeReference,
    TerminalOutcomeStatus,
    canonical_request_digest,
)
from eios.actions.in_memory import InMemoryActionClaimStore
from eios.actions.ports import (
    ActionClaimBindingConflictError,
    ActionClaimContractError,
    ActionClaimNotFoundError,
    ActionClaimOutcomeConflictError,
    ActionClaimPort,
    ActionClaimRevisionConflictError,
    ActionClaimStaleFenceError,
    ActionClaimTransitionError,
)
from eios.ontology.definitions import (
    CapabilityBinding,
    DefinitionReference,
    DefinitionType,
)


_NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
_ACTION_DIGEST = "a" * 64
_REQUEST_DIGEST = "b" * 64


class MutableClock:
    def __init__(self, current: datetime = _NOW) -> None:
        self.current = current
        self.calls = 0
        self.failure: Exception | None = None

    def __call__(self) -> datetime:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.current


def claim_store(clock: MutableClock | None = None) -> InMemoryActionClaimStore:
    return InMemoryActionClaimStore(clock=clock or MutableClock())


def action_reference(
    *,
    tenant_id: str = "tenant-a",
    stable_name: str = "customer.update_crm",
    version: int = 3,
    digest: str = _ACTION_DIGEST,
    definition_type: DefinitionType = DefinitionType.ACTION,
) -> DefinitionReference:
    return DefinitionReference(
        tenant_id=tenant_id,
        definition_type=definition_type,
        stable_name=stable_name,
        version=version,
        contract_digest=digest,
    )


def capability_binding() -> CapabilityBinding:
    return CapabilityBinding(
        capability_name="enterprise.crm.customer.update",
        capability_version="2.0.0",
        schema_hash="c" * 64,
    )


def reservation_key(
    *,
    tenant_id: str = "tenant-a",
    action_stable_name: str = "customer.update_crm",
    idempotency_key: str = "request-42",
) -> ActionReservationKey:
    return ActionReservationKey(
        tenant_id=tenant_id,
        action_stable_name=action_stable_name,
        idempotency_key=idempotency_key,
    )


def claim_binding(
    *,
    invocation_id: str = "invocation-42",
    action: DefinitionReference | None = None,
    request_digest: str = _REQUEST_DIGEST,
    capability: CapabilityBinding | None = None,
    adapter_id: str = "adapter.crm.primary",
    target_system: str = "crm",
) -> ClaimBindingPayload:
    return ClaimBindingPayload(
        invocation_id=invocation_id,
        action_reference=action or action_reference(),
        request_digest=request_digest,
        capability_binding=capability or capability_binding(),
        adapter_id=adapter_id,
        target_system=target_system,
    )


def policy_evidence(
    policy_reference: str = "policy.customer.crm_write",
    *,
    tenant_id: str = "tenant-a",
    invocation_id: str = "invocation-42",
    action: DefinitionReference | None = None,
    evidence_id: str = "evidence-42",
    decision_id: str = "decision-42",
    decision_revision: int = 7,
    allowed: bool = True,
    issued_at: datetime = _NOW - timedelta(minutes=1),
    expires_at: datetime = _NOW + timedelta(minutes=5),
) -> PolicyDecisionEvidence:
    return PolicyDecisionEvidence(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        action_reference=action or action_reference(tenant_id=tenant_id),
        policy_reference=policy_reference,
        evidence_id=evidence_id,
        decision_id=decision_id,
        decision_revision=decision_revision,
        allowed=allowed,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def approval_evidence(
    *,
    tenant_id: str = "tenant-a",
    invocation_id: str = "invocation-42",
    action: DefinitionReference | None = None,
    approval_id: str = "approval-42",
    approved_revision: int = 2,
    decision_id: str = "approval-decision-42",
    outcome: str = "approved",
    decided_at: datetime = _NOW - timedelta(minutes=3),
    issued_at: datetime = _NOW - timedelta(minutes=2),
    expires_at: datetime = _NOW + timedelta(minutes=5),
) -> ApprovalEvidence:
    return ApprovalEvidence(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        action_reference=action or action_reference(tenant_id=tenant_id),
        approval_id=approval_id,
        approved_revision=approved_revision,
        decision_id=decision_id,
        approval_record_digest="d" * 64,
        outcome=outcome,
        decided_at=decided_at,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def active_claim() -> ActionClaim:
    return ActionClaim(
        key=reservation_key(),
        binding=claim_binding(),
        state=ActionClaimState.ACTIVE,
        claim_revision=1,
        fencing_token="fence-1",
        lease_expires_at=_NOW + timedelta(minutes=4),
    )


def claim_request(
    *,
    key: ActionReservationKey | None = None,
    binding: ClaimBindingPayload | None = None,
    requested_at: datetime = _NOW,
    lease_expires_at: datetime = _NOW + timedelta(minutes=4),
) -> ActionClaimRequest:
    return ActionClaimRequest(
        key=key or reservation_key(),
        binding=binding or claim_binding(),
        requested_at=requested_at,
        lease_expires_at=lease_expires_at,
    )


def claim_ledger_snapshot(
    store: InMemoryActionClaimStore,
) -> tuple[
    dict[tuple[str, str, str], ActionClaim],
    frozenset[str],
    tuple[int, ...],
]:
    internal = vars(store)
    records = dict(internal["_records"])
    fences = frozenset(internal["_issued_fences"])
    revisions = tuple(sorted(record.claim_revision for record in records.values()))
    return records, fences, revisions


def claim_store_with_existing(
    existing_state: str,
) -> tuple[
    InMemoryActionClaimStore,
    MutableClock,
    ActionClaimRequest,
    ActionClaim,
]:
    clock = MutableClock()
    store = claim_store(clock)
    command = claim_request(lease_expires_at=_NOW + timedelta(seconds=10))
    claim = store.reserve(command).claim
    assert claim is not None

    if existing_state == "retryable":
        claim = store.mark_retryable(retryable_command(claim))
    elif existing_state == "terminal":
        claim = store.finalize(finalize_command(claim))
    elif existing_state == "expired":
        clock.current = claim.lease_expires_at
    else:
        assert existing_state == "active"
    return store, clock, command, claim


def invalid_claim_time(
    clock: MutableClock,
    timestamp_kind: str,
) -> tuple[datetime, datetime]:
    if timestamp_kind == "expired":
        return (
            clock.current - timedelta(minutes=2),
            clock.current - timedelta(minutes=1),
        )
    assert timestamp_kind == "future"
    return (
        clock.current + timedelta(minutes=1),
        clock.current + timedelta(minutes=2),
    )


def terminal_outcome(
    *,
    outcome_id: str = "outcome-42",
    outcome_revision: int = 4,
    status: TerminalOutcomeStatus = TerminalOutcomeStatus.SUCCEEDED,
    outcome_digest: str = "d" * 64,
    finalized_at: datetime = _NOW,
) -> TerminalOutcomeReference:
    return TerminalOutcomeReference(
        outcome_id=outcome_id,
        outcome_revision=outcome_revision,
        status=status,
        outcome_digest=outcome_digest,
        finalized_at=finalized_at,
    )


def retryable_command(claim: ActionClaim) -> ActionClaimRetryableCommand:
    return ActionClaimRetryableCommand(
        key=claim.key,
        binding=claim.binding,
        expected_claim_revision=claim.claim_revision,
        fencing_token=claim.fencing_token,
        marked_at=_NOW,
    )


def finalize_command(
    claim: ActionClaim,
    *,
    outcome: TerminalOutcomeReference | None = None,
) -> ActionClaimFinalizeCommand:
    return ActionClaimFinalizeCommand(
        key=claim.key,
        binding=claim.binding,
        expected_claim_revision=claim.claim_revision,
        fencing_token=claim.fencing_token,
        outcome=outcome or terminal_outcome(),
    )


def test_canonical_request_digest_is_stable_type_sensitive_and_fail_closed():
    first = canonical_request_digest(
        {"customer_id": "customer-7", "changes": {"active": True, "score": 1}}
    )
    reordered = canonical_request_digest(
        {"changes": {"score": 1, "active": True}, "customer_id": "customer-7"}
    )
    assert first == reordered
    assert first != canonical_request_digest(
        {"customer_id": "customer-7", "changes": {"active": 1, "score": 1}}
    )
    assert first != canonical_request_digest(
        {
            "customer_id": "customer-7",
            "changes": {"active": True, "score": 1.0},
        }
    )

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    for invalid in (
        {"value": math.nan},
        {"value": math.inf},
        {1: "non-string-key"},
        {"value": b"not-json"},
        cyclic,
    ):
        with pytest.raises(ValueError, match="canonical request"):
            canonical_request_digest(invalid)


def test_policy_evidence_is_immutable_and_binds_the_complete_action_identity():
    evidence = policy_evidence()
    assert evidence.action_reference.definition_type is DefinitionType.ACTION
    assert evidence.allowed is True
    assert evidence.issued_at.tzinfo is UTC
    with pytest.raises(ValidationError, match="frozen"):
        evidence.allowed = False

    with pytest.raises(ValidationError, match="Action"):
        policy_evidence(
            action=action_reference(definition_type=DefinitionType.FUNCTION)
        )
    with pytest.raises(ValidationError, match="tenant"):
        policy_evidence(action=action_reference(tenant_id="tenant-b"))
    with pytest.raises(ValidationError, match="expires_at"):
        policy_evidence(expires_at=_NOW - timedelta(minutes=1))
    with pytest.raises(ValidationError, match="decision_revision"):
        policy_evidence(decision_revision=0)
    with pytest.raises(ValidationError, match="allowed"):
        policy_evidence(allowed=1)
    with pytest.raises(ValidationError, match="independent"):
        policy_evidence(evidence_id="shared-id", decision_id="shared-id")


def test_policy_evidence_set_requires_exact_unique_current_allowing_evidence():
    second = policy_evidence(
        "policy.tenant.region",
        evidence_id="evidence-43",
        decision_id="decision-43",
        decision_revision=2,
    )
    evidence_set = PolicyEvidenceSet(
        tenant_id="tenant-a",
        invocation_id="invocation-42",
        action_reference=action_reference(),
        required_policy_references=(
            "policy.tenant.region",
            "policy.customer.crm_write",
        ),
        evidence=(second, policy_evidence()),
        evaluated_at=_NOW,
    )
    assert evidence_set.required_policy_references == (
        "policy.customer.crm_write",
        "policy.tenant.region",
    )
    assert evidence_set.bindings() == (
        PolicyEvidenceBinding(
            policy_reference="policy.customer.crm_write",
            evidence_id="evidence-42",
            decision_id="decision-42",
            decision_revision=7,
        ),
        PolicyEvidenceBinding(
            policy_reference="policy.tenant.region",
            evidence_id="evidence-43",
            decision_id="decision-43",
            decision_revision=2,
        ),
    )

    invalid_sets = (
        {"evidence": ()},
        {
            "evidence": (
                policy_evidence(),
                policy_evidence(
                    "policy.unexpected",
                    evidence_id="evidence-43",
                    decision_id="decision-43",
                ),
            )
        },
        {
            "evidence": (
                policy_evidence(),
                policy_evidence(
                    "policy.tenant.region",
                    evidence_id="decision-42",
                    decision_id="decision-43",
                ),
            )
        },
        {"evidence": (policy_evidence(allowed=False), second)},
        {
            "evidence": (
                policy_evidence(expires_at=_NOW),
                second,
            )
        },
        {
            "evidence": (
                policy_evidence(invocation_id="invocation-other"),
                second,
            )
        },
        {
            "evidence": (
                policy_evidence(),
                policy_evidence(
                    "policy.tenant.region",
                    evidence_id="evidence-42",
                    decision_id="decision-43",
                ),
            )
        },
        {
            "evidence": (
                policy_evidence(),
                policy_evidence(
                    "policy.tenant.region",
                    evidence_id="evidence-43",
                    decision_id="decision-42",
                ),
            )
        },
    )
    base = {
        "tenant_id": "tenant-a",
        "invocation_id": "invocation-42",
        "action_reference": action_reference(),
        "required_policy_references": (
            "policy.customer.crm_write",
            "policy.tenant.region",
        ),
        "evidence": (policy_evidence(), second),
        "evaluated_at": _NOW,
    }
    for changes in invalid_sets:
        with pytest.raises(ValidationError):
            PolicyEvidenceSet(**(base | changes))


def test_approval_evidence_requires_an_approved_current_exact_action_decision():
    evidence = approval_evidence()
    assert evidence.outcome == "approved"
    assert evidence.binding(evaluated_at=_NOW).approval_id == "approval-42"
    assert evidence.binding(evaluated_at=_NOW).approved_revision == 2

    for changes, message in (
        ({"outcome": "rejected"}, "approved"),
        ({"action": action_reference(tenant_id="tenant-b")}, "tenant"),
        (
            {"action": action_reference(definition_type=DefinitionType.FUNCTION)},
            "Action",
        ),
        ({"approved_revision": 0}, "approved_revision"),
        ({"issued_at": _NOW - timedelta(minutes=4)}, "issued_at"),
        ({"expires_at": _NOW - timedelta(minutes=2)}, "expires_at"),
    ):
        with pytest.raises(ValidationError, match=message):
            approval_evidence(**changes)

    expired = approval_evidence(
        decided_at=_NOW - timedelta(minutes=20),
        issued_at=_NOW - timedelta(minutes=10),
        expires_at=_NOW - timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="expired"):
        expired.binding(evaluated_at=_NOW)
    future = approval_evidence(
        decided_at=_NOW + timedelta(minutes=1),
        issued_at=_NOW + timedelta(minutes=2),
        expires_at=_NOW + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="not yet valid"):
        future.binding(evaluated_at=_NOW)


def test_reservation_key_is_stable_and_binding_contains_every_execution_coordinate():
    key = reservation_key()
    binding = claim_binding()
    assert tuple(type(key).model_fields) == (
        "tenant_id",
        "action_stable_name",
        "idempotency_key",
    )
    assert binding.action_reference.version == 3
    assert binding.action_reference.contract_digest == _ACTION_DIGEST
    assert binding.capability_binding == capability_binding()
    assert binding.adapter_id == "adapter.crm.primary"
    assert binding.target_system == "crm"

    with pytest.raises(ValidationError, match="Extra inputs"):
        ActionReservationKey(
            tenant_id="tenant-a",
            action_stable_name="customer.update_crm",
            idempotency_key="request-42",
            invocation_id="must-not-enter-the-stable-key",
        )
    with pytest.raises(ValidationError, match="request_digest"):
        claim_binding(request_digest="not-a-canonical-sha256")
    with pytest.raises(ValidationError, match="Action"):
        claim_binding(action=action_reference(definition_type=DefinitionType.FUNCTION))
    with pytest.raises(ValidationError, match="frozen"):
        binding.adapter_id = "adapter.crm.other"


def test_execution_permit_freezes_claim_and_exact_evidence_bindings():
    approval = approval_evidence()
    with pytest.raises(ValidationError, match="independent"):
        PolicyEvidenceBinding(
            policy_reference="policy.invalid",
            evidence_id="shared-id",
            decision_id="shared-id",
            decision_revision=1,
        )
    permit = ActionExecutionPermit(
        claim=active_claim(),
        policy_bindings=(
            PolicyEvidenceBinding(
                policy_reference="policy.customer.crm_write",
                evidence_id="evidence-42",
                decision_id="decision-42",
                decision_revision=7,
            ),
        ),
        approval_binding=approval.binding(evaluated_at=_NOW),
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=3),
    )
    assert permit.claim.key == reservation_key()
    assert permit.claim.binding == claim_binding()
    assert permit.claim.claim_revision == 1
    assert permit.claim.fencing_token == "fence-1"
    assert permit.policy_bindings[0].decision_revision == 7
    assert permit.approval_binding == approval.binding(evaluated_at=_NOW)
    with pytest.raises(ValidationError, match="frozen"):
        permit.claim.binding.request_digest = "d" * 64

    with pytest.raises(ValidationError, match="active"):
        ActionExecutionPermit(
            claim=active_claim().model_copy(
                update={"state": ActionClaimState.RETRYABLE}
            ),
            policy_bindings=permit.policy_bindings,
            approval_binding=permit.approval_binding,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=3),
        )
    with pytest.raises(ValidationError, match="lease"):
        permit.model_copy(update={"expires_at": _NOW + timedelta(minutes=5)})
    with pytest.raises(ValidationError, match="duplicate"):
        permit.model_copy(update={"policy_bindings": permit.policy_bindings * 2})
    with pytest.raises(ValidationError, match="independent"):
        permit.model_copy(
            update={
                "policy_bindings": (
                    permit.policy_bindings[0],
                    PolicyEvidenceBinding(
                        policy_reference="policy.tenant.region",
                        evidence_id="decision-42",
                        decision_id="decision-43",
                        decision_revision=2,
                    ),
                )
            }
        )


def test_action_contracts_round_trip_through_strict_json_validation():
    values = (
        policy_evidence(),
        approval_evidence(),
        reservation_key(),
        claim_binding(),
        active_claim(),
        claim_request(),
        retryable_command(active_claim()),
        finalize_command(active_claim()),
        ActionExecutionPermit(
            claim=active_claim(),
            policy_bindings=(),
            approval_binding=None,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=3),
        ),
    )
    for value in values:
        assert type(value).model_validate_json(value.model_dump_json()) == value


def test_claim_commands_require_aware_ordered_times_and_exact_identity():
    with pytest.raises(ValidationError, match="lease_expires_at"):
        claim_request(lease_expires_at=_NOW)
    with pytest.raises(ValidationError, match="timezone-aware"):
        claim_request(requested_at=_NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="timezone-aware"):
        retryable_command(active_claim()).model_copy(
            update={"marked_at": _NOW.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError, match="tenant"):
        finalize_command(active_claim()).model_copy(
            update={"key": reservation_key(tenant_id="tenant-b")}
        )


def test_claim_lease_uses_a_trusted_clock_not_caller_reported_time():
    clock = MutableClock()
    store = claim_store(clock)
    first = store.reserve(claim_request()).claim
    assert first is not None

    with pytest.raises(ActionClaimContractError) as future:
        store.reserve(
            claim_request(
                requested_at=_NOW + timedelta(days=1),
                lease_expires_at=_NOW + timedelta(days=1, minutes=4),
            )
        )
    assert future.value.code == "action_claim_time_invalid"
    assert (
        store.reserve(claim_request()).disposition is ActionClaimDisposition.IN_PROGRESS
    )

    clock.current = first.lease_expires_at
    reclaimed = store.reserve(
        claim_request(
            requested_at=clock.current,
            lease_expires_at=clock.current + timedelta(minutes=4),
        )
    ).claim
    assert reclaimed is not None
    assert reclaimed.claim_revision == 2
    assert reclaimed.fencing_token != first.fencing_token


def test_claim_clock_is_timezone_aware_monotonic_and_fail_closed():
    naive_store = InMemoryActionClaimStore(clock=lambda: _NOW.replace(tzinfo=None))
    with pytest.raises(ActionClaimContractError) as invalid:
        naive_store.reserve(claim_request())
    assert invalid.value.code == "action_claim_clock_invalid"

    clock = MutableClock()
    store = claim_store(clock)
    store.reserve(claim_request())
    clock.current -= timedelta(seconds=1)
    with pytest.raises(ActionClaimContractError) as regressed:
        store.reserve(claim_request())
    assert regressed.value.code == "action_claim_clock_regressed"


def test_terminal_equal_binding_replays_original_outcome_after_lease_expiry():
    store, clock, original, terminal = claim_store_with_existing("terminal")
    clock.current = original.lease_expires_at + timedelta(seconds=1)
    before = claim_ledger_snapshot(store)
    before_calls = clock.calls
    before_trusted = vars(store)["_last_trusted_at"]

    replay = store.reserve(original)

    assert replay.disposition is ActionClaimDisposition.REPLAY
    assert replay.terminal_outcome == terminal.terminal_outcome
    assert clock.calls == before_calls
    assert vars(store)["_last_trusted_at"] == before_trusted
    assert claim_ledger_snapshot(store) == before


@pytest.mark.parametrize(
    "existing_state", ["active", "retryable", "terminal", "expired"]
)
@pytest.mark.parametrize("timestamp_kind", ["expired", "future"])
def test_changed_binding_conflict_precedes_caller_time_for_every_existing_state(
    existing_state: str,
    timestamp_kind: str,
):
    store, clock, _original, _claim = claim_store_with_existing(existing_state)
    requested_at, lease_expires_at = invalid_claim_time(clock, timestamp_kind)
    changed = claim_request(
        binding=claim_binding(request_digest="e" * 64),
        requested_at=requested_at,
        lease_expires_at=lease_expires_at,
    )
    before = claim_ledger_snapshot(store)
    before_calls = clock.calls
    before_trusted = vars(store)["_last_trusted_at"]

    conflict = store.reserve(changed)

    assert conflict.disposition is ActionClaimDisposition.CONFLICT
    assert clock.calls == before_calls
    assert vars(store)["_last_trusted_at"] == before_trusted
    assert claim_ledger_snapshot(store) == before


@pytest.mark.parametrize("existing_state", ["absent", "active", "retryable", "expired"])
@pytest.mark.parametrize("timestamp_kind", ["expired", "future"])
def test_nonterminal_equal_or_absent_invalid_time_fails_closed_without_ledger_write(
    existing_state: str,
    timestamp_kind: str,
):
    if existing_state == "absent":
        clock = MutableClock()
        store = claim_store(clock)
    else:
        store, clock, _original, _claim = claim_store_with_existing(existing_state)
    requested_at, lease_expires_at = invalid_claim_time(clock, timestamp_kind)
    invalid = claim_request(
        requested_at=requested_at,
        lease_expires_at=lease_expires_at,
    )
    before = claim_ledger_snapshot(store)
    before_calls = clock.calls

    with pytest.raises(ActionClaimContractError) as error:
        store.reserve(invalid)

    assert error.value.code == "action_claim_time_invalid"
    assert clock.calls == before_calls + 1
    assert claim_ledger_snapshot(store) == before


@pytest.mark.parametrize("clock_fault", ["failure", "regression"])
@pytest.mark.parametrize(
    ("scenario", "existing_state"),
    [
        ("terminal_replay", "terminal"),
        ("changed_binding_conflict", "active"),
        ("changed_binding_conflict", "retryable"),
        ("changed_binding_conflict", "terminal"),
        ("changed_binding_conflict", "expired"),
    ],
)
def test_terminal_replay_and_binding_conflict_do_not_read_a_broken_clock(
    clock_fault: str,
    scenario: str,
    existing_state: str,
):
    store, clock, original, claim = claim_store_with_existing(existing_state)
    if scenario == "terminal_replay":
        command = original
        expected_disposition = ActionClaimDisposition.REPLAY
    else:
        command = claim_request(
            binding=claim_binding(request_digest="e" * 64),
            requested_at=original.requested_at,
            lease_expires_at=original.lease_expires_at,
        )
        expected_disposition = ActionClaimDisposition.CONFLICT

    if clock_fault == "failure":
        clock.failure = RuntimeError("clock offline")
    else:
        if existing_state == "expired":
            sampled_at = claim.lease_expires_at + timedelta(seconds=2)
            regressed_at = claim.lease_expires_at + timedelta(seconds=1)
        else:
            sampled_at = _NOW + timedelta(seconds=2)
            regressed_at = _NOW + timedelta(seconds=1)
        clock.current = sampled_at
        store._trusted_now()
        clock.current = regressed_at

    before = claim_ledger_snapshot(store)
    before_calls = clock.calls
    before_trusted = vars(store)["_last_trusted_at"]

    result = store.reserve(command)

    assert result.disposition is expected_disposition
    if scenario == "terminal_replay":
        assert result.terminal_outcome == claim.terminal_outcome
    assert clock.calls == before_calls
    assert vars(store)["_last_trusted_at"] == before_trusted
    assert claim_ledger_snapshot(store) == before


def test_future_claim_event_metadata_is_rejected_without_state_change():
    store = claim_store()
    claim = store.reserve(claim_request()).claim
    assert claim is not None

    future_retry = retryable_command(claim).model_copy(
        update={"marked_at": _NOW + timedelta(seconds=1)}
    )
    with pytest.raises(ActionClaimContractError) as retry_error:
        store.mark_retryable(future_retry)
    assert retry_error.value.code == "action_claim_time_invalid"

    future_finalize = finalize_command(
        claim,
        outcome=terminal_outcome(finalized_at=_NOW + timedelta(seconds=1)),
    )
    with pytest.raises(ActionClaimContractError) as finalize_error:
        store.finalize(future_finalize)
    assert finalize_error.value.code == "action_claim_time_invalid"
    assert (
        store.reserve(claim_request()).disposition is ActionClaimDisposition.IN_PROGRESS
    )


def test_atomic_reserve_returns_exactly_four_dispositions_without_fence_leakage():
    store = claim_store()
    command = claim_request()

    claimed = store.reserve(command)
    assert claimed.disposition is ActionClaimDisposition.CLAIMED
    assert claimed.claim is not None
    assert claimed.claim.claim_revision == 1
    assert claimed.claim.state is ActionClaimState.ACTIVE
    assert claimed.terminal_outcome is None

    in_progress = store.reserve(command)
    assert in_progress.disposition is ActionClaimDisposition.IN_PROGRESS
    assert in_progress.claim is None
    assert in_progress.terminal_outcome is None

    changed = store.reserve(
        claim_request(binding=claim_binding(request_digest="e" * 64))
    )
    assert changed.disposition is ActionClaimDisposition.CONFLICT
    assert changed.claim is None
    assert changed.terminal_outcome is None

    terminal = store.finalize(finalize_command(claimed.claim))
    replay = store.reserve(command)
    assert replay.disposition is ActionClaimDisposition.REPLAY
    assert replay.claim is None
    assert replay.terminal_outcome == terminal.terminal_outcome
    assert set(ActionClaimDisposition) == {
        ActionClaimDisposition.CLAIMED,
        ActionClaimDisposition.IN_PROGRESS,
        ActionClaimDisposition.REPLAY,
        ActionClaimDisposition.CONFLICT,
    }


@pytest.mark.parametrize(
    "changed_binding",
    [
        claim_binding(invocation_id="invocation-other"),
        claim_binding(action=action_reference(version=4)),
        claim_binding(action=action_reference(digest="e" * 64)),
        claim_binding(request_digest="e" * 64),
        claim_binding(
            capability=capability_binding().model_copy(
                update={"capability_name": "enterprise.crm.customer.merge"}
            )
        ),
        claim_binding(
            capability=capability_binding().model_copy(
                update={"capability_version": "2.1.0"}
            )
        ),
        claim_binding(
            capability=capability_binding().model_copy(update={"schema_hash": "e" * 64})
        ),
        claim_binding(adapter_id="adapter.crm.secondary"),
        claim_binding(target_system="erp"),
    ],
)
def test_every_valid_binding_coordinate_conflicts_for_the_same_stable_key(
    changed_binding: ClaimBindingPayload,
):
    store = claim_store()
    first = store.reserve(claim_request())
    assert first.disposition is ActionClaimDisposition.CLAIMED

    conflict = store.reserve(claim_request(binding=changed_binding))
    assert conflict.disposition is ActionClaimDisposition.CONFLICT


def test_request_identity_is_tenant_bound_and_contract_errors_are_stable():
    with pytest.raises(ValidationError, match="tenant"):
        claim_request(key=reservation_key(tenant_id="tenant-b"))
    with pytest.raises(ValidationError, match="stable name"):
        claim_request(key=reservation_key(action_stable_name="customer.merge_crm"))

    store = claim_store()
    tenant_a = store.reserve(claim_request())
    tenant_b = store.reserve(
        claim_request(
            key=reservation_key(tenant_id="tenant-b"),
            binding=claim_binding(action=action_reference(tenant_id="tenant-b")),
        )
    )
    assert tenant_a.disposition is ActionClaimDisposition.CLAIMED
    assert tenant_b.disposition is ActionClaimDisposition.CLAIMED

    for invalid in (None, {}, claim_binding()):
        with pytest.raises(ActionClaimContractError) as error:
            store.reserve(invalid)
        assert error.value.code == "action_claim_command_invalid"


def test_retryable_and_expired_claims_reclaim_with_new_revision_and_fence():
    retry_store = claim_store()
    first = retry_store.reserve(claim_request()).claim
    assert first is not None
    retryable = retry_store.mark_retryable(retryable_command(first))
    assert retryable.state is ActionClaimState.RETRYABLE
    assert retryable.claim_revision == first.claim_revision
    assert retryable.fencing_token == first.fencing_token

    reclaimed = retry_store.reserve(claim_request()).claim
    assert reclaimed is not None
    assert reclaimed.claim_revision == first.claim_revision + 1
    assert reclaimed.fencing_token != first.fencing_token

    for stale_operation in (
        lambda: retry_store.mark_retryable(retryable_command(first)),
        lambda: retry_store.finalize(finalize_command(first)),
    ):
        with pytest.raises(ActionClaimStaleFenceError) as stale:
            stale_operation()
        assert stale.value.code == "action_claim_stale_fence"

    expiry_clock = MutableClock()
    expiry_store = claim_store(expiry_clock)
    expiring = expiry_store.reserve(
        claim_request(lease_expires_at=_NOW + timedelta(seconds=10))
    ).claim
    assert expiring is not None
    expiry_clock.current = expiring.lease_expires_at
    expired_reclaim = expiry_store.reserve(
        claim_request(
            requested_at=expiring.lease_expires_at,
            lease_expires_at=expiring.lease_expires_at + timedelta(minutes=4),
        )
    ).claim
    assert expired_reclaim is not None
    assert expired_reclaim.claim_revision == 2
    assert expired_reclaim.fencing_token != expiring.fencing_token


def test_all_historical_fences_are_permanently_stale_after_multiple_reclaims():
    store = claim_store()
    generations: list[ActionClaim] = []
    current = store.reserve(claim_request()).claim
    assert current is not None
    generations.append(current)

    for _generation in range(2):
        store.mark_retryable(retryable_command(current))
        current = store.reserve(claim_request()).claim
        assert current is not None
        generations.append(current)

    assert tuple(item.claim_revision for item in generations) == (1, 2, 3)
    assert len({item.fencing_token for item in generations}) == 3
    for historical in generations[:-1]:
        for operation in (
            lambda historical=historical: store.mark_retryable(
                retryable_command(historical)
            ),
            lambda historical=historical: store.finalize(finalize_command(historical)),
        ):
            with pytest.raises(ActionClaimStaleFenceError) as stale:
                operation()
            assert stale.value.code == "action_claim_stale_fence"

    still_active = store.reserve(claim_request())
    assert still_active.disposition is ActionClaimDisposition.IN_PROGRESS


def test_changed_binding_never_reclaims_retryable_expired_or_terminal_claims():
    changed = claim_binding(request_digest="e" * 64)

    retryable_store = claim_store()
    retryable_claim = retryable_store.reserve(claim_request()).claim
    assert retryable_claim is not None
    retryable_store.mark_retryable(retryable_command(retryable_claim))
    assert (
        retryable_store.reserve(claim_request(binding=changed)).disposition
        is ActionClaimDisposition.CONFLICT
    )

    expired_clock = MutableClock()
    expired_store = claim_store(expired_clock)
    expired_claim = expired_store.reserve(
        claim_request(lease_expires_at=_NOW + timedelta(seconds=1))
    ).claim
    assert expired_claim is not None
    expired_clock.current = expired_claim.lease_expires_at
    assert (
        expired_store.reserve(
            claim_request(
                binding=changed,
                requested_at=expired_claim.lease_expires_at,
                lease_expires_at=expired_claim.lease_expires_at + timedelta(minutes=4),
            )
        ).disposition
        is ActionClaimDisposition.CONFLICT
    )

    terminal_store = claim_store()
    terminal_claim = terminal_store.reserve(claim_request()).claim
    assert terminal_claim is not None
    terminal_store.finalize(finalize_command(terminal_claim))
    assert (
        terminal_store.reserve(claim_request(binding=changed)).disposition
        is ActionClaimDisposition.CONFLICT
    )


def test_finalize_is_fenced_compare_and_set_and_exactly_replayable():
    store = claim_store()
    claim = store.reserve(claim_request()).claim
    assert claim is not None
    command = finalize_command(claim)

    finalized = store.finalize(command)
    exact_retry = store.finalize(command)
    assert exact_retry == finalized
    assert finalized.state is ActionClaimState.TERMINAL
    assert finalized.claim_revision == claim.claim_revision
    assert finalized.fencing_token == claim.fencing_token

    with pytest.raises(ActionClaimOutcomeConflictError) as changed_outcome:
        store.finalize(
            command.model_copy(
                update={"outcome": terminal_outcome(outcome_digest="e" * 64)}
            )
        )
    assert changed_outcome.value.code == "action_claim_outcome_conflict"

    replay = store.reserve(claim_request())
    assert replay.disposition is ActionClaimDisposition.REPLAY
    assert replay.terminal_outcome == terminal_outcome()


def test_claim_updates_fail_closed_for_missing_binding_revision_fence_and_state():
    store = claim_store()
    missing_claim = active_claim()
    for operation in (
        lambda: store.mark_retryable(retryable_command(missing_claim)),
        lambda: store.finalize(finalize_command(missing_claim)),
    ):
        with pytest.raises(ActionClaimNotFoundError) as missing:
            operation()
        assert missing.value.code == "action_claim_not_found"

    claim = store.reserve(claim_request()).claim
    assert claim is not None
    with pytest.raises(ActionClaimBindingConflictError):
        store.finalize(
            finalize_command(claim).model_copy(
                update={"binding": claim_binding(request_digest="e" * 64)}
            )
        )
    with pytest.raises(ActionClaimRevisionConflictError):
        store.finalize(
            finalize_command(claim).model_copy(update={"expected_claim_revision": 2})
        )
    with pytest.raises(ActionClaimStaleFenceError):
        store.finalize(
            finalize_command(claim).model_copy(update={"fencing_token": "old-fence"})
        )

    store.mark_retryable(retryable_command(claim))
    with pytest.raises(ActionClaimTransitionError):
        store.finalize(finalize_command(claim))


def test_expired_owner_cannot_mark_retryable_or_finalize_without_reclaim():
    for operation_name in ("retryable", "finalize"):
        clock = MutableClock()
        store = claim_store(clock)
        claim = store.reserve(
            claim_request(lease_expires_at=_NOW + timedelta(seconds=10))
        ).claim
        assert claim is not None
        clock.current = claim.lease_expires_at
        with pytest.raises(ActionClaimTransitionError) as expired:
            if operation_name == "retryable":
                store.mark_retryable(retryable_command(claim))
            else:
                store.finalize(finalize_command(claim))
        assert expired.value.code == "action_claim_lease_expired"


def test_claim_port_has_no_read_before_reserve_or_receipt_lookup_surface():
    public_methods = {
        name
        for name, value in vars(ActionClaimPort).items()
        if callable(value) and not name.startswith("_")
    }
    assert public_methods == {"reserve", "mark_retryable", "finalize"}


def test_concurrent_claim_reclaim_and_terminal_replay_are_single_winner():
    for _round in range(100):
        store = claim_store()
        command = claim_request()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(store.reserve, (command,) * 8))
        dispositions = tuple(item.disposition for item in results)
        assert dispositions.count(ActionClaimDisposition.CLAIMED) == 1
        assert dispositions.count(ActionClaimDisposition.IN_PROGRESS) == 7
        winner = next(item.claim for item in results if item.claim is not None)
        assert winner is not None

        store.mark_retryable(retryable_command(winner))
        with ThreadPoolExecutor(max_workers=8) as pool:
            reclaims = tuple(pool.map(store.reserve, (command,) * 8))
        reclaim_dispositions = tuple(item.disposition for item in reclaims)
        assert reclaim_dispositions.count(ActionClaimDisposition.CLAIMED) == 1
        assert reclaim_dispositions.count(ActionClaimDisposition.IN_PROGRESS) == 7
        reclaimed = next(item.claim for item in reclaims if item.claim is not None)
        assert reclaimed is not None
        assert reclaimed.claim_revision == 2
        assert reclaimed.fencing_token != winner.fencing_token

        store.finalize(finalize_command(reclaimed))
        with ThreadPoolExecutor(max_workers=8) as pool:
            replays = tuple(pool.map(store.reserve, (command,) * 8))
        assert {item.disposition for item in replays} == {ActionClaimDisposition.REPLAY}
        assert {item.terminal_outcome for item in replays} == {terminal_outcome()}


def test_concurrent_changed_bindings_never_share_one_reservation_owner():
    for _round in range(100):
        store = claim_store()
        first = claim_request()
        changed = claim_request(binding=claim_binding(request_digest="e" * 64))
        commands = (first,) * 4 + (changed,) * 4
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(store.reserve, commands))
        dispositions = tuple(item.disposition for item in results)
        assert dispositions.count(ActionClaimDisposition.CLAIMED) == 1
        assert dispositions.count(ActionClaimDisposition.IN_PROGRESS) == 3
        assert dispositions.count(ActionClaimDisposition.CONFLICT) == 4


def test_reclaim_and_old_finalize_race_never_accepts_an_old_fence_after_reclaim():
    for _round in range(100):
        clock = MutableClock()
        store = claim_store(clock)
        old_claim = store.reserve(
            claim_request(lease_expires_at=_NOW + timedelta(seconds=10))
        ).claim
        assert old_claim is not None
        clock.current = old_claim.lease_expires_at
        barrier = Barrier(2)

        def reclaim() -> object:
            barrier.wait()
            return store.reserve(
                claim_request(
                    requested_at=old_claim.lease_expires_at,
                    lease_expires_at=old_claim.lease_expires_at + timedelta(minutes=4),
                )
            )

        def finalize_old() -> object:
            barrier.wait()
            try:
                return store.finalize(
                    finalize_command(
                        old_claim,
                        outcome=terminal_outcome(
                            finalized_at=old_claim.lease_expires_at
                            - timedelta(seconds=1)
                        ),
                    )
                )
            except (
                ActionClaimStaleFenceError,
                ActionClaimTransitionError,
            ) as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            reclaim_future = pool.submit(reclaim)
            finalize_future = pool.submit(finalize_old)
            reclaim_result = reclaim_future.result()
            finalize_result = finalize_future.result()

        assert hasattr(reclaim_result, "disposition")
        assert reclaim_result.disposition is ActionClaimDisposition.CLAIMED
        assert isinstance(
            finalize_result,
            (ActionClaimStaleFenceError, ActionClaimTransitionError),
        )
        assert finalize_result.code in {
            "action_claim_stale_fence",
            "action_claim_lease_expired",
        }
        assert (
            store.reserve(claim_request()).disposition
            is ActionClaimDisposition.IN_PROGRESS
        )

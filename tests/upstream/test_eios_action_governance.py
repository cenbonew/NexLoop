from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
import traceback
import pytest

from eios.actions.governance import (
    ActionGovernanceError,
    ActionGovernor,
    required_action_scopes,
)
from eios.actions.models import (
    ActionClaim,
    ActionClaimDisposition,
    ActionClaimRequest,
    ActionClaimResult,
    ActionClaimState,
    ActionExecutionPermit,
    ActionReservationKey,
    ApprovalEvidence,
    ClaimBindingPayload,
    PolicyDecisionEvidence,
    PolicyEvidenceSet,
    TerminalOutcomeReference,
    TerminalOutcomeStatus,
    canonical_request_digest,
)
from eios.actions.ports import ActionClaimError
from eios.approvals.ports import InMemoryApprovalHumanAuthorityVerifier
from eios.approvals.models import (
    ApprovalDecision,
    ApprovalHumanAuthority,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalSubject,
    approval_record_snapshot_digest,
)
from eios.composition.action_approval import ApprovalEvidenceResolver
from eios.control.models import TenantContext

from eios.ontology.definitions import (
    ActionApprovalMode,
    ActionChangeScope,
    ActionDefinition,
    ActionPrecondition,
    ActionGovernanceContract,
    ActionIdempotencyPolicy,
    ActionRiskLevel,
    CapabilityBinding,
    DefinitionReference,
    DefinitionStatus,
    DefinitionType,
    OntologySchemaReference,
    OntologySchemaType,
    PropertyReference,
)
from eios.ontology.version_resolution import (
    CapabilityContractKind,
    CapabilityContractSnapshot,
)

_SUBJECT_DEFAULT = object()


_NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
_TENANT = "tenant-a"
_INVOCATION = "invocation-42"
_IDEMPOTENCY_KEY = "request-42"
_ADAPTER = "adapter.crm.primary"
_TARGET = "crm"
_CAPABILITY_SCOPE = "capability.crm.write"
_SHARED_SCOPE = "enterprise.customer.write"
_ACTION_SCOPE = "action.customer.update"
_POLICIES = (
    "policy.customer.data_residency",
    "policy.customer.write",
)
_REQUEST: Mapping[str, object] = {
    "customer_id": "customer-42",
    "status": "active",
}


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


class ExplodingTimezone(tzinfo):
    def utcoffset(self, value: datetime | None) -> timedelta:
        raise RuntimeError("TZ_SECRET_8")

    def dst(self, value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, value: datetime | None) -> str:
        return "exploding"


class ExplodingAstimezoneDateTime(datetime):
    def astimezone(self, tz: tzinfo | None = None) -> datetime:
        raise RuntimeError("ASTIMEZONE_SECRET_8")


class MalformedAstimezoneDateTime(datetime):
    def astimezone(self, tz: tzinfo | None = None):
        return "CLOCK_RETURN_SECRET_8"


class MalformedStartDateTime(datetime):
    def astimezone(self, tz: tzinfo | None = None):
        return "2026-07-16T09:00:00+00:00"


class MalformedEndDateTime(datetime):
    def astimezone(self, tz: tzinfo | None = None):
        return "2026-07-16T09:05:00+00:00"


class NonCanonicalAstimezoneDateTime(datetime):
    def astimezone(self, tz: tzinfo | None = None) -> datetime:
        return datetime(
            2026,
            7,
            16,
            8,
            59,
            tzinfo=timezone(timedelta(0), "NONCANON_SECRET_8"),
        )


class HostileRequestMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0

    def items(self):
        raise RuntimeError("REQUEST_SECRET_8")


def _unsafe_model_copy(instance: object, **updates: object):
    model_type = type(instance)
    payload = {
        field_name: getattr(instance, field_name)
        for field_name in model_type.model_fields
    }
    payload.update(updates)
    return model_type.model_construct(**payload)


class ClaimSpy:
    def __init__(self, result: ActionClaimResult | object) -> None:
        self.result = result
        self.reserve_calls: list[ActionClaimRequest] = []
        self.failure: Exception | None = None

    def reserve(self, command: ActionClaimRequest) -> ActionClaimResult:
        self.reserve_calls.append(command)
        if self.failure is not None:
            raise self.failure
        return self.result  # type: ignore[return-value]

    def mark_retryable(self, command: object) -> ActionClaim:  # pragma: no cover
        raise AssertionError(f"unexpected mark_retryable: {command!r}")

    def finalize(self, command: object) -> ActionClaim:  # pragma: no cover
        raise AssertionError(f"unexpected finalize: {command!r}")


class MutatingClaimPort(ClaimSpy):
    def __init__(
        self,
        mutation: str,
        *,
        disposition: ActionClaimDisposition = ActionClaimDisposition.CLAIMED,
    ) -> None:
        super().__init__(object())
        self.mutation = mutation
        self.disposition = disposition
        self.received_command: ActionClaimRequest | None = None

    def reserve(self, command: ActionClaimRequest) -> ActionClaimResult:
        self.reserve_calls.append(command)
        self.received_command = command
        if self.mutation == "key_container":
            object.__setattr__(
                command,
                "key",
                command.key.model_copy(
                    update={"idempotency_key": "request-container-mutated"}
                ),
            )
        elif self.mutation == "binding_container":
            object.__setattr__(
                command,
                "binding",
                command.binding.model_copy(
                    update={"adapter_id": "adapter.crm.container_mutated"}
                ),
            )
        elif self.mutation == "key_tenant":
            object.__setattr__(command.key, "tenant_id", "tenant-port-mutated")
            object.__setattr__(
                command.binding.action_reference,
                "tenant_id",
                "tenant-port-mutated",
            )
        elif self.mutation == "key_stable_name":
            object.__setattr__(
                command.key,
                "action_stable_name",
                "customer.port_mutated",
            )
            object.__setattr__(
                command.binding.action_reference,
                "stable_name",
                "customer.port_mutated",
            )
        elif self.mutation == "key_idempotency":
            object.__setattr__(
                command.key,
                "idempotency_key",
                "request-port-mutated",
            )
        elif self.mutation == "binding_invocation":
            object.__setattr__(
                command.binding,
                "invocation_id",
                "invocation-port-mutated",
            )
        elif self.mutation == "action_version":
            object.__setattr__(
                command.binding.action_reference,
                "version",
                command.binding.action_reference.version + 1,
            )
        elif self.mutation == "action_digest":
            object.__setattr__(
                command.binding.action_reference,
                "contract_digest",
                "e" * 64,
            )
        elif self.mutation == "request_digest":
            object.__setattr__(
                command.binding,
                "request_digest",
                "e" * 64,
            )
        elif self.mutation == "capability_name":
            object.__setattr__(
                command.binding.capability_binding,
                "capability_name",
                "enterprise.crm.customer.port_mutated",
            )
        elif self.mutation == "capability_version":
            object.__setattr__(
                command.binding.capability_binding,
                "capability_version",
                "99.0.0",
            )
        elif self.mutation == "capability_schema_hash":
            object.__setattr__(
                command.binding.capability_binding,
                "schema_hash",
                "e" * 64,
            )
        elif self.mutation == "adapter_id":
            object.__setattr__(
                command.binding,
                "adapter_id",
                "adapter.crm.mutated",
            )
        elif self.mutation == "target_system":
            object.__setattr__(
                command.binding,
                "target_system",
                "erp",
            )
        elif self.mutation == "requested_at":
            object.__setattr__(command, "requested_at", _NOW)
        elif self.mutation == "lease_expires_at":
            object.__setattr__(
                command,
                "lease_expires_at",
                command.lease_expires_at + timedelta(days=1),
            )
        else:  # pragma: no cover - fixture contract
            raise AssertionError(f"unknown mutation: {self.mutation}")

        if self.disposition is ActionClaimDisposition.CLAIMED:
            return _claimed_result(command)
        if self.disposition is ActionClaimDisposition.REPLAY:
            return ActionClaimResult(
                disposition=self.disposition,
                terminal_outcome=_terminal_outcome(),
            )
        return ActionClaimResult(disposition=self.disposition)


def _claim_command_native_snapshot(command: ActionClaimRequest) -> tuple[object, ...]:
    reference = command.binding.action_reference
    capability = command.binding.capability_binding
    return (
        command.key.tenant_id,
        command.key.action_stable_name,
        command.key.idempotency_key,
        command.binding.invocation_id,
        reference.tenant_id,
        reference.definition_type,
        reference.stable_name,
        reference.version,
        reference.contract_digest,
        command.binding.request_digest,
        capability.capability_name,
        capability.capability_version,
        capability.schema_hash,
        command.binding.adapter_id,
        command.binding.target_system,
        command.requested_at,
        command.lease_expires_at,
    )


class ApprovalSpy:
    def __init__(self, record: ApprovalRecord | None) -> None:
        self.record = record
        self.get_calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    def get(self, *, tenant_id: str, approval_id: str) -> ApprovalRecord | None:
        self.get_calls.append((tenant_id, approval_id))
        if self.failure is not None:
            raise self.failure
        return self.record

    def create(self, command: object) -> object:  # pragma: no cover
        raise AssertionError(f"unexpected create: {command!r}")

    def decide(self, command: object) -> object:  # pragma: no cover
        raise AssertionError(f"unexpected decide: {command!r}")

    def list(self, *, tenant_id: str) -> tuple[ApprovalRecord, ...]:
        raise AssertionError(f"unexpected list: {tenant_id!r}")


def _object_type() -> OntologySchemaReference:
    return OntologySchemaReference(
        tenant_id=_TENANT,
        schema_type=OntologySchemaType.OBJECT_TYPE,
        stable_name="Customer",
        version=1,
        schema_digest="a" * 64,
    )


def _capability_binding() -> CapabilityBinding:
    return CapabilityBinding(
        capability_name="enterprise.crm.customer.update",
        capability_version="2.0.0",
        schema_hash="b" * 64,
    )


def _action_definition(
    *,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    approval_mode: ActionApprovalMode = ActionApprovalMode.NONE,
    required_scopes: tuple[str, ...] = (
        _CAPABILITY_SCOPE,
        _SHARED_SCOPE,
        _ACTION_SCOPE,
    ),
    status: DefinitionStatus = DefinitionStatus.PUBLISHED,
    preconditions: tuple = (),
) -> ActionDefinition:
    object_type = _object_type()
    return ActionDefinition(
        tenant_id=_TENANT,
        stable_name="customer.update_crm",
        version=3,
        status=status,
        required_scopes=required_scopes,
        created_by="domain-owner",
        created_at=_NOW - timedelta(days=1),
        capability_binding=_capability_binding(),
        object_types=(object_type,),
        governance=ActionGovernanceContract(
            change_scope=ActionChangeScope(
                object_types=(object_type,),
                target_systems=(_TARGET,),
            ),
            risk_level=risk_level,
            policy_refs=_POLICIES,
            approval_mode=approval_mode,
            idempotency=ActionIdempotencyPolicy(key_fields=("request_id",)),
        ),
        receipt_schema={"type": "object"},
        preconditions=preconditions,
    )


def _capability_snapshot(
    *,
    required_scopes: tuple[str, ...] = (_CAPABILITY_SCOPE, _SHARED_SCOPE),
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
) -> CapabilityContractSnapshot:
    binding = _capability_binding()
    return CapabilityContractSnapshot(
        capability_name=binding.capability_name,
        capability_version=binding.capability_version,
        schema_hash=binding.schema_hash,
        kind=CapabilityContractKind.ATOMIC,
        has_side_effects=True,
        idempotent=True,
        required_scopes=required_scopes,
        risk_level=risk_level,
    )


def _policy_evidence(
    action_reference: DefinitionReference,
    policy_reference: str,
    *,
    tenant_id: str = _TENANT,
    invocation_id: str = _INVOCATION,
    allowed: bool = True,
    issued_at: datetime = _NOW - timedelta(minutes=2),
    expires_at: datetime = _NOW + timedelta(minutes=10),
    ordinal: int = 1,
) -> PolicyDecisionEvidence:
    return PolicyDecisionEvidence(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        action_reference=action_reference,
        policy_reference=policy_reference,
        evidence_id=f"policy-evidence-{ordinal}",
        decision_id=f"policy-decision-{ordinal}",
        decision_revision=ordinal,
        allowed=allowed,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _policy_set(
    action_reference: DefinitionReference,
    *,
    required_policy_references: tuple[str, ...] = _POLICIES,
    evidence: tuple[PolicyDecisionEvidence, ...] | None = None,
    tenant_id: str = _TENANT,
    invocation_id: str = _INVOCATION,
    evaluated_at: datetime = _NOW,
) -> PolicyEvidenceSet:
    items = evidence
    if items is None:
        items = tuple(
            _policy_evidence(action_reference, policy, ordinal=index)
            for index, policy in enumerate(required_policy_references, start=1)
        )
    return PolicyEvidenceSet(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        action_reference=action_reference,
        required_policy_references=required_policy_references,
        evidence=items,
        evaluated_at=evaluated_at,
    )


def _approval_evidence(
    action_reference: DefinitionReference,
    *,
    tenant_id: str = _TENANT,
    invocation_id: str = _INVOCATION,
    approval_id: str = "approval-42",
    approved_revision: int = 2,
    decision_id: str = "approval-decision-42",
    decided_at: datetime = _NOW - timedelta(minutes=4),
    issued_at: datetime = _NOW - timedelta(minutes=3),
    expires_at: datetime = _NOW + timedelta(minutes=10),
    approval_record_digest: str | None = None,
) -> ApprovalEvidence:
    provisional = ApprovalEvidence(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        action_reference=action_reference,
        approval_id=approval_id,
        approved_revision=approved_revision,
        decision_id=decision_id,
        approval_record_digest=approval_record_digest or "0" * 64,
        outcome="approved",
        decided_at=decided_at,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    if approval_record_digest is not None:
        return provisional
    record = _approval_record(provisional)
    return provisional.model_copy(
        update={"approval_record_digest": approval_record_snapshot_digest(record)}
    )


def _approval_subject(
    evidence: ApprovalEvidence,
    *,
    request_digest: str | None = None,
) -> ApprovalSubject:
    return ApprovalSubject(
        action_reference=evidence.action_reference,
        request_digest=request_digest or canonical_request_digest(_REQUEST),
        requester_id="requester-42",
        risk_level="low",
        justification="test approval",
    )


def _approval_record(
    evidence: ApprovalEvidence,
    *,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    revision: int | None = None,
    decision_id: str | None = None,
    decided_at: datetime | None = None,
    subject: ApprovalSubject | None | object = _SUBJECT_DEFAULT,
) -> ApprovalRecord:
    resolved_subject = (
        _approval_subject(evidence) if subject is _SUBJECT_DEFAULT else subject
    )
    if status is ApprovalStatus.WAITING_APPROVAL:
        return ApprovalRecord(
            tenant_id=evidence.tenant_id,
            approval_id=evidence.approval_id,
            status=status,
            revision=1,
            requested_at=evidence.decided_at - timedelta(minutes=2),
            subject=resolved_subject,
        )
    decision = ApprovalDecision(
        tenant_id=evidence.tenant_id,
        approval_id=evidence.approval_id,
        decision_id=decision_id or evidence.decision_id,
        outcome=status,
        actor="approver-42",
        reason_code="approval.reviewed",
        decided_at=decided_at or evidence.decided_at,
        human_authority=(
            ApprovalHumanAuthority(
                tenant_id=evidence.tenant_id,
                actor="approver-42",
                subject_id="approver-42",
                subject_kind="human",
                session_id="browser-session-42",
                session_revision=1,
                credential_id="credential-42",
                application_id="eios.portal",
                application_version="memory",
                eligible_scopes=("approvals.decide",),
            )
            if status is ApprovalStatus.APPROVED
            else None
        ),
    )
    return ApprovalRecord(
        tenant_id=evidence.tenant_id,
        approval_id=evidence.approval_id,
        status=status,
        revision=revision or 2,
        requested_at=evidence.decided_at - timedelta(minutes=2),
        decision=decision,
        subject=resolved_subject,
        decisions=(decision,),
    )


def _claim_request(
    action: ActionDefinition, request: Mapping[str, object] | None = None
) -> ActionClaimRequest:
    action_reference = action.reference()
    key = ActionReservationKey(
        tenant_id=_TENANT,
        action_stable_name=action_reference.stable_name,
        idempotency_key=_IDEMPOTENCY_KEY,
    )
    binding = ClaimBindingPayload(
        invocation_id=_INVOCATION,
        action_reference=action_reference,
        request_digest=canonical_request_digest(
            _REQUEST if request is None else dict(request)
        ),
        capability_binding=action.capability_binding,
        adapter_id=_ADAPTER,
        target_system=_TARGET,
    )
    return ActionClaimRequest(
        key=key,
        binding=binding,
        requested_at=_NOW,
        lease_expires_at=_NOW + timedelta(minutes=5),
    )


def _claimed_result(command: ActionClaimRequest) -> ActionClaimResult:
    return ActionClaimResult(
        disposition=ActionClaimDisposition.CLAIMED,
        claim=ActionClaim(
            key=command.key,
            binding=command.binding,
            state=ActionClaimState.ACTIVE,
            claim_revision=1,
            fencing_token="fence-1",
            lease_expires_at=command.lease_expires_at,
        ),
    )


@dataclass
class GovernanceHarness:
    tenant_id: str
    invocation_id: str
    action_reference: DefinitionReference
    action_definition: ActionDefinition
    capability_snapshot: CapabilityContractSnapshot
    request: Mapping[str, object]
    claim_request: ActionClaimRequest
    granted_scopes: frozenset[str]
    policy_evidence: PolicyEvidenceSet
    approval_evidence: ApprovalEvidence | None
    claim_port: ClaimSpy
    approval_port: ApprovalSpy
    clock: MutableClock
    governor: ActionGovernor

    def govern(self) -> ActionExecutionPermit | TerminalOutcomeReference:
        return self.governor.govern(
            tenant_id=self.tenant_id,
            invocation_id=self.invocation_id,
            action_reference=self.action_reference,
            action_definition=self.action_definition,
            capability_snapshot=self.capability_snapshot,
            request=self.request,
            claim_request=self.claim_request,
            granted_scopes=self.granted_scopes,
            policy_evidence=self.policy_evidence,
            approval_evidence=self.approval_evidence,
        )


def _harness(
    *,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    approval_mode: ActionApprovalMode = ActionApprovalMode.NONE,
    include_approval: bool = False,
    action_scopes: tuple[str, ...] = (
        _CAPABILITY_SCOPE,
        _SHARED_SCOPE,
        _ACTION_SCOPE,
    ),
    capability_scopes: tuple[str, ...] = (_CAPABILITY_SCOPE, _SHARED_SCOPE),
) -> GovernanceHarness:
    action = _action_definition(
        risk_level=risk_level,
        approval_mode=approval_mode,
        required_scopes=action_scopes,
    )
    action_reference = action.reference()
    request = _claim_request(action)
    policy = _policy_set(action_reference)
    approval = _approval_evidence(action_reference) if include_approval else None
    approval_record = _approval_record(approval) if approval is not None else None
    claim_port = ClaimSpy(_claimed_result(request))
    approval_port = ApprovalSpy(approval_record)
    clock = MutableClock()
    governor = ActionGovernor(
        claim_port=claim_port,
        approval_port=approval_port,
        clock=clock,
    )
    return GovernanceHarness(
        tenant_id=_TENANT,
        invocation_id=_INVOCATION,
        action_reference=action_reference,
        action_definition=action,
        capability_snapshot=_capability_snapshot(required_scopes=capability_scopes),
        request=_REQUEST,
        claim_request=request,
        granted_scopes=frozenset(action_scopes) | frozenset(capability_scopes),
        policy_evidence=policy,
        approval_evidence=approval,
        claim_port=claim_port,
        approval_port=approval_port,
        clock=clock,
        governor=governor,
    )


def _assert_governance_error(
    harness: GovernanceHarness,
    code: str,
) -> ActionGovernanceError:
    with pytest.raises(ActionGovernanceError) as captured:
        harness.govern()
    assert captured.value.code == code
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    return captured.value


_RISK_APPROVAL_TABLE = (
    (ActionRiskLevel.LOW, ActionApprovalMode.NONE, False),
    (ActionRiskLevel.LOW, ActionApprovalMode.REQUIRED, True),
    (ActionRiskLevel.MEDIUM, ActionApprovalMode.NONE, True),
    (ActionRiskLevel.MEDIUM, ActionApprovalMode.REQUIRED, True),
    (ActionRiskLevel.HIGH, ActionApprovalMode.NONE, True),
    (ActionRiskLevel.HIGH, ActionApprovalMode.REQUIRED, True),
    (ActionRiskLevel.CRITICAL, ActionApprovalMode.NONE, True),
    (ActionRiskLevel.CRITICAL, ActionApprovalMode.REQUIRED, True),
)


@pytest.mark.parametrize(
    ("risk_level", "approval_mode", "approval_required"),
    _RISK_APPROVAL_TABLE,
)
def test_frozen_a1_r1_table_has_exact_scopes_policies_and_approval_rule(
    risk_level: ActionRiskLevel,
    approval_mode: ActionApprovalMode,
    approval_required: bool,
) -> None:
    without_approval = _harness(
        risk_level=risk_level,
        approval_mode=approval_mode,
        include_approval=False,
    )
    expected_scopes = frozenset({_CAPABILITY_SCOPE, _SHARED_SCOPE, _ACTION_SCOPE})
    assert (
        required_action_scopes(
            without_approval.action_definition,
            without_approval.capability_snapshot,
        )
        == expected_scopes
    )
    assert (
        without_approval.policy_evidence.required_policy_references
        == without_approval.action_definition.governance.policy_refs
        == _POLICIES
    )

    if approval_required:
        _assert_governance_error(
            without_approval,
            "action_governance_approval_required",
        )
        assert without_approval.claim_port.reserve_calls == []
        with_approval = _harness(
            risk_level=risk_level,
            approval_mode=approval_mode,
            include_approval=True,
        )
        assert isinstance(with_approval.govern(), ActionExecutionPermit)
        return

    assert isinstance(without_approval.govern(), ActionExecutionPermit)


@pytest.mark.parametrize(
    "granted_scopes",
    (
        frozenset({_CAPABILITY_SCOPE, _SHARED_SCOPE, _ACTION_SCOPE}),
        frozenset(
            {
                _CAPABILITY_SCOPE,
                _SHARED_SCOPE,
                _ACTION_SCOPE,
                "unrelated.extra",
            }
        ),
    ),
)
def test_scope_union_accepts_exact_grants_and_supersets(
    granted_scopes: frozenset[str],
) -> None:
    harness = _harness()
    harness.granted_scopes = granted_scopes
    assert isinstance(harness.govern(), ActionExecutionPermit)
    assert harness.claim_port.reserve_calls == [harness.claim_request]


@pytest.mark.parametrize(
    "missing_scope",
    (_CAPABILITY_SCOPE, _SHARED_SCOPE, _ACTION_SCOPE),
)
def test_scope_union_rejects_each_missing_capability_or_action_scope_before_claim(
    missing_scope: str,
) -> None:
    harness = _harness()
    harness.granted_scopes = harness.granted_scopes - {missing_scope}
    _assert_governance_error(harness, "action_governance_scope_missing")
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []


def test_capability_scope_weakening_fails_before_scope_or_claim() -> None:
    harness = _harness(
        action_scopes=(_SHARED_SCOPE, _ACTION_SCOPE),
        capability_scopes=(_CAPABILITY_SCOPE, _SHARED_SCOPE),
    )
    harness.granted_scopes = frozenset()
    _assert_governance_error(harness, "capability_scope_weakening")
    assert harness.claim_port.reserve_calls == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("tenant", "action_governance_identity_mismatch"),
        ("invocation", "action_governance_identity_mismatch"),
        ("action_name", "action_governance_identity_mismatch"),
        ("action_version", "action_governance_identity_mismatch"),
        ("action_digest", "action_governance_identity_mismatch"),
        ("claim_action", "action_governance_identity_mismatch"),
        ("target_system", "action_governance_identity_mismatch"),
        ("request_digest", "action_governance_request_digest_mismatch"),
    ),
)
def test_identity_and_request_digest_mismatches_fail_before_claim(
    mutation: str,
    expected_code: str,
) -> None:
    harness = _harness()
    if mutation == "tenant":
        harness.tenant_id = "tenant-b"
    elif mutation == "invocation":
        harness.invocation_id = "invocation-other"
    elif mutation == "action_name":
        harness.action_reference = harness.action_reference.model_copy(
            update={"stable_name": "customer.other_action"}
        )
    elif mutation == "action_version":
        harness.action_reference = harness.action_reference.model_copy(
            update={"version": 4}
        )
    elif mutation == "action_digest":
        harness.action_reference = harness.action_reference.model_copy(
            update={"contract_digest": "f" * 64}
        )
    elif mutation == "claim_action":
        binding = harness.claim_request.binding.model_copy(
            update={
                "action_reference": harness.action_reference.model_copy(
                    update={"contract_digest": "f" * 64}
                )
            }
        )
        harness.claim_request = harness.claim_request.model_copy(
            update={"binding": binding}
        )
    elif mutation == "target_system":
        binding = harness.claim_request.binding.model_copy(
            update={"target_system": "erp"}
        )
        harness.claim_request = harness.claim_request.model_copy(
            update={"binding": binding}
        )
    else:
        harness.request = {"customer_id": "customer-43", "status": "active"}

    _assert_governance_error(harness, expected_code)
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []


def test_request_digest_native_failure_is_sanitized_before_claim() -> None:
    harness = _harness()
    harness.request = HostileRequestMapping()
    error = _assert_governance_error(
        harness,
        "action_governance_request_digest_mismatch",
    )
    assert str(error) == "Action request digest does not match"
    assert "REQUEST_SECRET_8" not in repr(error)
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []
    assert harness.clock.calls == 0


def test_request_digest_mismatch_precedes_claim_capability_binding_mismatch() -> None:
    harness = _harness()
    harness.request = {"customer_id": "customer-other", "status": "active"}
    changed_capability = harness.claim_request.binding.capability_binding.model_copy(
        update={"capability_name": "enterprise.crm.other"}
    )
    changed_binding = harness.claim_request.binding.model_copy(
        update={"capability_binding": changed_capability}
    )
    harness.claim_request = harness.claim_request.model_copy(
        update={"binding": changed_binding}
    )
    _assert_governance_error(
        harness,
        "action_governance_request_digest_mismatch",
    )
    assert harness.clock.calls == 0
    assert harness.approval_port.get_calls == []
    assert harness.claim_port.reserve_calls == []


@pytest.mark.parametrize(
    ("boundary", "expected_code", "clock_calls", "approval_calls"),
    (
        ("action", "action_governance_identity_mismatch", 0, 0),
        ("claim", "action_governance_identity_mismatch", 0, 0),
        ("policy", "action_governance_policy_invalid", 1, 0),
        ("approval_evidence", "action_governance_approval_invalid", 1, 0),
        ("approval_record", "action_governance_approval_invalid", 1, 1),
    ),
)
def test_external_frozen_contract_revalidation_native_failures_are_sanitized(
    boundary: str,
    expected_code: str,
    clock_calls: int,
    approval_calls: int,
) -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    hostile_time = datetime(2026, 7, 16, 9, 0, tzinfo=ExplodingTimezone())
    if boundary == "action":
        harness.action_definition = _unsafe_model_copy(
            harness.action_definition,
            created_at=hostile_time,
        )
    elif boundary == "claim":
        harness.claim_request = _unsafe_model_copy(
            harness.claim_request,
            requested_at=hostile_time,
        )
    elif boundary == "policy":
        harness.policy_evidence = _unsafe_model_copy(
            harness.policy_evidence,
            evaluated_at=hostile_time,
        )
    elif boundary == "approval_evidence":
        assert harness.approval_evidence is not None
        harness.approval_evidence = _unsafe_model_copy(
            harness.approval_evidence,
            issued_at=hostile_time,
        )
    else:
        assert harness.approval_port.record is not None
        harness.approval_port.record = _unsafe_model_copy(
            harness.approval_port.record,
            requested_at=hostile_time,
        )

    error = _assert_governance_error(harness, expected_code)
    rendered = "".join(traceback.format_exception(error))
    assert "MODEL_SECRET_8" not in str(error)
    assert "MODEL_SECRET_8" not in repr(error)
    assert "MODEL_SECRET_8" not in rendered
    assert "TZ_SECRET_8" not in rendered
    assert harness.claim_port.reserve_calls == []
    assert harness.clock.calls == clock_calls
    assert len(harness.approval_port.get_calls) == approval_calls


@pytest.mark.parametrize("boundary", ("action", "claim"))
def test_identity_revalidation_rejects_after_validator_wrong_time_types(
    boundary: str,
) -> None:
    harness = _harness()
    if boundary == "action":
        harness.action_definition = _unsafe_model_copy(
            harness.action_definition,
            created_at=MalformedAstimezoneDateTime(
                2026,
                7,
                16,
                9,
                0,
                tzinfo=UTC,
            ),
        )
    else:
        harness.claim_request = _unsafe_model_copy(
            harness.claim_request,
            requested_at=MalformedStartDateTime(
                2026,
                7,
                16,
                9,
                0,
                tzinfo=UTC,
            ),
            lease_expires_at=MalformedEndDateTime(
                2026,
                7,
                16,
                9,
                5,
                tzinfo=UTC,
            ),
        )
    _assert_governance_error(
        harness,
        "action_governance_identity_mismatch",
    )
    assert harness.clock.calls == 0
    assert harness.claim_port.reserve_calls == []


def test_claim_result_revalidation_native_failure_is_sanitized() -> None:
    harness = _harness()
    current = harness.claim_port.result
    assert isinstance(current, ActionClaimResult)
    assert current.claim is not None
    hostile_claim = _unsafe_model_copy(
        current.claim,
        lease_expires_at=datetime(
            2026,
            7,
            16,
            9,
            5,
            tzinfo=ExplodingTimezone(),
        ),
    )
    harness.claim_port.result = ActionClaimResult.model_construct(
        disposition=ActionClaimDisposition.CLAIMED,
        claim=hostile_claim,
        terminal_outcome=None,
    )
    error = _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    rendered = "".join(traceback.format_exception(error))
    assert "TZ_SECRET_8" not in rendered
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_replay_revalidation_rejects_wrong_finalized_time_type() -> None:
    harness = _harness()
    outcome = _unsafe_model_copy(
        _terminal_outcome(),
        finalized_at=MalformedAstimezoneDateTime(
            2026,
            7,
            16,
            8,
            59,
            tzinfo=UTC,
        ),
    )
    harness.claim_port.result = ActionClaimResult.model_construct(
        disposition=ActionClaimDisposition.REPLAY,
        claim=None,
        terminal_outcome=outcome,
    )
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_replay_revalidation_rejects_noncanonical_zero_offset_time() -> None:
    harness = _harness()
    outcome = _unsafe_model_copy(
        _terminal_outcome(),
        finalized_at=NonCanonicalAstimezoneDateTime(
            2026,
            7,
            16,
            8,
            59,
            tzinfo=UTC,
        ),
    )
    harness.claim_port.result = ActionClaimResult.model_construct(
        disposition=ActionClaimDisposition.REPLAY,
        claim=None,
        terminal_outcome=outcome,
    )

    error = _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    rendered = "".join(traceback.format_exception(error))
    assert "NONCANON_SECRET_8" not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_policy_revalidation_rejects_after_validator_wrong_time_types() -> None:
    harness = _harness()
    malformed_evidence = tuple(
        _unsafe_model_copy(
            item,
            issued_at=MalformedStartDateTime(
                2026,
                7,
                16,
                9,
                0,
                tzinfo=UTC,
            ),
            expires_at=MalformedEndDateTime(
                2026,
                7,
                16,
                9,
                5,
                tzinfo=UTC,
            ),
        )
        for item in harness.policy_evidence.evidence
    )
    harness.policy_evidence = _unsafe_model_copy(
        harness.policy_evidence,
        evidence=malformed_evidence,
        evaluated_at=MalformedStartDateTime(
            2026,
            7,
            16,
            9,
            0,
            tzinfo=UTC,
        ),
    )

    error = _assert_governance_error(
        harness,
        "action_governance_policy_invalid",
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    assert harness.claim_port.reserve_calls == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("name", "capability_name_mismatch"),
        ("version", "capability_version_mismatch"),
        ("hash", "capability_schema_hash_mismatch"),
        ("read_only", "action_capability_read_only"),
        ("non_idempotent", "action_capability_not_idempotent"),
        ("risk", "capability_risk_downgrade"),
        ("claim_binding", "capability_name_mismatch"),
    ),
)
def test_capability_contract_mismatches_precede_scopes_and_claim(
    mutation: str,
    expected_code: str,
) -> None:
    harness = _harness()
    if mutation == "name":
        harness.capability_snapshot = harness.capability_snapshot.model_copy(
            update={"capability_name": "enterprise.crm.other"}
        )
    elif mutation == "version":
        harness.capability_snapshot = harness.capability_snapshot.model_copy(
            update={"capability_version": "3.0.0"}
        )
    elif mutation == "hash":
        harness.capability_snapshot = harness.capability_snapshot.model_copy(
            update={"schema_hash": "c" * 64}
        )
    elif mutation == "read_only":
        harness.capability_snapshot = harness.capability_snapshot.model_copy(
            update={"has_side_effects": False}
        )
    elif mutation == "non_idempotent":
        harness.capability_snapshot = harness.capability_snapshot.model_copy(
            update={"idempotent": False}
        )
    elif mutation == "risk":
        harness.capability_snapshot = harness.capability_snapshot.model_copy(
            update={"risk_level": ActionRiskLevel.HIGH}
        )
    else:
        changed = harness.claim_request.binding.capability_binding.model_copy(
            update={"capability_name": "enterprise.crm.other"}
        )
        binding = harness.claim_request.binding.model_copy(
            update={"capability_binding": changed}
        )
        harness.claim_request = harness.claim_request.model_copy(
            update={"binding": binding}
        )

    harness.granted_scopes = frozenset()
    _assert_governance_error(harness, expected_code)
    assert harness.claim_port.reserve_calls == []


def test_gate_order_is_identity_then_capability_scope_policy_approval_claim() -> None:
    identity = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
    )
    identity.tenant_id = "tenant-b"
    identity.capability_snapshot = identity.capability_snapshot.model_copy(
        update={"capability_name": "enterprise.crm.other"}
    )
    identity.granted_scopes = frozenset()
    _assert_governance_error(identity, "action_governance_identity_mismatch")

    capability = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
    )
    capability.capability_snapshot = capability.capability_snapshot.model_copy(
        update={"capability_name": "enterprise.crm.other"}
    )
    capability.granted_scopes = frozenset()
    _assert_governance_error(capability, "capability_name_mismatch")

    scope = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
    )
    scope.granted_scopes = frozenset()
    _assert_governance_error(scope, "action_governance_scope_missing")

    policy = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
    )
    unexpected = ("policy.unexpected",)
    policy.policy_evidence = _policy_set(
        policy.action_reference,
        required_policy_references=unexpected,
    )
    _assert_governance_error(policy, "action_governance_policy_invalid")

    approval = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
    )
    _assert_governance_error(approval, "action_governance_approval_required")

    for harness in (identity, capability, scope, policy, approval):
        assert harness.claim_port.reserve_calls == []


def test_action_governance_preserves_existing_workflow_capability_contract() -> None:
    harness = _harness()
    harness.capability_snapshot = harness.capability_snapshot.model_copy(
        update={"kind": CapabilityContractKind.WORKFLOW}
    )
    assert isinstance(harness.govern(), ActionExecutionPermit)


def _policy_set_with_changed_identity(
    harness: GovernanceHarness,
    mutation: str,
) -> PolicyEvidenceSet:
    action_reference = harness.action_reference
    tenant_id = _TENANT
    invocation_id = _INVOCATION
    if mutation == "tenant":
        tenant_id = "tenant-b"
        action_reference = action_reference.model_copy(update={"tenant_id": tenant_id})
    elif mutation == "invocation":
        invocation_id = "invocation-other"
    elif mutation == "action":
        action_reference = action_reference.model_copy(
            update={"contract_digest": "f" * 64}
        )
    return _policy_set(
        action_reference,
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        evidence=tuple(
            _policy_evidence(
                action_reference,
                policy,
                tenant_id=tenant_id,
                invocation_id=invocation_id,
                ordinal=index,
            )
            for index, policy in enumerate(_POLICIES, start=1)
        ),
    )


@pytest.mark.parametrize(
    "variant",
    (
        "self_declared_missing",
        "self_declared_unexpected",
        "member_missing",
        "member_unexpected",
        "duplicate",
        "denied",
        "expired",
        "future",
        "tenant",
        "invocation",
        "action",
    ),
)
def test_policy_evidence_exact_set_and_current_binding_fail_before_claim(
    variant: str,
) -> None:
    harness = _harness()
    if variant == "self_declared_missing":
        declared = (_POLICIES[0],)
        harness.policy_evidence = _policy_set(
            harness.action_reference,
            required_policy_references=declared,
        )
    elif variant == "self_declared_unexpected":
        declared = (_POLICIES[0], "policy.unexpected")
        harness.policy_evidence = _policy_set(
            harness.action_reference,
            required_policy_references=declared,
        )
    elif variant == "member_missing":
        first = _policy_evidence(
            harness.action_reference,
            _POLICIES[0],
            ordinal=1,
        )
        harness.policy_evidence = PolicyEvidenceSet.model_construct(
            tenant_id=_TENANT,
            invocation_id=_INVOCATION,
            action_reference=harness.action_reference,
            required_policy_references=_POLICIES,
            evidence=(first,),
            evaluated_at=_NOW,
        )
    elif variant == "member_unexpected":
        evidence = (
            _policy_evidence(
                harness.action_reference,
                _POLICIES[0],
                ordinal=1,
            ),
            _policy_evidence(
                harness.action_reference,
                "policy.unexpected",
                ordinal=2,
            ),
        )
        harness.policy_evidence = PolicyEvidenceSet.model_construct(
            tenant_id=_TENANT,
            invocation_id=_INVOCATION,
            action_reference=harness.action_reference,
            required_policy_references=_POLICIES,
            evidence=evidence,
            evaluated_at=_NOW,
        )
    elif variant == "duplicate":
        duplicate = _policy_evidence(
            harness.action_reference,
            _POLICIES[0],
            ordinal=1,
        )
        harness.policy_evidence = PolicyEvidenceSet.model_construct(
            tenant_id=_TENANT,
            invocation_id=_INVOCATION,
            action_reference=harness.action_reference,
            required_policy_references=_POLICIES,
            evidence=(duplicate, duplicate),
            evaluated_at=_NOW,
        )
    elif variant == "denied":
        evidence = tuple(
            _policy_evidence(
                harness.action_reference,
                policy,
                allowed=index != 2,
                ordinal=index,
            )
            for index, policy in enumerate(_POLICIES, start=1)
        )
        harness.policy_evidence = PolicyEvidenceSet.model_construct(
            tenant_id=_TENANT,
            invocation_id=_INVOCATION,
            action_reference=harness.action_reference,
            required_policy_references=_POLICIES,
            evidence=evidence,
            evaluated_at=_NOW,
        )
    elif variant == "expired":
        evidence = tuple(
            _policy_evidence(
                harness.action_reference,
                policy,
                issued_at=_NOW - timedelta(minutes=10),
                expires_at=_NOW - timedelta(minutes=1),
                ordinal=index,
            )
            for index, policy in enumerate(_POLICIES, start=1)
        )
        harness.policy_evidence = _policy_set(
            harness.action_reference,
            evidence=evidence,
            evaluated_at=_NOW - timedelta(minutes=5),
        )
    elif variant == "future":
        evidence = tuple(
            _policy_evidence(
                harness.action_reference,
                policy,
                issued_at=_NOW + timedelta(minutes=1),
                expires_at=_NOW + timedelta(minutes=10),
                ordinal=index,
            )
            for index, policy in enumerate(_POLICIES, start=1)
        )
        harness.policy_evidence = _policy_set(
            harness.action_reference,
            evidence=evidence,
            evaluated_at=_NOW + timedelta(minutes=2),
        )
    else:
        harness.policy_evidence = _policy_set_with_changed_identity(
            harness,
            variant,
        )

    _assert_governance_error(harness, "action_governance_policy_invalid")
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []


def test_policy_exact_bindings_are_copied_to_claimed_permit() -> None:
    harness = _harness()
    result = harness.govern()
    assert isinstance(result, ActionExecutionPermit)
    assert result.policy_bindings == harness.policy_evidence.bindings()
    assert tuple(
        (
            item.policy_reference,
            item.evidence_id,
            item.decision_id,
            item.decision_revision,
        )
        for item in result.policy_bindings
    ) == tuple(
        (
            item.policy_reference,
            item.evidence_id,
            item.decision_id,
            item.decision_revision,
        )
        for item in harness.policy_evidence.evidence
    )


@pytest.mark.parametrize(
    ("risk_level", "approval_mode"),
    tuple(
        (risk_level, approval_mode)
        for risk_level, approval_mode, approval_required in _RISK_APPROVAL_TABLE
        if approval_required
    ),
)
def test_every_required_r1_row_rejects_missing_approval_before_claim(
    risk_level: ActionRiskLevel,
    approval_mode: ActionApprovalMode,
) -> None:
    harness = _harness(
        risk_level=risk_level,
        approval_mode=approval_mode,
        include_approval=False,
    )
    _assert_governance_error(harness, "action_governance_approval_required")
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []


def _replace_approval_identity(
    harness: GovernanceHarness,
    variant: str,
) -> ApprovalEvidence:
    current = _approval_evidence(harness.action_reference)
    if variant == "tenant":
        action_reference = harness.action_reference.model_copy(
            update={"tenant_id": "tenant-b"}
        )
        return _approval_evidence(
            action_reference,
            tenant_id="tenant-b",
        )
    if variant == "invocation":
        return _approval_evidence(
            harness.action_reference,
            invocation_id="invocation-other",
        )
    if variant == "action":
        action_reference = harness.action_reference.model_copy(
            update={"contract_digest": "f" * 64}
        )
        return _approval_evidence(action_reference)
    if variant == "expired":
        return _approval_evidence(
            harness.action_reference,
            decided_at=_NOW - timedelta(minutes=20),
            issued_at=_NOW - timedelta(minutes=10),
            expires_at=_NOW - timedelta(minutes=1),
        )
    if variant == "future":
        return _approval_evidence(
            harness.action_reference,
            decided_at=_NOW - timedelta(minutes=1),
            issued_at=_NOW + timedelta(minutes=1),
            expires_at=_NOW + timedelta(minutes=10),
        )
    if variant == "revision":
        return current.model_copy(update={"approved_revision": 1})
    if variant == "approval_id":
        return current.model_copy(update={"approval_id": "approval-other"})
    raise AssertionError(f"unknown Approval variant: {variant}")


@pytest.mark.parametrize(
    "variant",
    (
        "expired",
        "future",
        "tenant",
        "invocation",
        "action",
        "missing_record",
        "waiting_record",
        "rejected_record",
        "revision",
        "approval_id",
        "decision_id",
        "decision_time",
    ),
)
@pytest.mark.parametrize("optional_low_none", (False, True))
def test_any_supplied_approval_is_current_exact_bound_before_claim(
    variant: str,
    optional_low_none: bool,
) -> None:
    risk_level = ActionRiskLevel.LOW if optional_low_none else ActionRiskLevel.HIGH
    approval_mode = (
        ActionApprovalMode.NONE if optional_low_none else ActionApprovalMode.REQUIRED
    )
    harness = _harness(
        risk_level=risk_level,
        approval_mode=approval_mode,
        include_approval=True,
    )
    assert harness.approval_evidence is not None
    original = harness.approval_evidence

    if variant in {
        "expired",
        "future",
        "tenant",
        "invocation",
        "action",
        "revision",
        "approval_id",
    }:
        harness.approval_evidence = _replace_approval_identity(harness, variant)
    elif variant == "missing_record":
        harness.approval_port.record = None
    elif variant == "waiting_record":
        harness.approval_port.record = _approval_record(
            original,
            status=ApprovalStatus.WAITING_APPROVAL,
        )
    elif variant == "rejected_record":
        harness.approval_port.record = _approval_record(
            original,
            status=ApprovalStatus.REJECTED,
        )
    elif variant == "decision_id":
        harness.approval_port.record = _approval_record(
            original,
            decision_id="approval-decision-other",
        )
    else:
        harness.approval_port.record = _approval_record(
            original,
            decided_at=original.decided_at + timedelta(seconds=1),
        )

    _assert_governance_error(harness, "action_governance_approval_invalid")
    assert harness.claim_port.reserve_calls == []


@pytest.mark.parametrize("changed_field", ("requester_id", "risk_level"))
def test_approval_snapshot_change_between_resolver_and_governor_fails_before_claim(
    changed_field: str,
) -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=False,
    )
    seed = _approval_evidence(harness.action_reference)
    subject = _approval_subject(seed).model_copy(update={"risk_level": "high"})
    initial = _approval_record(seed, subject=subject)
    resolver_store = ApprovalSpy(initial)
    resolver = ApprovalEvidenceResolver(resolver_store, clock=lambda: _NOW, authority_verifier=InMemoryApprovalHumanAuthorityVerifier())
    context = TenantContext(
        tenant_id=_TENANT,
        principal_id="requester-42",
        api_key_id="key-42",
        scopes=frozenset(),
        request_id="request-42",
        trace_id="trace-42",
        correlation_id="correlation-42",
    )
    harness.approval_evidence = resolver.resolve(
        context=context,
        action=harness.action_definition,
        invocation_id=harness.invocation_id,
        request_digest=canonical_request_digest(_REQUEST),
        approval_id=initial.approval_id,
    )
    changed_value = "requester-other" if changed_field == "requester_id" else "medium"
    mutated = initial.model_copy(
        update={"subject": subject.model_copy(update={changed_field: changed_value})}
    )
    assert mutated.revision == initial.revision
    assert mutated.decision == initial.decision
    harness.approval_port.record = mutated

    _assert_governance_error(harness, "action_governance_approval_invalid")

    assert harness.claim_port.reserve_calls == []


def test_optional_low_none_valid_approval_is_validated_and_frozen_in_permit() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.LOW,
        approval_mode=ActionApprovalMode.NONE,
        include_approval=True,
    )
    result = harness.govern()
    assert isinstance(result, ActionExecutionPermit)
    assert harness.approval_evidence is not None
    assert result.approval_binding == harness.approval_evidence.binding(
        evaluated_at=_NOW
    )
    assert harness.approval_port.get_calls == [
        (
            harness.approval_evidence.tenant_id,
            harness.approval_evidence.approval_id,
        )
    ]


def _terminal_outcome() -> TerminalOutcomeReference:
    return TerminalOutcomeReference(
        outcome_id="outcome-42",
        outcome_revision=3,
        status=TerminalOutcomeStatus.SUCCEEDED,
        outcome_digest="d" * 64,
        finalized_at=_NOW - timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    ("disposition", "expected_code"),
    (
        (ActionClaimDisposition.IN_PROGRESS, "action_governance_in_progress"),
        (ActionClaimDisposition.CONFLICT, "action_governance_conflict"),
    ),
)
def test_non_owner_claim_dispositions_never_produce_permit(
    disposition: ActionClaimDisposition,
    expected_code: str,
) -> None:
    harness = _harness()
    harness.claim_port.result = ActionClaimResult(disposition=disposition)
    _assert_governance_error(harness, expected_code)
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_replay_returns_exact_terminal_outcome_without_permit() -> None:
    harness = _harness()
    outcome = _terminal_outcome()
    harness.claim_port.result = ActionClaimResult(
        disposition=ActionClaimDisposition.REPLAY,
        terminal_outcome=outcome,
    )
    result = harness.govern()
    assert result == outcome
    assert not isinstance(result, ActionExecutionPermit)
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_claimed_is_the_only_path_that_builds_exact_permit() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    result = harness.govern()
    assert isinstance(result, ActionExecutionPermit)
    expected_claim_result = harness.claim_port.result
    assert isinstance(expected_claim_result, ActionClaimResult)
    assert result.claim == expected_claim_result.claim
    assert result.issued_at == _NOW
    assert result.expires_at == harness.claim_request.lease_expires_at
    assert result.policy_bindings == harness.policy_evidence.bindings()
    assert harness.approval_evidence is not None
    assert result.approval_binding == harness.approval_evidence.binding(
        evaluated_at=_NOW
    )
    assert harness.claim_port.reserve_calls == [harness.claim_request]
    received_command = harness.claim_port.reserve_calls[0]
    assert result.claim.key is not received_command.key
    assert result.claim.binding is not received_command.binding
    assert (
        result.claim.binding.action_reference
        is not received_command.binding.action_reference
    )
    assert (
        result.claim.binding.capability_binding
        is not received_command.binding.capability_binding
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "key_container",
        "binding_container",
        "key_tenant",
        "key_stable_name",
        "key_idempotency",
        "binding_invocation",
        "action_version",
        "action_digest",
        "request_digest",
        "capability_name",
        "capability_version",
        "capability_schema_hash",
        "adapter_id",
        "target_system",
        "requested_at",
        "lease_expires_at",
    ),
)
def test_claim_port_cannot_mutate_pre_reserve_authorization_snapshot(
    mutation: str,
) -> None:
    harness = _harness()
    if mutation == "requested_at":
        harness.claim_request = harness.claim_request.model_copy(
            update={
                "requested_at": _NOW + timedelta(minutes=1),
                "lease_expires_at": _NOW + timedelta(minutes=6),
            }
        )
    original_command = harness.claim_request
    original_snapshot = _claim_command_native_snapshot(original_command)
    malicious_port = MutatingClaimPort(mutation)
    harness.claim_port = malicious_port
    harness.governor = ActionGovernor(
        claim_port=malicious_port,
        approval_port=harness.approval_port,
        clock=harness.clock,
    )

    returned: ActionExecutionPermit | TerminalOutcomeReference | None = None
    error: ActionGovernanceError | None = None
    try:
        returned = harness.govern()
    except ActionGovernanceError as caught:
        error = caught

    assert returned is None
    assert error is not None
    assert error.code == "action_governance_claim_result_invalid"
    assert str(error) == "Action claim result is invalid"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert malicious_port.received_command is not original_command
    assert _claim_command_native_snapshot(original_command) == original_snapshot
    assert len(malicious_port.reserve_calls) == 1


def test_claim_command_snapshot_surface_is_exact() -> None:
    assert set(ActionClaimRequest.model_fields) == {
        "key",
        "binding",
        "requested_at",
        "lease_expires_at",
    }
    assert set(ActionReservationKey.model_fields) == {
        "tenant_id",
        "action_stable_name",
        "idempotency_key",
    }
    assert set(ClaimBindingPayload.model_fields) == {
        "invocation_id",
        "action_reference",
        "request_digest",
        "capability_binding",
        "adapter_id",
        "target_system",
    }
    assert set(DefinitionReference.model_fields) == {
        "tenant_id",
        "definition_type",
        "stable_name",
        "version",
        "contract_digest",
    }
    assert set(CapabilityBinding.model_fields) == {
        "capability_name",
        "capability_version",
        "schema_hash",
    }


@pytest.mark.parametrize(
    "disposition",
    (ActionClaimDisposition.REPLAY, ActionClaimDisposition.CONFLICT),
)
def test_mutating_claim_port_preserves_replay_and_conflict_precedence(
    disposition: ActionClaimDisposition,
) -> None:
    harness = _harness()
    command = harness.claim_request.model_copy(
        update={
            "requested_at": _NOW + timedelta(minutes=1),
            "lease_expires_at": _NOW + timedelta(minutes=6),
        }
    )
    harness.claim_request = command
    malicious_port = MutatingClaimPort(
        "requested_at",
        disposition=disposition,
    )
    harness.claim_port = malicious_port
    harness.governor = ActionGovernor(
        claim_port=malicious_port,
        approval_port=harness.approval_port,
        clock=harness.clock,
    )

    if disposition is ActionClaimDisposition.REPLAY:
        assert harness.govern() == _terminal_outcome()
    else:
        _assert_governance_error(harness, "action_governance_conflict")
    assert len(malicious_port.reserve_calls) == 1


@pytest.mark.parametrize(
    "malformed_result",
    (
        None,
        object(),
        ActionClaimResult.model_construct(
            disposition=ActionClaimDisposition.CLAIMED,
            claim=None,
            terminal_outcome=None,
        ),
    ),
)
def test_malformed_atomic_claim_result_fails_closed(
    malformed_result: object,
) -> None:
    harness = _harness()
    harness.claim_port.result = malformed_result
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_claimed_result_must_match_exact_request_identity_and_binding() -> None:
    harness = _harness()
    assert isinstance(harness.claim_port.result, ActionClaimResult)
    assert harness.claim_port.result.claim is not None
    wrong_binding = harness.claim_port.result.claim.binding.model_copy(
        update={"request_digest": "e" * 64}
    )
    wrong_claim = harness.claim_port.result.claim.model_copy(
        update={"binding": wrong_binding}
    )
    harness.claim_port.result = ActionClaimResult(
        disposition=ActionClaimDisposition.CLAIMED,
        claim=wrong_claim,
    )
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_claimed_result_must_match_exact_reservation_key() -> None:
    harness = _harness()
    current = harness.claim_port.result
    assert isinstance(current, ActionClaimResult)
    assert current.claim is not None
    wrong_key = current.claim.key.model_copy(
        update={"idempotency_key": "request-other"}
    )
    wrong_claim = current.claim.model_copy(update={"key": wrong_key})
    harness.claim_port.result = ActionClaimResult(
        disposition=ActionClaimDisposition.CLAIMED,
        claim=wrong_claim,
    )
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_claimed_result_with_expired_lease_fails_closed_after_reserve() -> None:
    harness = _harness()
    command = harness.claim_request.model_copy(
        update={
            "requested_at": _NOW - timedelta(minutes=1),
            "lease_expires_at": _NOW,
        }
    )
    harness.claim_request = command
    harness.claim_port.result = _claimed_result(command)
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [command]


def test_claimed_result_for_future_request_fails_closed_after_reserve() -> None:
    harness = _harness()
    command = harness.claim_request.model_copy(
        update={"requested_at": _NOW + timedelta(minutes=1)}
    )
    harness.claim_request = command
    harness.claim_port.result = _claimed_result(command)
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [command]


@pytest.mark.parametrize(
    "time_update",
    (
        {
            "requested_at": _NOW - timedelta(minutes=2),
            "lease_expires_at": _NOW - timedelta(minutes=1),
        },
        {
            "requested_at": _NOW + timedelta(minutes=1),
            "lease_expires_at": _NOW + timedelta(minutes=5),
        },
    ),
)
@pytest.mark.parametrize(
    "disposition",
    (ActionClaimDisposition.REPLAY, ActionClaimDisposition.CONFLICT),
)
def test_replay_and_conflict_precede_caller_claim_time_classification(
    time_update: dict[str, datetime],
    disposition: ActionClaimDisposition,
) -> None:
    harness = _harness()
    command = harness.claim_request.model_copy(update=time_update)
    harness.claim_request = command
    outcome = _terminal_outcome()
    harness.claim_port.result = ActionClaimResult(
        disposition=disposition,
        terminal_outcome=(
            outcome if disposition is ActionClaimDisposition.REPLAY else None
        ),
    )
    if disposition is ActionClaimDisposition.REPLAY:
        assert harness.govern() == outcome
    else:
        _assert_governance_error(harness, "action_governance_conflict")
    assert harness.claim_port.reserve_calls == [command]


def test_claimed_result_cannot_extend_the_requested_lease() -> None:
    harness = _harness()
    harness.claim_port.result = ActionClaimResult(
        disposition=ActionClaimDisposition.CLAIMED,
        claim=ActionClaim(
            key=harness.claim_request.key,
            binding=harness.claim_request.binding,
            state=ActionClaimState.ACTIVE,
            claim_revision=1,
            fencing_token="fence-extended",
            lease_expires_at=_NOW + timedelta(days=1),
        ),
    )
    _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    assert harness.claim_port.reserve_calls == [harness.claim_request]


@pytest.mark.parametrize(
    "known_code",
    (
        "action_claim_binding_conflict",
        "action_claim_clock_invalid",
        "action_claim_clock_regressed",
        "action_claim_command_invalid",
        "action_claim_lease_expired",
        "action_claim_not_found",
        "action_claim_outcome_conflict",
        "action_claim_revision_conflict",
        "action_claim_stale_fence",
        "action_claim_time_invalid",
        "action_claim_transition_invalid",
    ),
)
def test_known_claim_port_error_code_is_preserved_without_native_context(
    known_code: str,
) -> None:
    harness = _harness()
    harness.claim_port.failure = ActionClaimError(
        known_code,
        "CLAIM_SECRET_8",
    )
    error = _assert_governance_error(harness, known_code)
    assert str(error) == "Action claim operation failed"
    assert "CLAIM_SECRET_8" not in repr(error)
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_unknown_claim_port_error_code_is_sanitized() -> None:
    harness = _harness()
    harness.claim_port.failure = ActionClaimError(
        "PORT_SENTINEL_SECRET_8",
        "CLAIM_MESSAGE_SECRET_8",
    )

    error = _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    rendered = "".join(traceback.format_exception(error))
    exposed = (
        error.code,
        str(error),
        repr(error),
        repr(error.args),
        rendered,
    )
    assert all("PORT_SENTINEL_SECRET_8" not in value for value in exposed)
    assert all("CLAIM_MESSAGE_SECRET_8" not in value for value in exposed)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert harness.claim_port.reserve_calls == [harness.claim_request]


def test_native_claim_port_failure_is_sanitized() -> None:
    harness = _harness()
    harness.claim_port.failure = RuntimeError("CLAIM_NATIVE_SECRET_8")
    error = _assert_governance_error(
        harness,
        "action_governance_claim_result_invalid",
    )
    rendered = "".join(traceback.format_exception(error))
    assert str(error) == "Action claim result is invalid"
    assert "CLAIM_NATIVE_SECRET_8" not in rendered
    assert harness.claim_port.reserve_calls == [harness.claim_request]


@pytest.mark.parametrize(
    "clock_value",
    (
        None,
        datetime(2026, 7, 16, 9, 0),
    ),
)
def test_invalid_trusted_clock_fails_before_approval_or_claim(
    clock_value: datetime | None,
) -> None:
    harness = _harness(include_approval=True)
    harness.clock.current = clock_value  # type: ignore[assignment]
    _assert_governance_error(harness, "action_governance_clock_invalid")
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []


def test_trusted_clock_exception_is_sanitized_before_claim() -> None:
    harness = _harness(include_approval=True)
    harness.clock.failure = RuntimeError("CLOCK_SECRET_8")
    error = _assert_governance_error(
        harness,
        "action_governance_clock_invalid",
    )
    assert "CLOCK_SECRET_8" not in str(error)
    assert "CLOCK_SECRET_8" not in repr(error)
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []


@pytest.mark.parametrize(
    ("clock_value", "sentinel"),
    (
        (
            datetime(2026, 7, 16, 9, 0, tzinfo=ExplodingTimezone()),
            "TZ_SECRET_8",
        ),
        (
            ExplodingAstimezoneDateTime(2026, 7, 16, 9, 0, tzinfo=UTC),
            "ASTIMEZONE_SECRET_8",
        ),
    ),
)
def test_trusted_clock_timezone_failures_are_sanitized_before_claim(
    clock_value: datetime,
    sentinel: str,
) -> None:
    harness = _harness(include_approval=True)
    harness.clock.current = clock_value
    error = _assert_governance_error(
        harness,
        "action_governance_clock_invalid",
    )
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert harness.claim_port.reserve_calls == []


def test_trusted_clock_malformed_normalization_does_not_poison_watermark() -> None:
    harness = _harness()
    harness.clock.current = MalformedAstimezoneDateTime(
        2026,
        7,
        16,
        9,
        0,
        tzinfo=UTC,
    )
    error = _assert_governance_error(
        harness,
        "action_governance_clock_invalid",
    )
    rendered = "".join(traceback.format_exception(error))
    assert "CLOCK_RETURN_SECRET_8" not in rendered
    assert vars(harness.governor)["_last_trusted_at"] is None
    assert harness.claim_port.reserve_calls == []
    assert harness.approval_port.get_calls == []

    harness.clock.current = _NOW
    assert isinstance(harness.govern(), ActionExecutionPermit)
    assert vars(harness.governor)["_last_trusted_at"] == _NOW


def test_trusted_clock_rejects_noncanonical_zero_offset_normalization() -> None:
    harness = _harness()
    harness.clock.current = NonCanonicalAstimezoneDateTime(
        2026,
        7,
        16,
        9,
        0,
        tzinfo=UTC,
    )

    error = _assert_governance_error(
        harness,
        "action_governance_clock_invalid",
    )
    rendered = "".join(traceback.format_exception(error))
    assert "NONCANON_SECRET_8" not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None
    assert vars(harness.governor)["_last_trusted_at"] is None
    assert harness.claim_port.reserve_calls == []


@pytest.mark.parametrize("expired_gate", ("policy", "approval"))
def test_trusted_clock_regression_cannot_resurrect_expired_evidence(
    expired_gate: str,
) -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    if expired_gate == "approval":
        evidence = tuple(
            _policy_evidence(
                harness.action_reference,
                policy,
                expires_at=_NOW + timedelta(minutes=30),
                ordinal=index,
            )
            for index, policy in enumerate(_POLICIES, start=1)
        )
        harness.policy_evidence = _policy_set(
            harness.action_reference,
            evidence=evidence,
        )
    harness.clock.current = _NOW + timedelta(minutes=11)
    expected_first = (
        "action_governance_policy_invalid"
        if expired_gate == "policy"
        else "action_governance_approval_invalid"
    )
    _assert_governance_error(harness, expected_first)
    assert harness.claim_port.reserve_calls == []

    harness.clock.current = _NOW
    _assert_governance_error(harness, "action_governance_clock_regressed")
    assert harness.claim_port.reserve_calls == []


def test_approval_lookup_exception_is_sanitized_before_claim() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    harness.approval_port.failure = RuntimeError("APPROVAL_SECRET_8")
    error = _assert_governance_error(
        harness,
        "action_governance_approval_invalid",
    )
    assert str(error) == "Action Approval evidence is invalid"
    assert "APPROVAL_SECRET_8" not in repr(error)
    assert harness.claim_port.reserve_calls == []
    assert len(harness.approval_port.get_calls) == 1


def test_current_approval_record_tenant_mismatch_fails_before_claim() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.HIGH,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    assert harness.approval_evidence is not None
    decision = ApprovalDecision(
        tenant_id="tenant-b",
        approval_id=harness.approval_evidence.approval_id,
        decision_id=harness.approval_evidence.decision_id,
        outcome=ApprovalStatus.APPROVED,
        actor="approver-42",
        reason_code="approval.reviewed",
        decided_at=harness.approval_evidence.decided_at,
    )
    harness.approval_port.record = ApprovalRecord(
        tenant_id="tenant-b",
        approval_id=harness.approval_evidence.approval_id,
        status=ApprovalStatus.APPROVED,
        revision=harness.approval_evidence.approved_revision,
        requested_at=harness.approval_evidence.decided_at - timedelta(minutes=2),
        decision=decision,
    )
    _assert_governance_error(
        harness,
        "action_governance_approval_invalid",
    )
    assert harness.claim_port.reserve_calls == []
    assert len(harness.approval_port.get_calls) == 1


_FORBIDDEN_GOVERNANCE_IMPORTS = (
    "eios.api",
    "eios.composition",
    "eios.control",
    "eios.runtime",
    "eios.migrations",
    "eios.apps",
    "eios.capabilities.models",
    "eios.adapters",
    "eios.sdk",
    "eios.tos",
    "psycopg",
    "fastapi",
    "starlette",
)


def _resolved_imports(
    source: str,
    *,
    package: str = "eios.actions",
) -> set[str]:
    modules: set[str] = set()
    package_parts = package.split(".")
    source_root = Path(__file__).parents[2] / "packages/eios-core/src"
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if any(alias.name == "*" for alias in node.names):
            raise ValueError("wildcard imports are not allowed")
        if node.level > 0:
            keep = len(package_parts) - node.level + 1
            if keep < 1:
                raise ValueError("relative import escapes the eios package")
            base = package_parts[:keep]
            if node.module is not None:
                base.extend(node.module.split("."))
        elif node.module is not None:
            base = node.module.split(".")
        else:
            continue
        resolved_base = ".".join(base)
        if source_root.joinpath(*base).is_dir():
            modules.update(f"{resolved_base}.{alias.name}" for alias in node.names)
        else:
            modules.add(resolved_base)
    return modules


def test_governance_import_guard_resolves_package_aliases_and_relative_imports() -> (
    None
):
    forbidden_cases = (
        ("from eios import runtime", {"eios.runtime"}),
        (
            "from eios import api, control",
            {"eios.api", "eios.control"},
        ),
        (
            "from .. import runtime, migrations, apps",
            {"eios.runtime", "eios.migrations", "eios.apps"},
        ),
        ("from ..runtime import engine", {"eios.runtime.engine"}),
        (
            "from eios.capabilities import models",
            {"eios.capabilities.models"},
        ),
    )
    for source, expected in forbidden_cases:
        imported = _resolved_imports(source)
        assert imported == expected
        assert all(
            any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in _FORBIDDEN_GOVERNANCE_IMPORTS
            )
            for module in imported
        )

    allowed_cases = (
        ("from eios.actions import models", {"eios.actions.models"}),
        ("from . import models", {"eios.actions.models"}),
        (
            "from ..approvals.models import ApprovalRecord",
            {"eios.approvals.models"},
        ),
    )
    for source, expected in allowed_cases:
        imported = _resolved_imports(source)
        assert imported == expected
        assert not any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in imported
            for prefix in _FORBIDDEN_GOVERNANCE_IMPORTS
        )


def test_task8_has_no_quota_rate_budget_or_forbidden_domain_binding() -> None:
    path = Path(__file__).parents[2] / "packages/eios-core/src/eios/actions/governance.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    identifiers.update(
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    )
    identifiers.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    assert identifiers.isdisjoint({"quota", "rate", "rate_limit", "budget"})

    imported = _resolved_imports(source)
    assert not any(
        module == root or module.startswith(f"{root}.")
        for module in imported
        for root in _FORBIDDEN_GOVERNANCE_IMPORTS
    )


def test_action_governor_public_surface_has_no_adapter_or_claim_read_path() -> None:
    public = {name for name in vars(ActionGovernor) if not name.startswith("_")}
    assert public == {"govern"}
    assert not any(
        marker in name
        for name in public
        for marker in ("adapter", "get", "read", "update", "finalize")
    )


# ---------------------------------------------------------------------------
# Stage 4: the Approval subject envelope hard-binds evidence to one request.
# ---------------------------------------------------------------------------


def _subject_harness(record: ApprovalRecord) -> GovernanceHarness:
    harness = _harness(
        risk_level=ActionRiskLevel.MEDIUM,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    harness.approval_port.record = record
    return harness


def test_subject_less_approval_record_is_refused_as_evidence() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.MEDIUM,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    assert harness.approval_evidence is not None
    harness.approval_port.record = _approval_record(
        harness.approval_evidence, subject=None
    )
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_governance_approval_invalid"


def test_subject_request_digest_mismatch_is_refused() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.MEDIUM,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    assert harness.approval_evidence is not None
    harness.approval_port.record = _approval_record(
        harness.approval_evidence,
        subject=_approval_subject(harness.approval_evidence, request_digest="f" * 64),
    )
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_governance_approval_invalid"


def test_subject_for_a_different_action_is_refused() -> None:
    harness = _harness(
        risk_level=ActionRiskLevel.MEDIUM,
        approval_mode=ActionApprovalMode.REQUIRED,
        include_approval=True,
    )
    assert harness.approval_evidence is not None
    foreign_reference = DefinitionReference(
        tenant_id=_TENANT,
        definition_type=DefinitionType.ACTION,
        stable_name="customer.other_action",
        version=1,
        contract_digest="e" * 64,
    )
    foreign_subject = ApprovalSubject(
        action_reference=foreign_reference,
        request_digest=canonical_request_digest(_REQUEST),
        requester_id="requester-42",
        risk_level="medium",
    )
    harness.approval_port.record = _approval_record(
        harness.approval_evidence, subject=foreign_subject
    )
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_governance_approval_invalid"


# ---------------------------------------------------------------------------
# Stage 5: submission criteria and the object edit fence run before the claim.
# ---------------------------------------------------------------------------


def _tier_precondition() -> ActionPrecondition:
    return ActionPrecondition(
        name="customer_is_gold",
        expression={"property": "tier", "operator": "eq", "value": "gold"},
        property_dependencies=(
            PropertyReference(object_type=_object_type(), property_name="tier"),
        ),
    )


def _gate_harness(
    *,
    preconditions: tuple = (),
    request: Mapping[str, object] | None = None,
    objects: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
) -> GovernanceHarness:
    action = _action_definition(preconditions=preconditions)
    resolved_request = dict(_REQUEST if request is None else request)
    claim = _claim_request(action, resolved_request)
    claim_port = ClaimSpy(_claimed_result(claim))
    approval_port = ApprovalSpy(None)

    def reader(tenant_id: str, type_name: str, object_id: str):
        if objects is None:
            return None
        return objects.get((type_name, object_id))

    clock = MutableClock()
    governor = ActionGovernor(
        claim_port=claim_port,
        approval_port=approval_port,
        clock=clock,
        object_reader=reader,
    )
    return GovernanceHarness(
        tenant_id=_TENANT,
        invocation_id=_INVOCATION,
        action_reference=action.reference(),
        action_definition=action,
        capability_snapshot=_capability_snapshot(
            required_scopes=(_CAPABILITY_SCOPE, _SHARED_SCOPE)
        ),
        request=resolved_request,
        claim_request=claim,
        granted_scopes=frozenset((_CAPABILITY_SCOPE, _SHARED_SCOPE, _ACTION_SCOPE)),
        policy_evidence=_policy_set(action.reference()),
        approval_evidence=None,
        claim_port=claim_port,
        approval_port=approval_port,
        clock=clock,
        governor=governor,
    )


def test_satisfied_precondition_admits_the_submission() -> None:
    harness = _gate_harness(
        preconditions=(_tier_precondition(),),
        request={
            **_REQUEST,
            "target_objects": [{"type": "Customer", "object_id": "c-1"}],
        },
        objects={
            ("Customer", "c-1"): {
                "properties": {"tier": "gold"},
                "updated_at": "2026-07-17T09:00:00+00:00",
            }
        },
    )
    permit = harness.govern()
    assert type(permit).__name__ == "ActionExecutionPermit"


def test_unsatisfied_precondition_fails_closed_before_the_claim() -> None:
    harness = _gate_harness(
        preconditions=(_tier_precondition(),),
        request={
            **_REQUEST,
            "target_objects": [{"type": "Customer", "object_id": "c-1"}],
        },
        objects={
            ("Customer", "c-1"): {
                "properties": {"tier": "bronze"},
                "updated_at": "2026-07-17T09:00:00+00:00",
            }
        },
    )
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_submission_criteria_failed"
    assert harness.claim_port.reserve_calls == []


def test_missing_object_evaluates_total_and_fails_closed() -> None:
    harness = _gate_harness(
        preconditions=(_tier_precondition(),),
        request={
            **_REQUEST,
            "target_objects": [{"type": "Customer", "object_id": "absent"}],
        },
        objects={},
    )
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_submission_criteria_failed"


def test_legacy_inert_expression_is_rejected_as_unevaluable() -> None:
    legacy = ActionPrecondition(
        name="customer_is_active",
        expression={"property_alias": "active", "equals": True},
        property_dependencies=(
            PropertyReference(object_type=_object_type(), property_name="active"),
        ),
    )
    harness = _gate_harness(preconditions=(legacy,), request=_REQUEST)
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_submission_criteria_failed"


def test_edit_fence_admits_exact_state_and_rejects_drift() -> None:
    fresh = {
        ("Customer", "c-1"): {
            "properties": {"tier": "gold"},
            "updated_at": "2026-07-17T09:00:00+00:00",
        }
    }
    harness = _gate_harness(
        request={
            **_REQUEST,
            "expected_object_revisions": [
                {
                    "type": "Customer",
                    "object_id": "c-1",
                    "updated_at": "2026-07-17T09:00:00+00:00",
                }
            ],
        },
        objects=fresh,
    )
    permit = harness.govern()
    assert type(permit).__name__ == "ActionExecutionPermit"

    drifted = _gate_harness(
        request={
            **_REQUEST,
            "expected_object_revisions": [
                {
                    "type": "Customer",
                    "object_id": "c-1",
                    "updated_at": "2026-07-17T08:00:00+00:00",
                }
            ],
        },
        objects=fresh,
    )
    with pytest.raises(ActionGovernanceError) as caught:
        drifted.govern()
    assert caught.value.code == "action_edit_conflict"
    assert drifted.claim_port.reserve_calls == []


def test_edit_fence_without_a_reader_fails_closed() -> None:
    harness = _gate_harness(
        request={
            **_REQUEST,
            "expected_object_revisions": [
                {
                    "type": "Customer",
                    "object_id": "c-1",
                    "updated_at": "2026-07-17T09:00:00+00:00",
                }
            ],
        },
        objects=None,
    )
    with pytest.raises(ActionGovernanceError) as caught:
        harness.govern()
    assert caught.value.code == "action_edit_conflict"

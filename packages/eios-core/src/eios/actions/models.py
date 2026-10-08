from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from eios.ontology.definitions import (
    CapabilityBinding,
    DefinitionReference,
    DefinitionType,
    FrozenContract,
)


_MAX_JSON_DEPTH = 64
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")


def _non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must be non-empty")
    return clean


def _stable_name(value: str, field_name: str) -> str:
    clean = _non_blank(value, field_name)
    if _STABLE_NAME_RE.fullmatch(clean) is None:
        raise ValueError(f"{field_name} must be a stable dotted name")
    return clean


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_request_value(
    value: object,
    *,
    path: str,
    active_ids: frozenset[int] = frozenset(),
    depth: int = 0,
) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("canonical request exceeds maximum JSON nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical request contains a non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("canonical request contains a cyclic JSON reference")
        child_ids = active_ids | {identity}
        canonical: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical request keys must be JSON strings")
            canonical[key] = _canonical_request_value(
                item,
                path=f"{path}.{key}",
                active_ids=child_ids,
                depth=depth + 1,
            )
        return canonical
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("canonical request contains a cyclic JSON reference")
        child_ids = active_ids | {identity}
        return [
            _canonical_request_value(
                item,
                path=f"{path}[]",
                active_ids=child_ids,
                depth=depth + 1,
            )
            for item in value
        ]
    raise ValueError(f"canonical request contains non-JSON value at {path}")


def canonical_request_digest(request: Mapping[str, object]) -> str:
    """Return a type-sensitive SHA-256 over one finite canonical JSON object."""

    if not isinstance(request, Mapping):
        raise ValueError("canonical request must be a JSON object")
    canonical = _canonical_request_value(request, path="request")
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_policy_references(
    values: Sequence[str], field_name: str
) -> tuple[str, ...]:
    normalized = tuple(_stable_name(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"duplicate {field_name}")
    return tuple(sorted(normalized))


class PolicyDecisionEvidence(FrozenContract):
    tenant_id: str
    invocation_id: str
    action_reference: DefinitionReference
    policy_reference: str
    evidence_id: str
    decision_id: str
    decision_revision: int = Field(gt=0)
    allowed: bool
    issued_at: datetime
    expires_at: datetime

    @field_validator("tenant_id", "invocation_id", "evidence_id", "decision_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("policy_reference")
    @classmethod
    def _validate_policy_reference(cls, value: str) -> str:
        return _stable_name(value, "policy_reference")

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _validate_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_binding(self) -> PolicyDecisionEvidence:
        if self.action_reference.definition_type is not DefinitionType.ACTION:
            raise ValueError("policy evidence must reference an Action")
        if self.action_reference.tenant_id != self.tenant_id:
            raise ValueError("policy evidence tenant must equal Action tenant")
        if self.evidence_id == self.decision_id:
            raise ValueError("evidence_id and decision_id must be independent")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self


class PolicyEvidenceBinding(FrozenContract):
    policy_reference: str
    evidence_id: str
    decision_id: str
    decision_revision: int = Field(gt=0)

    @field_validator("policy_reference")
    @classmethod
    def _validate_policy_reference(cls, value: str) -> str:
        return _stable_name(value, "policy_reference")

    @field_validator("evidence_id", "decision_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @model_validator(mode="after")
    def _validate_independent_ids(self) -> PolicyEvidenceBinding:
        if self.evidence_id == self.decision_id:
            raise ValueError("evidence_id and decision_id must be independent")
        return self


class PolicyEvidenceSet(FrozenContract):
    tenant_id: str
    invocation_id: str
    action_reference: DefinitionReference
    required_policy_references: tuple[str, ...]
    evidence: tuple[PolicyDecisionEvidence, ...]
    evaluated_at: datetime

    @field_validator("tenant_id", "invocation_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("required_policy_references")
    @classmethod
    def _validate_policy_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_policy_references(values, "policy reference")

    @field_validator("evidence")
    @classmethod
    def _canonicalize_evidence(
        cls, values: tuple[PolicyDecisionEvidence, ...]
    ) -> tuple[PolicyDecisionEvidence, ...]:
        return tuple(sorted(values, key=lambda item: item.policy_reference))

    @field_validator("evaluated_at")
    @classmethod
    def _validate_evaluated_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "evaluated_at")

    @model_validator(mode="after")
    def _validate_exact_set(self) -> PolicyEvidenceSet:
        if self.action_reference.definition_type is not DefinitionType.ACTION:
            raise ValueError("policy evidence set must reference an Action")
        if self.action_reference.tenant_id != self.tenant_id:
            raise ValueError("policy evidence set tenant must equal Action tenant")

        actual_references = tuple(item.policy_reference for item in self.evidence)
        if len(actual_references) != len(set(actual_references)):
            raise ValueError("duplicate policy evidence reference")
        if actual_references != self.required_policy_references:
            raise ValueError(
                "policy evidence must exactly match required policy references"
            )

        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        decision_ids = tuple(item.decision_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate policy evidence_id")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("duplicate policy decision_id")
        if not set(evidence_ids).isdisjoint(decision_ids):
            raise ValueError("policy evidence and decision IDs must be independent")

        for item in self.evidence:
            if item.tenant_id != self.tenant_id:
                raise ValueError("policy evidence tenant mismatch")
            if item.invocation_id != self.invocation_id:
                raise ValueError("policy evidence invocation mismatch")
            if item.action_reference != self.action_reference:
                raise ValueError("policy evidence Action mismatch")
            if not item.allowed:
                raise ValueError("policy evidence decision must allow the Action")
            if item.issued_at > self.evaluated_at:
                raise ValueError("policy evidence is not yet valid")
            if item.expires_at <= self.evaluated_at:
                raise ValueError("policy evidence is expired")
        return self

    def bindings(self) -> tuple[PolicyEvidenceBinding, ...]:
        return tuple(
            PolicyEvidenceBinding(
                policy_reference=item.policy_reference,
                evidence_id=item.evidence_id,
                decision_id=item.decision_id,
                decision_revision=item.decision_revision,
            )
            for item in self.evidence
        )


class ApprovalEvidenceBinding(FrozenContract):
    approval_id: str
    approved_revision: int = Field(gt=0)
    decision_id: str

    @field_validator("approval_id", "decision_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)


class ApprovalEvidence(FrozenContract):
    tenant_id: str
    invocation_id: str
    action_reference: DefinitionReference
    approval_id: str
    approved_revision: int = Field(gt=0)
    decision_id: str
    approval_record_digest: str = Field(pattern=_SHA256_PATTERN)
    outcome: Literal["approved"]
    decided_at: datetime
    issued_at: datetime
    expires_at: datetime

    @field_validator("tenant_id", "invocation_id", "approval_id", "decision_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("decided_at", "issued_at", "expires_at")
    @classmethod
    def _validate_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_binding(self) -> ApprovalEvidence:
        if self.action_reference.definition_type is not DefinitionType.ACTION:
            raise ValueError("Approval evidence must reference an Action")
        if self.action_reference.tenant_id != self.tenant_id:
            raise ValueError("Approval evidence tenant must equal Action tenant")
        if self.issued_at < self.decided_at:
            raise ValueError("issued_at cannot predate the Approval decision")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self

    def binding(self, *, evaluated_at: datetime) -> ApprovalEvidenceBinding:
        evaluated = _aware_utc(evaluated_at, "evaluated_at")
        if self.issued_at > evaluated:
            raise ValueError("Approval evidence is not yet valid")
        if self.expires_at <= evaluated:
            raise ValueError("Approval evidence is expired")
        return ApprovalEvidenceBinding(
            approval_id=self.approval_id,
            approved_revision=self.approved_revision,
            decision_id=self.decision_id,
        )


class ActionReservationKey(FrozenContract):
    tenant_id: str
    action_stable_name: str
    idempotency_key: str

    @field_validator("tenant_id", "idempotency_key")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("action_stable_name")
    @classmethod
    def _validate_action_stable_name(cls, value: str) -> str:
        return _stable_name(value, "action_stable_name")


class ClaimBindingPayload(FrozenContract):
    invocation_id: str
    action_reference: DefinitionReference
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    capability_binding: CapabilityBinding
    adapter_id: str
    target_system: str

    @field_validator("invocation_id", "target_system")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("adapter_id")
    @classmethod
    def _validate_adapter_id(cls, value: str) -> str:
        return _stable_name(value, "adapter_id")

    @model_validator(mode="after")
    def _validate_action_reference(self) -> ClaimBindingPayload:
        if self.action_reference.definition_type is not DefinitionType.ACTION:
            raise ValueError("claim binding must reference an Action")
        return self


class ActionClaimState(str, Enum):
    ACTIVE = "active"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class TerminalOutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PERMANENT_FAILURE = "permanent_failure"
    COMPENSATED = "compensated"


class TerminalOutcomeReference(FrozenContract):
    outcome_id: str
    outcome_revision: int = Field(gt=0)
    status: TerminalOutcomeStatus
    outcome_digest: str = Field(pattern=_SHA256_PATTERN)
    finalized_at: datetime

    @field_validator("outcome_id")
    @classmethod
    def _validate_outcome_id(cls, value: str) -> str:
        return _non_blank(value, "outcome_id")

    @field_validator("finalized_at")
    @classmethod
    def _validate_finalized_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "finalized_at")


class ActionClaim(FrozenContract):
    key: ActionReservationKey
    binding: ClaimBindingPayload
    state: ActionClaimState
    claim_revision: int = Field(gt=0)
    fencing_token: str
    lease_expires_at: datetime
    terminal_outcome: TerminalOutcomeReference | None = None

    @field_validator("fencing_token")
    @classmethod
    def _validate_fencing_token(cls, value: str) -> str:
        return _non_blank(value, "fencing_token")

    @field_validator("lease_expires_at")
    @classmethod
    def _validate_lease_expires_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "lease_expires_at")

    @model_validator(mode="after")
    def _validate_identity_and_state(self) -> ActionClaim:
        action = self.binding.action_reference
        if self.key.tenant_id != action.tenant_id:
            raise ValueError("claim key tenant must equal Action tenant")
        if self.key.action_stable_name != action.stable_name:
            raise ValueError("claim key stable name must equal Action stable name")
        if self.state is ActionClaimState.TERMINAL:
            if self.terminal_outcome is None:
                raise ValueError("terminal claim requires a terminal outcome")
        elif self.terminal_outcome is not None:
            raise ValueError("non-terminal claim cannot contain a terminal outcome")
        return self


def _validate_claim_identity(
    key: ActionReservationKey,
    binding: ClaimBindingPayload,
) -> None:
    action = binding.action_reference
    if key.tenant_id != action.tenant_id:
        raise ValueError("claim key tenant must equal Action tenant")
    if key.action_stable_name != action.stable_name:
        raise ValueError("claim key stable name must equal Action stable name")


class ActionClaimRequest(FrozenContract):
    key: ActionReservationKey
    binding: ClaimBindingPayload
    requested_at: datetime
    lease_expires_at: datetime

    @field_validator("requested_at", "lease_expires_at")
    @classmethod
    def _validate_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_request(self) -> ActionClaimRequest:
        _validate_claim_identity(self.key, self.binding)
        if self.lease_expires_at <= self.requested_at:
            raise ValueError("lease_expires_at must be later than requested_at")
        return self


class ActionClaimRetryableCommand(FrozenContract):
    key: ActionReservationKey
    binding: ClaimBindingPayload
    expected_claim_revision: int = Field(gt=0)
    fencing_token: str
    marked_at: datetime

    @field_validator("fencing_token")
    @classmethod
    def _validate_fencing_token(cls, value: str) -> str:
        return _non_blank(value, "fencing_token")

    @field_validator("marked_at")
    @classmethod
    def _validate_marked_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "marked_at")

    @model_validator(mode="after")
    def _validate_identity(self) -> ActionClaimRetryableCommand:
        _validate_claim_identity(self.key, self.binding)
        return self


class ActionClaimFinalizeCommand(FrozenContract):
    key: ActionReservationKey
    binding: ClaimBindingPayload
    expected_claim_revision: int = Field(gt=0)
    fencing_token: str
    outcome: TerminalOutcomeReference

    @field_validator("fencing_token")
    @classmethod
    def _validate_fencing_token(cls, value: str) -> str:
        return _non_blank(value, "fencing_token")

    @model_validator(mode="after")
    def _validate_identity(self) -> ActionClaimFinalizeCommand:
        _validate_claim_identity(self.key, self.binding)
        return self


class ActionClaimDisposition(str, Enum):
    CLAIMED = "claimed"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"


class ActionClaimResult(FrozenContract):
    disposition: ActionClaimDisposition
    claim: ActionClaim | None = None
    terminal_outcome: TerminalOutcomeReference | None = None

    @model_validator(mode="after")
    def _validate_disposition_payload(self) -> ActionClaimResult:
        if self.disposition is ActionClaimDisposition.CLAIMED:
            if self.claim is None or self.claim.state is not ActionClaimState.ACTIVE:
                raise ValueError("claimed result requires an active claim")
            if self.terminal_outcome is not None:
                raise ValueError("claimed result cannot contain a terminal outcome")
            return self
        if self.disposition is ActionClaimDisposition.REPLAY:
            if self.claim is not None or self.terminal_outcome is None:
                raise ValueError("replay result requires only a terminal outcome")
            return self
        if self.claim is not None or self.terminal_outcome is not None:
            raise ValueError("non-owner claim result cannot expose claim ownership")
        return self


class ActionExecutionPermit(FrozenContract):
    claim: ActionClaim
    policy_bindings: tuple[PolicyEvidenceBinding, ...]
    approval_binding: ApprovalEvidenceBinding | None
    issued_at: datetime
    expires_at: datetime

    @field_validator("policy_bindings")
    @classmethod
    def _canonicalize_policy_bindings(
        cls, values: tuple[PolicyEvidenceBinding, ...]
    ) -> tuple[PolicyEvidenceBinding, ...]:
        return tuple(sorted(values, key=lambda item: item.policy_reference))

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _validate_time(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_permit(self) -> ActionExecutionPermit:
        if self.claim.state is not ActionClaimState.ACTIVE:
            raise ValueError("execution permit requires an active claim")
        if self.expires_at <= self.issued_at:
            raise ValueError("permit expires_at must be later than issued_at")
        if self.issued_at >= self.claim.lease_expires_at:
            raise ValueError("execution permit cannot start after the claim lease")
        if self.expires_at > self.claim.lease_expires_at:
            raise ValueError("execution permit cannot outlive the claim lease")

        policy_refs = tuple(item.policy_reference for item in self.policy_bindings)
        evidence_ids = tuple(item.evidence_id for item in self.policy_bindings)
        decision_ids = tuple(item.decision_id for item in self.policy_bindings)
        if len(policy_refs) != len(set(policy_refs)):
            raise ValueError("duplicate permit policy reference")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate permit policy evidence_id")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("duplicate permit policy decision_id")
        if not set(evidence_ids).isdisjoint(decision_ids):
            raise ValueError(
                "permit policy evidence and decision IDs must be independent"
            )
        return self

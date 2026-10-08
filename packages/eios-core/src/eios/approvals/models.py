from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import hashlib
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from eios.ontology.definitions import (
    DefinitionReference,
    DefinitionType,
    FrozenContract,
    FrozenJsonMap,
)


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
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class ApprovalStatus(str, Enum):
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_APPROVAL_STATUSES = frozenset(
    {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.CANCELLED,
        ApprovalStatus.EXPIRED,
    }
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ApprovalSubject(FrozenContract):
    """Immutable statement of WHAT one Approval endorses.

    Binds the Approval to one exact Action request: the exact Action
    reference, the request digest computed by the claim binding, the
    requesting principal, and the governance context a reviewer decides on.
    An Approval carrying a subject can never endorse a different Action or a
    different request payload — the governor enforces equality fail-closed.
    """

    action_reference: DefinitionReference
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    requester_id: str
    risk_level: str
    change_scope: FrozenJsonMap = Field(default_factory=lambda: FrozenJsonMap({}))
    justification: str = ""
    # Stage-5 approver policy, snapshotted at creation so the rules an
    # Approval is decided under can never drift after the fact. Additive:
    # pre-stage-5 subjects hydrate with the single-approver defaults.
    quorum: int = Field(default=1, ge=1)
    eligible_approver_scopes: tuple[str, ...] = ()
    require_distinct_requester: bool = True
    # Stage-11 SLA deadline. Additive and nullable: subjects written before
    # this field hydrate with `None` and never auto-expire. When set, the
    # expiry sweeper transitions a still-waiting Approval to EXPIRED at exactly
    # this instant (the expiry decision's decided_at equals due_by, so a
    # re-sweep produces a byte-identical decision and is a pure replay).
    due_by: datetime | None = None

    @field_validator("eligible_approver_scopes")
    @classmethod
    def _validate_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_non_blank(item, "eligible_approver_scopes") for item in values)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("duplicate eligible_approver_scopes")
        return tuple(sorted(cleaned))

    @field_validator("requester_id", "risk_level")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("due_by")
    @classmethod
    def _validate_due_by(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, "due_by")

    @model_validator(mode="after")
    def _validate_subject(self) -> ApprovalSubject:
        if self.action_reference.definition_type is not DefinitionType.ACTION:
            raise ValueError("approval subject must reference an Action")
        return self


class ApprovalCreate(FrozenContract):
    tenant_id: str
    approval_id: str
    requested_at: datetime
    # Additive (stage 4): pre-existing creators carry no subject; the action
    # governor refuses subject-less records as approval evidence.
    subject: ApprovalSubject | None = None

    @field_validator("tenant_id", "approval_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("requested_at")
    @classmethod
    def _validate_requested_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "requested_at")

    @model_validator(mode="after")
    def _validate_subject_tenant(self) -> ApprovalCreate:
        if self.subject is None:
            return self
        if self.subject.action_reference.tenant_id != self.tenant_id:
            raise ValueError("approval subject tenant must equal Approval tenant")
        # The expiry sweeper uses due_by as the decision instant; a deadline
        # before the request would produce a decision predating the request,
        # which apply_decision refuses. Forbid it at creation, fail-closed.
        if self.subject.due_by is not None and self.subject.due_by < self.requested_at:
            raise ValueError("approval subject due_by cannot predate the request")
        return self


class ApprovalHumanAuthority(FrozenContract):
    """Immutable human/browser authority bound to one APPROVE decision."""

    tenant_id: str
    actor: str
    subject_id: str
    subject_kind: Literal["human"]
    session_id: str
    session_revision: int = Field(ge=1)
    credential_id: str
    application_id: str
    application_version: str
    eligible_scopes: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "tenant_id",
        "actor",
        "subject_id",
        "session_id",
        "credential_id",
        "application_id",
        "application_version",
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("eligible_scopes")
    @classmethod
    def _validate_eligible_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_non_blank(item, "eligible_scopes") for item in values)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("duplicate eligible_scopes")
        return tuple(sorted(cleaned))


class ApprovalDecisionCommand(FrozenContract):
    tenant_id: str
    approval_id: str
    expected_revision: int = Field(gt=0)
    decision_id: str
    outcome: ApprovalStatus
    actor: str
    reason_code: str
    decided_at: datetime
    human_authority: ApprovalHumanAuthority | None = None

    @field_validator("tenant_id", "approval_id", "decision_id", "actor")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, value: str) -> str:
        return _stable_name(value, "reason_code")

    @field_validator("outcome")
    @classmethod
    def _validate_outcome(cls, value: ApprovalStatus) -> ApprovalStatus:
        if value not in TERMINAL_APPROVAL_STATUSES:
            raise ValueError("outcome must be a terminal Approval status")
        return value

    @field_validator("decided_at")
    @classmethod
    def _validate_decided_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "decided_at")

    @model_validator(mode="after")
    def _validate_human_authority(self) -> ApprovalDecisionCommand:
        if self.outcome is ApprovalStatus.APPROVED:
            if self.human_authority is not None and (
                self.human_authority.tenant_id != self.tenant_id
                or self.human_authority.actor != self.actor
            ):
                raise ValueError("human authority tenant and actor must match decision")
        elif (
            self.outcome is not ApprovalStatus.REJECTED
            and self.human_authority is not None
        ):
            raise ValueError("system terminal decision cannot carry human authority")
        return self


class ApprovalDecision(FrozenContract):
    tenant_id: str
    approval_id: str
    decision_id: str
    outcome: ApprovalStatus
    actor: str
    reason_code: str
    decided_at: datetime
    human_authority: ApprovalHumanAuthority | None = None

    @field_validator("tenant_id", "approval_id", "decision_id", "actor")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, value: str) -> str:
        return _stable_name(value, "reason_code")

    @field_validator("outcome")
    @classmethod
    def _validate_outcome(cls, value: ApprovalStatus) -> ApprovalStatus:
        if value not in TERMINAL_APPROVAL_STATUSES:
            raise ValueError("outcome must be a terminal Approval status")
        return value

    @field_validator("decided_at")
    @classmethod
    def _validate_decided_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "decided_at")

    @model_validator(mode="after")
    def _validate_human_authority(self) -> ApprovalDecision:
        if self.outcome is ApprovalStatus.APPROVED:
            if self.human_authority is not None and (
                self.human_authority.tenant_id != self.tenant_id
                or self.human_authority.actor != self.actor
            ):
                raise ValueError("human authority tenant and actor must match decision")
        elif (
            self.outcome is not ApprovalStatus.REJECTED
            and self.human_authority is not None
        ):
            raise ValueError("system terminal decision cannot carry human authority")
        return self


class ApprovalRecord(FrozenContract):
    tenant_id: str
    approval_id: str
    status: ApprovalStatus
    revision: int = Field(gt=0)
    requested_at: datetime
    decision: ApprovalDecision | None = None
    # Additive (stage 4): rows stored before the subject envelope existed
    # hydrate as None; the action governor refuses them as evidence.
    subject: ApprovalSubject | None = None
    # Additive (stage 5): every recorded decision in arrival order. Under a
    # quorum the record stays waiting while approvals accumulate; `decision`
    # remains the terminal decision only. Pre-stage-5 terminal rows hydrate
    # with an empty tuple (single-decision legacy shape).
    decisions: tuple[ApprovalDecision, ...] = ()

    @field_validator("tenant_id", "approval_id")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("requested_at")
    @classmethod
    def _validate_requested_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "requested_at")

    @model_validator(mode="after")
    def _validate_state_metadata(self) -> ApprovalRecord:
        for recorded in self.decisions:
            if recorded.tenant_id != self.tenant_id:
                raise ValueError("recorded decision tenant must equal record tenant")
            if recorded.approval_id != self.approval_id:
                raise ValueError(
                    "recorded decision Approval ID must equal record Approval ID"
                )
            if recorded.decided_at < self.requested_at:
                raise ValueError("recorded decision cannot predate the request")
        decision_ids = tuple(item.decision_id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("recorded decision IDs must be unique")

        if self.status is ApprovalStatus.WAITING_APPROVAL:
            if self.decision is not None:
                raise ValueError("waiting Approval decision metadata must be absent")
            # Cumulative model: each recorded (non-terminal) approval bumps
            # the revision; the legacy shape is the zero-decision base case.
            if self.revision != 1 + len(self.decisions):
                raise ValueError(
                    "waiting Approval revision must equal 1 + recorded decisions"
                )
            for recorded in self.decisions:
                if recorded.outcome is not ApprovalStatus.APPROVED:
                    raise ValueError("waiting Approval can only accumulate approvals")
            return self

        if self.decision is None:
            raise ValueError("terminal Approval decision metadata must be present")
        if self.decisions:
            if self.decision != self.decisions[-1]:
                raise ValueError("terminal decision must be the last recorded decision")
            if self.revision != 1 + len(self.decisions):
                raise ValueError(
                    "terminal Approval revision must equal 1 + recorded decisions"
                )
        elif self.revision != 2:
            # Legacy single-decision shape (stored before stage 5).
            raise ValueError("terminal Approval revision must equal 2")
        if self.status is not self.decision.outcome:
            raise ValueError("Approval status must equal decision outcome")
        if self.tenant_id != self.decision.tenant_id:
            raise ValueError("Approval decision tenant must equal record tenant")
        if self.approval_id != self.decision.approval_id:
            raise ValueError("Approval decision ID must equal record Approval ID")
        if self.decision.decided_at < self.requested_at:
            raise ValueError("Approval decision cannot predate the request")
        return self


def approval_record_snapshot_digest(record: ApprovalRecord) -> str:
    """Digest one fully revalidated immutable Approval record snapshot."""

    if type(record) is not ApprovalRecord:
        raise TypeError("exact ApprovalRecord is required")
    validated = ApprovalRecord.model_validate_json(record.model_dump_json())
    canonical = validated.model_dump_json()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalCreateResult(FrozenContract):
    record: ApprovalRecord
    replayed: bool

    @model_validator(mode="after")
    def _validate_waiting_record(self) -> ApprovalCreateResult:
        if (
            not self.replayed
            and self.record.status is not ApprovalStatus.WAITING_APPROVAL
        ):
            raise ValueError("new create result must contain a waiting Approval")
        return self


class ApprovalDecisionResult(FrozenContract):
    record: ApprovalRecord
    replayed: bool

    @model_validator(mode="after")
    def _validate_decided_record(self) -> ApprovalDecisionResult:
        # Cumulative model (stage 5): a decision may leave the Approval
        # waiting while the quorum accumulates, but the record must then
        # carry at least one recorded decision — a decision result can never
        # wrap a pristine record.
        if (
            self.record.status not in TERMINAL_APPROVAL_STATUSES
            and not self.record.decisions
        ):
            raise ValueError("decision result must contain a decided Approval")
        return self


POLICY_AUTO_APPROVED_REASON = "policy_auto_approved"


def is_policy_auto_approved(decision: ApprovalDecision) -> bool:
    """True for a registered-policy auto approval decision.

    Such a decision deliberately carries no human authority; every consumer
    that accepts it must still run the record through
    ``assert_fresh_approval_human_authority``, whose policy branch verifies
    the actor against the ``authz.delegated_policy_approvers`` registry.
    """

    return (
        type(decision) is ApprovalDecision
        and decision.outcome is ApprovalStatus.APPROVED
        and decision.reason_code == POLICY_AUTO_APPROVED_REASON
        and decision.human_authority is None
    )

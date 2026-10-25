from __future__ import annotations

from datetime import datetime
from threading import RLock

from pydantic import ValidationError

from eios.approvals.models import (
    ApprovalCreate,
    ApprovalCreateResult,
    ApprovalDecisionCommand,
    ApprovalDecisionResult,
    ApprovalRecord,
    ApprovalStatus,
)
from eios.approvals.ports import (
    ApprovalContractError,
    ApprovalDecisionConflictError,
    ApprovalIdentityConflictError,
    ApprovalNotFoundError,
    ApprovalRevisionConflictError,
    ApprovalTransitionError,
)
from eios.approvals.state_machine import (
    ApprovalStateMachineError,
    apply_decision,
    decision_from_command,
)


class InMemoryApprovalStore:
    """Thread-safe reference Approval Port with revision-first decisions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[tuple[str, str], ApprovalRecord] = {}

    def create(self, command: ApprovalCreate) -> ApprovalCreateResult:
        validated = self._validate_create(command)
        key = (validated.tenant_id, validated.approval_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if (
                    existing.requested_at != validated.requested_at
                    or existing.subject != validated.subject
                ):
                    raise ApprovalIdentityConflictError(
                        "approval_identity_conflict",
                        "Approval identity is bound to a different creation envelope",
                    )
                return ApprovalCreateResult(record=existing, replayed=True)

            record = ApprovalRecord(
                tenant_id=validated.tenant_id,
                approval_id=validated.approval_id,
                status=ApprovalStatus.WAITING_APPROVAL,
                revision=1,
                requested_at=validated.requested_at,
                subject=validated.subject,
            )
            self._records[key] = record
            return ApprovalCreateResult(record=record, replayed=False)

    def decide(self, command: ApprovalDecisionCommand) -> ApprovalDecisionResult:
        validated = self._validate_decision(command)
        key = (validated.tenant_id, validated.approval_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                raise ApprovalNotFoundError(
                    "approval_not_found",
                    "no Approval exists for the exact tenant-bound identity",
                )

            if validated.expected_revision != existing.revision:
                raise ApprovalRevisionConflictError(
                    "approval_revision_conflict",
                    "expected revision does not equal the current Approval revision",
                )

            if existing.status is not ApprovalStatus.WAITING_APPROVAL:
                candidate = decision_from_command(validated)
                if existing.decision == candidate:
                    return ApprovalDecisionResult(record=existing, replayed=True)
                if (
                    existing.decision is not None
                    and existing.decision.decision_id == candidate.decision_id
                ):
                    raise ApprovalDecisionConflictError(
                        "approval_decision_conflict",
                        "decision ID is bound to different immutable decision content",
                    )
                raise ApprovalTransitionError(
                    "approval_terminal",
                    "terminal Approval records cannot transition",
                )

            try:
                decided = apply_decision(existing, validated)
            except ApprovalStateMachineError as error:
                if error.code == "approval_revision_conflict":  # pragma: no cover
                    raise ApprovalRevisionConflictError(error.code, str(error)) from error
                raise ApprovalTransitionError(error.code, str(error)) from error
            self._records[key] = decided
            return ApprovalDecisionResult(record=decided, replayed=False)

    def due_waiting(
        self, *, now: datetime, batch_size: int
    ) -> tuple[ApprovalRecord, ...]:
        """Cross-tenant scan of waiting Approvals whose SLA deadline has passed.

        Only WAITING records carrying a subject ``due_by`` at or before ``now``
        are returned. Terminal records are excluded, so an expired Approval is
        never re-surfaced — this is the sweeper's primary idempotency boundary.
        """
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise ApprovalContractError(
                "approval_query_invalid",
                "now must be a timezone-aware datetime",
            )
        if type(batch_size) is not int or batch_size <= 0:
            raise ApprovalContractError(
                "approval_query_invalid",
                "batch_size must be a positive integer",
            )
        with self._lock:
            due = [
                record
                for record in self._records.values()
                if record.status is ApprovalStatus.WAITING_APPROVAL
                and record.subject is not None
                and record.subject.due_by is not None
                and record.subject.due_by <= now
            ]
        # Oldest deadline first, then a stable identity tiebreak, so a bounded
        # batch drains the most-overdue Approvals deterministically.
        due.sort(key=lambda item: (item.subject.due_by, item.tenant_id, item.approval_id))  # type: ignore[union-attr]
        return tuple(due[:batch_size])

    def get(self, *, tenant_id: str, approval_id: str) -> ApprovalRecord | None:
        key = self._validate_query_identity(tenant_id, approval_id)
        with self._lock:
            return self._records.get(key)

    def list(self, *, tenant_id: str) -> tuple[ApprovalRecord, ...]:
        if type(tenant_id) is not str:
            raise ApprovalContractError(
                "approval_query_invalid",
                "tenant_id must be a string",
            )
        clean_tenant = tenant_id.strip()
        if not clean_tenant:
            raise ApprovalContractError(
                "approval_query_invalid",
                "tenant_id must be non-empty",
            )
        with self._lock:
            records = tuple(
                record
                for (record_tenant, _approval_id), record in self._records.items()
                if record_tenant == clean_tenant
            )
        return tuple(sorted(records, key=lambda item: item.approval_id))

    @staticmethod
    def _validate_create(command: ApprovalCreate) -> ApprovalCreate:
        if type(command) is not ApprovalCreate:
            raise ApprovalContractError(
                "approval_command_invalid",
                "create requires the exact ApprovalCreate contract",
            )
        try:
            return ApprovalCreate.model_validate(command)
        except ValidationError as error:
            raise ApprovalContractError(
                "approval_command_invalid",
                "Approval create command is invalid",
            ) from error

    @staticmethod
    def _validate_decision(
        command: ApprovalDecisionCommand,
    ) -> ApprovalDecisionCommand:
        if type(command) is not ApprovalDecisionCommand:
            raise ApprovalContractError(
                "approval_command_invalid",
                "decide requires the exact ApprovalDecisionCommand contract",
            )
        try:
            return ApprovalDecisionCommand.model_validate(command)
        except ValidationError as error:
            raise ApprovalContractError(
                "approval_command_invalid",
                "Approval decision command is invalid",
            ) from error

    @staticmethod
    def _validate_query_identity(tenant_id: str, approval_id: str) -> tuple[str, str]:
        if type(tenant_id) is not str or type(approval_id) is not str:
            raise ApprovalContractError(
                "approval_query_invalid",
                "tenant_id and approval_id must be strings",
            )
        clean_tenant = tenant_id.strip()
        clean_approval = approval_id.strip()
        if not clean_tenant or not clean_approval:
            raise ApprovalContractError(
                "approval_query_invalid",
                "tenant_id and approval_id must be non-empty",
            )
        return clean_tenant, clean_approval

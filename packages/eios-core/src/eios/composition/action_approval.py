"""Resolve persisted Approval records into short-lived Action evidence.

The resolver is an application-boundary adapter.  It reads one exact
tenant-bound Approval identity, validates the immutable subject against the
trusted caller and resolved published Action, and projects only the evidence
fields already persisted on the terminal record.  The Action governor remains
the final authority and revalidates the returned evidence against the store.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import re

from eios.actions.models import ApprovalEvidence
from eios.approvals.models import (
    is_policy_auto_approved as _is_policy_auto_approved,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalSubject,
    approval_record_snapshot_digest,
)
from eios.approvals.ports import ApprovalPort
from eios.approvals.ports import (
    ApprovalHumanAuthorityVerifier,
)
from eios.control.models import TenantContext
from eios.ontology.definitions import ActionDefinition


_EVIDENCE_WINDOW = timedelta(minutes=5)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ApprovalEvidenceResolutionError(RuntimeError):
    """Stable, safe failure raised while resolving persisted evidence."""

    def __init__(self, code: str, message: str, *, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ApprovalEvidenceResolver:
    """Resolve an exact persisted Approval into invocation-bound evidence."""

    def __init__(
        self,
        approvals: ApprovalPort,
        *,
        clock: Callable[[], datetime],
        authority_verifier: ApprovalHumanAuthorityVerifier,
    ) -> None:
        if not isinstance(approvals, ApprovalPort):
            raise TypeError("Approval Port is required")
        if not callable(clock):
            raise TypeError("trusted clock is required")
        self._approvals = approvals
        self._clock = clock
        if not callable(getattr(authority_verifier, "assert_fresh", None)):
            raise TypeError("trusted Approval authority verifier is required")
        self._authority_verifier = authority_verifier

    def resolve(
        self,
        *,
        context: TenantContext,
        action: ActionDefinition,
        invocation_id: str,
        request_digest: str,
        approval_id: str,
    ) -> ApprovalEvidence:
        if (
            type(context) is not TenantContext
            or type(action) is not ActionDefinition
            or type(invocation_id) is not str
            or not invocation_id.strip()
            or type(request_digest) is not str
            or _SHA256_RE.fullmatch(request_digest) is None
            or type(approval_id) is not str
            or not approval_id.strip()
            or approval_id != approval_id.strip()
        ):
            self._invalid()
        issued_at = self._now()
        try:
            expires_at = issued_at + _EVIDENCE_WINDOW
        except (OverflowError, ValueError):
            self._unavailable()

        try:
            candidate = self._approvals.get(
                tenant_id=context.tenant_id,
                approval_id=approval_id,
            )
        except Exception:
            self._unavailable()
        if type(candidate) is not ApprovalRecord:
            self._invalid()
        try:
            # Rehydrate through the serialized contract so even an in-process
            # object built with ``model_construct`` cannot bypass validators.
            record = ApprovalRecord.model_validate_json(candidate.model_dump_json())
        except Exception:
            self._invalid()
        try:
            self._authority_verifier.assert_fresh(record)
            subject = record.subject
            eligible_approver_scopes = frozenset(
                (
                    subject.eligible_approver_scopes
                    if subject is not None
                    else ()
                )
                or ("approvals.decide",)
            )
            # Registered policy approvals carry no human authority by design;
            # their legitimacy is proven by assert_fresh above (database-side
            # policy approver registry), not by session scopes.
            if any(
                item.outcome is ApprovalStatus.APPROVED
                and not _is_policy_auto_approved(item)
                and (
                    item.human_authority is None
                    or not eligible_approver_scopes.intersection(
                        item.human_authority.eligible_scopes
                    )
                )
                for item in record.decisions
            ):
                self._invalid()
        except Exception:
            self._invalid()

        subject = record.subject
        decision = record.decision
        reference = action.reference()
        decisions = record.decisions
        history_invalid = False
        if decisions:
            actors = tuple(item.actor for item in decisions)
            distinct_actors = set(actors)
            history_invalid = (
                type(subject) is not ApprovalSubject
                or len(decisions) != subject.quorum
                or len(distinct_actors) != len(actors)
                or len(distinct_actors) != subject.quorum
                or decision != decisions[-1]
                or any(
                    item.outcome is not ApprovalStatus.APPROVED for item in decisions
                )
                or any(
                    current.decided_at > following.decided_at
                    for current, following in zip(decisions, decisions[1:])
                )
            )
        if subject is not None and subject.due_by is not None:
            history_invalid = history_invalid or (
                decision is None
                or decision.decided_at >= subject.due_by
                or any(item.decided_at >= subject.due_by for item in decisions)
            )
        if (
            history_invalid
            or record.tenant_id != context.tenant_id
            or record.approval_id != approval_id
            or record.status is not ApprovalStatus.APPROVED
            or type(subject) is not ApprovalSubject
            or subject.action_reference.tenant_id != record.tenant_id
            or subject.action_reference != reference
            or subject.request_digest != request_digest
            or subject.requester_id != context.principal_id
            or subject.risk_level != action.governance.risk_level.value
            or type(decision) is not ApprovalDecision
            or decision.tenant_id != record.tenant_id
            or decision.approval_id != record.approval_id
            or decision.outcome is not ApprovalStatus.APPROVED
        ):
            self._invalid()

        try:
            return ApprovalEvidence(
                tenant_id=record.tenant_id,
                invocation_id=invocation_id,
                action_reference=reference,
                approval_id=record.approval_id,
                approved_revision=record.revision,
                decision_id=decision.decision_id,
                approval_record_digest=approval_record_snapshot_digest(record),
                outcome="approved",
                decided_at=decision.decided_at,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except Exception:
            self._invalid()

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            self._unavailable()
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            self._unavailable()
        try:
            return value.astimezone(UTC)
        except Exception:
            self._unavailable()

    @staticmethod
    def _invalid() -> None:
        raise ApprovalEvidenceResolutionError(
            "action_approval_invalid",
            "Action approval evidence is invalid",
            status=409,
        )

    @staticmethod
    def _unavailable() -> None:
        raise ApprovalEvidenceResolutionError(
            "action_approval_unavailable",
            "Action approval evidence is unavailable",
            status=503,
        )


__all__ = ["ApprovalEvidenceResolutionError", "ApprovalEvidenceResolver"]

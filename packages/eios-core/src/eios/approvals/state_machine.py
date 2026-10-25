from __future__ import annotations

from eios.approvals.models import (
    ApprovalDecision,
    ApprovalDecisionCommand,
    ApprovalRecord,
    ApprovalStatus,
)


class ApprovalStateMachineError(ValueError):
    """Internal state transition error mapped to a stable Port error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def decision_from_command(command: ApprovalDecisionCommand) -> ApprovalDecision:
    return ApprovalDecision(
        tenant_id=command.tenant_id,
        approval_id=command.approval_id,
        decision_id=command.decision_id,
        outcome=command.outcome,
        actor=command.actor,
        reason_code=command.reason_code,
        decided_at=command.decided_at,
        human_authority=command.human_authority,
    )


def apply_decision(
    record: ApprovalRecord,
    command: ApprovalDecisionCommand,
) -> ApprovalRecord:
    """Apply the sole waiting-to-terminal transition for one exact revision."""
    if command.tenant_id != record.tenant_id:
        raise ApprovalStateMachineError(
            "approval_identity_mismatch",
            "decision tenant does not equal Approval tenant",
        )
    if command.approval_id != record.approval_id:
        raise ApprovalStateMachineError(
            "approval_identity_mismatch",
            "decision Approval ID does not equal record Approval ID",
        )
    if command.expected_revision != record.revision:
        raise ApprovalStateMachineError(
            "approval_revision_conflict",
            "expected revision does not equal the current Approval revision",
        )
    if record.status is not ApprovalStatus.WAITING_APPROVAL:
        raise ApprovalStateMachineError(
            "approval_terminal",
            "terminal Approval records cannot transition",
        )
    if command.decided_at < record.requested_at:
        raise ApprovalStateMachineError(
            "approval_decision_time_invalid",
            "Approval decision cannot predate the request",
        )
    decision = decision_from_command(command)
    if decision.outcome is ApprovalStatus.APPROVED and any(
        item.actor == decision.actor
        and item.outcome is ApprovalStatus.APPROVED
        for item in record.decisions
    ):
        raise ApprovalStateMachineError(
            "approval_duplicate_approver",
            "an approver can endorse one Approval at most once",
        )
    decisions = (*record.decisions, decision)
    quorum = 1 if record.subject is None else record.subject.quorum

    if decision.outcome is not ApprovalStatus.APPROVED:
        # Any terminal non-approval (rejected/cancelled/expired) ends the
        # Approval immediately regardless of accumulated approvals.
        return ApprovalRecord(
            tenant_id=record.tenant_id,
            approval_id=record.approval_id,
            status=decision.outcome,
            revision=record.revision + 1,
            requested_at=record.requested_at,
            decision=decision,
            subject=record.subject,
            decisions=decisions,
        )

    distinct_approvers = {
        item.actor
        for item in decisions
        if item.outcome is ApprovalStatus.APPROVED
    }
    if len(distinct_approvers) >= quorum:
        return ApprovalRecord(
            tenant_id=record.tenant_id,
            approval_id=record.approval_id,
            status=ApprovalStatus.APPROVED,
            revision=record.revision + 1,
            requested_at=record.requested_at,
            decision=decision,
            subject=record.subject,
            decisions=decisions,
        )
    # Quorum not reached: stay waiting with the approval recorded.
    return ApprovalRecord(
        tenant_id=record.tenant_id,
        approval_id=record.approval_id,
        status=ApprovalStatus.WAITING_APPROVAL,
        revision=record.revision + 1,
        requested_at=record.requested_at,
        decision=None,
        subject=record.subject,
        decisions=decisions,
    )

"""NX-056 A0: the vendored Approval state machine's semantics the unified approvals rely on (design §1): one rejection ends an
Approval, a quorum counts distinct approvers, one person approves at most once, a decision never predates the request."""
from datetime import timedelta

import pytest

from eios.approvals.in_memory import InMemoryApprovalStore
from eios.approvals.models import ApprovalDecisionCommand, ApprovalStatus
from eios.approvals.state_machine import ApprovalStateMachineError, apply_decision
from upstream.test_eios_approval_expiry import _REQUESTED_AT, _create


def decide(record, actor, outcome=ApprovalStatus.APPROVED, minutes=1, decision=None):
    return apply_decision(record, ApprovalDecisionCommand(
        tenant_id=record.tenant_id, approval_id=record.approval_id, expected_revision=record.revision,
        decision_id=decision or f'd-{actor}-{record.revision}', outcome=outcome, actor=actor, reason_code='synthetic',
        decided_at=_REQUESTED_AT + timedelta(minutes=minutes)))


def test_a_quorum_counts_distinct_approvers():
    record = _create(InMemoryApprovalStore(), quorum=2)
    once = decide(record, 'approver-1')
    assert once.status is ApprovalStatus.WAITING_APPROVAL
    with pytest.raises(ApprovalStateMachineError):
        decide(once, 'approver-1', minutes=2)  # the same person twice
    assert decide(once, 'approver-2', minutes=2).status is ApprovalStatus.APPROVED


def test_one_rejection_ends_it_whatever_was_approved_before():
    record = _create(InMemoryApprovalStore(), quorum=2)
    rejected = decide(decide(record, 'approver-1'), 'approver-2', ApprovalStatus.REJECTED, minutes=2)
    assert rejected.status is ApprovalStatus.REJECTED
    with pytest.raises(ApprovalStateMachineError):
        decide(rejected, 'approver-3', minutes=3)  # terminal


def test_a_decision_never_predates_the_request():
    record = _create(InMemoryApprovalStore())
    with pytest.raises(ApprovalStateMachineError):
        decide(record, 'approver-1', minutes=-1)

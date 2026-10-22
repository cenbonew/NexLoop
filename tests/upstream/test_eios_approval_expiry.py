"""Stage-11 deliverable 6: approval SLA expiry sweep semantics.

The in-memory Approval store is the reference implementation, so these tests
pin the semantic contract the PostgreSQL scanner must mirror: only waiting
Approvals past their subject due_by are swept; expiry is enacted through the
ordinary decision Port as a deterministic system decision; the sweep is
idempotent (a terminal record never re-surfaces); revision conflicts and
competing terminal decisions are skipped, never errors; and the observer fires
exactly once per genuine transition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eios.approvals.expiry import (
    EXPIRY_ACTOR,
    EXPIRY_REASON_CODE,
    ApprovalExpirySweeper,
    ExpirySweepStats,
)
from eios.approvals.in_memory import InMemoryApprovalStore
from eios.approvals.models import (
    ApprovalCreate,
    ApprovalDecisionCommand,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalSubject,
)
from eios.ontology.definitions import DefinitionReference, DefinitionType


_REQUESTED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)
_DUE_BY = datetime(2026, 7, 15, 18, tzinfo=UTC)


def _subject(
    *,
    tenant_id: str = "tenant-a",
    due_by: datetime | None = _DUE_BY,
    quorum: int = 1,
    digest: str = "a" * 64,
) -> ApprovalSubject:
    return ApprovalSubject(
        action_reference=DefinitionReference(
            tenant_id=tenant_id,
            definition_type=DefinitionType.ACTION,
            stable_name="customer.update_crm",
            version=1,
            contract_digest="b" * 64,
        ),
        request_digest=digest,
        requester_id="requester-7",
        risk_level="medium",
        quorum=quorum,
        due_by=due_by,
    )


def _create(
    store: InMemoryApprovalStore,
    *,
    tenant_id: str = "tenant-a",
    approval_id: str = "approval-1",
    due_by: datetime | None = _DUE_BY,
    quorum: int = 1,
) -> ApprovalRecord:
    result = store.create(
        ApprovalCreate(
            tenant_id=tenant_id,
            approval_id=approval_id,
            requested_at=_REQUESTED_AT,
            subject=_subject(tenant_id=tenant_id, due_by=due_by, quorum=quorum),
        )
    )
    return result.record


class _RecordingObserver:
    def __init__(self) -> None:
        self.expired: list[ApprovalRecord] = []

    def on_expired(self, record: ApprovalRecord) -> None:
        self.expired.append(record)


def _sweeper(
    store: InMemoryApprovalStore,
    now: datetime,
    observer: _RecordingObserver | None = None,
) -> ApprovalExpirySweeper:
    return ApprovalExpirySweeper(
        scanner=store,
        approvals=store,
        clock=lambda: now,
        observer=observer,
    )


def test_overdue_waiting_approval_is_expired_at_due_by() -> None:
    store = InMemoryApprovalStore()
    _create(store)
    observer = _RecordingObserver()
    now = _DUE_BY + timedelta(hours=1)

    stats = _sweeper(store, now, observer).run_once()

    assert stats == ExpirySweepStats(scanned=1, expired=1, skipped=0)
    record = store.get(tenant_id="tenant-a", approval_id="approval-1")
    assert record is not None
    assert record.status is ApprovalStatus.EXPIRED
    assert record.decision is not None
    # The decision instant is exactly due_by, not the wall clock of the sweep.
    assert record.decision.decided_at == _DUE_BY
    assert record.decision.actor == EXPIRY_ACTOR
    assert record.decision.reason_code == EXPIRY_REASON_CODE
    assert [item.approval_id for item in observer.expired] == ["approval-1"]


class _RaisingObserver:
    def on_expired(self, record: ApprovalRecord) -> None:
        raise RuntimeError("audit backend is unavailable")


def test_observer_failure_does_not_abort_the_sweep_or_undo_the_expiry() -> None:
    # The EXPIRED transition is durably committed before the observer runs; an
    # observer failure (e.g. audit DB down) must stay contained so the sweep
    # batch and the scheduler loop survive, and the expiry is not undone.
    store = InMemoryApprovalStore()
    _create(store, approval_id="approval-1")
    _create(store, approval_id="approval-2", due_by=_DUE_BY + timedelta(minutes=1))
    now = _DUE_BY + timedelta(hours=1)

    sweeper = ApprovalExpirySweeper(
        scanner=store,
        approvals=store,
        clock=lambda: now,
        observer=_RaisingObserver(),
    )
    stats = sweeper.run_once()

    # Both overdue approvals still transition to EXPIRED despite the raises.
    assert stats == ExpirySweepStats(scanned=2, expired=2, skipped=0)
    for approval_id in ("approval-1", "approval-2"):
        record = store.get(tenant_id="tenant-a", approval_id=approval_id)
        assert record is not None and record.status is ApprovalStatus.EXPIRED
    # A re-sweep is a pure no-op: terminal records left the waiting set.
    assert sweeper.run_once() == ExpirySweepStats(scanned=0, expired=0, skipped=0)


def test_not_yet_due_is_left_waiting() -> None:
    store = InMemoryApprovalStore()
    _create(store)
    observer = _RecordingObserver()
    now = _DUE_BY - timedelta(minutes=1)

    stats = _sweeper(store, now, observer).run_once()

    assert stats == ExpirySweepStats(scanned=0, expired=0, skipped=0)
    record = store.get(tenant_id="tenant-a", approval_id="approval-1")
    assert record is not None and record.status is ApprovalStatus.WAITING_APPROVAL
    assert observer.expired == []


def test_subject_without_due_by_never_expires() -> None:
    store = InMemoryApprovalStore()
    _create(store, due_by=None)
    now = _DUE_BY + timedelta(days=365)

    stats = _sweeper(store, now).run_once()

    assert stats == ExpirySweepStats(scanned=0, expired=0, skipped=0)
    record = store.get(tenant_id="tenant-a", approval_id="approval-1")
    assert record is not None and record.status is ApprovalStatus.WAITING_APPROVAL


def test_resweep_is_idempotent_and_observer_fires_once() -> None:
    store = InMemoryApprovalStore()
    _create(store)
    observer = _RecordingObserver()
    now = _DUE_BY + timedelta(hours=1)
    sweeper = _sweeper(store, now, observer)

    first = sweeper.run_once()
    second = sweeper.run_once()

    assert first == ExpirySweepStats(scanned=1, expired=1, skipped=0)
    # The terminal record has left the waiting set: the scanner returns nothing.
    assert second == ExpirySweepStats(scanned=0, expired=0, skipped=0)
    assert len(observer.expired) == 1


def test_already_decided_approval_is_not_expired() -> None:
    store = InMemoryApprovalStore()
    record = _create(store)
    store.decide(
        ApprovalDecisionCommand(
            tenant_id="tenant-a",
            approval_id="approval-1",
            expected_revision=record.revision,
            decision_id="human-approval",
            outcome=ApprovalStatus.APPROVED,
            actor="user:approver-9",
            reason_code="approval.policy_satisfied",
            decided_at=_DUE_BY - timedelta(hours=1),
        )
    )
    observer = _RecordingObserver()
    now = _DUE_BY + timedelta(hours=1)

    stats = _sweeper(store, now, observer).run_once()

    # Terminal (approved) record is outside the waiting set entirely.
    assert stats == ExpirySweepStats(scanned=0, expired=0, skipped=0)
    final = store.get(tenant_id="tenant-a", approval_id="approval-1")
    assert final is not None and final.status is ApprovalStatus.APPROVED
    assert observer.expired == []


def test_revision_advanced_between_scan_and_decide_is_skipped() -> None:
    # A quorum-2 Approval accumulates one approval (still waiting) after the
    # scanner captured it at revision 1; the sweeper's expected_revision is now
    # stale and the transition is skipped, not an error.
    store = InMemoryApprovalStore()
    stale = _create(store, quorum=2)
    now = _DUE_BY + timedelta(hours=1)

    class _AdvancingScanner:
        def due_waiting(self, *, now: datetime, batch_size: int):
            # Return the pre-advance snapshot (revision 1), then advance the
            # real record so the sweeper's expected_revision is stale.
            store.decide(
                ApprovalDecisionCommand(
                    tenant_id="tenant-a",
                    approval_id="approval-1",
                    expected_revision=stale.revision,
                    decision_id="human-approval-1",
                    outcome=ApprovalStatus.APPROVED,
                    actor="user:approver-1",
                    reason_code="approval.policy_satisfied",
                    decided_at=_DUE_BY - timedelta(hours=1),
                )
            )
            return (stale,)

    sweeper = ApprovalExpirySweeper(
        scanner=_AdvancingScanner(),
        approvals=store,
        clock=lambda: now,
    )
    stats = sweeper.run_once()

    assert stats == ExpirySweepStats(scanned=1, expired=0, skipped=1)
    record = store.get(tenant_id="tenant-a", approval_id="approval-1")
    # Still waiting for its second approval — the stale expiry did not land.
    assert record is not None and record.status is ApprovalStatus.WAITING_APPROVAL


def test_scan_is_cross_tenant_and_ordered_by_deadline() -> None:
    store = InMemoryApprovalStore()
    _create(store, tenant_id="tenant-b", approval_id="late", due_by=_DUE_BY)
    _create(
        store,
        tenant_id="tenant-a",
        approval_id="early",
        due_by=_DUE_BY - timedelta(hours=2),
    )
    now = _DUE_BY + timedelta(hours=1)

    due = store.due_waiting(now=now, batch_size=10)

    assert [record.approval_id for record in due] == ["early", "late"]

    stats = _sweeper(store, now).run_once()
    assert stats == ExpirySweepStats(scanned=2, expired=2, skipped=0)


def test_batch_size_bounds_the_sweep() -> None:
    store = InMemoryApprovalStore()
    for index in range(3):
        _create(
            store,
            approval_id=f"approval-{index}",
            due_by=_DUE_BY + timedelta(minutes=index),
        )
    now = _DUE_BY + timedelta(hours=1)

    stats = _sweeper(store, now).run_once(batch_size=2)

    assert stats.scanned == 2
    assert stats.expired == 2
    remaining = [
        record
        for record in store.list(tenant_id="tenant-a")
        if record.status is ApprovalStatus.WAITING_APPROVAL
    ]
    assert len(remaining) == 1


def test_due_by_before_request_is_rejected_at_creation() -> None:
    import pytest
    from pydantic import ValidationError

    store = InMemoryApprovalStore()
    with pytest.raises(ValidationError):
        store.create(
            ApprovalCreate(
                tenant_id="tenant-a",
                approval_id="approval-1",
                requested_at=_REQUESTED_AT,
                subject=_subject(due_by=_REQUESTED_AT - timedelta(seconds=1)),
            )
        )

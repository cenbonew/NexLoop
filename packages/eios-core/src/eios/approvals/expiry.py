from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from eios.approvals.models import (
    ApprovalDecisionCommand,
    ApprovalRecord,
    ApprovalStatus,
)
from eios.approvals.ports import (
    ApprovalError,
    ApprovalPort,
)


# The system principal that owns SLA-driven expiry transitions. Recorded as the
# expiry decision's actor so the audit trail names the sweeper, not a human.
EXPIRY_ACTOR = "system.approval-expiry"
# Stable dotted reason code (satisfies ApprovalDecisionCommand's reason vocab).
EXPIRY_REASON_CODE = "sla.expired"
_MAX_BATCH_SIZE = 1000


@runtime_checkable
class DueApprovalScanner(Protocol):
    """Cross-tenant scan for still-waiting Approvals past their SLA deadline.

    Implementations MUST return only records whose status is
    ``WAITING_APPROVAL`` and whose subject carries a ``due_by`` at or before
    ``now``. Terminal records leave the waiting set forever, so the scanner is
    the primary idempotency boundary: an expired Approval is never returned
    again, and a re-sweep is a genuine no-op rather than a replay.
    """

    def due_waiting(
        self, *, now: datetime, batch_size: int
    ) -> tuple[ApprovalRecord, ...]: ...


@runtime_checkable
class ApprovalExpiryObserver(Protocol):
    """Side-effect sink invoked once per genuine expiry transition.

    The composition root maps this to a first-class audit event
    (``approval.expired``) and a transactional notification enqueue. It is
    called only when a waiting Approval actually transitions to EXPIRED in this
    sweep — never on a replay or a concurrency skip — so downstream delivery is
    naturally deduplicated by the terminal transition itself.
    """

    def on_expired(self, record: ApprovalRecord) -> None: ...


@dataclass(frozen=True)
class ExpirySweepStats:
    scanned: int = 0
    expired: int = 0
    skipped: int = 0


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


class ApprovalExpirySweeper:
    """Idempotently transition SLA-expired waiting Approvals to EXPIRED.

    Mirrors ``RecoveryService``: a scheduler sector with a bounded ``run_once``
    that performs no human decision and publishes no application work. Each
    expiry is enacted through the ordinary decision Port with a synthetic
    system decision, so every waiting→terminal invariant (revision fencing,
    duplicate-decision replay, quorum bookkeeping) is reused rather than
    duplicated. The decision instant is the subject's ``due_by``, making the
    synthetic decision deterministic and any re-application a byte-identical
    replay.
    """

    def __init__(
        self,
        *,
        scanner: DueApprovalScanner,
        approvals: ApprovalPort,
        clock: Callable[[], datetime],
        observer: ApprovalExpiryObserver | None = None,
    ) -> None:
        self._scanner = scanner
        self._approvals = approvals
        self._clock = clock
        self._observer = observer

    @staticmethod
    def _batch_size(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("batch_size must be a positive integer")
        return min(value, _MAX_BATCH_SIZE)

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), "expiry clock")

    def run_once(self, *, batch_size: int = 100) -> ExpirySweepStats:
        now = self._now()
        due = self._scanner.due_waiting(
            now=now, batch_size=self._batch_size(batch_size)
        )
        expired = 0
        skipped = 0
        for record in due:
            if self._expire_one(record, now=now):
                expired += 1
            else:
                skipped += 1
        return ExpirySweepStats(scanned=len(due), expired=expired, skipped=skipped)

    def _expire_one(self, record: ApprovalRecord, *, now: datetime) -> bool:
        # Defense in depth: trust the scanner for discovery but re-verify the
        # eligibility predicate here so a mis-behaving scanner can never drive a
        # spurious terminal transition.
        if type(record) is not ApprovalRecord:
            return False
        subject = record.subject
        if (
            record.status is not ApprovalStatus.WAITING_APPROVAL
            or subject is None
            or subject.due_by is None
            or subject.due_by > now
        ):
            return False

        command = ApprovalDecisionCommand(
            tenant_id=record.tenant_id,
            approval_id=record.approval_id,
            expected_revision=record.revision,
            decision_id=f"sla-expiry:{record.approval_id}",
            outcome=ApprovalStatus.EXPIRED,
            actor=EXPIRY_ACTOR,
            reason_code=EXPIRY_REASON_CODE,
            decided_at=subject.due_by,
        )
        try:
            result = self._approvals.decide(command)
        except ApprovalError:
            # Revision conflict (a concurrent approval bumped the record),
            # terminal (another decider won), or contract error: leave it for a
            # later cycle or accept it is already resolved. Never a sweep error.
            return False

        if result.replayed or result.record.status is not ApprovalStatus.EXPIRED:
            # Already expired (replay) or the accumulated decision left it
            # waiting/otherwise-terminal — not a fresh expiry this sweep.
            return False

        # The EXPIRED transition is already durably committed by decide(). The
        # observer side effect (audit / notification) is best-effort and MUST
        # NOT propagate: the record is now terminal so due_waiting never
        # re-surfaces it, and an uncaught error here would abort the sweep batch
        # and crash the scheduler loop (run_forever only catches configuration
        # errors). This mirrors the lineage producer's advisory-side-effect
        # contract. A durable per-expiry audit awaits the transactional
        # notification outbox (enqueued in the decide transaction).
        if self._observer is not None:
            try:
                self._observer.on_expired(result.record)
            except Exception:
                pass
        return True

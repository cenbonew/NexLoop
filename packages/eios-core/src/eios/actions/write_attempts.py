"""Runtime ledger for one governed external write attempt.

One row records the full lifecycle of a single side-effecting provider write
(started → completed/rejected/unknown, with one optional safe retry). This is
adapter bookkeeping — concurrency fencing, replay detection and orphan
reconciliation — so it lives on the runtime plane next to
``runtime.action_claims`` instead of inside world-model objects. Only a
genuinely ambiguous outcome escalates into an ontology SyncIssue; the ledger
then records the raised issue's id and stays terminal.

State machine (mirrors the yundong8 adapter's frozen semantics):

    STARTED ──► COMPLETED | REJECTED | UNKNOWN | SAFE_TO_RETRY
            └─► ORPHAN_CONFLICT      (orphan preconditions no longer hold)
    SAFE_TO_RETRY ──► RETRY_CLAIMED | COMPLETED
                                     (exactly one retry, token-fenced; a
                                      reconciled replay may confirm directly)
    RETRY_CLAIMED ──► COMPLETED | REJECTED | UNKNOWN
                    | RETRY_EXHAUSTED | RETRY_UNKNOWN | RETRY_CONFLICT
    UNKNOWN ──► COMPLETED | REJECTED | UNKNOWN
                                     (an escalated-but-open outcome is
                                      reconciliation-eligible: a later replay
                                      may confirm, reject, or re-mark it)

COMPLETED / REJECTED / RETRY_EXHAUSTED / RETRY_UNKNOWN / RETRY_CONFLICT /
ORPHAN_CONFLICT are terminal. Every transition is a compare-and-swap on
``attempt_revision``; a lost race surfaces as
:class:`ExternalWriteAttemptRevisionConflict` and the caller re-reads. A
``STARTED`` row observed by a later execution is an orphan (the writer died
mid-flight); ``retry_preconditions`` carries the snapshot needed to
reconcile it safely.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, field_validator

from eios.ontology.definitions import FrozenContract

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")


class ExternalWriteAttemptError(RuntimeError):
    """Base class for stable, fail-closed ledger errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExternalWriteAttemptContractError(ExternalWriteAttemptError):
    """A command violates the frozen ledger contract."""


class ExternalWriteAttemptNotFound(ExternalWriteAttemptError):
    """No ledger row exists for the exact attempt key."""


class ExternalWriteAttemptRevisionConflict(ExternalWriteAttemptError):
    """The CAS fence does not match the current attempt revision."""


class ExternalWriteAttemptTransitionError(ExternalWriteAttemptError):
    """The requested transition is not legal from the current state."""


class ExternalWriteAttemptState(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    REJECTED = "rejected"
    # Ambiguous external outcome, escalated to a real world-model SyncIssue
    # whose id the row records. Reconciliation-eligible: a later replay may
    # still confirm, reject, or re-mark it.
    UNKNOWN = "unknown"
    SAFE_TO_RETRY = "safe_to_retry"
    RETRY_CLAIMED = "retry_claimed"
    RETRY_EXHAUSTED = "retry_exhausted"
    # The claimed retry's preconditions could not be revalidated: ambiguous
    # (escalated like UNKNOWN, but replay-terminal) vs. cleanly conflicted
    # (nothing was written; a new request is required).
    RETRY_UNKNOWN = "retry_unknown"
    RETRY_CONFLICT = "retry_conflict"
    # An orphaned STARTED row whose recorded preconditions no longer hold.
    ORPHAN_CONFLICT = "orphan_conflict"


_TERMINAL_STATES = frozenset(
    {
        ExternalWriteAttemptState.COMPLETED,
        ExternalWriteAttemptState.REJECTED,
        ExternalWriteAttemptState.RETRY_EXHAUSTED,
        ExternalWriteAttemptState.RETRY_UNKNOWN,
        ExternalWriteAttemptState.RETRY_CONFLICT,
        ExternalWriteAttemptState.ORPHAN_CONFLICT,
    }
)

# Legal source states for finish (complete/reject) and unknown marking; the
# ledger fails closed on anything else. UNKNOWN itself stays writable so a
# reconciling replay can settle or re-mark an escalated-but-open outcome.
_WRITABLE_STATES = frozenset(
    {
        ExternalWriteAttemptState.STARTED,
        ExternalWriteAttemptState.RETRY_CLAIMED,
        ExternalWriteAttemptState.UNKNOWN,
    }
)


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


class ExternalWriteAttemptKey(FrozenContract):
    """Deterministic identity: one governed request digest per tenant per target.

    ``target_system`` is part of the key so a single governed request that fans
    a write out to more than one external system (韵动8 and 易馆云) records one
    ledger row per system instead of colliding on a shared (tenant, digest).
    """

    tenant_id: str
    request_digest: str = Field(pattern=_SHA256_PATTERN)
    target_system: str

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant(cls, value: str) -> str:
        clean = value.strip()
        if not clean or clean != value:
            raise ValueError("tenant_id must be non-empty and trimmed")
        return clean

    @field_validator("target_system")
    @classmethod
    def _validate_target_system(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("target_system must be non-empty and trimmed")
        return value


class ExternalWriteAttempt(FrozenContract):
    """One immutable snapshot of a ledger row."""

    key: ExternalWriteAttemptKey
    action_stable_name: str
    attempt_kind: str
    target_system: str
    marker: str
    request_target_identity: dict[str, Any] = Field(default_factory=dict)
    external_units: tuple[str, ...] = ()
    facility_block_id: str | None = None
    retry_preconditions: dict[str, Any] = Field(default_factory=dict)
    state: ExternalWriteAttemptState
    attempt_revision: int = Field(ge=1)
    retry_count: int = Field(ge=0, le=1)
    retry_claim_token: str | None = None
    sync_issue_id: str | None = None
    detected_at: datetime
    updated_at: datetime

    @field_validator("action_stable_name")
    @classmethod
    def _validate_stable_name(cls, value: str) -> str:
        if not _STABLE_NAME_RE.fullmatch(value):
            raise ValueError("action_stable_name is invalid")
        return value

    @field_validator("attempt_kind", "target_system", "marker")
    @classmethod
    def _validate_non_blank(cls, value: str, info) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(f"{info.field_name} must be non-empty and trimmed")
        return value

    @field_validator("detected_at", "updated_at")
    @classmethod
    def _validate_aware(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


class ExternalWriteAttemptStartCommand(FrozenContract):
    """Get-or-create the ledger row before touching the external system."""

    key: ExternalWriteAttemptKey
    action_stable_name: str
    attempt_kind: str
    target_system: str
    marker: str
    request_target_identity: dict[str, Any] = Field(default_factory=dict)
    external_units: tuple[str, ...] = ()
    facility_block_id: str | None = None
    retry_preconditions: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime

    @field_validator("action_stable_name")
    @classmethod
    def _validate_stable_name(cls, value: str) -> str:
        if not _STABLE_NAME_RE.fullmatch(value):
            raise ValueError("action_stable_name is invalid")
        return value

    @field_validator("attempt_kind", "target_system", "marker")
    @classmethod
    def _validate_non_blank(cls, value: str, info) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(f"{info.field_name} must be non-empty and trimmed")
        return value

    @field_validator("started_at")
    @classmethod
    def _validate_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "started_at")


class ExternalWriteAttemptTransitionCommand(FrozenContract):
    """One CAS-fenced transition on an existing ledger row."""

    key: ExternalWriteAttemptKey
    expected_revision: int = Field(ge=1)
    occurred_at: datetime
    # UNKNOWN escalation payload: the raised world-model issue plus the last
    # observed external units (the reconciliation evidence).
    sync_issue_id: str | None = None
    external_units: tuple[str, ...] | None = None
    # RETRY_CLAIMED payload: the caller-generated fence for the single retry.
    retry_claim_token: str | None = None
    # COMPLETED payload for a block creation: the created block's object id,
    # recorded so a later unblock can resolve its creating attempt.
    facility_block_id: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _validate_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


@runtime_checkable
class ExternalWriteAttemptStore(Protocol):
    """Runtime-plane ledger port for governed external write attempts."""

    def start(
        self, command: ExternalWriteAttemptStartCommand
    ) -> ExternalWriteAttempt: ...

    def get(self, key: ExternalWriteAttemptKey) -> ExternalWriteAttempt | None: ...

    def find_by_facility_block(
        self,
        tenant_id: str,
        facility_block_id: str,
        *,
        action_stable_name: str | None = None,
    ) -> ExternalWriteAttempt | None: ...

    def find_creators_by_target_window(
        self,
        tenant_id: str,
        *,
        action_stable_name: str,
        target_system: str,
        start_at: datetime,
        court_id: str,
    ) -> tuple[ExternalWriteAttempt, ...]: ...

    def complete(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def reject(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def mark_unknown(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def mark_safe_to_retry(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def claim_retry(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def mark_retry_exhausted(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def mark_retry_unknown(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def mark_retry_conflict(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...

    def mark_orphan_conflict(
        self, command: ExternalWriteAttemptTransitionCommand
    ) -> ExternalWriteAttempt: ...


def validate_transition(
    current: ExternalWriteAttempt,
    command: ExternalWriteAttemptTransitionCommand,
    *,
    target: ExternalWriteAttemptState,
) -> dict[str, Any]:
    """Shared fail-closed transition rules; returns the model update payload.

    Both store implementations funnel through this so the state machine has
    exactly one definition. Raises the stable ledger errors on violation.
    """

    if current.attempt_revision != command.expected_revision:
        raise ExternalWriteAttemptRevisionConflict(
            "external_write_attempt_revision_conflict",
            "expected revision does not match the current attempt revision",
        )

    update: dict[str, Any] = {
        "state": target,
        "attempt_revision": current.attempt_revision + 1,
        "updated_at": command.occurred_at,
    }

    if target is ExternalWriteAttemptState.COMPLETED:
        # A reconciled replay may confirm directly from SAFE_TO_RETRY (the
        # external state turned out to be the target after all).
        allowed = _WRITABLE_STATES | {ExternalWriteAttemptState.SAFE_TO_RETRY}
        if current.state not in allowed:
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                f"cannot complete an attempt from state {current.state.value}",
            )
        if command.facility_block_id is not None:
            update["facility_block_id"] = command.facility_block_id
        return update

    if target is ExternalWriteAttemptState.REJECTED:
        if current.state not in _WRITABLE_STATES:
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                f"cannot reject an attempt from state {current.state.value}",
            )
        return update

    if target in (
        ExternalWriteAttemptState.UNKNOWN,
        ExternalWriteAttemptState.RETRY_UNKNOWN,
    ):
        # A safe-to-retry replay may itself hit an ambiguous outcome (lease
        # lost, unreadable state) and escalate without claiming the retry.
        sources = (
            _WRITABLE_STATES | {ExternalWriteAttemptState.SAFE_TO_RETRY}
            if target is ExternalWriteAttemptState.UNKNOWN
            else frozenset({ExternalWriteAttemptState.RETRY_CLAIMED})
        )
        if current.state not in sources:
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                f"cannot mark {target.value} from state {current.state.value}",
            )
        issue = (command.sync_issue_id or "").strip()
        if not issue:
            raise ExternalWriteAttemptContractError(
                "external_write_attempt_command_invalid",
                "an unknown outcome requires the escalated sync issue id",
            )
        update["sync_issue_id"] = issue
        if command.external_units is not None:
            update["external_units"] = tuple(command.external_units)
        return update

    if target is ExternalWriteAttemptState.SAFE_TO_RETRY:
        if (
            current.state is not ExternalWriteAttemptState.STARTED
            or current.retry_count != 0
        ):
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                "only an unclaimed started attempt can become safe to retry",
            )
        return update

    if target is ExternalWriteAttemptState.RETRY_CLAIMED:
        if current.state is not ExternalWriteAttemptState.SAFE_TO_RETRY:
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                "only a safe-to-retry attempt can be claimed",
            )
        token = (command.retry_claim_token or "").strip()
        if not token:
            raise ExternalWriteAttemptContractError(
                "external_write_attempt_command_invalid",
                "claiming the retry requires a caller-generated token",
            )
        update["retry_count"] = 1
        update["retry_claim_token"] = token
        return update

    if target in (
        ExternalWriteAttemptState.RETRY_EXHAUSTED,
        ExternalWriteAttemptState.RETRY_CONFLICT,
    ):
        if current.state is not ExternalWriteAttemptState.RETRY_CLAIMED:
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                f"only a claimed retry can become {target.value}",
            )
        return update

    if target is ExternalWriteAttemptState.ORPHAN_CONFLICT:
        if current.state is not ExternalWriteAttemptState.STARTED:
            raise ExternalWriteAttemptTransitionError(
                "external_write_attempt_transition_invalid",
                "only an orphaned started attempt can conflict",
            )
        issue = (command.sync_issue_id or "").strip()
        if issue:
            update["sync_issue_id"] = issue
        return update

    raise ExternalWriteAttemptContractError(  # pragma: no cover - enum total
        "external_write_attempt_command_invalid",
        f"unsupported transition target {target.value}",
    )


__all__ = [
    "ExternalWriteAttempt",
    "ExternalWriteAttemptContractError",
    "ExternalWriteAttemptError",
    "ExternalWriteAttemptKey",
    "ExternalWriteAttemptNotFound",
    "ExternalWriteAttemptRevisionConflict",
    "ExternalWriteAttemptStartCommand",
    "ExternalWriteAttemptState",
    "ExternalWriteAttemptStore",
    "ExternalWriteAttemptTransitionCommand",
    "ExternalWriteAttemptTransitionError",
    "validate_transition",
]

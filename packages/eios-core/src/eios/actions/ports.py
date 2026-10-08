from __future__ import annotations

from typing import Protocol, runtime_checkable

from eios.actions.models import (
    ActionClaim,
    ActionClaimFinalizeCommand,
    ActionClaimRequest,
    ActionClaimResult,
    ActionClaimRetryableCommand,
)


class ActionClaimError(ValueError):
    """Stable fail-closed Action claim error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActionClaimContractError(ActionClaimError):
    """A claim command violates the exact frozen contract."""


class ActionClaimNotFoundError(ActionClaimError):
    """No claim exists for the exact tenant-bound reservation key."""


class ActionClaimBindingConflictError(ActionClaimError):
    """A claim update supplied a different immutable binding."""


class ActionClaimRevisionConflictError(ActionClaimError):
    """A claim update supplied a stale ownership revision."""


class ActionClaimStaleFenceError(ActionClaimError):
    """A claim update supplied a non-current fencing token."""


class ActionClaimTransitionError(ActionClaimError):
    """A claim update is illegal for the current claim state."""


class ActionClaimOutcomeConflictError(ActionClaimError):
    """A terminal claim was finalized with changed immutable outcome content."""


@runtime_checkable
class ActionClaimPort(Protocol):
    def reserve(self, command: ActionClaimRequest) -> ActionClaimResult: ...

    def mark_retryable(self, command: ActionClaimRetryableCommand) -> ActionClaim: ...

    def finalize(self, command: ActionClaimFinalizeCommand) -> ActionClaim: ...

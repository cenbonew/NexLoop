from __future__ import annotations

from typing import Protocol, runtime_checkable

from eios.approvals.models import (
    ApprovalCreate,
    ApprovalCreateResult,
    ApprovalDecisionCommand,
    ApprovalDecisionResult,
    ApprovalRecord,
)


class ApprovalError(ValueError):
    """Stable fail-closed Approval Port error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalContractError(ApprovalError):
    """An Approval command or query violates its frozen contract."""


class ApprovalIdentityConflictError(ApprovalError):
    """An Approval identity was reused with a different creation envelope."""


class ApprovalRevisionConflictError(ApprovalError):
    """The decision expected revision is stale or otherwise incorrect."""


class ApprovalDecisionConflictError(ApprovalError):
    """A decision ID was reused with different immutable decision content."""


class ApprovalTransitionError(ApprovalError):
    """The requested Approval state transition is not legal."""


class ApprovalNotFoundError(ApprovalError):
    """No Approval exists for the exact tenant-bound identity."""


class ApprovalDeciderNotEligibleError(ApprovalError):
    """The decider fails the subject's approver policy (HTTP 403)."""


class ApprovalHumanAuthorityUnavailable(RuntimeError):
    """No fresh human authority satisfies the Approval boundary."""


class ApprovalHumanAuthorityVerifier:
    """Structural verifier contract used by Action and Approval consumers."""

    def assert_fresh(self, record: ApprovalRecord) -> None:
        raise NotImplementedError


class InMemoryApprovalHumanAuthorityVerifier:
    """Validate immutable bindings against the active in-memory application."""

    def __init__(
        self,
        *,
        application_id: str | None = None,
        application_version: str | None = None,
        policy_approvers: frozenset[str] = frozenset(),
    ) -> None:
        self._application_id = application_id
        self._application_version = application_version
        # In-memory twin of authz.delegated_policy_approvers: actors listed
        # here may carry policy auto-approvals without a human authority.
        self._policy_approvers = frozenset(policy_approvers)

    def assert_fresh(self, record: ApprovalRecord) -> None:
        from eios.approvals.models import is_policy_auto_approved

        decisions = record.decisions or (
            () if record.decision is None else (record.decision,)
        )
        approvals = [
            item for item in decisions if item.outcome.value == "approved"
        ]
        if not approvals:
            raise ApprovalHumanAuthorityUnavailable()
        for decision in approvals:
            authority = decision.human_authority
            if (
                is_policy_auto_approved(decision)
                and decision.actor in self._policy_approvers
            ):
                continue
            if (
                authority is None
                or authority.tenant_id != record.tenant_id
                or authority.actor != decision.actor
                or authority.subject_kind != "human"
                or not authority.eligible_scopes
                or (
                    self._application_id is not None
                    and authority.application_id != self._application_id
                )
                or (
                    self._application_version is not None
                    and authority.application_version != self._application_version
                )
            ):
                raise ApprovalHumanAuthorityUnavailable()


@runtime_checkable
class ApprovalPort(Protocol):
    def create(self, command: ApprovalCreate) -> ApprovalCreateResult: ...

    def decide(self, command: ApprovalDecisionCommand) -> ApprovalDecisionResult: ...

    def get(self, *, tenant_id: str, approval_id: str) -> ApprovalRecord | None: ...

    def list(self, *, tenant_id: str) -> tuple[ApprovalRecord, ...]: ...

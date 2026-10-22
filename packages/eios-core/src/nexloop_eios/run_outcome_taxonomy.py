"""EIOS gap B5 (NX-061): the outcome classes of a Run or effect, and which of them a failure rate counts.

Rewritten from upstream src/eios/orchestration/run_outcome_taxonomy.py (frozen 1e363db, RunOutcomeClass and its rule "a failure
rate counts transient, authorization and defect only"); the upstream code table is domain codes and is not carried over. The
NexLoop table classifies the dispatch refusal codes (control.nexloop_dispatch_refusal_codes); migration 0324 seeds the same table in
SQL (control.nexloop_outcome_code_classes) and the metrics snapshot's `outcomes` block counts by it. A refusal by rule (the owner's
pause, a contact restriction, a takeover, an exhausted budget) is the system working as intended, never a failure; a code without a
class is UNRESOLVED and counts as a failure (fail closed: a new code must be classified to leave the failure rate).
NX-057 R0 (migration 0325): the reason of a final effect failure (runtime.nexloop_effect_failures) has its class too; a refused
one-reply intent is counted under its refusal's code, never a second time as a failure.
"""
from __future__ import annotations

from enum import Enum
from types import MappingProxyType


class RunOutcomeClass(str, Enum):
    SUCCEEDED = 'succeeded'
    """Done (an idempotent replay included)."""
    NO_OP = 'no_op'
    """Nothing to do: the target state already held."""
    SUPERSEDED = 'superseded'
    """A newer control, goal or object version took over; the next evaluation acts on it."""
    CONFLICT_PROTECTED = 'conflict_protected'
    """Refused by rule: the state read did not allow it, so nothing was written. Not a failure."""
    OUTCOME_UNKNOWN = 'outcome_unknown'
    """Sent but not confirmed: the outside world may have changed. Reconcile first; neither success nor failure."""
    TRANSIENT = 'transient'
    """Unavailable for now; a retry heals it."""
    AUTHORIZATION = 'authorization'
    """Deterministic refusal by missing configuration or authority; a retry gives the same answer."""
    DEFECT = 'defect'
    """A failure with no rule behind it (a failed effect, a dead letter): look at the code."""
    UNRESOLVED = 'unresolved'
    """A code without a class: counted as a failure until it is classified."""


#: The classes a failure rate counts.
FAILURE_CLASSES = frozenset({RunOutcomeClass.TRANSIENT, RunOutcomeClass.AUTHORIZATION, RunOutcomeClass.DEFECT, RunOutcomeClass.UNRESOLVED})

CODE_CLASSES = MappingProxyType({
    'NXC01': RunOutcomeClass.CONFLICT_PROTECTED,  # control_paused: the owner's pause holds
    'NXC02': RunOutcomeClass.SUPERSEDED,          # control_revision_stale
    'NXC03': RunOutcomeClass.SUPERSEDED,          # goal_version_stale
    'NXC04': RunOutcomeClass.SUPERSEDED,          # object_revision_stale
    'NXC05': RunOutcomeClass.CONFLICT_PROTECTED,  # contact_restricted
    'NXC06': RunOutcomeClass.CONFLICT_PROTECTED,  # taken_over: a person handles the conversation
    'NXC07': RunOutcomeClass.CONFLICT_PROTECTED,  # real_dispatch_closed: the deployment switch is off
    'NXB01': RunOutcomeClass.CONFLICT_PROTECTED,  # budget_exhausted: the cost cap holds
    'NXB02': RunOutcomeClass.TRANSIENT,           # budget_consumption_conflict: a concurrent consumption, retried
    'NXB03': RunOutcomeClass.AUTHORIZATION,       # budget_unconfigured
    'NXM01': RunOutcomeClass.CONFLICT_PROTECTED,  # observation_conflict: the observed state does not match, nothing written
    'NXR01': RunOutcomeClass.CONFLICT_PROTECTED,  # restore_unverified: real dispatch waits for the restore verification
    'NXE01': RunOutcomeClass.AUTHORIZATION,       # effect_route_unconfigured: no provider route for the Action
})


#: NX-057 R0 (0325): the reason of a final effect failure (runtime.nexloop_effect_failures.reason_code). A refused one-reply intent
#: ('refused_one_reply:<reason>') is counted under its refusal's code and has no class of its own here.
FAILURE_REASON_CLASSES = MappingProxyType({
    'origin_expired': RunOutcomeClass.DEFECT,           # 0174: the origin Run expired before any dispatch; nothing was sent
    'confirmed_not_delivered': RunOutcomeClass.DEFECT,  # 0210: the owner confirmed with evidence that it was not delivered
})
REFUSED_ONE_REPLY = 'refused_one_reply:'


def classify_failure(reason: str) -> RunOutcomeClass | None:
    """The class a failure rate counts a final effect failure under; None when its refusal already counts it."""
    if reason.startswith(REFUSED_ONE_REPLY):
        return None
    return FAILURE_REASON_CLASSES.get(reason, RunOutcomeClass.UNRESOLVED)


def classify(code: str | None) -> RunOutcomeClass:
    return CODE_CLASSES.get(code, RunOutcomeClass.UNRESOLVED) if code else RunOutcomeClass.UNRESOLVED


def counts_as_failure(outcome: RunOutcomeClass) -> bool:
    return outcome in FAILURE_CLASSES


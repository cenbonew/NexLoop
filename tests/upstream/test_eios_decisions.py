from __future__ import annotations

from datetime import UTC, datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from eios.authz.decisions import AuthorizationDecision, DecisionOutcome
from eios.authz.operations import Operation


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)


def _decision(**changes: object) -> AuthorizationDecision:
    fields: dict[str, object] = {
        "decision_id": "decision-1",
        "outcome": DecisionOutcome.DENY,
        "reason_codes": ("resource_not_allowed",),
        "missing_operations": (Operation.READ,),
        "policy_revision": 7,
        "expires_at": NOW,
    }
    fields.update(changes)
    return AuthorizationDecision(**fields)


def test_operation_vocabulary_is_frozen() -> None:
    assert {item.value for item in Operation} == {
        "discover",
        "view_metadata",
        "read",
        "create",
        "edit",
        "delete",
        "execute",
        "approve",
        "delegate",
        "manage_permissions",
        "manage_policy",
        "export",
        "identity.user.invite",
        "identity.user.invitation.reissue",
        "identity.user.invitation.revoke",
        "identity.user.disable",
        "identity.user.restore",
        "identity.user.force-reset",
        "identity.user.recovery-codes.regenerate",
    }


def test_decision_outcomes_are_closed() -> None:
    assert {item.value for item in DecisionOutcome} == {
        "allow",
        "deny",
        "mask",
        "waiting_approval",
    }


def test_decision_fields_and_defaults_match_the_frozen_plan() -> None:
    decision = _decision()
    assert set(type(decision).model_fields) == {
        "decision_id",
        "outcome",
        "reason_codes",
        "matched_grants",
        "matched_policies",
        "missing_operations",
        "missing_controls",
        "required_approvals",
        "obligations",
        "policy_revision",
        "expires_at",
    }
    assert decision.matched_grants == ()
    assert decision.matched_policies == ()
    assert decision.missing_controls == ()
    assert decision.required_approvals == ()
    assert decision.obligations == ()


def test_decision_is_strict_frozen_and_extra_forbid() -> None:
    decision = _decision()

    with pytest.raises(ValidationError, match="frozen"):
        decision.outcome = DecisionOutcome.ALLOW
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AuthorizationDecision(**{**decision.model_dump(), "allowed": False})
    with pytest.raises(ValidationError, match="is_instance_of"):
        AuthorizationDecision(**{**decision.model_dump(), "outcome": "deny"})
    with pytest.raises(ValidationError, match="tuple_type"):
        AuthorizationDecision(**{**decision.model_dump(), "reason_codes": ["deny"]})


def test_decision_requires_utc_expiry_and_positive_policy_revision() -> None:
    plus_eight = timezone(timedelta(hours=8))
    decision = _decision(expires_at=NOW.astimezone(plus_eight))
    assert decision.expires_at.tzinfo is UTC

    with pytest.raises(ValidationError, match="timezone-aware"):
        _decision(expires_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="greater_than_equal"):
        _decision(policy_revision=0)


def test_decision_reason_and_evidence_ids_are_non_blank_and_unique() -> None:
    with pytest.raises(ValidationError, match="reason_codes"):
        _decision(reason_codes=("",))
    with pytest.raises(ValidationError, match="matched_grants"):
        _decision(matched_grants=("grant-1", "grant-1"))

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eios.authz.policy import PolicyEvaluationResult
from eios.authz.policy.registry import (
    ObligationConfigEntry,
    PolicyEffect,
    PolicyEffectReason,
    PolicyObligation,
    PolicyObligationKind,
    apply_policy_effect,
)


def _evaluation(*, matched: bool, had_error: bool) -> PolicyEvaluationResult:
    return PolicyEvaluationResult(
        matched=matched,
        had_error=had_error,
        matched_node_ids=("condition",) if matched and not had_error else (),
    )


def _approval() -> PolicyObligation:
    return PolicyObligation(
        kind=PolicyObligationKind.APPROVAL,
        config=(
            ObligationConfigEntry(name="approval_policy_id", value="four-eyes"),
            ObligationConfigEntry(name="quorum", value=2),
        ),
    )


def test_policy_effect_and_obligation_vocabularies_are_closed() -> None:
    assert tuple(effect.value for effect in PolicyEffect) == (
        "DENY_IF",
        "REQUIRE",
        "OBLIGATION_IF",
    )
    assert tuple(kind.value for kind in PolicyObligationKind) == (
        "approval",
        "mask",
        "deny_export",
        "watermark",
        "result_control",
        "max_cache_ttl",
    )

    with pytest.raises(ValidationError):
        PolicyObligation(
            kind=PolicyObligationKind.APPROVAL,
            config=(ObligationConfigEntry(name="raw_subject_facts", value="all"),),
        )
    with pytest.raises(ValidationError):
        PolicyObligation(
            kind=PolicyObligationKind.MAX_CACHE_TTL,
            config=(ObligationConfigEntry(name="seconds", value=301),),
        )
    with pytest.raises(ValidationError):
        PolicyObligation(
            kind=PolicyObligationKind.DENY_EXPORT,
            config=(ObligationConfigEntry(name="reason", value="secret"),),
        )


@pytest.mark.parametrize(
    ("effect", "matched", "had_error", "must_deny", "obligation_count", "reason"),
    (
        (PolicyEffect.DENY_IF, True, False, True, 0, PolicyEffectReason.DENY_MATCHED),
        (PolicyEffect.DENY_IF, False, False, False, 0, None),
        (
            PolicyEffect.DENY_IF,
            False,
            True,
            True,
            0,
            PolicyEffectReason.EVALUATION_ERROR,
        ),
        (
            PolicyEffect.DENY_IF,
            True,
            True,
            True,
            0,
            PolicyEffectReason.EVALUATION_ERROR,
        ),
        (PolicyEffect.REQUIRE, True, False, False, 0, None),
        (
            PolicyEffect.REQUIRE,
            False,
            False,
            True,
            0,
            PolicyEffectReason.REQUIREMENT_UNSATISFIED,
        ),
        (
            PolicyEffect.REQUIRE,
            False,
            True,
            True,
            0,
            PolicyEffectReason.EVALUATION_ERROR,
        ),
        (
            PolicyEffect.REQUIRE,
            True,
            True,
            True,
            0,
            PolicyEffectReason.EVALUATION_ERROR,
        ),
        (PolicyEffect.OBLIGATION_IF, True, False, False, 1, None),
        (PolicyEffect.OBLIGATION_IF, False, False, False, 0, None),
        (
            PolicyEffect.OBLIGATION_IF,
            False,
            True,
            True,
            0,
            PolicyEffectReason.EVALUATION_ERROR,
        ),
        (
            PolicyEffect.OBLIGATION_IF,
            True,
            True,
            True,
            0,
            PolicyEffectReason.EVALUATION_ERROR,
        ),
    ),
)
def test_policy_effect_truth_table_only_narrows_authority(
    effect: PolicyEffect,
    matched: bool,
    had_error: bool,
    must_deny: bool,
    obligation_count: int,
    reason: PolicyEffectReason | None,
) -> None:
    declared = (_approval(),) if effect is PolicyEffect.OBLIGATION_IF else ()

    decision = apply_policy_effect(
        effect=effect,
        evaluation=_evaluation(matched=matched, had_error=had_error),
        declared_obligations=declared,
    )

    assert decision.must_deny is must_deny
    assert len(decision.obligations) == obligation_count
    assert decision.reason_codes == (() if reason is None else (reason,))
    assert "allowed" not in type(decision).model_fields
    assert not {
        "matched_grants",
        "grants",
        "application_operations",
        "scopes",
        "controls",
    } & set(type(decision).model_fields)


def test_effect_rejects_unrelated_or_duplicate_obligations() -> None:
    approval = _approval()
    with pytest.raises(ValueError, match="obligations are invalid"):
        apply_policy_effect(
            effect=PolicyEffect.DENY_IF,
            evaluation=_evaluation(matched=False, had_error=False),
            declared_obligations=(approval,),
        )
    with pytest.raises(ValueError, match="obligations are invalid"):
        apply_policy_effect(
            effect=PolicyEffect.OBLIGATION_IF,
            evaluation=_evaluation(matched=True, had_error=False),
            declared_obligations=(approval, approval),
        )


def test_effect_revalidates_evaluation_and_obligations_defensively() -> None:
    forged_evaluation = PolicyEvaluationResult.model_construct(
        matched=False,
        had_error=False,
        matched_node_ids=(),
    )
    with pytest.raises(ValueError, match="policy evaluation is invalid"):
        apply_policy_effect(
            effect=PolicyEffect.REQUIRE,
            evaluation=forged_evaluation,
        )

    polluted = _approval()
    polluted.__pydantic_private__["raw_facts"] = {"clearance": "all"}
    with pytest.raises(ValueError, match="obligations are invalid"):
        apply_policy_effect(
            effect=PolicyEffect.OBLIGATION_IF,
            evaluation=_evaluation(matched=True, had_error=False),
            declared_obligations=(polluted,),
        )


def test_each_closed_obligation_schema_accepts_only_bounded_canonical_config() -> None:
    obligations = (
        _approval(),
        PolicyObligation(
            kind=PolicyObligationKind.MASK,
            config=(ObligationConfigEntry(name="mask_profile_id", value="pii"),),
        ),
        PolicyObligation(kind=PolicyObligationKind.DENY_EXPORT),
        PolicyObligation(
            kind=PolicyObligationKind.WATERMARK,
            config=(ObligationConfigEntry(name="template_id", value="internal"),),
        ),
        PolicyObligation(
            kind=PolicyObligationKind.RESULT_CONTROL,
            config=(ObligationConfigEntry(name="control_id", value="restricted"),),
        ),
        PolicyObligation(
            kind=PolicyObligationKind.MAX_CACHE_TTL,
            config=(ObligationConfigEntry(name="seconds", value=30),),
        ),
    )

    decision = apply_policy_effect(
        effect=PolicyEffect.OBLIGATION_IF,
        evaluation=_evaluation(matched=True, had_error=False),
        declared_obligations=obligations,
    )
    assert decision.obligations == obligations


def test_obligation_config_requires_one_canonical_key_order() -> None:
    with pytest.raises(ValidationError, match="canonical key order"):
        PolicyObligation(
            kind=PolicyObligationKind.APPROVAL,
            config=(
                ObligationConfigEntry(name="quorum", value=2),
                ObligationConfigEntry(
                    name="approval_policy_id",
                    value="four-eyes",
                ),
            ),
        )

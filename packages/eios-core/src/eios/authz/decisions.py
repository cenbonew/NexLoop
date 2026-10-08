from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .operations import Operation


class DecisionOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    MASK = "mask"
    WAITING_APPROVAL = "waiting_approval"


def _non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must be non-empty")
    return clean


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    decision_id: str
    outcome: DecisionOutcome
    reason_codes: tuple[str, ...]
    matched_grants: tuple[str, ...] = ()
    matched_policies: tuple[str, ...] = ()
    missing_operations: tuple[Operation, ...] = ()
    missing_controls: tuple[str, ...] = ()
    required_approvals: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    policy_revision: int = Field(ge=1)
    expires_at: datetime

    @field_validator("decision_id")
    @classmethod
    def _validate_decision_id(cls, value: str) -> str:
        return _non_blank(value, "decision_id")

    @field_validator(
        "reason_codes",
        "matched_grants",
        "matched_policies",
        "missing_controls",
        "required_approvals",
        "obligations",
    )
    @classmethod
    def _validate_evidence(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        checked = tuple(_non_blank(item, info.field_name) for item in value)
        if len(checked) != len(set(checked)):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        return checked

    @field_validator("missing_operations")
    @classmethod
    def _validate_operations(
        cls, value: tuple[Operation, ...]
    ) -> tuple[Operation, ...]:
        if len(value) != len(set(value)):
            raise ValueError("missing_operations must not contain duplicates")
        return value

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value: datetime) -> datetime:
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError("expires_at must be a timezone-aware datetime")
        return value.astimezone(UTC)


__all__ = ["AuthorizationDecision", "DecisionOutcome"]

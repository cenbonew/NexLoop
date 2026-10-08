from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def digest_identifier(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("identifier must be non-empty")
    return sha256(clean.encode("utf-8")).hexdigest()


class AuthorizationAuditRecord(BaseModel):
    """Secret-free canonical decision evidence submitted to the DB authority."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    decision_id: str
    request_id: str
    trace_id: str
    tenant_id: str
    subject_digest: str
    credential_digest: str
    application_digest: str
    agent_chain_digest: str
    target_digest: str
    dependency_digest: str
    operation: str
    requested_scopes_digest: str
    server_scopes_digest: str
    intersection_results: dict[str, bool]
    policy_evidence: tuple[str, ...]
    revision_vector_digest: str
    outcome: Literal["allow", "deny", "unavailable"]
    cache_status: Literal["miss", "hit", "bypass"]
    authoritative_now: datetime
    authoritative_expires_at: datetime

    @field_validator("decision_id", "request_id", "trace_id", "tenant_id", "operation")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or len(value) > 512:
            raise ValueError("audit identifier is invalid")
        return value

    @field_validator(
        "subject_digest", "credential_digest", "application_digest",
        "agent_chain_digest", "target_digest", "dependency_digest",
        "requested_scopes_digest", "server_scopes_digest", "revision_vector_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("audit digest must be canonical lowercase sha256")
        return value

    @field_validator("intersection_results")
    @classmethod
    def _intersections(cls, value: dict[str, bool]) -> dict[str, bool]:
        if not value or len(value) > 32 or any(not key.strip() for key in value):
            raise ValueError("intersection results are invalid")
        return dict(sorted(value.items()))

    @field_validator("policy_evidence")
    @classmethod
    def _policies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 128 or len(value) != len(set(value)):
            raise ValueError("policy evidence is invalid")
        return tuple(sorted(value))

    @field_validator("authoritative_now", "authoritative_expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _expiry(self) -> AuthorizationAuditRecord:
        if self.authoritative_expires_at <= self.authoritative_now:
            raise ValueError("audit expiry must follow authoritative time")
        return self

__all__ = ["AuthorizationAuditRecord", "digest_identifier"]

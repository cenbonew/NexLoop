from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eios.identity.models import FrozenJsonMap, FrozenJsonObject


def _non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must be non-empty")
    return clean


class AuthorizationContext(BaseModel):
    """Immutable trusted facts shared by every authorization entry point."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    tenant_id: str
    session_id: str | None
    subject_principal_id: str
    actor_principal_id: str
    application_id: str
    application_version: str
    agent_id: str | None
    credential_id: str | None
    authentication_method: str
    requested_scopes: frozenset[str]
    groups: frozenset[str]
    roles: frozenset[str]
    attributes: FrozenJsonObject = Field(default_factory=lambda: FrozenJsonMap({}))
    clearances: frozenset[str]
    membership_revision: int = Field(ge=1)
    grant_revision: int = Field(ge=1)
    clearance_revision: int = Field(ge=1)
    policy_revision: int = Field(ge=1)
    request_id: str
    trace_id: str

    @field_validator(
        "tenant_id",
        "subject_principal_id",
        "actor_principal_id",
        "application_id",
        "application_version",
        "authentication_method",
        "request_id",
        "trace_id",
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("session_id", "agent_id", "credential_id")
    @classmethod
    def _validate_optional_identifiers(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _non_blank(value, info.field_name)

    @field_validator("requested_scopes", "groups", "roles", "clearances")
    @classmethod
    def _validate_sets(cls, value: frozenset[str], info: Any) -> frozenset[str]:
        for item in value:
            _non_blank(item, info.field_name)
        return value

    @model_validator(mode="after")
    def _validate_credential_binding(self) -> AuthorizationContext:
        if self.session_id is None and self.credential_id is None:
            raise ValueError("session_id or credential_id must be present")
        return self


__all__ = ["AuthorizationContext"]

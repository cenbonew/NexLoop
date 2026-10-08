"""Secret-safe contracts for tenant-local account administration."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import FrozenJsonMap, SecretDigest32, _FrozenModel, _aware_utc


IdentityAdminOperation = Literal[
    "read",
    "view_metadata",
    "identity.user.invite",
    "identity.user.invitation.reissue",
    "identity.user.invitation.revoke",
    "identity.user.disable",
    "identity.user.restore",
    "identity.user.force-reset",
    "identity.user.recovery-codes.regenerate",
    "identity.admin.grant",
    "identity.admin.revoke",
]


class IdentityAdminAction(_FrozenModel):
    """Idempotent administrator intent bound to one authorization decision."""

    tenant_id: str
    action_id: str
    idempotency_key: str
    payload_digest: SecretDigest32 = Field(repr=False, exclude=True)
    actor_session_id: str
    actor_subject_id: str
    actor_principal_id: str
    target_subject_id: str | None
    target_principal_id: str | None
    target_local_account_id: str | None
    operation: IdentityAdminOperation
    authorization_decision_id: str
    authorization_decision_version: int = Field(ge=1)
    authorization_vector: FrozenJsonMap
    status: Literal["pending", "succeeded", "failed"]
    created_at: datetime
    completed_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator(
        "tenant_id",
        "action_id",
        "idempotency_key",
        "actor_session_id",
        "actor_subject_id",
        "actor_principal_id",
        "target_subject_id",
        "target_principal_id",
        "target_local_account_id",
        "authorization_decision_id",
    )
    @classmethod
    def _validate_identifier(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        if not value.strip() or len(value) > 320:
            field_name = getattr(info, "field_name", "identifier")
            raise ValueError(f"{field_name} must be a bounded non-blank string")
        return value

    @field_validator("created_at", "completed_at")
    @classmethod
    def _validate_time(cls, value: datetime | None, info: object) -> datetime | None:
        return _aware_utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def _validate_state(self) -> IdentityAdminAction:
        if (self.status == "pending") != (self.completed_at is None):
            raise ValueError("admin action completion state is invalid")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot predate created_at")
        return self


class IdentityLifecycleEvent(_FrozenModel):
    """Append-only, tenant-bound account lifecycle evidence."""

    event_id: str
    tenant_id: str
    action_id: str
    actor_subject_id: str
    actor_principal_id: str
    target_subject_id: str
    target_principal_id: str
    target_local_account_id: str | None
    event_type: Literal[
        "local_account.invited",
        "local_account.invitation_reissued",
        "local_account.invitation_revoked",
        "local_account.disabled",
        "local_account.restored",
        "local_account.force_reset",
        "local_account.recovery_codes_regenerated",
        "tenant_admin.granted",
        "tenant_admin.revoked",
    ]
    before_revision: int = Field(ge=0)
    after_revision: int = Field(ge=1)
    details: FrozenJsonMap
    request_id: str
    trace_id: str
    created_at: datetime

    @field_validator(
        "event_id",
        "tenant_id",
        "action_id",
        "actor_subject_id",
        "actor_principal_id",
        "target_subject_id",
        "target_principal_id",
        "target_local_account_id",
        "request_id",
        "trace_id",
    )
    @classmethod
    def _validate_identifier(cls, value: str, info: object) -> str:
        if not value.strip() or len(value) > 320:
            field_name = getattr(info, "field_name", "identifier")
            raise ValueError(f"{field_name} must be a bounded non-blank string")
        return value

    @field_validator("created_at")
    @classmethod
    def _validate_time(cls, value: datetime, info: object) -> datetime:
        checked = _aware_utc(value, getattr(info, "field_name", "created_at"))
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def _validate_revision_fence(self) -> IdentityLifecycleEvent:
        if self.after_revision != self.before_revision + 1:
            raise ValueError("lifecycle event revision fence is invalid")
        return self


class IdentityDeliveryOutboxItem(_FrozenModel):
    """Durable delivery item whose credential-bearing envelope stays opaque."""

    tenant_id: str
    delivery_id: str
    action_id: str
    kind: Literal["invitation", "recovery_codes"]
    sealed_envelope: bytes = Field(
        repr=False, exclude=True, min_length=1, max_length=16_384
    )
    status: Literal["pending", "leased", "succeeded", "dead"]
    attempts: int = Field(ge=0, le=10)
    next_action_at: datetime
    lease_owner_id: str | None
    lease_until: datetime | None
    lease_fence: int = Field(ge=0, le=10)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "delivery_id", "action_id", "lease_owner_id")
    @classmethod
    def _validate_identifier(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        if not value.strip() or len(value) > 320:
            field_name = getattr(info, "field_name", "identifier")
            raise ValueError(f"{field_name} must be a bounded non-blank string")
        return value

    @field_validator(
        "next_action_at", "lease_until", "created_at", "updated_at", "completed_at"
    )
    @classmethod
    def _validate_time(cls, value: datetime | None, info: object) -> datetime | None:
        return _aware_utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def _validate_state(self) -> IdentityDeliveryOutboxItem:
        if self.lease_fence != self.attempts:
            raise ValueError("delivery lease fence must equal attempts")
        if self.updated_at < self.created_at or self.next_action_at < self.created_at:
            raise ValueError("delivery timestamps are invalid")
        if self.status == "pending":
            valid = (
                self.lease_owner_id is None
                and self.lease_until is None
                and self.completed_at is None
            )
        elif self.status == "leased":
            valid = (
                self.lease_owner_id is not None
                and self.lease_until is not None
                and self.updated_at < self.lease_until
                and self.lease_until <= self.updated_at + timedelta(seconds=30)
                and self.completed_at is None
            )
        else:
            valid = (
                self.lease_owner_id is None
                and self.lease_until is None
                and self.completed_at is not None
            )
        if not valid:
            raise ValueError("delivery lifecycle state is invalid")
        return self


class IdentityAdminUserProjection(_FrozenModel):
    """Secret-free tenant user projection for the authorized management plane."""

    tenant_id: str
    subject_id: str
    principal_id: str
    membership_kind: Literal["home", "guest"]
    membership_status: Literal["invited", "active", "suspended", "revoked"]
    membership_revision: int = Field(ge=1)
    local_account_id: str | None
    username: str | None
    verified_email: str | None
    local_account_status: Literal["invited", "active", "disabled"] | None
    locked_until: datetime | None
    must_change_password: bool | None
    account_revision: int | None = Field(default=None, ge=1)
    has_external_identity: bool

    @model_validator(mode="after")
    def _validate_credential_projection(self) -> IdentityAdminUserProjection:
        local_fields = (
            self.local_account_id,
            self.username,
            self.verified_email,
            self.local_account_status,
            self.must_change_password,
            self.account_revision,
        )
        if not (
            all(value is None for value in local_fields)
            or all(value is not None for value in local_fields)
        ):
            raise ValueError(
                "local account projection must be all-present or all-absent"
            )
        _aware_utc(self.locked_until, "locked_until")
        return self


__all__ = [
    "IdentityAdminAction",
    "IdentityAdminOperation",
    "IdentityDeliveryOutboxItem",
    "IdentityLifecycleEvent",
    "IdentityAdminUserProjection",
]

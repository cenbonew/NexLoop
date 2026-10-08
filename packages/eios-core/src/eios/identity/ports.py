from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import Field, SecretBytes, SecretStr, field_validator, model_validator

from .models import (
    AccountRecoveryCode,
    BrowserSession,
    EncodedPasswordHash,
    ExternalIdentity,
    IdentityProvider,
    LocalAccount,
    PasswordReset,
    SessionRotationKind,
    Subject,
    SubjectKind,
    TenantMembership,
    Invitation,
    SecretDigest32,
    _FrozenModel,
    _aware_utc,
    _non_blank,
)


def _command_identifier(value: str, info: Any) -> str:
    return _non_blank(value, info.field_name)


def _command_time(value: datetime, info: Any) -> datetime:
    checked = _aware_utc(value, info.field_name)
    assert checked is not None
    return checked


def _command_identifiers(value: tuple[str, ...], info: Any) -> tuple[str, ...]:
    if len(value) > 32:
        raise ValueError(f"{info.field_name} cannot contain more than 32 values")
    checked = tuple(_non_blank(item, info.field_name) for item in value)
    if len(set(checked)) != len(checked):
        raise ValueError(f"{info.field_name} must contain unique values")
    return checked


def _command_fingerprint(value: str, info: Any) -> str:
    checked = _non_blank(value, info.field_name)
    if len(checked) != 64 or any(
        character not in "0123456789abcdef" for character in checked
    ):
        raise ValueError(f"{info.field_name} must be a lowercase SHA-256 fingerprint")
    return checked


class TrustedIdentityOperator(_FrozenModel):
    """Trusted operator facts supplied by the composition root, never a command."""

    operator_principal_id: str
    request_id: str
    trace_id: str

    @field_validator("operator_principal_id", "request_id", "trace_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)


class IdentityClock(Protocol):
    """Trusted security clock supplied by the composition root or durable UoW."""

    def current_time(self) -> datetime: ...


class CreateSubjectCommand(_FrozenModel):
    subject: Subject

    @model_validator(mode="after")
    def _validate_initial_revision(self) -> CreateSubjectCommand:
        if self.subject.revision != 1:
            raise ValueError("subject revision must be 1 on create")
        return self


class UpdateSubjectCommand(_FrozenModel):
    subject: Subject
    expected_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_expected_revision(self) -> UpdateSubjectCommand:
        if self.subject.revision != self.expected_revision:
            raise ValueError("subject revision must match expected_revision")
        return self


class CreateMembershipCommand(_FrozenModel):
    membership: TenantMembership

    @model_validator(mode="after")
    def _validate_initial_revision(self) -> CreateMembershipCommand:
        if self.membership.revision != 1:
            raise ValueError("membership revision must be 1 on create")
        return self


class UpdateMembershipCommand(_FrozenModel):
    membership: TenantMembership
    expected_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_expected_revision(self) -> UpdateMembershipCommand:
        if self.membership.revision != self.expected_revision:
            raise ValueError("membership revision must match expected_revision")
        return self


class UpsertIdentityProviderCommand(_FrozenModel):
    provider: IdentityProvider
    expected_revision: int | None = Field(default=None, ge=1)


class LinkExternalIdentityCommand(_FrozenModel):
    external_identity: ExternalIdentity


class ResolveExternalIdentityCommand(_FrozenModel):
    tenant_id: str
    provider_id: str
    issuer: str
    external_subject: str
    expected_provider_revision: int = Field(ge=1)
    expected_provider_configuration_fingerprint: str
    resolved_at: datetime

    _validate_identifiers = field_validator(
        "tenant_id",
        "provider_id",
        "issuer",
        "external_subject",
    )(_command_identifier)
    _validate_fingerprint = field_validator(
        "expected_provider_configuration_fingerprint"
    )(_command_fingerprint)
    _validate_resolved_at = field_validator("resolved_at")(_command_time)


class ResolvedExternalIdentity(_FrozenModel):
    external_identity_id: str
    tenant_id: str
    provider_id: str
    issuer: str
    external_subject: str
    subject_id: str
    external_identity_revision: int = Field(ge=1)
    external_identity_status: Literal["active"]
    external_identity_session_epoch: int = Field(ge=1)
    subject_revision: int = Field(ge=1)
    subject_kind: Literal["human"]
    subject_status: Literal["active"]
    provider_revision: int = Field(ge=1)
    provider_configuration_fingerprint: str

    _validate_identifiers = field_validator(
        "external_identity_id",
        "tenant_id",
        "provider_id",
        "issuer",
        "external_subject",
        "subject_id",
    )(_command_identifier)
    _validate_fingerprint = field_validator("provider_configuration_fingerprint")(
        _command_fingerprint
    )


class SaveLocalAccountCommand(_FrozenModel):
    local_account: LocalAccount
    expected_revision: int | None = Field(default=None, ge=1)


class RecordLocalAccountFailureCommand(_FrozenModel):
    """Atomically increment persistent failure state and apply lockout policy."""

    tenant_id: str
    local_account_id: str
    failed_at: datetime

    _validate_identifiers = field_validator("tenant_id", "local_account_id")(
        _command_identifier
    )
    _validate_failed_at = field_validator("failed_at")(_command_time)


class CreateBrowserSessionCommand(_FrozenModel):
    """Create a Session with a globally unique opaque session_id."""

    session: BrowserSession
    replaced_session_id: str | None = None
    replaced_session_token_digest: SecretDigest32 | None = Field(
        default=None, repr=False, exclude=True
    )
    expected_replaced_session_revision: int | None = Field(default=None, ge=1)
    authentication_evidence_id: str
    authentication_evidence_proof: SecretStr = Field(repr=False, exclude=True)

    _validate_evidence_id = field_validator("authentication_evidence_id")(
        _command_identifier
    )

    @field_validator("replaced_session_id")
    @classmethod
    def _validate_replaced_session_id(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _command_identifier(value, info)

    @model_validator(mode="after")
    def _validate_replacement_guard(self) -> CreateBrowserSessionCommand:
        guard = (
            self.replaced_session_id,
            self.replaced_session_token_digest,
            self.expected_replaced_session_revision,
        )
        if any(value is None for value in guard) != all(
            value is None for value in guard
        ):
            raise ValueError("Session replacement guard must be all or none")
        if self.replaced_session_id == self.session.session_id:
            raise ValueError("replaced and created Session IDs must differ")
        return self


class TouchBrowserSessionCommand(_FrozenModel):
    session_id: str
    subject_id: str
    tenant_id: str
    principal_id: str
    application_id: str
    application_revision: int = Field(ge=1)
    credential_tenant_id: str
    membership_revision: int = Field(ge=1)
    new_csrf_token_digest: SecretDigest32 | None = Field(
        default=None,
        repr=False,
        exclude=True,
    )
    seen_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_identifiers = field_validator(
        "session_id",
        "subject_id",
        "tenant_id",
        "principal_id",
        "application_id",
        "credential_tenant_id",
    )(_command_identifier)
    _validate_seen_at = field_validator("seen_at")(_command_time)


class RevokeBrowserSessionCommand(_FrozenModel):
    """Revoke by globally unique opaque session_id."""

    session_id: str
    revoked_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_session_id = field_validator("session_id")(_command_identifier)
    _validate_revoked_at = field_validator("revoked_at")(_command_time)


def _validate_initial_session_replacement(
    *,
    source_session_id: str,
    source_revoked_at: datetime,
    source_revoked_at_label: str,
    replacement_session: BrowserSession,
) -> None:
    if source_session_id == replacement_session.session_id:
        raise ValueError("source and replacement session IDs must be different")
    if replacement_session.revision != 1:
        raise ValueError("replacement session revision must be 1")
    if replacement_session.created_at != source_revoked_at:
        raise ValueError(f"replacement created_at must equal {source_revoked_at_label}")
    if replacement_session.last_seen_at != replacement_session.created_at:
        raise ValueError("replacement last_seen_at must equal created_at")
    if replacement_session.revoked_at is not None:
        raise ValueError("replacement session must be active")


class RotateBrowserSessionCommand(_FrozenModel):
    """Atomically revoke one Session and create its replacement."""

    source_session_id: str
    source_session_token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    source_csrf_token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    source_subject_id: str
    source_subject_kind: SubjectKind
    source_subject_revision: int = Field(ge=1)
    source_tenant_id: str
    source_principal_id: str
    source_membership_revision: int = Field(ge=1)
    source_application_id: str
    source_application_revision: int = Field(ge=1)
    source_credential_tenant_id: str
    source_credential_kind: Literal["local_account", "external_identity"]
    source_credential_id: str
    source_credential_revision: int = Field(ge=1)
    source_credential_session_epoch: int = Field(ge=1)
    source_provider_id: str | None = None
    source_provider_revision: int | None = Field(default=None, ge=1)
    source_provider_configuration_fingerprint: str | None = None
    source_authentication_methods: tuple[str, ...]
    source_restricted: bool
    source_absolute_expires_at: datetime
    rotation_kind: SessionRotationKind
    replacement_session: BrowserSession
    target_membership_revision: int = Field(ge=1)
    revoked_at: datetime
    expected_source_revision: int = Field(ge=1)

    _validate_source_identifiers = field_validator(
        "source_session_id",
        "source_subject_id",
        "source_tenant_id",
        "source_principal_id",
        "source_application_id",
        "source_credential_tenant_id",
        "source_credential_id",
    )(_command_identifier)
    _validate_revoked_at = field_validator("revoked_at")(_command_time)
    _validate_source_absolute_expires_at = field_validator(
        "source_absolute_expires_at"
    )(_command_time)
    _validate_source_authentication_methods = field_validator(
        "source_authentication_methods"
    )(_command_identifiers)

    @field_validator("source_provider_id")
    @classmethod
    def _validate_source_provider_id(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _command_identifier(value, info)

    @field_validator("source_provider_configuration_fingerprint")
    @classmethod
    def _validate_source_provider_fingerprint(
        cls, value: str | None, info: Any
    ) -> str | None:
        if value is None:
            return None
        return _command_fingerprint(value, info)

    @model_validator(mode="after")
    def _validate_rotation(self) -> RotateBrowserSessionCommand:
        _validate_initial_session_replacement(
            source_session_id=self.source_session_id,
            source_revoked_at=self.revoked_at,
            source_revoked_at_label="source revoked_at",
            replacement_session=self.replacement_session,
        )
        if self.source_subject_id != self.replacement_session.subject_id:
            raise ValueError("replacement subject must match source subject")
        if self.source_subject_kind is not self.replacement_session.subject_kind:
            raise ValueError("replacement subject kind must match source")
        if self.source_subject_revision != self.replacement_session.subject_revision:
            raise ValueError("replacement subject revision must match source")
        if self.source_application_id != self.replacement_session.application_id:
            raise ValueError("replacement application must match source application")
        if (
            self.source_application_revision
            != self.replacement_session.application_revision
        ):
            raise ValueError("replacement application revision must match source")
        if (
            self.source_credential_tenant_id
            != self.replacement_session.credential_tenant_id
        ):
            raise ValueError("replacement credential tenant must match source")
        source_credential_binding = (
            self.source_credential_kind,
            self.source_credential_id,
            self.source_credential_revision,
            self.source_credential_session_epoch,
            self.source_provider_id,
            self.source_provider_revision,
            self.source_provider_configuration_fingerprint,
        )
        replacement_credential_binding = (
            self.replacement_session.credential_kind,
            self.replacement_session.credential_id,
            self.replacement_session.credential_revision,
            self.replacement_session.credential_session_epoch,
            self.replacement_session.provider_id,
            self.replacement_session.provider_revision,
            self.replacement_session.provider_configuration_fingerprint,
        )
        if source_credential_binding != replacement_credential_binding:
            raise ValueError("replacement credential binding must match source")
        if (
            self.source_authentication_methods
            != self.replacement_session.authentication_methods
        ):
            raise ValueError("replacement authentication methods must match source")
        if self.source_restricted is not self.replacement_session.restricted:
            raise ValueError("replacement restricted flag must match source")
        if (
            self.source_absolute_expires_at
            != self.replacement_session.absolute_expires_at
        ):
            raise ValueError("replacement absolute expiry must match source")
        same_tenant = self.source_tenant_id == self.replacement_session.tenant_id
        if self.rotation_kind is SessionRotationKind.TENANT_SWITCH:
            if self.source_restricted:
                raise ValueError("restricted Session cannot switch tenant")
            if same_tenant:
                raise ValueError(
                    "tenant switch replacement must use a different tenant"
                )
        elif not same_tenant:
            raise ValueError(
                "non-tenant-switch replacement must remain in the same tenant"
            )
        if (
            same_tenant
            and self.source_principal_id != self.replacement_session.principal_id
        ):
            raise ValueError(
                "same-tenant replacement principal must match source principal"
            )
        if (
            self.target_membership_revision
            != self.replacement_session.membership_revision
        ):
            raise ValueError(
                "target_membership_revision must match replacement membership_revision"
            )
        return self


class RevokeSubjectSessionsCommand(_FrozenModel):
    """Revoke every Session for one global Subject across tenant memberships."""

    subject_id: str
    revoked_at: datetime

    _validate_subject_id = field_validator("subject_id")(_command_identifier)
    _validate_revoked_at = field_validator("revoked_at")(_command_time)


class BrowserSessionRotationResult(_FrozenModel):
    source_session_id: str
    source_revoked_at: datetime
    replacement_session: BrowserSession

    _validate_source_session_id = field_validator("source_session_id")(
        _command_identifier
    )
    _validate_source_revoked_at = field_validator("source_revoked_at")(_command_time)

    @model_validator(mode="after")
    def _validate_result(self) -> BrowserSessionRotationResult:
        _validate_initial_session_replacement(
            source_session_id=self.source_session_id,
            source_revoked_at=self.source_revoked_at,
            source_revoked_at_label="source_revoked_at",
            replacement_session=self.replacement_session,
        )
        return self


class SubjectSessionsRevocationResult(_FrozenModel):
    subject_id: str
    revoked_at: datetime
    revoked_session_ids: tuple[str, ...]

    _validate_subject_id = field_validator("subject_id")(_command_identifier)
    _validate_revoked_at = field_validator("revoked_at")(_command_time)

    @field_validator("revoked_session_ids")
    @classmethod
    def _validate_session_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_non_blank(item, "revoked_session_ids") for item in value)
        if len(checked) != len(set(checked)):
            raise ValueError("revoked_session_ids must not contain duplicates")
        return checked


class CreateInvitationCommand(_FrozenModel):
    """Create an invitation with a globally unique opaque invitation_id."""

    invitation: Invitation
    local_account_id: str
    expected_account_revision: int = Field(ge=1)
    expected_membership_revision: int = Field(ge=1)

    _validate_local_account_id = field_validator("local_account_id")(
        _command_identifier
    )

    @model_validator(mode="after")
    def _validate_issue_revisions(self) -> CreateInvitationCommand:
        if self.invitation.membership_revision != self.expected_membership_revision:
            raise ValueError(
                "invitation membership revision must match expected revision"
            )
        return self


class ConsumeInvitationCommand(_FrozenModel):
    """Consume by globally unique opaque invitation_id."""

    invitation_id: str
    consumed_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_invitation_id = field_validator("invitation_id")(_command_identifier)
    _validate_consumed_at = field_validator("consumed_at")(_command_time)


class RevokeInvitationCommand(_FrozenModel):
    """Revoke by globally unique opaque invitation_id."""

    invitation_id: str
    revoked_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_invitation_id = field_validator("invitation_id")(_command_identifier)
    _validate_revoked_at = field_validator("revoked_at")(_command_time)


class AcceptInvitationCommand(_FrozenModel):
    invitation: Invitation
    activated_account: LocalAccount
    membership: TenantMembership
    accepted_at: datetime
    expected_invitation_revision: int = Field(ge=1)
    expected_account_revision: int = Field(ge=1)
    expected_membership_revision: int = Field(ge=1)

    _validate_accepted_at = field_validator("accepted_at")(_command_time)

    @model_validator(mode="after")
    def _validate_binding(self) -> AcceptInvitationCommand:
        if self.invitation.status != "pending":
            raise ValueError("invitation must be pending")
        if self.invitation.revision != self.expected_invitation_revision:
            raise ValueError("invitation revision must match expected revision")
        if self.accepted_at >= self.invitation.expires_at:
            raise ValueError("accepted_at must be before invitation expires_at")
        if (
            self.activated_account.status != "active"
            or self.activated_account.password_hash is None
        ):
            raise ValueError("activated account must have a first password")
        if self.activated_account.revision != self.expected_account_revision:
            raise ValueError("account revision must match expected revision")
        if (
            self.invitation.tenant_id != self.activated_account.tenant_id
            or self.invitation.email != self.activated_account.verified_email
            or self.invitation.subject_id != self.activated_account.subject_id
        ):
            raise ValueError("invitation and account must remain bound")
        membership = self.membership
        if (
            membership.tenant_id != self.invitation.tenant_id
            or membership.subject_id != self.invitation.subject_id
            or membership.principal_id != self.invitation.principal_id
            or membership.kind != self.invitation.membership_kind
            or membership.status != "invited"
            or membership.revision != self.invitation.membership_revision
            or membership.revision != self.expected_membership_revision
            or membership.valid_from > self.accepted_at
            or (
                membership.valid_until is not None
                and membership.valid_until <= self.accepted_at
            )
        ):
            raise ValueError("invitation membership must remain exactly bound")
        return self


class InvitationAcceptanceResult(_FrozenModel):
    invitation: Invitation
    local_account: LocalAccount
    membership: TenantMembership

    @model_validator(mode="after")
    def _validate_result(self) -> InvitationAcceptanceResult:
        if self.invitation.status != "consumed" or self.invitation.consumed_at is None:
            raise ValueError("invitation must be consumed")
        if (
            self.local_account.status != "active"
            or self.local_account.password_hash is None
        ):
            raise ValueError("local account must be active")
        if (
            self.invitation.tenant_id != self.local_account.tenant_id
            or self.invitation.email != self.local_account.verified_email
            or self.invitation.tenant_id != self.membership.tenant_id
            or self.local_account.subject_id != self.membership.subject_id
            or self.invitation.subject_id != self.membership.subject_id
            or self.invitation.principal_id != self.membership.principal_id
            or self.invitation.membership_kind != self.membership.kind
            or self.membership.revision != self.invitation.membership_revision + 1
            or self.membership.status != "active"
            or self.membership.valid_from > self.invitation.consumed_at
            or (
                self.membership.valid_until is not None
                and self.membership.valid_until <= self.invitation.consumed_at
            )
        ):
            raise ValueError("invitation result must remain bound")
        return self


class CreatePasswordResetCommand(_FrozenModel):
    """Create a reset with a globally unique opaque password_reset_id."""

    password_reset: PasswordReset
    delivery_envelope: SecretBytes = Field(repr=False, exclude=True)
    prior_pending_reset_ids: tuple[str, ...]
    expected_account_revision: int = Field(ge=1)
    expected_session_epoch: int = Field(ge=1)

    _validate_prior_pending_reset_ids = field_validator("prior_pending_reset_ids")(
        _command_identifiers
    )

    @field_validator("delivery_envelope")
    @classmethod
    def _validate_delivery_envelope(cls, value: SecretBytes) -> SecretBytes:
        if type(value) is not SecretBytes:
            raise ValueError("delivery_envelope must be protected bytes")
        raw = value.get_secret_value()
        if not raw or len(raw) > 16_384:
            raise ValueError("delivery_envelope must be bounded protected bytes")
        return value

    @model_validator(mode="after")
    def _validate_new_reset(self) -> CreatePasswordResetCommand:
        if self.password_reset.status != "pending":
            raise ValueError("password reset must be pending")
        if self.password_reset.password_reset_id in self.prior_pending_reset_ids:
            raise ValueError("new password reset cannot be in prior pending set")
        return self


class PasswordResetIssueResult(_FrozenModel):
    password_reset: PasswordReset
    revoked_password_reset_ids: tuple[str, ...]
    account_revision: int = Field(ge=1)
    session_epoch: int = Field(ge=1)

    _validate_revoked_password_reset_ids = field_validator(
        "revoked_password_reset_ids"
    )(_command_identifiers)

    @model_validator(mode="after")
    def _validate_result(self) -> PasswordResetIssueResult:
        if self.password_reset.status != "pending":
            raise ValueError("issued password reset must be pending")
        if self.password_reset.password_reset_id in self.revoked_password_reset_ids:
            raise ValueError("issued password reset cannot be revoked")
        return self


PasswordResetRequestInboxStatus = Literal["pending", "leased", "acked", "dead"]


class EnqueuePasswordResetRequestCommand(_FrozenModel):
    """Persist one opaque, tenant-bound reset request without account lookup."""

    tenant_id: str
    inbox_request_id: str
    sealed_envelope: SecretBytes = Field(repr=False, exclude=True)
    idempotency_key: str

    _validate_identifiers = field_validator(
        "tenant_id", "inbox_request_id", "idempotency_key"
    )(_command_identifier)

    @field_validator("sealed_envelope")
    @classmethod
    def _validate_sealed_envelope(cls, value: SecretBytes) -> SecretBytes:
        if type(value) is not SecretBytes:
            raise ValueError("sealed_envelope must be protected bytes")
        raw = value.get_secret_value()
        if not raw or len(raw) > 16_384:
            raise ValueError("sealed_envelope must be bounded protected bytes")
        return value


class PasswordResetRequestEnqueueReceipt(_FrozenModel):
    tenant_id: str
    inbox_request_id: str
    accepted_at: datetime

    _validate_identifiers = field_validator("tenant_id", "inbox_request_id")(
        _command_identifier
    )
    _validate_accepted_at = field_validator("accepted_at")(_command_time)


class ClaimedPasswordResetRequest(_FrozenModel):
    """One globally claimed, fenced lease over a sealed account identifier."""

    tenant_id: str
    inbox_request_id: str
    sealed_envelope: SecretBytes = Field(repr=False, exclude=True)
    attempt: int = Field(ge=1, le=10)
    lease_owner_id: str
    lease_until: datetime
    lease_fence: int = Field(ge=1)

    _validate_identifiers = field_validator(
        "tenant_id", "inbox_request_id", "lease_owner_id"
    )(_command_identifier)
    _validate_lease_until = field_validator("lease_until")(_command_time)
    _validate_sealed_envelope = field_validator("sealed_envelope")(
        EnqueuePasswordResetRequestCommand._validate_sealed_envelope.__func__
    )


class AckPasswordResetRequestCommand(_FrozenModel):
    tenant_id: str
    inbox_request_id: str
    lease_owner_id: str
    lease_fence: int = Field(ge=1)

    _validate_identifiers = field_validator(
        "tenant_id", "inbox_request_id", "lease_owner_id"
    )(_command_identifier)


class RetryPasswordResetRequestCommand(_FrozenModel):
    inbox: AckPasswordResetRequestCommand
    retry_after_seconds: int = Field(ge=1, le=900)


class CompletePasswordResetRequestWithResetCommand(_FrozenModel):
    """Atomically create the reset and acknowledge its exact inbox lease."""

    inbox: AckPasswordResetRequestCommand
    reset: CreatePasswordResetCommand

    @model_validator(mode="after")
    def _validate_tenant_binding(
        self,
    ) -> CompletePasswordResetRequestWithResetCommand:
        if self.reset.password_reset.tenant_id != self.inbox.tenant_id:
            raise ValueError("password reset request tenant binding is invalid")
        return self


class ConsumePasswordResetCommand(_FrozenModel):
    """Consume by globally unique opaque password_reset_id."""

    password_reset_id: str
    consumed_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_password_reset_id = field_validator("password_reset_id")(
        _command_identifier
    )
    _validate_consumed_at = field_validator("consumed_at")(_command_time)


class RevokePasswordResetCommand(_FrozenModel):
    """Revoke by globally unique opaque password_reset_id."""

    password_reset_id: str
    revoked_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_password_reset_id = field_validator("password_reset_id")(
        _command_identifier
    )
    _validate_revoked_at = field_validator("revoked_at")(_command_time)


class CompletePasswordResetCommand(_FrozenModel):
    password_reset: PasswordReset
    updated_account: LocalAccount
    completed_at: datetime
    expected_password_reset_revision: int = Field(ge=1)
    expected_account_revision: int = Field(ge=1)
    expected_session_epoch: int = Field(ge=1)
    prior_pending_reset_ids: tuple[str, ...]

    _validate_completed_at = field_validator("completed_at")(_command_time)
    _validate_prior_pending_reset_ids = field_validator("prior_pending_reset_ids")(
        _command_identifiers
    )

    @model_validator(mode="after")
    def _validate_binding(self) -> CompletePasswordResetCommand:
        if self.password_reset.status != "pending":
            raise ValueError("password reset must be pending")
        if self.password_reset.revision != self.expected_password_reset_revision:
            raise ValueError("password reset revision must match expected revision")
        if self.password_reset.password_reset_id not in self.prior_pending_reset_ids:
            raise ValueError("password reset must be in prior pending set")
        if self.completed_at >= self.password_reset.expires_at:
            raise ValueError("completed_at must be before password reset expires_at")
        if (
            self.updated_account.status != "active"
            or self.updated_account.password_hash is None
        ):
            raise ValueError("updated account must have a password")
        if (
            self.updated_account.revision != self.expected_account_revision
            or self.updated_account.session_epoch != self.expected_session_epoch + 1
        ):
            raise ValueError("account revision or session epoch is invalid")
        if (
            self.password_reset.tenant_id != self.updated_account.tenant_id
            or self.password_reset.local_account_id
            != self.updated_account.local_account_id
        ):
            raise ValueError("password reset and account must remain bound")
        return self


class PasswordResetCompletionResult(_FrozenModel):
    password_reset: PasswordReset
    local_account: LocalAccount
    session_revocation: SubjectSessionsRevocationResult
    invalidated_password_reset_ids: tuple[str, ...]

    _validate_invalidated_password_reset_ids = field_validator(
        "invalidated_password_reset_ids"
    )(_command_identifiers)

    @model_validator(mode="after")
    def _validate_result(self) -> PasswordResetCompletionResult:
        if (
            self.password_reset.status != "consumed"
            or self.password_reset.consumed_at is None
        ):
            raise ValueError("password reset must be consumed")
        if (
            self.password_reset.tenant_id != self.local_account.tenant_id
            or self.password_reset.local_account_id
            != self.local_account.local_account_id
            or self.session_revocation.subject_id != self.local_account.subject_id
            or self.session_revocation.revoked_at != self.password_reset.consumed_at
            or self.password_reset.password_reset_id
            in self.invalidated_password_reset_ids
        ):
            raise ValueError("password reset result must remain bound")
        return self


class ChangeLocalPasswordCommand(_FrozenModel):
    """Atomic password, Session-revocation, and replacement-Evidence command."""

    tenant_id: str
    subject_id: str
    local_account_id: str
    session_id: str
    session_token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    csrf_token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    expected_session_revision: int = Field(ge=1)
    current_password_evidence_id: str
    current_password_evidence_proof_digest: SecretDigest32 = Field(
        repr=False, exclude=True
    )
    new_password_hash: EncodedPasswordHash = Field(repr=False, exclude=True)
    new_password_history: tuple[EncodedPasswordHash, ...] = Field(
        repr=False, exclude=True
    )
    expected_account_revision: int = Field(ge=1)
    expected_session_epoch: int = Field(ge=1)
    completed_at: datetime
    new_evidence_id: str
    new_evidence_proof_digest: SecretDigest32 = Field(repr=False, exclude=True)
    new_evidence_expires_at: datetime

    _validate_identifiers = field_validator(
        "tenant_id",
        "subject_id",
        "local_account_id",
        "session_id",
        "current_password_evidence_id",
        "new_evidence_id",
    )(_command_identifier)
    _validate_completed_at = field_validator("completed_at")(_command_time)
    _validate_new_evidence_expires_at = field_validator("new_evidence_expires_at")(
        _command_time
    )

    @field_validator("new_password_history")
    @classmethod
    def _validate_password_history(
        cls, value: tuple[EncodedPasswordHash, ...]
    ) -> tuple[EncodedPasswordHash, ...]:
        if len(value) > 10 or len(value) != len(set(value)):
            raise ValueError("new_password_history must be a unique bounded history")
        return value

    @model_validator(mode="after")
    def _validate_binding(self) -> ChangeLocalPasswordCommand:
        if self.new_evidence_expires_at != self.completed_at + timedelta(minutes=5):
            raise ValueError(
                "new_evidence_expires_at must be exactly five minutes after completion"
            )
        if self.current_password_evidence_id == self.new_evidence_id:
            raise ValueError("current and replacement Evidence IDs must differ")
        if self.new_password_hash in self.new_password_history:
            raise ValueError("new password hash must not be present in history")
        return self


class LocalPasswordChangeResult(_FrozenModel):
    local_account: LocalAccount
    session_revocation: SubjectSessionsRevocationResult
    new_evidence_id: str
    new_evidence_proof_digest: SecretDigest32 = Field(repr=False, exclude=True)
    new_evidence_expires_at: datetime

    _validate_new_evidence_id = field_validator("new_evidence_id")(_command_identifier)
    _validate_new_evidence_expires_at = field_validator("new_evidence_expires_at")(
        _command_time
    )

    @model_validator(mode="after")
    def _validate_binding(self) -> LocalPasswordChangeResult:
        account = self.local_account
        revocation = self.session_revocation
        if (
            account.status != "active"
            or account.password_hash is None
            or account.must_change_password
            or account.failed_attempts != 0
            or account.lockout_level != 0
            or account.locked_until is not None
            or account.subject_id != revocation.subject_id
            or account.updated_at != revocation.revoked_at
            or self.new_evidence_expires_at
            != revocation.revoked_at + timedelta(minutes=5)
        ):
            raise ValueError("local password change result must remain bound")
        return self


class ReplaceRecoveryCodesCommand(_FrozenModel):
    """Create globally unique opaque recovery_code_id values as one set."""

    tenant_id: str
    local_account_id: str
    code_set_id: str
    codes: tuple[AccountRecoveryCode, ...]
    prior_active_code_ids: tuple[str, ...]
    expected_account_revision: int = Field(ge=1)
    expected_session_epoch: int = Field(ge=1)

    @field_validator("tenant_id", "local_account_id", "code_set_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("codes")
    @classmethod
    def _validate_code_count(
        cls, value: tuple[AccountRecoveryCode, ...]
    ) -> tuple[AccountRecoveryCode, ...]:
        if len(value) != 10:
            raise ValueError("codes must contain exactly 10 recovery codes")
        return value

    @field_validator("prior_active_code_ids")
    @classmethod
    def _validate_prior_active_code_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_non_blank(item, "prior_active_code_ids") for item in value)
        if len(checked) > 10 or len(checked) != len(set(checked)):
            raise ValueError("prior_active_code_ids must be a unique bounded set")
        return checked

    @model_validator(mode="after")
    def _validate_code_set(self) -> ReplaceRecoveryCodesCommand:
        for code in self.codes:
            for field_name in ("tenant_id", "local_account_id", "code_set_id"):
                if getattr(code, field_name) != getattr(self, field_name):
                    raise ValueError(f"nested recovery code {field_name} must match")
        recovery_code_ids = tuple(code.recovery_code_id for code in self.codes)
        if len(recovery_code_ids) != len(set(recovery_code_ids)):
            raise ValueError("recovery_code_id must be unique within a code set")
        if set(recovery_code_ids) & set(self.prior_active_code_ids):
            raise ValueError("new recovery_code_id must not reuse prior active IDs")
        keyed_digests = tuple(code.keyed_digest for code in self.codes)
        if len(keyed_digests) != len(set(keyed_digests)):
            raise ValueError("keyed_digest must be unique within a code set")
        if len({code.digest_key_id for code in self.codes}) != 1:
            raise ValueError("all recovery codes must use the same digest_key_id")
        if any(code.consumed_at is not None for code in self.codes):
            raise ValueError("replacement recovery codes must be unconsumed")
        if any(code.revision != 1 for code in self.codes):
            raise ValueError("replacement recovery code revision must be 1")
        if len({code.created_at for code in self.codes}) != 1:
            raise ValueError("replacement recovery code created_at must match")
        return self


class RecoveryCodeReplacementResult(_FrozenModel):
    active_codes: tuple[AccountRecoveryCode, ...]
    invalidated_code_ids: tuple[str, ...]
    account_revision: int = Field(ge=1)
    session_epoch: int = Field(ge=1)

    @field_validator("invalidated_code_ids")
    @classmethod
    def _validate_invalidated_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_non_blank(item, "invalidated_code_ids") for item in value)
        if len(checked) > 10 or len(checked) != len(set(checked)):
            raise ValueError("invalidated_code_ids must be a unique bounded set")
        return checked

    @model_validator(mode="after")
    def _validate_complete_active_set(self) -> RecoveryCodeReplacementResult:
        if len(self.active_codes) != 10:
            raise ValueError("active_codes must contain exactly 10 recovery codes")
        first = self.active_codes[0]
        for code in self.active_codes:
            if (
                code.tenant_id != first.tenant_id
                or code.local_account_id != first.local_account_id
                or code.code_set_id != first.code_set_id
                or code.consumed_at is not None
            ):
                raise ValueError("active_codes must be one unconsumed bound set")
        active_ids = tuple(code.recovery_code_id for code in self.active_codes)
        if len(active_ids) != len(set(active_ids)):
            raise ValueError("active recovery code IDs must be unique")
        if set(active_ids) & set(self.invalidated_code_ids):
            raise ValueError(
                "active and invalidated recovery code IDs must not overlap"
            )
        return self


class ConsumeRecoveryCodeCommand(_FrozenModel):
    """Consume by globally unique opaque recovery_code_id."""

    recovery_code_id: str
    consumed_at: datetime
    expected_revision: int = Field(ge=1)

    _validate_recovery_code_id = field_validator("recovery_code_id")(
        _command_identifier
    )
    _validate_consumed_at = field_validator("consumed_at")(_command_time)


class ConsumeRecoveryCredentialCommand(_FrozenModel):
    """One atomic recovery-code, password, account, and Session mutation."""

    tenant_id: str
    local_account_id: str
    subject_id: str
    recovery_code_id: str
    expected_recovery_revision: int = Field(ge=1)
    expected_account_revision: int = Field(ge=1)
    expected_session_epoch: int = Field(ge=1)
    updated_account: LocalAccount
    consumed_at: datetime

    _validate_identifiers = field_validator(
        "tenant_id", "local_account_id", "subject_id", "recovery_code_id"
    )(_command_identifier)
    _validate_consumed_at = field_validator("consumed_at")(_command_time)

    @model_validator(mode="after")
    def _validate_updated_account(self) -> ConsumeRecoveryCredentialCommand:
        account = self.updated_account
        if (
            account.tenant_id != self.tenant_id
            or account.local_account_id != self.local_account_id
            or account.subject_id != self.subject_id
            or account.revision != self.expected_account_revision
            or account.session_epoch != self.expected_session_epoch + 1
            or account.updated_at != self.consumed_at
            or account.status != "active"
            or account.password_hash is None
            or account.must_change_password
            or account.failed_attempts != 0
            or account.lockout_level != 0
            or account.locked_until is not None
        ):
            raise ValueError("updated recovery account is invalid")
        return self


class RecoveryCredentialTransactionResult(_FrozenModel):
    consumed_code: AccountRecoveryCode
    local_account: LocalAccount
    session_revocation: SubjectSessionsRevocationResult

    @model_validator(mode="after")
    def _validate_binding(self) -> RecoveryCredentialTransactionResult:
        if self.consumed_code.consumed_at is None:
            raise ValueError("recovery code must be consumed")
        if (
            self.consumed_code.tenant_id != self.local_account.tenant_id
            or self.consumed_code.local_account_id
            != self.local_account.local_account_id
            or self.session_revocation.subject_id != self.local_account.subject_id
            or self.session_revocation.revoked_at != self.consumed_code.consumed_at
        ):
            raise ValueError("recovery transaction result must remain bound")
        if self.local_account.must_change_password:
            raise ValueError("recovery must complete password change")
        return self


class SubjectRepository(Protocol):
    def get_subject(self, subject_id: str) -> Subject | None:
        """Lookup by the platform-wide global Subject ID."""
        ...

    def create_subject(
        self,
        command: CreateSubjectCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> Subject: ...

    def update_subject(
        self,
        command: UpdateSubjectCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> Subject: ...


class MembershipRepository(Protocol):
    def get_membership(
        self, tenant_id: str, principal_id: str
    ) -> TenantMembership | None: ...

    def list_memberships(self, subject_id: str) -> tuple[TenantMembership, ...]:
        """List memberships for the platform-wide global Subject ID."""
        ...

    def create_membership(
        self,
        command: CreateMembershipCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> TenantMembership: ...

    def update_membership(
        self,
        command: UpdateMembershipCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> TenantMembership: ...


class IdentityProviderRepository(Protocol):
    def get_provider(
        self, tenant_id: str, provider_id: str
    ) -> IdentityProvider | None: ...

    def list_providers(self, tenant_id: str) -> tuple[IdentityProvider, ...]: ...

    def upsert_provider(
        self,
        command: UpsertIdentityProviderCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> IdentityProvider: ...


class IdentityProviderRegistry(Protocol):
    """Trusted provider snapshots keyed only by server-owned tenant context."""

    def get_provider(
        self, tenant_id: str, provider_id: str
    ) -> IdentityProvider | None: ...


class ExternalIdentityRepository(Protocol):
    def find_by_provider_subject(
        self,
        tenant_id: str,
        provider_id: str,
        issuer: str,
        subject: str,
    ) -> ExternalIdentity | None: ...

    def link_external_identity(
        self,
        command: LinkExternalIdentityCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> ExternalIdentity: ...


class ExternalIdentityResolver(Protocol):
    def resolve(
        self,
        command: ResolveExternalIdentityCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> ResolvedExternalIdentity:
        """Resolve one exact external key in the same transaction.

        Reload the active provider at the expected revision, the exact
        provider/issuer/external-subject mapping, and its active internal
        Subject. Return their durable revisions and tenant binding. The email
        attribute is never a lookup, linking, or fallback key. Any revision/status race or
        missing mapping fails without minting authentication evidence.
        """
        ...


class LocalAccountRepository(Protocol):
    def get_local_account(
        self, tenant_id: str, local_account_id: str
    ) -> LocalAccount | None: ...

    def find_by_username(
        self, tenant_id: str, username: str
    ) -> LocalAccount | None: ...

    def find_by_verified_email(
        self, tenant_id: str, verified_email: str
    ) -> LocalAccount | None: ...

    def save_local_account(
        self,
        command: SaveLocalAccountCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> LocalAccount: ...

    def record_authentication_failure(
        self,
        command: RecordLocalAccountFailureCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> LocalAccount:
        """Atomically apply one failure against the latest persisted account.

        An account with an unexpired lock remains locked: a stale concurrent
        failure must not clear or shorten the lock. Lockout level and revision
        are monotonic, with the 15/30/60 minute policy capped at 60 minutes.
        """
        ...


class BrowserSessionRepository(Protocol):
    def get_session(self, session_id: str) -> BrowserSession | None:
        """Lookup by globally unique opaque session_id across tenants."""
        ...

    def find_by_token_digest(
        self, token_digest: SecretDigest32
    ) -> BrowserSession | None:
        """Lookup a globally unique Session token digest across tenants."""
        ...

    def create_session(
        self,
        command: CreateBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSession:
        """Create a globally unique opaque session_id in one transaction.

        Atomically consume Evidence and reload the exact human Subject, active credential with revision and
        session epoch, active membership, and active Application revision.
        Compare every binding to canonical Evidence, consume it, and persist
        the Session in the same transaction. Any mismatch persists nothing and does
        not consume Evidence. The backend-global unique constraints act as the
        atomic arbiter across concurrent Units of Work.
        """
        ...

    def touch_session(
        self,
        command: TouchBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSession:
        """Reload Session and its complete identity contract in the same transaction.

        Subject kind/status/revision, credential status/revision/session epoch,
        active membership revision, Application revision, and provider binding must
        remain exact before extending idle expiry by exactly one revision.
        """
        ...

    def revoke_session(
        self,
        command: RevokeBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSession: ...

    def rotate_session(
        self,
        command: RotateBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSessionRotationResult:
        """In the same transaction, reload every source and target authority fact.

        Revalidate both active memberships plus the source Session's exact human
        Subject, credential status/revision/session epoch/tenant, Application
        revision, provider binding, authentication methods, and absolute expiry.
        The replacement must inherit every non-tenant authority binding exactly.
        The backend-global unique constraints act as the atomic arbiter for
        replacement session_id and Session token digest collisions across concurrent
        Units of Work.
        Any missing, suspended, expired, or mismatched record fails with the
        source Session unchanged and no replacement persisted.
        A failure at any stage, including commit, rolls back source and replacement
        before another transaction may observe or mutate either row.
        """
        ...

    def revoke_subject_sessions(
        self,
        command: RevokeSubjectSessionsCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> SubjectSessionsRevocationResult:
        """Revoke Sessions for one platform-wide global Subject ID."""
        ...


class InvitationRepository(Protocol):
    def find_by_token_digest(self, token_digest: SecretDigest32) -> Invitation | None:
        """Lookup a globally unique invitation token digest across tenants."""
        ...

    def create_invitation(
        self,
        command: CreateInvitationCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> Invitation:
        """Create a globally unique opaque invitation_id across tenants."""
        ...

    def consume_invitation(
        self,
        command: ConsumeInvitationCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> Invitation:
        """Consume by globally unique opaque invitation_id across tenants."""
        ...

    def revoke_invitation(
        self,
        command: RevokeInvitationCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> Invitation:
        """Revoke by globally unique opaque invitation_id across tenants."""
        ...


class InvitationAcceptanceRepository(Protocol):
    def accept_invitation(
        self,
        command: AcceptInvitationCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> InvitationAcceptanceResult:
        """Reload all records, verify bindings/revisions/expiry, then commit.

        Invitation consumption, account activation, and creation or activation
        of the exact intended Membership must occur in one transaction. The
        returned Membership must be active and bound to the invitation tenant,
        membership kind, and account Subject. Any mismatch or failure leaves
        every record unchanged.
        """
        ...


class PasswordResetRepository(Protocol):
    def find_by_token_digest(
        self, token_digest: SecretDigest32
    ) -> PasswordReset | None:
        """Lookup a globally unique reset token digest across tenants."""
        ...

    def list_pending_resets(
        self, tenant_id: str, local_account_id: str
    ) -> tuple[PasswordReset, ...]: ...

    def create_password_reset(
        self,
        command: CreatePasswordResetCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetIssueResult:
        """Create a globally unique opaque password_reset_id atomically.

        Also revoke the entire frozen prior set. The implementation must reload the active account at the exact
        revision and Session epoch and compare the complete prior pending reset
        ID set in the same transaction. Any mismatch leaves all resets intact.
        """
        ...

    def consume_password_reset(
        self,
        command: ConsumePasswordResetCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordReset:
        """Consume by globally unique opaque password_reset_id across tenants."""
        ...

    def revoke_password_reset(
        self,
        command: RevokePasswordResetCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordReset:
        """Revoke by globally unique opaque password_reset_id across tenants."""
        ...


class PasswordResetRequestInboxRepository(Protocol):
    """Durable global inbox with strict fenced completion operations."""

    def enqueue(
        self,
        command: EnqueuePasswordResetRequestCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetRequestEnqueueReceipt: ...

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int,
        operator: TrustedIdentityOperator,
    ) -> tuple[ClaimedPasswordResetRequest, ...]: ...

    def ack_noop(
        self,
        command: AckPasswordResetRequestCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> None: ...

    def retry(
        self,
        command: RetryPasswordResetRequestCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> None: ...

    def dead(
        self,
        command: AckPasswordResetRequestCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> None: ...

    def complete_reset_request_with_reset(
        self,
        command: CompletePasswordResetRequestWithResetCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetIssueResult: ...


class PasswordResetCompletionRepository(Protocol):
    def complete_password_reset(
        self,
        command: CompletePasswordResetCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetCompletionResult:
        """Reload bound records and atomically complete every reset effect.

        The implementation must verify both revisions and the expiry inside
        the transaction before consuming the reset, changing the credential,
        incrementing Session epoch, and revoking all Subject Sessions. Any
        mismatch or failure leaves every record unchanged. The transaction must
        also compare the entire frozen pending-reset set and invalidate every
        other pending reset for the account.
        """
        ...


class LocalPasswordChangeRepository(Protocol):
    def change_local_password(
        self,
        command: ChangeLocalPasswordCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> LocalPasswordChangeResult:
        """Commit password, epoch, Session revocation, and Evidence atomically."""
        ...


class AccountRecoveryCodeRepository(Protocol):
    def list_active_codes(
        self, tenant_id: str, local_account_id: str
    ) -> tuple[AccountRecoveryCode, ...]: ...

    def replace_recovery_codes(
        self,
        command: ReplaceRecoveryCodesCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> RecoveryCodeReplacementResult:
        """Create globally unique opaque recovery_code_id values atomically.

        Replace the complete set in the same transaction. Reload the active account
        and the entire prior active set, require the command's exact account
        revision/status/session epoch and prior IDs, then invalidate every prior
        code and insert exactly ten new codes. Any mismatch or failure leaves the
        account and all codes unchanged.
        """
        ...

    def consume_recovery_code(
        self,
        command: ConsumeRecoveryCodeCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> AccountRecoveryCode:
        """Consume by globally unique opaque recovery_code_id across tenants."""
        ...


class RecoveryCredentialTransactionRepository(Protocol):
    def consume_recovery_credential(
        self,
        command: ConsumeRecoveryCredentialCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> RecoveryCredentialTransactionResult:
        """Reload exact revisions, then atomically consume, save, and revoke.

        The adapter persists ``command.updated_account`` with exactly one
        revision increment. It consumes exactly the selected code and revokes
        every Session for the Subject in the same transaction. Any mismatch or
        failure rolls all effects back.
        """
        ...


__all__ = [
    "AcceptInvitationCommand",
    "AccountRecoveryCodeRepository",
    "AckPasswordResetRequestCommand",
    "BrowserSessionRotationResult",
    "BrowserSessionRepository",
    "CompletePasswordResetCommand",
    "CompletePasswordResetRequestWithResetCommand",
    "ChangeLocalPasswordCommand",
    "ConsumeInvitationCommand",
    "ConsumePasswordResetCommand",
    "ConsumeRecoveryCodeCommand",
    "ConsumeRecoveryCredentialCommand",
    "CreateBrowserSessionCommand",
    "CreateInvitationCommand",
    "CreateMembershipCommand",
    "CreatePasswordResetCommand",
    "CreateSubjectCommand",
    "ClaimedPasswordResetRequest",
    "EnqueuePasswordResetRequestCommand",
    "ExternalIdentityRepository",
    "ExternalIdentityResolver",
    "IdentityProviderRepository",
    "IdentityProviderRegistry",
    "InvitationRepository",
    "InvitationAcceptanceRepository",
    "InvitationAcceptanceResult",
    "LinkExternalIdentityCommand",
    "LocalPasswordChangeRepository",
    "LocalPasswordChangeResult",
    "LocalAccountRepository",
    "MembershipRepository",
    "PasswordResetRepository",
    "PasswordResetRequestEnqueueReceipt",
    "PasswordResetRequestInboxRepository",
    "PasswordResetRequestInboxStatus",
    "PasswordResetCompletionRepository",
    "PasswordResetCompletionResult",
    "PasswordResetIssueResult",
    "RecordLocalAccountFailureCommand",
    "RetryPasswordResetRequestCommand",
    "ResolveExternalIdentityCommand",
    "ResolvedExternalIdentity",
    "ReplaceRecoveryCodesCommand",
    "RecoveryCodeReplacementResult",
    "RecoveryCredentialTransactionRepository",
    "RecoveryCredentialTransactionResult",
    "RevokeBrowserSessionCommand",
    "RevokeInvitationCommand",
    "RevokePasswordResetCommand",
    "RevokeSubjectSessionsCommand",
    "RotateBrowserSessionCommand",
    "SaveLocalAccountCommand",
    "SubjectRepository",
    "SubjectSessionsRevocationResult",
    "TouchBrowserSessionCommand",
    "TrustedIdentityOperator",
    "UpdateMembershipCommand",
    "UpdateSubjectCommand",
    "UpsertIdentityProviderCommand",
]

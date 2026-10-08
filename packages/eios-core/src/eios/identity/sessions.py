"""Human browser Session services."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_bytes
from typing import Callable, Protocol

from pydantic import SecretStr

from .errors import CredentialInvalid, IdentityConflict, IdentityUnavailable
from .evidence import (
    AuthenticationEvidence,
    AuthenticationEvidenceAuthority,
    AuthenticationEvidenceRecord,
    AuthenticationEvidenceSessionStore,
)
from .models import (
    BrowserSession,
    SESSION_ABSOLUTE_LIMIT,
    SESSION_IDLE_LIMIT,
    SecretDigest32,
    SessionRotationKind,
    SubjectKind,
    TenantMembership,
)
from .ports import (
    BrowserSessionRotationResult,
    BrowserSessionRepository,
    CreateBrowserSessionCommand,
    MembershipRepository,
    RevokeBrowserSessionCommand,
    RotateBrowserSessionCommand,
    TouchBrowserSessionCommand,
    TrustedIdentityOperator,
)


_FAILURE = "session is invalid or expired"


class SessionCreationUnitOfWork(
    AuthenticationEvidenceSessionStore, BrowserSessionRepository, Protocol
):
    """One durable transaction boundary for Evidence and browser Sessions.

    ``create_session`` must compare the opaque Evidence proof digest, reload
    every canonical Evidence/Subject/credential/membership/Application fact,
    consume Evidence, and insert the Session in one database transaction.  A
    mismatch or failure at any point, including commit, rolls back both writes.
    Restricted Evidence is valid for a restricted Session, and the persisted
    Session ``restricted`` flag must exactly equal the Evidence flag.
    Evidence IDs, proof digests, Session IDs, and Session token digests are
    globally unique because the lookup ports intentionally accept no tenant
    discriminator. The backend-global unique constraints act as the atomic arbiter
    across every Unit of Work sharing that durable backend.
    """


@dataclass(frozen=True, slots=True)
class IssuedBrowserSession:
    session: BrowserSession
    session_token: SecretStr
    csrf_token: SecretStr


class BrowserSessionService:
    def __init__(
        self,
        repository: SessionCreationUnitOfWork,
        membership_repository: MembershipRepository,
        *,
        application_id: str,
        application_revision: int = 1,
        operator: TrustedIdentityOperator,
        random_bytes: Callable[[int], bytes] = token_bytes,
    ) -> None:
        self._repository = repository
        self._memberships = membership_repository
        self._evidence = AuthenticationEvidenceAuthority(store=repository)
        if not _identifier(application_id):
            raise ValueError("application_id is invalid")
        self._application_id = application_id
        if type(application_revision) is not int or application_revision < 1:
            raise ValueError("application_revision is invalid")
        self._application_revision = application_revision
        self._operator = TrustedIdentityOperator.model_validate(operator)
        self._random_bytes = random_bytes

    def create(
        self,
        tenant_id: str,
        subject_id: str,
        principal_id: str,
        authentication_evidence: AuthenticationEvidence,
        *,
        replaced_session_token: str | None = None,
    ) -> IssuedBrowserSession:
        created_at = self._current_time()
        replaced: BrowserSession | None = None
        if replaced_session_token is not None:
            try:
                replaced = self._lookup_active(replaced_session_token, created_at)
            except CredentialInvalid:
                replaced = None
        try:
            evidence_record = self._evidence.inspect_for_session(
                authentication_evidence,
                tenant_id=tenant_id,
                subject_id=subject_id,
                application_id=self._application_id,
            )
        except CredentialInvalid:
            raise _invalid() from None
        if evidence_record.application_revision != self._application_revision:
            raise _invalid()
        membership = self._membership(tenant_id, principal_id)
        if (
            membership is None
            or membership.subject_id != subject_id
            or not _membership_is_active(membership, created_at)
        ):
            raise _invalid()
        issued = self._new_session(
            membership,
            application_id=self._application_id,
            authentication_methods=evidence_record.authentication_methods,
            identity_binding=evidence_record,
            now=created_at,
        )
        try:
            evidence_id, evidence_proof = (
                authentication_evidence._export_for_trusted_transport()  # noqa: SLF001
            )
            created = self._repository.create_session(
                CreateBrowserSessionCommand(
                    session=issued.session,
                    replaced_session_id=(
                        None if replaced is None else replaced.session_id
                    ),
                    replaced_session_token_digest=(
                        None if replaced is None else replaced.session_token_digest
                    ),
                    expected_replaced_session_revision=(
                        None if replaced is None else replaced.revision
                    ),
                    authentication_evidence_id=evidence_id,
                    authentication_evidence_proof=SecretStr(evidence_proof),
                ),
                operator=self._operator,
            )
        except (IdentityConflict, CredentialInvalid):
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        try:
            checked = BrowserSession.model_validate(created)
        except (TypeError, ValueError):
            raise _invalid() from None
        if checked != issued.session:
            raise _invalid()
        return issued

    def inspect(self, session_token: str) -> BrowserSession:
        """Read one active Session without rotating revision or credentials."""

        return self._lookup_active(session_token, self._current_time())

    def authenticate(self, session_token: str) -> BrowserSession:
        current_time = self._current_time()
        session = self._lookup_active(session_token, current_time)
        try:
            touched = self._repository.touch_session(
                TouchBrowserSessionCommand(
                    session_id=session.session_id,
                    subject_id=session.subject_id,
                    tenant_id=session.tenant_id,
                    principal_id=session.principal_id,
                    application_id=self._application_id,
                    application_revision=self._application_revision,
                    credential_tenant_id=session.credential_tenant_id,
                    membership_revision=session.membership_revision,
                    seen_at=current_time,
                    expected_revision=session.revision,
                ),
                operator=self._operator,
            )
            checked = BrowserSession.model_validate(touched)
        except (CredentialInvalid, IdentityConflict, TypeError, ValueError):
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        expected_idle = min(
            current_time + SESSION_IDLE_LIMIT, session.absolute_expires_at
        )
        if (
            checked.session_id != session.session_id
            or checked.subject_id != session.subject_id
            or checked.tenant_id != session.tenant_id
            or checked.principal_id != session.principal_id
            or checked.application_id != session.application_id
            or checked.application_revision != session.application_revision
            or checked.subject_kind is not session.subject_kind
            or checked.subject_revision != session.subject_revision
            or checked.credential_kind != session.credential_kind
            or checked.credential_tenant_id != session.credential_tenant_id
            or checked.credential_id != session.credential_id
            or checked.credential_revision != session.credential_revision
            or checked.credential_session_epoch != session.credential_session_epoch
            or checked.provider_id != session.provider_id
            or checked.provider_revision != session.provider_revision
            or checked.provider_configuration_fingerprint
            != session.provider_configuration_fingerprint
            or checked.membership_revision != session.membership_revision
            or checked.session_token_digest != session.session_token_digest
            or checked.csrf_token_digest != session.csrf_token_digest
            or checked.authentication_methods != session.authentication_methods
            or checked.restricted is not session.restricted
            or checked.created_at != session.created_at
            or checked.last_seen_at != current_time
            or checked.idle_expires_at != expected_idle
            or checked.absolute_expires_at != session.absolute_expires_at
            or checked.revision != session.revision
            or checked.revoked_at is not None
        ):
            raise _invalid()
        return checked

    def refresh_csrf(self, session_token: str) -> IssuedBrowserSession:
        """Atomically touch one Session and replace only its CSRF credential."""

        current_time = self._current_time()
        session = self._lookup_active(session_token, current_time)
        csrf_token = _random_token(self._random_bytes, 32)
        csrf_digest = _digest("csrf", csrf_token)
        if csrf_digest == session.csrf_token_digest:
            raise RuntimeError("secure random source repeated a CSRF token")
        try:
            touched = self._repository.touch_session(
                TouchBrowserSessionCommand(
                    session_id=session.session_id,
                    subject_id=session.subject_id,
                    tenant_id=session.tenant_id,
                    principal_id=session.principal_id,
                    application_id=self._application_id,
                    application_revision=self._application_revision,
                    credential_tenant_id=session.credential_tenant_id,
                    membership_revision=session.membership_revision,
                    new_csrf_token_digest=csrf_digest,
                    seen_at=current_time,
                    expected_revision=session.revision,
                ),
                operator=self._operator,
            )
            checked = BrowserSession.model_validate(touched)
        except (CredentialInvalid, IdentityConflict, TypeError, ValueError):
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        expected_idle = min(
            current_time + SESSION_IDLE_LIMIT, session.absolute_expires_at
        )
        if (
            checked.session_id != session.session_id
            or checked.subject_id != session.subject_id
            or checked.tenant_id != session.tenant_id
            or checked.principal_id != session.principal_id
            or checked.application_id != session.application_id
            or checked.application_revision != session.application_revision
            or checked.subject_kind is not session.subject_kind
            or checked.subject_revision != session.subject_revision
            or checked.credential_kind != session.credential_kind
            or checked.credential_tenant_id != session.credential_tenant_id
            or checked.credential_id != session.credential_id
            or checked.credential_revision != session.credential_revision
            or checked.credential_session_epoch != session.credential_session_epoch
            or checked.provider_id != session.provider_id
            or checked.provider_revision != session.provider_revision
            or checked.provider_configuration_fingerprint
            != session.provider_configuration_fingerprint
            or checked.membership_revision != session.membership_revision
            or checked.session_token_digest != session.session_token_digest
            or checked.csrf_token_digest != csrf_digest
            or checked.authentication_methods != session.authentication_methods
            or checked.restricted is not session.restricted
            or checked.created_at != session.created_at
            or checked.last_seen_at != current_time
            or checked.idle_expires_at != expected_idle
            or checked.absolute_expires_at != session.absolute_expires_at
            or checked.revision != session.revision
            or checked.revoked_at is not None
        ):
            raise _invalid()
        return IssuedBrowserSession(
            session=checked,
            session_token=SecretStr(session_token),
            csrf_token=SecretStr(csrf_token),
        )

    def switch_tenant(
        self,
        source_session_token: str,
        source_csrf_token: str,
        target_tenant_id: str,
    ) -> IssuedBrowserSession:
        rotated_at = self._current_time()
        source = self._lookup_active(source_session_token, rotated_at)
        source_session_digest = _token_digest("session", source_session_token)
        source_csrf_digest = _token_digest("csrf", source_csrf_token)
        if (
            source_session_digest is None
            or source_csrf_digest is None
            or source_session_digest != source.session_token_digest
            or source_csrf_digest != source.csrf_token_digest
            or source.restricted
            or not _identifier(target_tenant_id)
            or target_tenant_id == source.tenant_id
        ):
            raise _invalid()
        try:
            listed = self._memberships.list_memberships(source.subject_id)
        except Exception:
            raise _unavailable() from None
        try:
            candidates = tuple(TenantMembership.model_validate(item) for item in listed)
        except (TypeError, ValueError):
            raise _invalid() from None
        if len(candidates) > 1000:
            raise _invalid()
        active_targets = tuple(
            item
            for item in candidates
            if item.tenant_id == target_tenant_id
            and item.subject_id == source.subject_id
            and _membership_is_active(item, rotated_at)
        )
        if len(active_targets) != 1:
            raise _invalid()
        target = active_targets[0]
        issued = self._new_session(
            target,
            application_id=self._application_id,
            authentication_methods=source.authentication_methods,
            identity_binding=source,
            now=rotated_at,
            absolute_expires_at=source.absolute_expires_at,
        )
        try:
            result = self._repository.rotate_session(
                RotateBrowserSessionCommand(
                    source_session_id=source.session_id,
                    source_session_token_digest=source_session_digest,
                    source_csrf_token_digest=source_csrf_digest,
                    source_subject_id=source.subject_id,
                    source_subject_kind=source.subject_kind,
                    source_subject_revision=source.subject_revision,
                    source_tenant_id=source.tenant_id,
                    source_principal_id=source.principal_id,
                    source_membership_revision=source.membership_revision,
                    source_application_id=self._application_id,
                    source_application_revision=self._application_revision,
                    source_credential_tenant_id=source.credential_tenant_id,
                    source_credential_kind=source.credential_kind,
                    source_credential_id=source.credential_id,
                    source_credential_revision=source.credential_revision,
                    source_credential_session_epoch=source.credential_session_epoch,
                    source_provider_id=source.provider_id,
                    source_provider_revision=source.provider_revision,
                    source_provider_configuration_fingerprint=(
                        source.provider_configuration_fingerprint
                    ),
                    source_authentication_methods=source.authentication_methods,
                    source_restricted=source.restricted,
                    source_absolute_expires_at=source.absolute_expires_at,
                    rotation_kind=SessionRotationKind.TENANT_SWITCH,
                    replacement_session=issued.session,
                    target_membership_revision=target.revision,
                    revoked_at=rotated_at,
                    expected_source_revision=source.revision,
                ),
                operator=self._operator,
            )
        except (CredentialInvalid, IdentityConflict):
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        try:
            checked_result = BrowserSessionRotationResult.model_validate(result)
        except (TypeError, ValueError):
            raise _invalid() from None
        if (
            checked_result.source_session_id != source.session_id
            or checked_result.source_revoked_at != rotated_at
            or checked_result.replacement_session != issued.session
        ):
            raise _invalid()
        return issued

    def revoke(self, session_token: str, csrf_token: str) -> BrowserSession:
        """Verify the Session-bound CSRF credential and revoke once."""

        revoked_at = self._current_time()
        session = self._lookup_active(session_token, revoked_at)
        csrf_digest = _token_digest("csrf", csrf_token)
        if csrf_digest is None or csrf_digest != session.csrf_token_digest:
            raise _invalid()
        try:
            revoked = self._repository.revoke_session(
                RevokeBrowserSessionCommand(
                    session_id=session.session_id,
                    revoked_at=revoked_at,
                    expected_revision=session.revision,
                ),
                operator=self._operator,
            )
            checked = BrowserSession.model_validate(revoked)
        except (CredentialInvalid, IdentityConflict, TypeError, ValueError):
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        expected = session.model_copy(
            update={
                "revoked_at": revoked_at,
                "revision": session.revision + 1,
            }
        )
        if checked != expected:
            raise _invalid()
        return checked

    def verify_csrf(self, session: BrowserSession, csrf_token: str) -> None:
        try:
            checked = BrowserSession.model_validate(session)
        except (TypeError, ValueError):
            raise _invalid() from None
        digest = _token_digest("csrf", csrf_token)
        current_time = self._current_time()
        if (
            checked.application_id != self._application_id
            or checked.application_revision != self._application_revision
            or digest is None
            or digest != checked.csrf_token_digest
            or checked.revoked_at is not None
            or current_time >= checked.idle_expires_at
            or current_time >= checked.absolute_expires_at
        ):
            raise _invalid()

    def _current_time(self) -> datetime:
        try:
            return _utc_time(self._evidence.current_time())
        except Exception:
            raise _unavailable() from None

    def _lookup_active(self, token: str, now: datetime) -> BrowserSession:
        digest = _token_digest("session", token)
        if digest is None:
            raise _invalid()
        try:
            found = self._repository.find_by_token_digest(digest)
        except CredentialInvalid:
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        try:
            session = (
                BrowserSession.model_validate(found) if found is not None else None
            )
        except (TypeError, ValueError):
            raise _invalid() from None
        if (
            session is None
            or session.session_token_digest != digest
            or session.application_id != self._application_id
            or session.application_revision != self._application_revision
            or session.revoked_at is not None
            or now >= session.idle_expires_at
            or now >= session.absolute_expires_at
        ):
            raise _invalid()
        membership = self._membership(session.tenant_id, session.principal_id)
        if (
            membership is None
            or membership.subject_id != session.subject_id
            or membership.revision != session.membership_revision
            or not _membership_is_active(membership, now)
        ):
            raise _invalid()
        return session

    def _membership(self, tenant_id: str, principal_id: str) -> TenantMembership | None:
        if not _identifier(tenant_id) or not _identifier(principal_id):
            return None
        try:
            found = self._memberships.get_membership(tenant_id, principal_id)
        except Exception:
            raise _unavailable() from None
        try:
            checked = (
                TenantMembership.model_validate(found) if found is not None else None
            )
        except (TypeError, ValueError):
            return None
        if (
            checked is None
            or checked.tenant_id != tenant_id
            or checked.principal_id != principal_id
        ):
            return None
        return checked

    def _new_session(
        self,
        membership: TenantMembership,
        *,
        application_id: str,
        authentication_methods: tuple[str, ...],
        identity_binding: AuthenticationEvidenceRecord | BrowserSession,
        now: datetime,
        absolute_expires_at: datetime | None = None,
    ) -> IssuedBrowserSession:
        if not _identifier(application_id):
            raise _invalid()
        session_id = "ses_" + _random_token(self._random_bytes, 18)
        session_token = _random_token(self._random_bytes, 32)
        csrf_token = _random_token(self._random_bytes, 32)
        if session_token == csrf_token:
            raise RuntimeError("secure random source repeated a Session token")
        absolute_expiry = absolute_expires_at or now + SESSION_ABSOLUTE_LIMIT
        if absolute_expiry <= now:
            raise _invalid()
        session = BrowserSession(
            session_id=session_id,
            tenant_id=membership.tenant_id,
            subject_id=membership.subject_id,
            principal_id=membership.principal_id,
            application_id=application_id,
            application_revision=identity_binding.application_revision,
            subject_kind=SubjectKind.HUMAN,
            subject_revision=identity_binding.subject_revision,
            credential_kind=identity_binding.credential_kind,
            credential_tenant_id=(
                identity_binding.credential_tenant_id
                if isinstance(identity_binding, BrowserSession)
                else identity_binding.tenant_id
            ),
            credential_id=identity_binding.credential_id,
            credential_revision=identity_binding.credential_revision,
            credential_session_epoch=identity_binding.credential_session_epoch,
            provider_id=identity_binding.provider_id,
            provider_revision=identity_binding.provider_revision,
            provider_configuration_fingerprint=(
                identity_binding.provider_configuration_fingerprint
            ),
            session_token_digest=_digest("session", session_token),
            csrf_token_digest=_digest("csrf", csrf_token),
            authentication_methods=authentication_methods,
            restricted=identity_binding.restricted,
            membership_revision=membership.revision,
            created_at=now,
            last_seen_at=now,
            idle_expires_at=min(now + SESSION_IDLE_LIMIT, absolute_expiry),
            absolute_expires_at=absolute_expiry,
            revoked_at=None,
            revision=1,
        )
        return IssuedBrowserSession(
            session=session,
            session_token=SecretStr(session_token),
            csrf_token=SecretStr(csrf_token),
        )


def _membership_is_active(membership: TenantMembership, now: datetime) -> bool:
    return (
        membership.status == "active"
        and membership.valid_from <= now
        and (membership.valid_until is None or now < membership.valid_until)
    )


def _random_token(random_bytes: Callable[[int], bytes], size: int) -> str:
    value = random_bytes(size)
    if type(value) is not bytes or len(value) != size:
        raise RuntimeError("secure random source failed")
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token_digest(purpose: str, token: object) -> SecretDigest32 | None:
    if type(token) is not str or len(token) != 43:
        return None
    try:
        decoded = urlsafe_b64decode(token + "=")
    except (UnicodeError, ValueError):
        return None
    if len(decoded) != 32 or urlsafe_b64encode(decoded).rstrip(b"=").decode() != token:
        return None
    return _digest(purpose, token)


def _digest(purpose: str, token: str) -> SecretDigest32:
    return SecretDigest32(
        sha256(purpose.encode("ascii") + b"\0" + token.encode("ascii")).digest()
    )


def _identifier(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 320 and bool(value.strip())


def _utc_time(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _invalid()
    return value.astimezone(UTC)


def _invalid() -> CredentialInvalid:
    return CredentialInvalid(_FAILURE)


def _unavailable() -> IdentityUnavailable:
    return IdentityUnavailable("identity service is unavailable")


__all__ = [
    "BrowserSessionService",
    "IssuedBrowserSession",
    "SessionCreationUnitOfWork",
]

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from dataclasses import fields as dataclass_fields, is_dataclass, replace
from heapq import heapify, heappush
import inspect
import json
from threading import Barrier, Event, Lock, RLock, Thread
from typing import get_args, get_type_hints

from pydantic import SecretStr
import pytest

from eios.identity.errors import (
    CredentialInvalid,
    IdentityConflict,
    IdentityUnavailable,
)
from eios.identity.evidence import (
    AuthenticationEvidenceConsumerAuthority as AuthenticationEvidenceAuthority,
    AuthenticationEvidenceRecord,
    AuthenticationEvidenceSessionStore,
    AuthenticationEvidenceStore,
    InMemoryAuthenticationEvidenceStore,
)
import eios.identity.evidence as evidence_module
from eios.identity.models import (
    BrowserSession,
    EncodedPasswordHash,
    LocalAccount,
    MembershipKind,
    SecretDigest32,
    SubjectKind,
    Subject,
    TenantMembership,
)
from eios.identity.ports import (
    BrowserSessionRepository,
    BrowserSessionRotationResult,
    CreateBrowserSessionCommand,
    RotateBrowserSessionCommand,
    ResolvedExternalIdentity,
    TouchBrowserSessionCommand,
    TrustedIdentityOperator,
)
from eios.identity.sessions import (
    BrowserSessionService,
    IssuedBrowserSession,
    SessionCreationUnitOfWork,
)
import eios.identity as identity_package


NOW = datetime(2026, 7, 20, 8, tzinfo=UTC)


def test_authentication_evidence_api_is_opaque_and_explicit() -> None:
    authority_type = getattr(identity_package, "AuthenticationEvidenceAuthority", None)
    evidence_type = getattr(identity_package, "AuthenticationEvidence", None)
    assert authority_type is not None
    assert evidence_type is not None
    with pytest.raises(TypeError, match="trusted authority"):
        evidence_type()
    assert not hasattr(evidence_type, "model_construct")
    store_parameter = inspect.signature(authority_type).parameters["store"]
    assert store_parameter.default is inspect.Parameter.empty
    assert "now" not in inspect.signature(authority_type.issue_oidc).parameters
    assert "now" not in inspect.signature(authority_type.issue_password).parameters
    assert "proof_digest" in AuthenticationEvidenceRecord.__dataclass_fields__
    assert "provider_id" in AuthenticationEvidenceRecord.__dataclass_fields__
    facts_type = getattr(identity_package, "AuthenticationEvidenceFacts", None)
    proof_digest = getattr(
        identity_package, "authentication_evidence_proof_digest", None
    )
    restore = getattr(
        identity_package,
        "restore_authentication_evidence_for_trusted_transport",
        None,
    )
    record_exact = getattr(
        identity_package, "authentication_evidence_record_is_exact", None
    )
    assert inspect.isclass(facts_type) and is_dataclass(facts_type)
    assert callable(proof_digest)
    assert callable(restore)
    assert callable(record_exact)
    assert "purpose" in {field.name for field in dataclass_fields(facts_type)}
    assert set(get_args(get_type_hints(facts_type)["purpose"])) == {
        "session.create",
        "password.change",
    }
    invalid_facts = facts_type(
        tenant_id="tenant-a",
        subject_id="subject-1",
        subject_kind="human",
        subject_revision=1,
        subject_status="active",
        credential_kind="local_account",
        credential_tenant_id="tenant-a",
        credential_id="account-1",
        credential_revision=1,
        credential_session_epoch=1,
        application_id="portal",
        application_revision=1,
        authentication_methods=("password",),
        purpose="not-authorized",
        restricted=False,
        authentication_issued_at=None,
        authentication_not_before=None,
        authentication_expires_at=None,
        provider_id=None,
        provider_revision=None,
        provider_configuration_fingerprint=None,
    )
    with pytest.raises(evidence_module.AuthenticationEvidenceRejected):
        InMemoryAuthenticationEvidenceStore(clock=MutableClock()).issue(
            "evidence-invalid-purpose",
            b"p" * 32,
            invalid_facts,
        )


class RandomBytes:
    def __init__(self) -> None:
        self.next = 1

    def __call__(self, size: int) -> bytes:
        result = bytes([self.next]) * size
        self.next += 1
        return result


class RandomBytesFrom(RandomBytes):
    def __init__(self, start: int) -> None:
        self.next = start


class ScriptedSessionRandom:
    def __init__(self, identifier: bytes, token: bytes, csrf: bytes) -> None:
        self.values = ((18, identifier), (32, token), (32, csrf))
        self.index = 0

    def __call__(self, size: int) -> bytes:
        expected_size, byte = self.values[self.index]
        self.index += 1
        assert size == expected_size
        return byte * size


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ReferenceSessionBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.transaction_lock = RLock()


def _membership(
    tenant_id: str,
    principal_id: str,
    *,
    status: str = "active",
    revision: int = 1,
) -> TenantMembership:
    return TenantMembership(
        tenant_id=tenant_id,
        subject_id="subject-1",
        principal_id=principal_id,
        kind=MembershipKind.HOME if tenant_id == "tenant-a" else MembershipKind.GUEST,
        status=status,
        valid_from=NOW - timedelta(days=1),
        valid_until=None,
        trusted_attributes={},
        revision=revision,
    )


class MembershipStore:
    def __init__(self, memberships: tuple[TenantMembership, ...]) -> None:
        self.memberships = memberships

    def get_membership(
        self, tenant_id: str, principal_id: str
    ) -> TenantMembership | None:
        return next(
            (
                item
                for item in self.memberships
                if (item.tenant_id, item.principal_id) == (tenant_id, principal_id)
            ),
            None,
        )

    def list_memberships(self, subject_id: str) -> tuple[TenantMembership, ...]:
        return tuple(item for item in self.memberships if item.subject_id == subject_id)


class SessionStore:
    def __init__(
        self,
        memberships: MembershipStore | None = None,
        evidence_backend: object | None = None,
        *,
        session_backend: ReferenceSessionBackend,
    ) -> None:
        self.create_attempts: list[BrowserSession] = []
        self.rotation_attempts: list[BrowserSession] = []
        self.memberships = memberships
        if isinstance(evidence_backend, AuthenticationEvidenceAuthority):
            self.evidence_store = evidence_backend._store  # noqa: SLF001
        elif evidence_backend is None:
            self.evidence_store = InMemoryAuthenticationEvidenceStore(
                clock=MutableClock()
            )
        else:
            self.evidence_store = evidence_backend
        self.session_backend = session_backend
        self.sessions = self.session_backend.sessions
        self.lock = self.session_backend.transaction_lock
        self.evidence_authority = AuthenticationEvidenceAuthority(
            store=self.evidence_store  # type: ignore[arg-type]
        )
        self.before_create_commit: object | None = None
        self.after_uniqueness_check: object | None = None
        self.before_touch_commit: object | None = None
        self.before_rotation_commit: object | None = None
        self.after_rotation_uniqueness_check: object | None = None
        self.before_rotation_commit_complete: object | None = None
        self.before_rotation_failure: object | None = None
        self.replacement_mismatch = False
        self.failure_stage: str | None = None
        self.rotation_failure_stage: str | None = None
        self.subjects: dict[str, Subject] = {
            "subject-1": Subject(
                subject_id="subject-1",
                kind=SubjectKind.HUMAN,
                status="active",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
                revision=1,
            )
        }
        self.credentials: dict[tuple[str, str], dict[str, object]] = {
            ("external_identity", "external-identity-1"): {
                "tenant_id": "tenant-a",
                "subject_id": "subject-1",
                "status": "active",
                "revision": 1,
                "session_epoch": 1,
                "provider_status": "active",
                "provider_id": "provider-1",
                "provider_revision": 1,
                "provider_configuration_fingerprint": "0" * 64,
            }
        }
        self.applications: dict[str, tuple[str, int]] = {
            "portal": ("active", 1),
            "other-app": ("active", 1),
            "admin": ("active", 1),
        }

    def issue(self, *args: object, **kwargs: object) -> object:
        return self.evidence_store.issue(*args, **kwargs)  # type: ignore[attr-defined]

    def current_time(self) -> datetime:
        return self.evidence_store.current_time()  # type: ignore[attr-defined]

    def inspect(self, *args: object, **kwargs: object) -> object:
        return self.evidence_store.inspect(*args, **kwargs)  # type: ignore[attr-defined]

    def consume(self, *args: object, **kwargs: object) -> object:
        return self.evidence_store.consume(*args, **kwargs)  # type: ignore[attr-defined]

    def _identity_is_current(self, session: BrowserSession) -> bool:
        subject = self.subjects.get(session.subject_id)
        credential = self.credentials.get(
            (session.credential_kind, session.credential_id)
        )
        application = self.applications.get(session.application_id)
        return bool(
            subject is not None
            and subject.kind is SubjectKind.HUMAN
            and subject.status == "active"
            and subject.revision == session.subject_revision
            and credential is not None
            and credential["tenant_id"] == session.credential_tenant_id
            and credential["subject_id"] == session.subject_id
            and credential["status"] == "active"
            and credential["revision"] == session.credential_revision
            and credential["session_epoch"] == session.credential_session_epoch
            and (
                session.credential_kind != "external_identity"
                or credential.get("provider_status") == "active"
            )
            and credential.get("provider_id") == session.provider_id
            and credential["provider_revision"] == session.provider_revision
            and credential["provider_configuration_fingerprint"]
            == session.provider_configuration_fingerprint
            and application == ("active", session.application_revision)
        )

    def create_session(
        self,
        command: CreateBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSession:
        del operator
        with self.lock:
            consumed_record: AuthenticationEvidenceRecord | None = None
            inserted_session: BrowserSession | None = None
            replaced_session: BrowserSession | None = None
            try:
                if callable(self.before_create_commit):
                    self.before_create_commit()
                session = command.session
                self.create_attempts.append(session)
                membership = (
                    None
                    if self.memberships is None
                    else self.memberships.get_membership(
                        session.tenant_id, session.principal_id
                    )
                )
                if (
                    membership is None
                    or membership.status != "active"
                    or membership.subject_id != session.subject_id
                    or membership.revision != session.membership_revision
                    or membership.valid_from > session.created_at
                    or (
                        membership.valid_until is not None
                        and session.created_at >= membership.valid_until
                    )
                    or not self._identity_is_current(session)
                ):
                    raise IdentityConflict("membership changed")
                if command.replaced_session_id is not None:
                    replaced_session = self.sessions.get(command.replaced_session_id)
                    if (
                        replaced_session is None
                        or replaced_session.revoked_at is not None
                        or replaced_session.session_token_digest
                        != command.replaced_session_token_digest
                        or replaced_session.revision
                        != command.expected_replaced_session_revision
                        or replaced_session.application_id != session.application_id
                        or replaced_session.application_revision
                        != session.application_revision
                    ):
                        raise IdentityConflict("replaced Session changed")
                if session.session_id in self.sessions or any(
                    current.session_token_digest == session.session_token_digest
                    for current in self.sessions.values()
                ):
                    raise IdentityConflict("global Session identifier collision")
                if callable(self.after_uniqueness_check):
                    self.after_uniqueness_check()
                self._fail_create("after_revalidation")
                restored = evidence_module.restore_authentication_evidence_for_trusted_transport(
                    command.authentication_evidence_id,
                    command.authentication_evidence_proof.get_secret_value(),
                )
                consumed_record = self.evidence_authority.inspect_for_session(
                    restored,
                    tenant_id=session.tenant_id,
                    subject_id=session.subject_id,
                    application_id=session.application_id,
                )
                record = self.evidence_authority.consume_for_session(
                    restored,
                    tenant_id=session.tenant_id,
                    subject_id=session.subject_id,
                    application_id=session.application_id,
                )
                self._fail_create("after_consume")
                if (
                    record.subject_revision != session.subject_revision
                    or record.credential_kind != session.credential_kind
                    or record.credential_tenant_id != session.credential_tenant_id
                    or record.credential_id != session.credential_id
                    or record.credential_revision != session.credential_revision
                    or record.credential_session_epoch
                    != session.credential_session_epoch
                    or record.application_revision != session.application_revision
                    or record.provider_id != session.provider_id
                    or record.provider_revision != session.provider_revision
                    or record.provider_configuration_fingerprint
                    != session.provider_configuration_fingerprint
                    or record.restricted is not session.restricted
                ):
                    raise IdentityConflict("evidence binding changed")
                if replaced_session is not None:
                    self.sessions[replaced_session.session_id] = (
                        replaced_session.model_copy(
                            update={
                                "revoked_at": session.created_at,
                                "revision": replaced_session.revision + 1,
                            }
                        )
                    )
                self.sessions[session.session_id] = session
                inserted_session = session
                self._fail_create("after_insert")
                self._fail_create("commit")
                return session
            except Exception:
                if (
                    inserted_session is not None
                    and self.sessions.get(inserted_session.session_id)
                    == inserted_session
                ):
                    del self.sessions[inserted_session.session_id]
                if consumed_record is not None:
                    self._restore_consumed_evidence(consumed_record)
                if replaced_session is not None:
                    self.sessions[replaced_session.session_id] = replaced_session
                raise

    def _restore_consumed_evidence(self, record: AuthenticationEvidenceRecord) -> None:
        store = self.evidence_store
        now = store.current_time()  # type: ignore[attr-defined]
        with store._lock:  # type: ignore[attr-defined]  # noqa: SLF001
            tombstone = store._replay_tombstones.get(record.evidence_id)  # type: ignore[attr-defined]  # noqa: SLF001
            if tombstone is None or tombstone.proof_digest != record.proof_digest:
                return
            store._replay_tombstones.pop(record.evidence_id)  # type: ignore[attr-defined]  # noqa: SLF001
            store._proof_tombstones.pop(record.proof_digest, None)  # type: ignore[attr-defined]  # noqa: SLF001
            store._replay_expirations = [  # type: ignore[attr-defined]  # noqa: SLF001
                item
                for item in store._replay_expirations  # type: ignore[attr-defined]  # noqa: SLF001
                if item[1] != record.evidence_id
            ]
            heapify(store._replay_expirations)  # type: ignore[attr-defined]  # noqa: SLF001
            if now >= record.expires_at:
                store._decrement_scope(record.tenant_id, record.application_id)  # type: ignore[attr-defined]  # noqa: SLF001
                store._expirations = [  # type: ignore[attr-defined]  # noqa: SLF001
                    item
                    for item in store._expirations  # type: ignore[attr-defined]  # noqa: SLF001
                    if item[1] != record.evidence_id
                ]
                store._issuances = [  # type: ignore[attr-defined]  # noqa: SLF001
                    item
                    for item in store._issuances  # type: ignore[attr-defined]  # noqa: SLF001
                    if item[1] != record.evidence_id
                ]
                heapify(store._expirations)  # type: ignore[attr-defined]  # noqa: SLF001
                heapify(store._issuances)  # type: ignore[attr-defined]  # noqa: SLF001
                return
            store._records[record.evidence_id] = evidence_module._StoredEvidence(  # type: ignore[attr-defined]  # noqa: SLF001
                record=record,
                proof_digest=record.proof_digest,
            )
            store._proof_index[record.proof_digest] = record.evidence_id  # type: ignore[attr-defined]  # noqa: SLF001
            expiration = (record.expires_at, record.evidence_id)
            issuance = (-record.issued_at.timestamp(), record.evidence_id)
            if expiration not in store._expirations:  # type: ignore[attr-defined]  # noqa: SLF001
                heappush(store._expirations, expiration)  # type: ignore[attr-defined]  # noqa: SLF001
            if issuance not in store._issuances:  # type: ignore[attr-defined]  # noqa: SLF001
                heappush(store._issuances, issuance)  # type: ignore[attr-defined]  # noqa: SLF001

    def _fail_create(self, stage: str) -> None:
        if self.failure_stage == stage:
            raise RuntimeError(f"injected Session creation {stage} failure")

    def find_by_token_digest(
        self, token_digest: SecretDigest32
    ) -> BrowserSession | None:
        with self.lock:
            return next(
                (
                    item
                    for item in self.sessions.values()
                    if item.session_token_digest == token_digest
                ),
                None,
            )

    def get_session(self, session_id: str) -> BrowserSession | None:
        with self.lock:
            return self.sessions.get(session_id)

    def touch_session(
        self,
        command: TouchBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSession:
        del operator
        with self.lock:
            if callable(self.before_touch_commit):
                self.before_touch_commit()
            current = self.sessions[command.session_id]
            membership = (
                None
                if self.memberships is None
                else self.memberships.get_membership(
                    command.tenant_id, command.principal_id
                )
            )
            if (
                current.revision != command.expected_revision
                or current.subject_id != command.subject_id
                or current.tenant_id != command.tenant_id
                or current.principal_id != command.principal_id
                or current.application_id != command.application_id
                or current.application_revision != command.application_revision
                or current.credential_tenant_id != command.credential_tenant_id
                or current.membership_revision != command.membership_revision
                or membership is None
                or membership.status != "active"
                or membership.subject_id != command.subject_id
                or membership.revision != command.membership_revision
                or membership.valid_from > command.seen_at
                or (
                    membership.valid_until is not None
                    and command.seen_at >= membership.valid_until
                )
                or not self._identity_is_current(current)
            ):
                raise IdentityConflict("session or membership changed")
            updated = current.model_copy(
                update={
                    "last_seen_at": command.seen_at,
                    "idle_expires_at": min(
                        command.seen_at + timedelta(days=7),
                        current.absolute_expires_at,
                    ),
                    "csrf_token_digest": (
                        current.csrf_token_digest
                        if command.new_csrf_token_digest is None
                        else command.new_csrf_token_digest
                    ),
                    "revision": current.revision,
                }
            )
            self.sessions[current.session_id] = updated
            return updated

    def revoke_session(
        self,
        command,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSession:
        del operator
        with self.lock:
            current = self.sessions[command.session_id]
            if (
                current.revision != command.expected_revision
                or current.revoked_at is not None
            ):
                raise IdentityConflict("session changed")
            revoked = current.model_copy(
                update={
                    "revoked_at": command.revoked_at,
                    "revision": current.revision + 1,
                }
            )
            self.sessions[current.session_id] = revoked
            return revoked

    def rotate_session(
        self,
        command: RotateBrowserSessionCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> BrowserSessionRotationResult:
        del operator
        with self.lock:
            if callable(self.before_rotation_commit):
                self.before_rotation_commit()
            replacement = command.replacement_session
            if self.replacement_mismatch:
                replacement = replacement.model_copy(
                    update={"authentication_methods": ("oidc", "pwd")}
                )
            self.rotation_attempts.append(replacement)
            current = self.sessions.get(command.source_session_id)
            target = (
                None
                if self.memberships is None
                else self.memberships.get_membership(
                    replacement.tenant_id,
                    replacement.principal_id,
                )
            )
            source_membership = (
                None
                if self.memberships is None
                else self.memberships.get_membership(
                    command.source_tenant_id, command.source_principal_id
                )
            )
            if (
                current is None
                or current.revoked_at is not None
                or current.revision != command.expected_source_revision
                or current.session_token_digest != command.source_session_token_digest
                or current.csrf_token_digest != command.source_csrf_token_digest
                or current.subject_id != command.source_subject_id
                or current.subject_kind is not command.source_subject_kind
                or current.subject_revision != command.source_subject_revision
                or current.tenant_id != command.source_tenant_id
                or current.principal_id != command.source_principal_id
                or current.application_id != command.source_application_id
                or current.application_revision != command.source_application_revision
                or current.credential_tenant_id != command.source_credential_tenant_id
                or current.credential_kind != command.source_credential_kind
                or current.credential_id != command.source_credential_id
                or current.credential_revision != command.source_credential_revision
                or current.credential_session_epoch
                != command.source_credential_session_epoch
                or current.provider_id != command.source_provider_id
                or current.provider_revision != command.source_provider_revision
                or current.provider_configuration_fingerprint
                != command.source_provider_configuration_fingerprint
                or current.authentication_methods
                != command.source_authentication_methods
                or current.restricted is not command.source_restricted
                or current.absolute_expires_at != command.source_absolute_expires_at
                or source_membership is None
                or source_membership.status != "active"
                or source_membership.subject_id != command.source_subject_id
                or source_membership.revision != command.source_membership_revision
                or source_membership.valid_from > command.revoked_at
                or (
                    source_membership.valid_until is not None
                    and command.revoked_at >= source_membership.valid_until
                )
                or target is None
                or target.status != "active"
                or target.subject_id != command.source_subject_id
                or target.revision != command.target_membership_revision
                or target.valid_from > command.revoked_at
                or (
                    target.valid_until is not None
                    and command.revoked_at >= target.valid_until
                )
                or not self._identity_is_current(current)
                or not self._identity_is_current(replacement)
                or not self._replacement_inherits(current, replacement)
                or replacement.session_id in self.sessions
                or any(
                    session.session_token_digest == replacement.session_token_digest
                    for session in self.sessions.values()
                )
            ):
                raise IdentityConflict("session state changed")
            if callable(self.after_rotation_uniqueness_check):
                self.after_rotation_uniqueness_check()
            revoked_source = current.model_copy(
                update={
                    "revoked_at": command.revoked_at,
                    "revision": current.revision + 1,
                }
            )
            try:
                self.sessions[current.session_id] = revoked_source
                self._fail_rotation("after_source_revoke")
                self.sessions[replacement.session_id] = replacement
                self._fail_rotation("after_replacement_insert")
                if callable(self.before_rotation_commit_complete):
                    self.before_rotation_commit_complete()
                self._fail_rotation("commit")
                return BrowserSessionRotationResult(
                    source_session_id=current.session_id,
                    source_revoked_at=command.revoked_at,
                    replacement_session=replacement,
                )
            except Exception:
                if self.sessions.get(replacement.session_id) == replacement:
                    del self.sessions[replacement.session_id]
                if self.sessions.get(current.session_id) == revoked_source:
                    self.sessions[current.session_id] = current
                raise

    def _fail_rotation(self, stage: str) -> None:
        if self.rotation_failure_stage == stage:
            if callable(self.before_rotation_failure):
                self.before_rotation_failure(stage)
            raise RuntimeError(f"injected Session rotation {stage} failure")

    @staticmethod
    def _replacement_inherits(
        source: BrowserSession,
        replacement: BrowserSession,
    ) -> bool:
        return all(
            getattr(source, field_name) == getattr(replacement, field_name)
            for field_name in (
                "subject_id",
                "subject_kind",
                "subject_revision",
                "application_id",
                "application_revision",
                "credential_kind",
                "credential_tenant_id",
                "credential_id",
                "credential_revision",
                "credential_session_epoch",
                "provider_id",
                "provider_revision",
                "provider_configuration_fingerprint",
                "authentication_methods",
                "restricted",
                "absolute_expires_at",
            )
        )


def _operator() -> TrustedIdentityOperator:
    return TrustedIdentityOperator(
        operator_principal_id="identity-service",
        request_id="request-1",
        trace_id="trace-1",
    )


def _service(
    *,
    memberships: tuple[TenantMembership, ...] | None = None,
    application_id: str = "portal",
    now: datetime = NOW,
) -> tuple[BrowserSessionService, SessionStore, AuthenticationEvidenceAuthority]:
    member_store = MembershipStore(
        memberships
        or (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_store = InMemoryAuthenticationEvidenceStore(clock=MutableClock(now))
    store = SessionStore(
        member_store,
        evidence_store,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        store,
        member_store,
        application_id=application_id,
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    return (
        service,
        store,
        service._evidence,  # noqa: SLF001
    )


def _evidence(
    authority: AuthenticationEvidenceAuthority,
    *,
    tenant_id: str = "tenant-a",
    subject_id: str = "subject-1",
    methods: tuple[str, ...] = ("oidc",),
    application_id: str = "portal",
    now: datetime = NOW,
) -> object:
    try:
        token_time = authority.current_time()
    except IdentityUnavailable:
        token_time = now
    return authority.issue_oidc(
        resolved_identity=ResolvedExternalIdentity(
            external_identity_id="external-identity-1",
            tenant_id=tenant_id,
            provider_id="provider-1",
            issuer="https://id.example.com",
            external_subject="external-subject-1",
            subject_id=subject_id,
            external_identity_revision=1,
            external_identity_status="active",
            external_identity_session_epoch=1,
            subject_revision=1,
            subject_kind="human",
            subject_status="active",
            provider_revision=1,
            provider_configuration_fingerprint="0" * 64,
        ),
        authentication_methods=methods,
        application_id=application_id,
        token_issued_at=token_time - timedelta(minutes=1),
        token_not_before=None,
        token_expires_at=token_time + timedelta(minutes=10),
    )


def test_session_application_is_trusted_configuration_not_caller_input() -> None:
    assert (
        "application_id"
        not in inspect.signature(BrowserSessionService.create).parameters
    )
    service, store, authority = _service(application_id="portal")
    evidence = _evidence(authority, application_id="other-app")

    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        service.create("tenant-a", "subject-1", "principal-a", evidence)
    assert store.sessions == {}
    with pytest.raises(CredentialInvalid):
        service.create("tenant-a", "subject-1", "principal-a", evidence)


def test_every_session_operation_is_bound_to_trusted_application() -> None:
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    authority = AuthenticationEvidenceAuthority(
        store=InMemoryAuthenticationEvidenceStore(), random_bytes=RandomBytes()
    )
    sessions = SessionStore(
        memberships,
        authority,
        session_backend=ReferenceSessionBackend(),
    )
    portal = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    other = BrowserSessionService(
        sessions,
        memberships,
        application_id="other-app",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    issued = tuple(
        portal.create(
            "tenant-a",
            "subject-1",
            "principal-a",
            _evidence(authority),
        )
        for _ in range(2)
    )

    with pytest.raises(CredentialInvalid):
        other.authenticate(issued[0].session_token.get_secret_value())
    assert sessions.sessions[issued[0].session.session_id].revision == 1
    with pytest.raises(CredentialInvalid):
        other.verify_csrf(issued[0].session, issued[0].csrf_token.get_secret_value())
    with pytest.raises(CredentialInvalid):
        other.switch_tenant(
            issued[1].session_token.get_secret_value(),
            issued[1].csrf_token.get_secret_value(),
            "tenant-b",
        )
    assert sessions.sessions[issued[1].session.session_id].revoked_at is None


def test_must_change_password_evidence_creates_a_restricted_session_end_to_end() -> (
    None
):
    service, store, authority = _service()
    account = LocalAccount(
        local_account_id="account-1",
        tenant_id="tenant-a",
        subject_id="subject-1",
        username="alice",
        verified_email="alice@example.com",
        password_hash=EncodedPasswordHash(
            "pbkdf2_sha256$100000$" + "00" * 16 + "$" + "00" * 32
        ),
        password_history=(),
        status="active",
        failed_attempts=0,
        lockout_level=0,
        locked_until=None,
        must_change_password=True,
        session_epoch=1,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
        revision=1,
    )
    evidence = authority.issue_password(
        account,
        Subject(
            subject_id="subject-1",
            kind=SubjectKind.HUMAN,
            status="active",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
            revision=1,
        ),
        application_id="portal",
    )
    store.credentials[("local_account", "account-1")] = {
        "tenant_id": "tenant-a",
        "subject_id": "subject-1",
        "status": "active",
        "revision": 1,
        "session_epoch": 1,
        "provider_id": None,
        "provider_revision": None,
        "provider_configuration_fingerprint": None,
    }

    issued = service.create("tenant-a", "subject-1", "principal-a", evidence)

    assert issued.session.restricted is True
    assert store.sessions[issued.session.session_id].restricted is True
    with pytest.raises(CredentialInvalid):
        service.create("tenant-a", "subject-1", "principal-a", evidence)


def test_authentication_evidence_exact_ttl_clock_rollback_and_shared_store() -> None:
    clock = MutableClock()
    store = InMemoryAuthenticationEvidenceStore(capacity=2, clock=clock)
    issuer = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    consumer = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    evidence = _evidence(issuer)
    clock.now = NOW + timedelta(minutes=5) - timedelta(microseconds=1)
    assert consumer.consume_for_session(
        evidence,
        tenant_id="tenant-a",
        subject_id="subject-1",
        application_id="portal",
    ).authentication_methods == ("oidc",)

    for offset in (timedelta(minutes=5), timedelta(minutes=5, microseconds=1)):
        boundary_clock = MutableClock()
        boundary_store = InMemoryAuthenticationEvidenceStore(
            capacity=1, clock=boundary_clock
        )
        boundary = AuthenticationEvidenceAuthority(
            store=boundary_store, random_bytes=RandomBytes()
        )
        expired = _evidence(boundary)
        boundary_clock.now = NOW + offset
        with pytest.raises(CredentialInvalid):
            boundary.consume_for_session(
                expired,
                tenant_id="tenant-a",
                subject_id="subject-1",
                application_id="portal",
            )

    contract = inspect.getdoc(AuthenticationEvidenceStore.issue) or ""
    assert "shared" in contract
    assert "atomically" in contract


def test_authentication_evidence_handle_reconstructs_across_processes_once() -> None:
    restore = getattr(
        evidence_module,
        "restore_authentication_evidence_for_trusted_transport",
        None,
    )
    assert restore is not None
    clock = MutableClock()
    store = InMemoryAuthenticationEvidenceStore(clock=clock)
    issuer = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    consumer = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    handle = _evidence(issuer)
    exported = handle._export_for_trusted_transport()  # noqa: SLF001
    assert exported[0] == handle.evidence_id
    assert "<redacted>" in repr(handle)
    assert exported[1] not in repr(handle)
    with pytest.raises(TypeError):
        json.dumps(handle)

    reconstructed = restore(*exported)
    assert reconstructed is not handle
    record = consumer.consume_for_session(
        reconstructed,
        tenant_id="tenant-a",
        subject_id="subject-1",
        application_id="portal",
    )
    assert record.authentication_methods == ("oidc",)
    assert record.credential_revision == 1
    with pytest.raises(CredentialInvalid):
        consumer.consume_for_session(
            reconstructed,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )


def test_authentication_evidence_forged_proof_does_not_consume_valid_record() -> None:
    restore = getattr(
        evidence_module,
        "restore_authentication_evidence_for_trusted_transport",
        None,
    )
    assert restore is not None
    store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    authority = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    handle = _evidence(authority)
    evidence_id, proof = handle._export_for_trusted_transport()  # noqa: SLF001
    forged = restore(evidence_id, "f" * len(proof))

    with pytest.raises(CredentialInvalid):
        authority.consume_for_session(
            forged,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )
    assert (
        authority.consume_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        ).tenant_id
        == "tenant-a"
    )


def test_authentication_evidence_wrong_scope_is_rejected_and_burned() -> None:
    store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    authority = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    handle = _evidence(authority)

    with pytest.raises(CredentialInvalid):
        authority.consume_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="other-app",
        )
    with pytest.raises(CredentialInvalid):
        authority.consume_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )


def test_evidence_inspection_delegates_expected_scope_to_the_store() -> None:
    class ScopedInspectionStore:
        def __init__(self) -> None:
            self.delegate = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
            self.inspection: tuple[str, str, str] | None = None

        def current_time(self) -> datetime:
            return self.delegate.current_time()

        def issue(self, *args: object, **kwargs: object) -> object:
            return self.delegate.issue(*args, **kwargs)  # type: ignore[arg-type]

        def inspect(
            self,
            evidence_id: str,
            proof_digest: bytes,
            *,
            tenant_id: str,
            application_id: str,
            purpose: str,
        ) -> object:
            self.inspection = (tenant_id, application_id, purpose)
            return self.delegate.inspect(
                evidence_id,
                proof_digest,
                tenant_id=tenant_id,
                application_id=application_id,
                purpose=purpose,
            )

        def consume(self, *args: object, **kwargs: object) -> object:
            return self.delegate.consume(*args, **kwargs)  # type: ignore[arg-type]

    store = ScopedInspectionStore()
    authority = AuthenticationEvidenceAuthority(
        store=store,  # type: ignore[arg-type]
        random_bytes=RandomBytes(),
    )
    handle = _evidence(authority)

    assert (
        authority.inspect_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        ).purpose
        == "session.create"
    )
    assert store.inspection == ("tenant-a", "portal", "session.create")


def test_authentication_evidence_consumption_is_single_winner_across_threads() -> None:
    store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    issuer = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    handle = _evidence(issuer)
    exported = handle._export_for_trusted_transport()  # noqa: SLF001
    barrier = Barrier(2)
    outcomes: list[str] = []

    def consume() -> None:
        consumer = AuthenticationEvidenceAuthority(store=store)
        reconstructed = (
            evidence_module.restore_authentication_evidence_for_trusted_transport(
                *exported
            )
        )
        barrier.wait()
        try:
            consumer.consume_for_session(
                reconstructed,
                tenant_id="tenant-a",
                subject_id="subject-1",
                application_id="portal",
            )
        except CredentialInvalid:
            outcomes.append("invalid")
        else:
            outcomes.append("consumed")

    threads = (Thread(target=consume), Thread(target=consume))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["consumed", "invalid"]


def test_authentication_evidence_capacity_is_per_tenant_and_application() -> None:
    clock = MutableClock()
    store = InMemoryAuthenticationEvidenceStore(capacity=1, clock=clock)
    authority = AuthenticationEvidenceAuthority(store=store, random_bytes=RandomBytes())
    _evidence(authority, tenant_id="tenant-a", application_id="portal")
    with pytest.raises(IdentityUnavailable):
        _evidence(authority, tenant_id="tenant-a", application_id="portal")
    _evidence(authority, tenant_id="tenant-b", application_id="portal")
    _evidence(authority, tenant_id="tenant-a", application_id="admin")

    clock.now = NOW + timedelta(minutes=5)
    _evidence(
        authority,
        tenant_id="tenant-a",
        application_id="portal",
        now=clock.now,
    )
    clock.now = NOW
    _evidence(authority, tenant_id="tenant-a", application_id="portal")


def test_authentication_evidence_duplicate_id_never_overwrites_first_record() -> None:
    def fixed_random(size: int) -> bytes:
        return b"x" * size

    store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    authority = AuthenticationEvidenceAuthority(store=store, random_bytes=fixed_random)
    first = _evidence(authority, subject_id="subject-1")
    with pytest.raises(IdentityUnavailable):
        _evidence(authority, subject_id="subject-2")
    assert (
        authority.consume_for_session(
            first,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        ).subject_id
        == "subject-1"
    )


def test_authentication_evidence_store_failures_are_redacted() -> None:
    class BrokenStore:
        def issue(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("redis password=super-secret")

        def inspect(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("redis password=super-secret")

        def consume(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("redis password=super-secret")

    authority = AuthenticationEvidenceAuthority(
        store=BrokenStore(),  # type: ignore[arg-type]
        random_bytes=RandomBytes(),
    )
    with pytest.raises(IdentityUnavailable) as error:
        _evidence(authority)
    assert "super-secret" not in str(error.value)


@pytest.mark.parametrize(
    "store_error",
    (
        IdentityConflict("database secret-needle"),
        CredentialInvalid("database secret-needle"),
        IdentityUnavailable("database secret-needle"),
    ),
)
def test_authentication_evidence_normalizes_preexisting_identity_errors(
    store_error: Exception,
) -> None:
    class BrokenStore:
        def issue(self, *args: object, **kwargs: object) -> object:
            raise store_error

        def inspect(self, *args: object, **kwargs: object) -> object:
            raise store_error

        def consume(self, *args: object, **kwargs: object) -> object:
            raise store_error

    authority = AuthenticationEvidenceAuthority(
        store=BrokenStore(),  # type: ignore[arg-type]
        random_bytes=RandomBytes(),
    )
    with pytest.raises(
        IdentityUnavailable, match="^authentication evidence is unavailable$"
    ) as caught:
        _evidence(authority)
    assert "secret-needle" not in str(caught.value)


def test_authentication_evidence_rejects_malformed_store_records_generically() -> None:
    class MalformedStore:
        def __init__(self) -> None:
            self.delegate = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
            self.inspect_mutation: str | None = None

        def issue(self, *args: object, **kwargs: object) -> object:
            record = self.delegate.issue(*args, **kwargs)  # type: ignore[arg-type]
            return object() if self.inspect_mutation == "issue_type" else record

        def current_time(self) -> datetime:
            return self.delegate.current_time()

        def inspect(self, *args: object, **kwargs: object) -> object:
            record = self.delegate.inspect(*args, **kwargs)  # type: ignore[arg-type]
            if self.inspect_mutation == "inspect_type":
                return object()
            if self.inspect_mutation == "wrong_id" and record is not None:
                return replace(record, evidence_id="aev_" + "f" * 36)
            if self.inspect_mutation == "wrong_digest" and record is not None:
                return replace(record, proof_digest=b"f" * 32)
            if self.inspect_mutation == "methods_type" and record is not None:
                return replace(record, authentication_methods=object())  # type: ignore[arg-type]
            if self.inspect_mutation == "revision_zero" and record is not None:
                return replace(record, credential_revision=0)
            if self.inspect_mutation == "provider_missing" and record is not None:
                return replace(record, provider_revision=None)
            if self.inspect_mutation == "ttl_too_long" and record is not None:
                return replace(
                    record,
                    expires_at=record.issued_at + timedelta(minutes=6),
                )
            if self.inspect_mutation == "expired" and record is not None:
                return replace(record, expires_at=record.issued_at)
            if self.inspect_mutation == "restricted_int" and record is not None:
                return replace(record, restricted=0)  # type: ignore[arg-type]
            if self.inspect_mutation == "ttl_shortened" and record is not None:
                return replace(
                    record,
                    expires_at=record.issued_at + timedelta(minutes=1),
                )
            return record

        def consume(self, *args: object, **kwargs: object) -> object:
            return self.delegate.consume(*args, **kwargs)  # type: ignore[arg-type]

    malformed = MalformedStore()
    authority = AuthenticationEvidenceAuthority(
        store=malformed,  # type: ignore[arg-type]
        random_bytes=RandomBytes(),
    )
    malformed.inspect_mutation = "issue_type"
    with pytest.raises(IdentityUnavailable):
        _evidence(authority)

    for mutation in (
        "inspect_type",
        "wrong_id",
        "wrong_digest",
        "methods_type",
        "revision_zero",
        "provider_missing",
        "ttl_too_long",
        "expired",
        "restricted_int",
        "ttl_shortened",
    ):
        malformed = MalformedStore()
        authority = AuthenticationEvidenceAuthority(
            store=malformed,  # type: ignore[arg-type]
            random_bytes=RandomBytes(),
        )
        handle = _evidence(authority)
        malformed.inspect_mutation = mutation
        with pytest.raises(
            CredentialInvalid, match="^authentication evidence is invalid or expired$"
        ):
            authority.inspect_for_session(
                handle,
                tenant_id="tenant-a",
                subject_id="subject-1",
                application_id="portal",
            )


@pytest.mark.parametrize("operation", ("issue", "inspect", "consume"))
@pytest.mark.parametrize(
    "offset",
    (timedelta(hours=1), timedelta(hours=-5, minutes=-30)),
)
def test_evidence_public_operations_reject_store_nonzero_offset_datetimes(
    operation: str,
    offset: timedelta,
) -> None:
    class OffsetStore:
        def __init__(self) -> None:
            self.delegate = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
            self.mutate = False

        def current_time(self) -> datetime:
            return self.delegate.current_time()

        def _returned(self, record: AuthenticationEvidenceRecord) -> object:
            if not self.mutate:
                return record
            represented = record.issued_at.astimezone(timezone(offset))
            assert represented == record.issued_at
            assert represented.utcoffset() == offset
            return replace(record, issued_at=represented)

        def issue(self, *args: object, **kwargs: object) -> object:
            record = self.delegate.issue(*args, **kwargs)  # type: ignore[arg-type]
            return self._returned(record)

        def inspect(self, *args: object, **kwargs: object) -> object:
            record = self.delegate.inspect(*args, **kwargs)  # type: ignore[arg-type]
            assert record is not None
            return self._returned(record)

        def consume(self, *args: object, **kwargs: object) -> object:
            record = self.delegate.consume(*args, **kwargs)  # type: ignore[arg-type]
            assert record is not None
            return self._returned(record)

    store = OffsetStore()
    authority = AuthenticationEvidenceAuthority(
        store=store,  # type: ignore[arg-type]
        random_bytes=RandomBytes(),
    )
    if operation == "issue":
        store.mutate = True
        with pytest.raises(
            IdentityUnavailable,
            match="^authentication evidence is unavailable$",
        ):
            _evidence(authority)
        return

    handle = _evidence(authority)
    store.mutate = True
    call = (
        authority.inspect_for_session
        if operation == "inspect"
        else authority.consume_for_session
    )
    with pytest.raises(
        CredentialInvalid,
        match="^authentication evidence is invalid or expired$",
    ):
        call(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )


def test_evidence_expiry_is_checked_even_when_cleanup_index_is_missing() -> None:
    clock = MutableClock()
    store = InMemoryAuthenticationEvidenceStore(clock=clock)
    authority = AuthenticationEvidenceAuthority(
        store=store,
        random_bytes=RandomBytes(),
    )
    handle = _evidence(authority)
    store._expirations.clear()  # noqa: SLF001 - corrupt-index resilience
    clock.now = NOW + timedelta(minutes=5)

    with pytest.raises(CredentialInvalid):
        authority.inspect_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )


def test_authentication_evidence_capacity_backpressure_and_expiry_cleanup() -> None:
    clock = MutableClock()
    authority = AuthenticationEvidenceAuthority(
        store=InMemoryAuthenticationEvidenceStore(capacity=1, clock=clock),
        random_bytes=RandomBytes(),
    )
    _evidence(authority)
    with pytest.raises(IdentityUnavailable):
        _evidence(authority)

    clock.now = NOW + timedelta(minutes=5)
    _evidence(authority, now=clock.now)


def test_authentication_evidence_has_isolated_scope_and_global_o1_quotas() -> None:
    clock = MutableClock()
    store = InMemoryAuthenticationEvidenceStore(
        capacity=1,
        global_capacity=2,
        clock=clock,
    )
    authority = AuthenticationEvidenceAuthority(
        store=store,
        random_bytes=RandomBytes(),
    )
    first = _evidence(authority, tenant_id="tenant-a", application_id="portal")
    _evidence(authority, tenant_id="tenant-b", application_id="portal")

    with pytest.raises(IdentityUnavailable):
        _evidence(authority, tenant_id="tenant-c", application_id="admin")
    assert store._scope_counts == {  # noqa: SLF001 - O(1) reference invariant
        ("tenant-a", "portal"): 1,
        ("tenant-b", "portal"): 1,
    }

    authority.consume_for_session(
        first,
        tenant_id="tenant-a",
        subject_id="subject-1",
        application_id="portal",
    )
    with pytest.raises(IdentityUnavailable):
        _evidence(authority, tenant_id="tenant-c", application_id="admin")
    assert store._scope_counts == {  # noqa: SLF001 - tombstones retain quota
        ("tenant-a", "portal"): 1,
        ("tenant-b", "portal"): 1,
    }

    clock.now += timedelta(minutes=5)
    _evidence(authority, tenant_id="tenant-c", application_id="admin")
    assert store._scope_counts == {  # noqa: SLF001 - exact expiry decrement
        ("tenant-c", "admin"): 1,
    }
    assert not hasattr(store, "_purge_expired_by_scanning")


def test_evidence_rejects_id_or_proof_reuse_during_original_replay_retention() -> None:
    class CollidingRandom:
        def __init__(self, *, collide_id: bool) -> None:
            self.collide_id = collide_id
            self.identifier_call = 0
            self.proof_call = 0

        def __call__(self, size: int) -> bytes:
            if size == 18:
                self.identifier_call += 1
                byte = 1 if self.collide_id else self.identifier_call
                return bytes([byte]) * size
            self.proof_call += 1
            byte = self.proof_call if self.collide_id else ord("p")
            return bytes([byte]) * size

    for collide_id in (True, False):
        store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
        authority = AuthenticationEvidenceAuthority(
            store=store,
            random_bytes=CollidingRandom(collide_id=collide_id),
        )
        first = _evidence(authority)
        authority.consume_for_session(
            first,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )
        with pytest.raises(
            IdentityUnavailable,
            match="^authentication evidence is unavailable$",
        ):
            _evidence(authority)

        clock = store._clock  # noqa: SLF001 - trusted test clock
        assert isinstance(clock, MutableClock)
        clock.now = NOW + timedelta(minutes=5)
        _evidence(authority)


def test_consumed_evidence_replay_retention_cleanup_is_bounded_and_caps_count_it() -> (
    None
):
    class CounterRandom:
        def __init__(self) -> None:
            self.value = 0

        def __call__(self, size: int) -> bytes:
            self.value += 1
            return self.value.to_bytes(size, "big")

    clock = MutableClock()
    store = InMemoryAuthenticationEvidenceStore(
        capacity=3,
        global_capacity=3,
        clock=clock,
    )
    authority = AuthenticationEvidenceAuthority(
        store=store,
        random_bytes=CounterRandom(),
    )
    for _ in range(3):
        handle = _evidence(authority)
        authority.consume_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )
    with pytest.raises(IdentityUnavailable):
        _evidence(authority)
    assert len(store._replay_tombstones) == 3  # noqa: SLF001

    for _ in range(1_000):
        clock.now += timedelta(minutes=5)
        handle = _evidence(authority)
        authority.consume_for_session(
            handle,
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        )
    assert len(store._replay_tombstones) <= 1  # noqa: SLF001
    assert len(store._expirations) <= 4  # noqa: SLF001
    assert len(store._issuances) <= 4  # noqa: SLF001


def test_create_session_has_7d_idle_30d_absolute_and_reveals_tokens_once() -> None:
    service, store, authority = _service()
    issued = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )

    assert isinstance(issued.session_token, SecretStr)
    assert isinstance(issued.csrf_token, SecretStr)
    session = issued.session
    assert session.idle_expires_at == NOW + timedelta(days=7)
    assert session.absolute_expires_at == NOW + timedelta(days=30)
    assert session.subject_kind is SubjectKind.HUMAN
    assert session.subject_revision == 1
    assert session.credential_kind == "external_identity"
    assert session.credential_tenant_id == "tenant-a"
    assert session.credential_id == "external-identity-1"
    assert session.credential_revision == 1
    assert session.credential_session_epoch == 1
    assert session.provider_id == "provider-1"
    assert session.provider_revision == 1
    assert session.provider_configuration_fingerprint == "0" * 64
    assert session.application_revision == 1
    assert issued.session_token.get_secret_value() not in session.model_dump_json()
    assert issued.csrf_token.get_secret_value() not in session.model_dump_json()
    assert "password" not in BrowserSession.model_fields
    assert "oidc_token" not in BrowserSession.model_fields
    assert store.sessions[session.session_id] == session


@pytest.mark.parametrize(
    ("collision", "second_id", "second_token"),
    (
        ("digest", b"j", b"s"),
        ("id", b"i", b"t"),
    ),
)
def test_session_global_id_and_digest_collisions_are_independent_and_never_overwrite(
    collision: str,
    second_id: bytes,
    second_token: bytes,
) -> None:
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    sessions = SessionStore(
        memberships,
        evidence_store,
        session_backend=ReferenceSessionBackend(),
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_store,
        random_bytes=RandomBytes(),
    )
    first_service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=ScriptedSessionRandom(b"i", b"s", b"c"),
    )
    second_service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=ScriptedSessionRandom(second_id, second_token, b"d"),
    )
    first = first_service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    before = dict(sessions.sessions)
    sessions.credentials[("external_identity", "external-identity-b")] = {
        **sessions.credentials[("external_identity", "external-identity-1")],
        "tenant_id": "tenant-b",
    }
    second_evidence = authority.issue_oidc(
        resolved_identity=ResolvedExternalIdentity(
            external_identity_id="external-identity-b",
            tenant_id="tenant-b",
            provider_id="provider-1",
            issuer="https://id.example.com",
            external_subject="external-subject-b",
            subject_id="subject-1",
            external_identity_revision=1,
            external_identity_status="active",
            external_identity_session_epoch=1,
            subject_revision=1,
            subject_kind="human",
            subject_status="active",
            provider_revision=1,
            provider_configuration_fingerprint="0" * 64,
        ),
        authentication_methods=("oidc",),
        application_id="portal",
        token_issued_at=NOW - timedelta(minutes=1),
        token_not_before=None,
        token_expires_at=NOW + timedelta(minutes=5),
    )

    with pytest.raises(CredentialInvalid):
        second_service.create("tenant-b", "subject-1", "principal-b", second_evidence)

    assert sessions.sessions == before
    assert sessions.sessions[first.session.session_id] == first.session
    attempted = sessions.create_attempts[-1]
    if collision == "digest":
        assert attempted.session_id != first.session.session_id
        assert attempted.session_token_digest == first.session.session_token_digest
    else:
        assert attempted.session_id == first.session.session_id
        assert attempted.session_token_digest != first.session.session_token_digest


@pytest.mark.parametrize(
    ("collision", "second_id", "second_token"),
    (
        ("digest", b"j", b"s"),
        ("id", b"i", b"t"),
    ),
)
def test_distinct_session_uows_share_backend_global_uniqueness_and_loser_evidence(
    collision: str,
    second_id: bytes,
    second_token: bytes,
) -> None:
    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    evidence_backend = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    session_backend = ReferenceSessionBackend()
    stores = (
        SessionStore(
            memberships,
            evidence_backend,
            session_backend=session_backend,
        ),
        SessionStore(
            memberships,
            evidence_backend,
            session_backend=session_backend,
        ),
    )
    random_specs = ((b"i", b"s", b"c"), (second_id, second_token, b"d"))
    services = tuple(
        BrowserSessionService(
            store,
            memberships,
            application_id="portal",
            operator=_operator(),
            random_bytes=ScriptedSessionRandom(*random_spec),
        )
        for store, random_spec in zip(stores, random_specs, strict=True)
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_backend,
        random_bytes=RandomBytes(),
    )
    evidence = (_evidence(authority), _evidence(authority))
    outcomes: list[tuple[int, str]] = []
    staged = Event()
    release = Event()
    second_started = Event()
    second_done = Event()
    stage_calls = 0
    stage_guard = Lock()

    def after_uniqueness_check() -> None:
        nonlocal stage_calls
        with stage_guard:
            stage_calls += 1
            first = stage_calls == 1
        if first:
            staged.set()
            assert release.wait(5)

    stores[0].after_uniqueness_check = after_uniqueness_check
    stores[1].after_uniqueness_check = after_uniqueness_check

    def create(index: int) -> None:
        if index == 1:
            second_started.set()
        try:
            services[index].create(
                "tenant-a", "subject-1", "principal-a", evidence[index]
            )
        except CredentialInvalid as error:
            assert str(error) == "session is invalid or expired"
            outcomes.append((index, "rejected"))
        else:
            outcomes.append((index, "created"))
        finally:
            if index == 1:
                second_done.set()

    threads = (Thread(target=create, args=(0,)), Thread(target=create, args=(1,)))
    threads[0].start()
    reached_actual_write_path = staged.wait(1)
    threads[1].start()
    assert second_started.wait(1)
    second_was_blocked = not second_done.wait(0.1)
    release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    assert reached_actual_write_path
    assert second_was_blocked
    assert stage_calls == 1
    assert sorted(status for _, status in outcomes) == ["created", "rejected"]
    assert stores[0].sessions is stores[1].sessions
    assert stores[0].lock is stores[1].lock
    assert len(stores[0].sessions) == 1
    first_attempt = stores[0].create_attempts[0]
    second_attempt = stores[1].create_attempts[0]
    if collision == "digest":
        assert first_attempt.session_id != second_attempt.session_id
        assert first_attempt.session_token_digest == second_attempt.session_token_digest
    else:
        assert first_attempt.session_id == second_attempt.session_id
        assert first_attempt.session_token_digest != second_attempt.session_token_digest
    loser = next(index for index, status in outcomes if status == "rejected")
    assert (
        authority.inspect_for_session(
            evidence[loser],
            tenant_id="tenant-a",
            subject_id="subject-1",
            application_id="portal",
        ).purpose
        == "session.create"
    )
    retry = BrowserSessionService(
        stores[loser],
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=ScriptedSessionRandom(*random_specs[loser]),
    )
    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        retry.create("tenant-a", "subject-1", "principal-a", evidence[loser])
    authority.inspect_for_session(
        evidence[loser],
        tenant_id="tenant-a",
        subject_id="subject-1",
        application_id="portal",
    )


def test_concurrent_uow_construction_uses_one_explicit_session_backend() -> None:
    assert (
        inspect.signature(SessionStore).parameters["session_backend"].default
        is inspect.Parameter.empty
    )
    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    evidence_backend = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    session_backend = ReferenceSessionBackend()
    entry = Barrier(8)
    stores: list[SessionStore | None] = [None] * 8
    failures: list[Exception] = []

    def construct(index: int) -> None:
        entry.wait()
        try:
            stores[index] = SessionStore(
                memberships,
                evidence_backend,
                session_backend=session_backend,
            )
        except Exception as error:  # noqa: BLE001 - thread result assertion
            failures.append(error)

    threads = tuple(Thread(target=construct, args=(index,)) for index in range(8))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    assert failures == []
    assert all(store is not None for store in stores)
    for store in stores:
        assert store is not None
        assert store.session_backend is session_backend
        assert store.sessions is session_backend.sessions
        assert store.lock is session_backend.transaction_lock
    assert not hasattr(evidence_backend, "_reference_session_backend")


def test_session_creation_rejects_raw_methods_forgery_replay_cross_tenant_and_mfa() -> (
    None
):
    service, store, authority = _service()
    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        service.create(
            "tenant-a",
            "subject-1",
            "principal-a",
            ("password",),  # type: ignore[arg-type]
        )
    assert store.sessions == {}

    evidence = _evidence(authority)
    service.create("tenant-a", "subject-1", "principal-a", evidence)
    with pytest.raises(CredentialInvalid):
        service.create("tenant-a", "subject-1", "principal-a", evidence)

    cross_tenant = _evidence(authority)
    with pytest.raises(CredentialInvalid):
        service.create(
            "tenant-b",
            "subject-1",
            "principal-b",
            cross_tenant,
        )
    service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        cross_tenant,
    )

    with pytest.raises(CredentialInvalid):
        _evidence(authority, methods=("oidc", "mfa"))


def test_authenticate_enforces_idle_and_absolute_boundaries_and_touches_session() -> (
    None
):
    service, store, authority = _service()
    issued = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )
    token = issued.session_token.get_secret_value()
    clock = store.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=29)
    touched = service.authenticate(token)
    assert touched.last_seen_at == NOW + timedelta(minutes=29)
    assert touched.idle_expires_at == NOW + timedelta(days=7, minutes=29)

    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        clock.now = touched.idle_expires_at
        service.authenticate(token)


def test_tenant_switch_rotates_session_csrf_and_requires_active_membership() -> None:
    service, store, authority = _service()
    source = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )
    clock = store.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)

    replacement = service.switch_tenant(
        source.session_token.get_secret_value(),
        source.csrf_token.get_secret_value(),
        "tenant-b",
    )

    assert replacement.session.tenant_id == "tenant-b"
    assert replacement.session.credential_tenant_id == "tenant-a"
    assert replacement.session.absolute_expires_at == source.session.absolute_expires_at
    assert replacement.session.principal_id == "principal-b"
    assert replacement.session.session_id != source.session.session_id
    assert replacement.csrf_token != source.csrf_token
    assert store.sessions[source.session.session_id].revoked_at == NOW + timedelta(
        minutes=1
    )
    with pytest.raises(CredentialInvalid):
        clock.now = NOW + timedelta(minutes=2)
        service.switch_tenant(
            source.session_token.get_secret_value(),
            source.csrf_token.get_secret_value(),
            "tenant-b",
        )

    inactive, inactive_store, inactive_authority = _service(
        memberships=(
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b", status="suspended"),
        )
    )
    inactive_source = inactive.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(inactive_authority),
    )
    with pytest.raises(CredentialInvalid):
        inactive_clock = inactive_store.evidence_store._clock  # noqa: SLF001
        assert isinstance(inactive_clock, MutableClock)
        inactive.switch_tenant(
            inactive_source.session_token.get_secret_value(),
            inactive_source.csrf_token.get_secret_value(),
            "tenant-b",
        )


def test_repeated_tenant_switches_preserve_home_credential_and_absolute_expiry() -> (
    None
):
    service, store, authority = _service()
    assert set(store.credentials) == {("external_identity", "external-identity-1")}
    source = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    clock = store.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)

    clock.now = NOW + timedelta(minutes=1)
    guest = service.switch_tenant(
        source.session_token.get_secret_value(),
        source.csrf_token.get_secret_value(),
        "tenant-b",
    )
    clock.now = NOW + timedelta(minutes=2)
    home = service.switch_tenant(
        guest.session_token.get_secret_value(),
        guest.csrf_token.get_secret_value(),
        "tenant-a",
    )

    for issued in (guest, home):
        assert issued.session.credential_tenant_id == "tenant-a"
        assert issued.session.absolute_expires_at == source.session.absolute_expires_at
    assert home.session.idle_expires_at == NOW + timedelta(days=7, minutes=2)


def test_tenant_switch_is_source_bound_and_race_has_one_winner() -> None:
    service, _, authority = _service()
    source = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )
    barrier = Barrier(2)
    outcomes: list[str] = []

    def switch() -> None:
        barrier.wait()
        try:
            service.switch_tenant(
                source.session_token.get_secret_value(),
                source.csrf_token.get_secret_value(),
                "tenant-b",
            )
            outcomes.append("rotated")
        except CredentialInvalid:
            outcomes.append("rejected")

    threads = [Thread(target=switch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["rejected", "rotated"]

    for malformed in ("", "invalid", "secret-needle" * 1000):
        with pytest.raises(
            CredentialInvalid, match="^session is invalid or expired$"
        ) as caught:
            service.authenticate(malformed)
        assert "secret-needle" not in str(caught.value)


@pytest.mark.parametrize(
    ("collision", "replacement_id", "replacement_token"),
    (
        ("id", b"x", b"r"),
        ("digest", b"y", b"q"),
    ),
)
def test_rotation_rejects_replacement_global_id_or_digest_collision_without_mutation(
    collision: str,
    replacement_id: bytes,
    replacement_token: bytes,
) -> None:
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_backend = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    sessions = SessionStore(
        memberships,
        evidence_backend,
        session_backend=ReferenceSessionBackend(),
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_backend,
        random_bytes=RandomBytes(),
    )

    def service(identifier: bytes, token: bytes, csrf: bytes) -> BrowserSessionService:
        return BrowserSessionService(
            sessions,
            memberships,
            application_id="portal",
            operator=_operator(),
            random_bytes=ScriptedSessionRandom(identifier, token, csrf),
        )

    source = service(b"a", b"p", b"c").create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    existing = service(b"x", b"q", b"d").create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    before = dict(sessions.sessions)
    clock = evidence_backend._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)

    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        service(replacement_id, replacement_token, b"e").switch_tenant(
            source.session_token.get_secret_value(),
            source.csrf_token.get_secret_value(),
            "tenant-b",
        )

    assert sessions.sessions == before
    attempted = sessions.rotation_attempts[-1]
    if collision == "id":
        assert attempted.session_id == existing.session.session_id
        assert attempted.session_token_digest != existing.session.session_token_digest
    else:
        assert attempted.session_id != existing.session.session_id
        assert attempted.session_token_digest == existing.session.session_token_digest


@pytest.mark.parametrize(
    "failure_stage",
    ("after_source_revoke", "after_replacement_insert", "commit"),
)
def test_rotation_rolls_back_source_and_replacement_on_every_failure(
    failure_stage: str,
) -> None:
    service, sessions, authority = _service()
    source = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    before = dict(sessions.sessions)
    sessions.rotation_failure_stage = failure_stage
    clock = sessions.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)

    with pytest.raises(IdentityUnavailable, match="^identity service is unavailable$"):
        service.switch_tenant(
            source.session_token.get_secret_value(),
            source.csrf_token.get_secret_value(),
            "tenant-b",
        )

    assert sessions.sessions == before
    attempted = sessions.rotation_attempts[-1]
    assert attempted.session_id not in sessions.sessions
    sessions.rotation_failure_stage = None
    replacement = service.switch_tenant(
        source.session_token.get_secret_value(),
        source.csrf_token.get_secret_value(),
        "tenant-b",
    )
    assert sessions.sessions[source.session.session_id].revoked_at == clock.now
    assert sessions.sessions[replacement.session.session_id] == replacement.session


def _rotation_visibility_fixture() -> tuple[
    SessionStore,
    SessionStore,
    BrowserSessionService,
    BrowserSessionService,
    IssuedBrowserSession,
]:
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_backend = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    session_backend = ReferenceSessionBackend()
    writer = SessionStore(
        memberships,
        evidence_backend,
        session_backend=session_backend,
    )
    reader = SessionStore(
        memberships,
        evidence_backend,
        session_backend=session_backend,
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_backend,
        random_bytes=RandomBytes(),
    )

    def service(
        store: SessionStore,
        spec: tuple[bytes, bytes, bytes],
    ) -> BrowserSessionService:
        return BrowserSessionService(
            store,
            memberships,
            application_id="portal",
            operator=_operator(),
            random_bytes=ScriptedSessionRandom(*spec),
        )

    source = service(writer, (b"a", b"p", b"c")).create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    clock = evidence_backend._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)
    return (
        writer,
        reader,
        service(writer, (b"x", b"r", b"e")),
        service(reader, (b"u", b"v", b"w")),
        source,
    )


def test_failed_rotation_hides_uncommitted_state_from_token_lookups() -> None:
    writer, reader, rotation, _, source = _rotation_visibility_fixture()
    staged = Event()
    release = Event()
    source_started = Event()
    source_done = Event()
    replacement_started = Event()
    replacement_done = Event()
    writer.rotation_failure_stage = "after_replacement_insert"

    def before_rotation_failure(stage: str) -> None:
        assert stage == "after_replacement_insert"
        staged.set()
        assert release.wait(5)

    writer.before_rotation_failure = before_rotation_failure
    rotation_errors: list[Exception] = []

    def rotate() -> None:
        try:
            rotation.switch_tenant(
                source.session_token.get_secret_value(),
                source.csrf_token.get_secret_value(),
                "tenant-b",
            )
        except Exception as error:  # noqa: BLE001 - asserted below
            rotation_errors.append(error)

    rotation_thread = Thread(target=rotate)
    rotation_thread.start()
    reached_dirty_state = staged.wait(1)
    assert writer.rotation_attempts
    replacement = writer.rotation_attempts[-1]
    lookup_results: dict[str, BrowserSession | None] = {}

    def lookup_source() -> None:
        source_started.set()
        lookup_results["source"] = reader.find_by_token_digest(
            source.session.session_token_digest
        )
        source_done.set()

    def lookup_replacement() -> None:
        replacement_started.set()
        lookup_results["replacement"] = reader.find_by_token_digest(
            replacement.session_token_digest
        )
        replacement_done.set()

    source_thread = Thread(target=lookup_source)
    replacement_thread = Thread(target=lookup_replacement)
    source_thread.start()
    replacement_thread.start()
    assert source_started.wait(1)
    assert replacement_started.wait(1)
    source_was_blocked = not source_done.wait(0.1)
    replacement_was_blocked = not replacement_done.wait(0.1)
    release.set()
    for thread in (rotation_thread, source_thread, replacement_thread):
        thread.join(5)
        assert not thread.is_alive()

    assert reached_dirty_state
    assert source_was_blocked
    assert replacement_was_blocked
    assert len(rotation_errors) == 1
    assert isinstance(rotation_errors[0], IdentityUnavailable)
    assert lookup_results == {"source": source.session, "replacement": None}


def test_failed_rotation_does_not_transiently_reject_source_authentication() -> None:
    writer, _, rotation, reader_service, source = _rotation_visibility_fixture()
    staged = Event()
    release = Event()
    authentication_started = Event()
    authentication_done = Event()
    writer.rotation_failure_stage = "after_replacement_insert"

    def before_rotation_failure(stage: str) -> None:
        assert stage == "after_replacement_insert"
        staged.set()
        assert release.wait(5)

    writer.before_rotation_failure = before_rotation_failure
    rotation_errors: list[Exception] = []
    authentication_results: list[BrowserSession] = []
    authentication_errors: list[Exception] = []

    def rotate() -> None:
        try:
            rotation.switch_tenant(
                source.session_token.get_secret_value(),
                source.csrf_token.get_secret_value(),
                "tenant-b",
            )
        except Exception as error:  # noqa: BLE001 - asserted below
            rotation_errors.append(error)

    def authenticate() -> None:
        authentication_started.set()
        try:
            authentication_results.append(
                reader_service.authenticate(source.session_token.get_secret_value())
            )
        except Exception as error:  # noqa: BLE001 - asserted below
            authentication_errors.append(error)
        finally:
            authentication_done.set()

    rotation_thread = Thread(target=rotate)
    rotation_thread.start()
    reached_dirty_state = staged.wait(1)
    authentication_thread = Thread(target=authenticate)
    authentication_thread.start()
    assert authentication_started.wait(1)
    authentication_was_blocked = not authentication_done.wait(0.1)
    release.set()
    for thread in (rotation_thread, authentication_thread):
        thread.join(5)
        assert not thread.is_alive()

    assert reached_dirty_state
    assert authentication_was_blocked
    assert len(rotation_errors) == 1
    assert isinstance(rotation_errors[0], IdentityUnavailable)
    assert authentication_errors == []
    assert len(authentication_results) == 1
    assert authentication_results[0].session_id == source.session.session_id
    assert authentication_results[0].revoked_at is None


def test_successful_rotation_publishes_source_and_replacement_atomically() -> None:
    writer, reader, rotation, _, source = _rotation_visibility_fixture()
    staged = Event()
    release = Event()
    source_started = Event()
    source_done = Event()
    replacement_started = Event()
    replacement_done = Event()

    def before_rotation_commit_complete() -> None:
        staged.set()
        assert release.wait(5)

    writer.before_rotation_commit_complete = before_rotation_commit_complete
    rotation_results: list[IssuedBrowserSession] = []

    def rotate() -> None:
        rotation_results.append(
            rotation.switch_tenant(
                source.session_token.get_secret_value(),
                source.csrf_token.get_secret_value(),
                "tenant-b",
            )
        )

    rotation_thread = Thread(target=rotate)
    rotation_thread.start()
    reached_dirty_state = staged.wait(1)
    assert writer.rotation_attempts
    replacement = writer.rotation_attempts[-1]
    lookup_results: dict[str, BrowserSession | None] = {}

    def lookup_source() -> None:
        source_started.set()
        lookup_results["source"] = reader.find_by_token_digest(
            source.session.session_token_digest
        )
        source_done.set()

    def lookup_replacement() -> None:
        replacement_started.set()
        lookup_results["replacement"] = reader.find_by_token_digest(
            replacement.session_token_digest
        )
        replacement_done.set()

    source_thread = Thread(target=lookup_source)
    replacement_thread = Thread(target=lookup_replacement)
    source_thread.start()
    replacement_thread.start()
    assert source_started.wait(1)
    assert replacement_started.wait(1)
    source_was_blocked = not source_done.wait(0.1)
    replacement_was_blocked = not replacement_done.wait(0.1)
    release.set()
    for thread in (rotation_thread, source_thread, replacement_thread):
        thread.join(5)
        assert not thread.is_alive()

    assert reached_dirty_state
    assert source_was_blocked
    assert replacement_was_blocked
    assert len(rotation_results) == 1
    assert rotation_results[0].session == replacement
    assert lookup_results["source"] is not None
    assert lookup_results["source"].revoked_at == NOW + timedelta(minutes=1)
    assert lookup_results["replacement"] == replacement


def test_session_reads_are_reentrant_inside_backend_transaction_lock() -> None:
    writer, reader, _, _, source = _rotation_visibility_fixture()
    completed = Event()
    results: list[BrowserSession | None] = []
    errors: list[Exception] = []

    def read_inside_transaction() -> None:
        try:
            with writer.session_backend.transaction_lock:
                results.append(reader.get_session(source.session.session_id))
                results.append(
                    reader.find_by_token_digest(source.session.session_token_digest)
                )
        except Exception as error:  # noqa: BLE001 - asserted below
            errors.append(error)
        finally:
            completed.set()

    thread = Thread(target=read_inside_transaction, daemon=True)
    thread.start()
    finished_without_deadlock = completed.wait(1)

    assert finished_without_deadlock
    assert errors == []
    assert results == [source.session, source.session]


def test_failed_rotation_rolls_back_before_unrelated_concurrent_success() -> None:
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_backend = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    session_backend = ReferenceSessionBackend()
    stores = (
        SessionStore(
            memberships,
            evidence_backend,
            session_backend=session_backend,
        ),
        SessionStore(
            memberships,
            evidence_backend,
            session_backend=session_backend,
        ),
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_backend,
        random_bytes=RandomBytes(),
    )

    def service(
        store: SessionStore,
        spec: tuple[bytes, bytes, bytes],
    ) -> BrowserSessionService:
        return BrowserSessionService(
            store,
            memberships,
            application_id="portal",
            operator=_operator(),
            random_bytes=ScriptedSessionRandom(*spec),
        )

    sources = (
        service(stores[0], (b"a", b"p", b"c")).create(
            "tenant-a", "subject-1", "principal-a", _evidence(authority)
        ),
        service(stores[1], (b"b", b"q", b"d")).create(
            "tenant-a", "subject-1", "principal-a", _evidence(authority)
        ),
    )
    source_snapshots = tuple(source.session for source in sources)
    rotations = (
        service(stores[0], (b"x", b"r", b"e")),
        service(stores[1], (b"y", b"s", b"f")),
    )
    clock = evidence_backend._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)
    staged = Event()
    release = Event()
    second_started = Event()
    second_done = Event()
    outcomes: list[tuple[int, str]] = []
    stores[0].rotation_failure_stage = "after_replacement_insert"

    def before_rotation_failure(stage: str) -> None:
        assert stage == "after_replacement_insert"
        staged.set()
        assert release.wait(5)

    stores[0].before_rotation_failure = before_rotation_failure

    def rotate(index: int) -> None:
        if index == 1:
            second_started.set()
        try:
            rotations[index].switch_tenant(
                sources[index].session_token.get_secret_value(),
                sources[index].csrf_token.get_secret_value(),
                "tenant-b",
            )
        except IdentityUnavailable:
            outcomes.append((index, "failed"))
        else:
            outcomes.append((index, "rotated"))
        finally:
            if index == 1:
                second_done.set()

    threads = (Thread(target=rotate, args=(0,)), Thread(target=rotate, args=(1,)))
    threads[0].start()
    reached_failure_stage = staged.wait(1)
    threads[1].start()
    assert second_started.wait(1)
    second_was_blocked = not second_done.wait(0.1)
    release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    assert reached_failure_stage
    assert second_was_blocked
    assert sorted(status for _, status in outcomes) == ["failed", "rotated"]
    assert stores[0].sessions[source_snapshots[0].session_id] == source_snapshots[0]
    assert stores[0].sessions[source_snapshots[1].session_id].revoked_at == clock.now
    failed_replacement = stores[0].rotation_attempts[-1]
    successful_replacement = stores[1].rotation_attempts[-1]
    assert failed_replacement.session_id not in stores[0].sessions
    assert stores[0].sessions[successful_replacement.session_id] == (
        successful_replacement
    )
    assert len(stores[0].sessions) == 3


@pytest.mark.parametrize(
    ("collision", "first_spec", "second_spec"),
    (
        ("id", (b"x", b"r", b"e"), (b"x", b"s", b"f")),
        ("digest", (b"x", b"r", b"e"), (b"y", b"r", b"f")),
    ),
)
def test_concurrent_rotations_have_one_global_replacement_collision_winner(
    collision: str,
    first_spec: tuple[bytes, bytes, bytes],
    second_spec: tuple[bytes, bytes, bytes],
) -> None:
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_backend = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    session_backend = ReferenceSessionBackend()
    stores = (
        SessionStore(
            memberships,
            evidence_backend,
            session_backend=session_backend,
        ),
        SessionStore(
            memberships,
            evidence_backend,
            session_backend=session_backend,
        ),
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_backend,
        random_bytes=RandomBytes(),
    )

    def service(
        store: SessionStore,
        spec: tuple[bytes, bytes, bytes],
    ) -> BrowserSessionService:
        return BrowserSessionService(
            store,
            memberships,
            application_id="portal",
            operator=_operator(),
            random_bytes=ScriptedSessionRandom(*spec),
        )

    sources = (
        service(stores[0], (b"a", b"p", b"c")).create(
            "tenant-a", "subject-1", "principal-a", _evidence(authority)
        ),
        service(stores[1], (b"b", b"q", b"d")).create(
            "tenant-a", "subject-1", "principal-a", _evidence(authority)
        ),
    )
    rotations = (service(stores[0], first_spec), service(stores[1], second_spec))
    clock = evidence_backend._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)
    outcomes: list[tuple[int, str]] = []
    staged = Event()
    release = Event()
    second_started = Event()
    second_done = Event()
    stage_calls = 0
    stage_guard = Lock()

    def after_rotation_uniqueness_check() -> None:
        nonlocal stage_calls
        with stage_guard:
            stage_calls += 1
            first = stage_calls == 1
        if first:
            staged.set()
            assert release.wait(5)

    stores[0].after_rotation_uniqueness_check = after_rotation_uniqueness_check
    stores[1].after_rotation_uniqueness_check = after_rotation_uniqueness_check

    def rotate(index: int) -> None:
        if index == 1:
            second_started.set()
        try:
            rotations[index].switch_tenant(
                sources[index].session_token.get_secret_value(),
                sources[index].csrf_token.get_secret_value(),
                "tenant-b",
            )
        except CredentialInvalid:
            outcomes.append((index, "rejected"))
        else:
            outcomes.append((index, "rotated"))
        finally:
            if index == 1:
                second_done.set()

    threads = (Thread(target=rotate, args=(0,)), Thread(target=rotate, args=(1,)))
    threads[0].start()
    reached_actual_write_path = staged.wait(1)
    threads[1].start()
    assert second_started.wait(1)
    second_was_blocked = not second_done.wait(0.1)
    release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()

    assert reached_actual_write_path
    assert second_was_blocked
    assert stage_calls == 1
    assert sorted(status for _, status in outcomes) == ["rejected", "rotated"]
    assert stores[0].session_backend is stores[1].session_backend is session_backend
    assert stores[0].lock is stores[1].lock is session_backend.transaction_lock
    assert len(session_backend.sessions) == 3
    assert (
        sum(
            session_backend.sessions[source.session.session_id].revoked_at is not None
            for source in sources
        )
        == 1
    )
    first_attempt = stores[0].rotation_attempts[-1]
    second_attempt = stores[1].rotation_attempts[-1]
    if collision == "id":
        assert first_attempt.session_id == second_attempt.session_id
        assert first_attempt.session_token_digest != second_attempt.session_token_digest
    else:
        assert first_attempt.session_id != second_attempt.session_id
        assert first_attempt.session_token_digest == second_attempt.session_token_digest


def test_csrf_is_session_bound_and_purpose_separated() -> None:
    service, _, authority = _service()
    first = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )
    second = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )

    service.verify_csrf(first.session, first.csrf_token.get_secret_value())
    for invalid in (
        "wrong",
        second.csrf_token.get_secret_value(),
        first.session_token.get_secret_value(),
    ):
        with pytest.raises(CredentialInvalid):
            service.verify_csrf(first.session, invalid)
    with pytest.raises(CredentialInvalid):
        service.authenticate(first.csrf_token.get_secret_value())

    stale_application = first.session.model_copy(
        update={"application_revision": first.session.application_revision + 1}
    )
    with pytest.raises(CredentialInvalid):
        service.verify_csrf(
            stale_application,
            first.csrf_token.get_secret_value(),
        )


def test_session_reloads_bound_membership_revision_and_rejects_stale_session() -> None:
    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    authority = AuthenticationEvidenceAuthority(
        store=InMemoryAuthenticationEvidenceStore(), random_bytes=RandomBytes()
    )
    sessions = SessionStore(
        memberships,
        authority,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    issued = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )
    memberships.memberships = (_membership("tenant-a", "principal-a", revision=2),)

    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        service.authenticate(issued.session_token.get_secret_value())


def test_rotation_rechecks_membership_inside_atomic_commit_and_preserves_source() -> (
    None
):
    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    authority = AuthenticationEvidenceAuthority(
        store=InMemoryAuthenticationEvidenceStore(), random_bytes=RandomBytes()
    )
    sessions = SessionStore(
        memberships,
        authority,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    source = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )

    def suspend_at_commit() -> None:
        memberships.memberships = (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b", status="suspended", revision=2),
        )

    sessions.before_rotation_commit = suspend_at_commit
    with pytest.raises(CredentialInvalid, match="^session is invalid or expired$"):
        service.switch_tenant(
            source.session_token.get_secret_value(),
            source.csrf_token.get_secret_value(),
            "tenant-b",
        )
    assert sessions.sessions[source.session.session_id].revoked_at is None


@pytest.mark.parametrize(
    "race",
    (
        "source_membership_status",
        "source_membership_revision",
        "source_membership_valid_from_future",
        "source_membership_valid_until_past",
        "source_membership_valid_until_boundary",
        "target_membership_status",
        "target_membership_revision",
        "target_membership_valid_from_future",
        "target_membership_valid_until_past",
        "target_membership_valid_until_boundary",
        "subject_kind",
        "subject_status",
        "subject_revision",
        "credential_status",
        "credential_tenant",
        "credential_revision",
        "credential_epoch",
        "provider_status",
        "provider_revision",
        "provider_fingerprint",
        "application_status",
        "application_revision",
        "replacement_mismatch",
    ),
)
def test_tenant_rotation_atomically_revalidates_full_binding_and_rolls_back(
    race: str,
) -> None:
    service, sessions, authority = _service()
    source = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    before = dict(sessions.sessions)

    def mutate_at_commit() -> None:
        assert sessions.memberships is not None
        if race.startswith(("source_membership_", "target_membership_")):
            source_membership, target_membership = sessions.memberships.memberships
            target_name, fact = race.split("_membership_", 1)
            field_name, value = {
                "status": ("status", "suspended"),
                "revision": ("revision", 2),
                "valid_from_future": (
                    "valid_from",
                    NOW + timedelta(minutes=1, microseconds=1),
                ),
                "valid_until_past": (
                    "valid_until",
                    NOW + timedelta(seconds=30),
                ),
                "valid_until_boundary": (
                    "valid_until",
                    NOW + timedelta(minutes=1),
                ),
            }[fact]
            if target_name == "source":
                source_membership = source_membership.model_copy(
                    update={field_name: value}
                )
            else:
                target_membership = target_membership.model_copy(
                    update={field_name: value}
                )
            sessions.memberships.memberships = (
                source_membership,
                target_membership,
            )
        elif race.startswith("subject_"):
            field, value = {
                "subject_kind": ("kind", SubjectKind.AGENT),
                "subject_status": ("status", "disabled"),
                "subject_revision": ("revision", 2),
            }[race]
            sessions.subjects["subject-1"] = sessions.subjects["subject-1"].model_copy(
                update={field: value}
            )
        elif race.startswith("application_"):
            field, value = {
                "application_status": ("status", "disabled"),
                "application_revision": ("revision", 2),
            }[race]
            status, revision = sessions.applications["portal"]
            sessions.applications["portal"] = (
                value if field == "status" else status,
                value if field == "revision" else revision,
            )
        elif race == "replacement_mismatch":
            sessions.replacement_mismatch = True
        else:
            key = ("external_identity", "external-identity-1")
            credential = dict(sessions.credentials[key])
            field, value = {
                "credential_status": ("status", "disabled"),
                "credential_tenant": ("tenant_id", "tenant-b"),
                "credential_revision": ("revision", 2),
                "credential_epoch": ("session_epoch", 2),
                "provider_status": ("provider_status", "disabled"),
                "provider_revision": ("provider_revision", 2),
                "provider_fingerprint": (
                    "provider_configuration_fingerprint",
                    "f" * 64,
                ),
            }[race]
            credential[field] = value
            sessions.credentials[key] = credential

    sessions.before_rotation_commit = mutate_at_commit
    clock = sessions.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)
    with pytest.raises(CredentialInvalid):
        service.switch_tenant(
            source.session_token.get_secret_value(),
            source.csrf_token.get_secret_value(),
            "tenant-b",
        )

    assert sessions.sessions == before


@pytest.mark.parametrize(
    ("membership_name", "field_name", "boundary_value"),
    (
        ("source", "valid_from", NOW + timedelta(minutes=1)),
        ("target", "valid_from", NOW + timedelta(minutes=1)),
        (
            "source",
            "valid_until",
            NOW + timedelta(minutes=1, microseconds=1),
        ),
        (
            "target",
            "valid_until",
            NOW + timedelta(minutes=1, microseconds=1),
        ),
    ),
)
def test_rotation_membership_validity_boundaries_are_inclusive_then_exclusive(
    membership_name: str,
    field_name: str,
    boundary_value: datetime,
) -> None:
    service, sessions, authority = _service()
    source = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )

    def mutate_one_validity_fact() -> None:
        assert sessions.memberships is not None
        source_membership, target_membership = sessions.memberships.memberships
        if membership_name == "source":
            source_membership = source_membership.model_copy(
                update={field_name: boundary_value}
            )
        else:
            target_membership = target_membership.model_copy(
                update={field_name: boundary_value}
            )
        sessions.memberships.memberships = source_membership, target_membership

    sessions.before_rotation_commit = mutate_one_validity_fact
    clock = sessions.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)

    replacement = service.switch_tenant(
        source.session_token.get_secret_value(),
        source.csrf_token.get_secret_value(),
        "tenant-b",
    )

    assert replacement.session.tenant_id == "tenant-b"
    assert sessions.sessions[source.session.session_id].revoked_at == clock.now


def test_create_failure_does_not_consume_evidence_outside_repository_transaction() -> (
    None
):
    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    evidence_store = InMemoryAuthenticationEvidenceStore()
    authority = AuthenticationEvidenceAuthority(
        store=evidence_store, random_bytes=RandomBytes()
    )
    sessions = SessionStore(
        memberships,
        authority,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    handle = _evidence(authority)

    def race_membership() -> None:
        memberships.memberships = (
            _membership("tenant-a", "principal-a", status="suspended", revision=2),
        )

    sessions.before_create_commit = race_membership
    with pytest.raises(CredentialInvalid):
        service.create("tenant-a", "subject-1", "principal-a", handle)
    assert sessions.sessions == {}

    sessions.before_create_commit = None
    memberships.memberships = (_membership("tenant-a", "principal-a"),)
    service.create("tenant-a", "subject-1", "principal-a", handle)


@pytest.mark.parametrize(
    "failure_stage",
    ("after_revalidation", "after_consume", "after_insert", "commit"),
)
def test_session_creation_uow_rolls_back_evidence_and_session_on_every_failure(
    failure_stage: str,
) -> None:
    service, sessions, authority = _service()
    handle = _evidence(authority)
    evidence_state = _evidence_state(sessions.evidence_store)
    sessions.failure_stage = failure_stage

    with pytest.raises(IdentityUnavailable):
        service.create("tenant-a", "subject-1", "principal-a", handle)
    assert sessions.sessions == {}
    assert _evidence_state(sessions.evidence_store) == evidence_state

    sessions.failure_stage = None
    service.create("tenant-a", "subject-1", "principal-a", handle)
    assert len(sessions.sessions) == 1


def _evidence_state(store: InMemoryAuthenticationEvidenceStore) -> tuple[object, ...]:
    return (
        dict(store._records),  # noqa: SLF001
        dict(store._proof_index),  # noqa: SLF001
        dict(store._scope_counts),  # noqa: SLF001
        list(store._expirations),  # noqa: SLF001
        list(store._issuances),  # noqa: SLF001
        dict(store._replay_tombstones),  # noqa: SLF001
        dict(store._proof_tombstones),  # noqa: SLF001
        list(store._replay_expirations),  # noqa: SLF001
    )


def test_failed_uow_rollback_preserves_a_concurrent_success() -> None:
    staged = Event()
    release_failure = Event()
    concurrent_done = Event()

    class FailingInterleavedStore(SessionStore):
        def _fail_create(self, stage: str) -> None:
            if stage == "after_insert" and not getattr(self, "failure_disabled", False):
                staged.set()
                assert release_failure.wait(5)
                raise RuntimeError("injected interleaved commit failure")

    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    evidence_store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    session_backend = ReferenceSessionBackend()
    store_a = FailingInterleavedStore(
        memberships,
        evidence_store,
        session_backend=session_backend,
    )
    store_b = SessionStore(
        memberships,
        evidence_store,
        session_backend=session_backend,
    )
    service_a = BrowserSessionService(
        store_a,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    service_b = BrowserSessionService(
        store_b,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytesFrom(97),
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_store,
        random_bytes=RandomBytes(),
    )
    handle_a = _evidence(authority)
    handle_b = _evidence(authority)
    outcome: list[Exception] = []
    successful: list[IssuedBrowserSession] = []

    def fail_a() -> None:
        try:
            service_a.create("tenant-a", "subject-1", "principal-a", handle_a)
        except Exception as error:  # noqa: BLE001 - thread result assertion
            outcome.append(error)

    def create_b() -> None:
        successful.append(
            service_b.create("tenant-a", "subject-1", "principal-a", handle_b)
        )
        concurrent_done.set()

    failing_thread = Thread(target=fail_a)
    successful_thread = Thread(target=create_b)
    failing_thread.start()
    assert staged.wait(5)
    successful_thread.start()
    assert not concurrent_done.wait(0.1)
    release_failure.set()
    failing_thread.join(5)
    successful_thread.join(5)
    assert not failing_thread.is_alive()
    assert not successful_thread.is_alive()

    assert len(outcome) == 1
    assert isinstance(outcome[0], IdentityUnavailable)
    assert store_b.sessions == {successful[0].session.session_id: successful[0].session}
    store_a.failure_disabled = True
    service_a.create("tenant-a", "subject-1", "principal-a", handle_a)
    with pytest.raises(CredentialInvalid):
        service_b.create("tenant-a", "subject-1", "principal-a", handle_b)


def test_failed_uow_never_revives_evidence_after_its_ttl_boundary() -> None:
    class ExpiringFailureStore(SessionStore):
        def _fail_create(self, stage: str) -> None:
            if stage == "after_consume":
                clock.now = NOW + timedelta(minutes=5)
                raise RuntimeError("injected failure at evidence expiry")

    clock = MutableClock()
    evidence_store = InMemoryAuthenticationEvidenceStore(clock=clock)
    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    sessions = ExpiringFailureStore(
        memberships,
        evidence_store,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    authority = AuthenticationEvidenceAuthority(
        store=evidence_store,
        random_bytes=RandomBytes(),
    )
    handle = _evidence(authority)
    evidence_id = handle.evidence_id

    with pytest.raises(IdentityUnavailable):
        service.create("tenant-a", "subject-1", "principal-a", handle)

    assert evidence_id not in evidence_store._records  # noqa: SLF001
    assert evidence_id not in evidence_store._replay_tombstones  # noqa: SLF001
    assert evidence_store._scope_counts == {}  # noqa: SLF001


def test_session_creation_uow_has_one_concurrent_winner() -> None:
    service, sessions, authority = _service()
    handle = _evidence(authority)
    barrier = Barrier(2)
    outcomes: list[str] = []

    def create() -> None:
        barrier.wait()
        try:
            service.create("tenant-a", "subject-1", "principal-a", handle)
        except CredentialInvalid:
            outcomes.append("invalid")
        else:
            outcomes.append("created")

    threads = (Thread(target=create), Thread(target=create))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "invalid"]
    assert len(sessions.sessions) == 1


def test_session_creation_replay_loser_inside_uow_is_stable_invalid() -> None:
    entry = Barrier(2)

    class SynchronizedSessionStore(SessionStore):
        def create_session(
            self,
            command: CreateBrowserSessionCommand,
            *,
            operator: TrustedIdentityOperator,
        ) -> BrowserSession:
            entry.wait()
            return super().create_session(command, operator=operator)

    memberships = MembershipStore(
        (
            _membership("tenant-a", "principal-a"),
            _membership("tenant-b", "principal-b"),
        )
    )
    evidence_store = InMemoryAuthenticationEvidenceStore(clock=MutableClock())
    sessions = SynchronizedSessionStore(
        memberships,
        evidence_store,
        session_backend=ReferenceSessionBackend(),
    )
    authority = AuthenticationEvidenceAuthority(
        store=sessions,
        random_bytes=RandomBytes(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    handle = _evidence(authority)
    outcomes: list[str] = []

    def create() -> None:
        try:
            service.create("tenant-a", "subject-1", "principal-a", handle)
        except CredentialInvalid as error:
            assert str(error) == "session is invalid or expired"
            outcomes.append("invalid")
        else:
            outcomes.append("created")

    threads = (Thread(target=create), Thread(target=create))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["created", "invalid"]


@pytest.mark.parametrize(
    "race",
    (
        "subject_disabled",
        "subject_revision",
        "subject_agent",
        "credential_disabled",
        "credential_revision",
        "credential_epoch",
        "provider_revision",
        "provider_configuration",
        "application_disabled",
        "application_revision",
    ),
)
def test_create_atomically_revalidates_subject_credential_and_application(
    race: str,
) -> None:
    service, sessions, authority = _service()
    handle = _evidence(authority)
    original_subject = sessions.subjects["subject-1"]
    credential_key = ("external_identity", "external-identity-1")
    original_credential = dict(sessions.credentials[credential_key])
    original_application = sessions.applications["portal"]

    def mutate_at_commit() -> None:
        if race == "subject_disabled":
            sessions.subjects["subject-1"] = original_subject.model_copy(
                update={"status": "disabled", "revision": 2}
            )
        elif race == "subject_revision":
            sessions.subjects["subject-1"] = original_subject.model_copy(
                update={"revision": 2}
            )
        elif race == "subject_agent":
            sessions.subjects["subject-1"] = original_subject.model_copy(
                update={"kind": SubjectKind.AGENT, "revision": 2}
            )
        elif race == "application_disabled":
            sessions.applications["portal"] = ("disabled", 2)
        elif race == "application_revision":
            sessions.applications["portal"] = ("active", 2)
        else:
            changed = dict(original_credential)
            field, value = {
                "credential_disabled": ("status", "disabled"),
                "credential_revision": ("revision", 2),
                "credential_epoch": ("session_epoch", 2),
                "provider_revision": ("provider_revision", 2),
                "provider_configuration": (
                    "provider_configuration_fingerprint",
                    "f" * 64,
                ),
            }[race]
            changed[field] = value
            sessions.credentials[credential_key] = changed

    sessions.before_create_commit = mutate_at_commit
    with pytest.raises(CredentialInvalid):
        service.create("tenant-a", "subject-1", "principal-a", handle)
    assert sessions.sessions == {}

    sessions.before_create_commit = None
    sessions.subjects["subject-1"] = original_subject
    sessions.credentials[credential_key] = original_credential
    sessions.applications["portal"] = original_application
    service.create("tenant-a", "subject-1", "principal-a", handle)


@pytest.mark.parametrize("changed_component", ("subject", "credential", "application"))
def test_authenticate_revalidates_persisted_identity_contract(
    changed_component: str,
) -> None:
    service, sessions, authority = _service()
    issued = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    if changed_component == "subject":
        sessions.subjects["subject-1"] = sessions.subjects["subject-1"].model_copy(
            update={"status": "disabled", "revision": 2}
        )
    elif changed_component == "credential":
        key = ("external_identity", "external-identity-1")
        sessions.credentials[key] = {
            **sessions.credentials[key],
            "session_epoch": 2,
        }
    else:
        sessions.applications["portal"] = ("disabled", 2)

    with pytest.raises(CredentialInvalid):
        service.authenticate(issued.session_token.get_secret_value())
    assert sessions.sessions[issued.session.session_id].revision == 1
    assert len(sessions.sessions) == 1


def test_create_rechecks_membership_inside_atomic_commit_and_persists_nothing() -> None:
    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    authority = AuthenticationEvidenceAuthority(
        store=InMemoryAuthenticationEvidenceStore(), random_bytes=RandomBytes()
    )
    sessions = SessionStore(
        memberships,
        authority,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )

    def suspend_at_commit() -> None:
        memberships.memberships = (
            _membership("tenant-a", "principal-a", status="suspended", revision=2),
        )

    sessions.before_create_commit = suspend_at_commit
    with pytest.raises(CredentialInvalid):
        service.create(
            "tenant-a",
            "subject-1",
            "principal-a",
            _evidence(authority),
        )
    assert sessions.sessions == {}


def test_touch_rechecks_membership_inside_atomic_commit_and_preserves_session() -> None:
    service, sessions, authority = _service()
    issued = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )
    before = sessions.sessions[issued.session.session_id]

    def revise_at_commit() -> None:
        assert sessions.memberships is not None
        sessions.memberships.memberships = (
            _membership("tenant-a", "principal-a", revision=2),
            _membership("tenant-b", "principal-b"),
        )

    sessions.before_touch_commit = revise_at_commit
    clock = sessions.evidence_store._clock  # noqa: SLF001 - trusted test clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW + timedelta(minutes=1)
    with pytest.raises(CredentialInvalid):
        service.authenticate(issued.session_token.get_secret_value())
    assert sessions.sessions[issued.session.session_id] == before


@pytest.mark.parametrize(
    "mutation",
    (
        "record_type",
        "subject_revision",
        "revision_jump",
        "session_id",
        "application_revision",
        "credential_tenant",
    ),
)
def test_authenticate_rejects_malicious_touch_identity_binding_result(
    mutation: str,
) -> None:
    class MaliciousTouchStore(SessionStore):
        def touch_session(
            self,
            command: TouchBrowserSessionCommand,
            *,
            operator: TrustedIdentityOperator,
        ) -> object:
            result = super().touch_session(command, operator=operator)
            if mutation == "record_type":
                return object()
            field, value = {
                "subject_revision": (
                    "subject_revision",
                    result.subject_revision + 1,
                ),
                "revision_jump": ("revision", result.revision + 1),
                "session_id": ("session_id", "ses_malicious"),
                "application_revision": (
                    "application_revision",
                    result.application_revision + 1,
                ),
                "credential_tenant": ("credential_tenant_id", "tenant-b"),
            }[mutation]
            return result.model_copy(update={field: value})

    memberships = MembershipStore((_membership("tenant-a", "principal-a"),))
    authority = AuthenticationEvidenceAuthority(
        store=InMemoryAuthenticationEvidenceStore(), random_bytes=RandomBytes()
    )
    sessions = MaliciousTouchStore(
        memberships,
        authority,
        session_backend=ReferenceSessionBackend(),
    )
    service = BrowserSessionService(
        sessions,
        memberships,
        application_id="portal",
        operator=_operator(),
        random_bytes=RandomBytes(),
    )
    issued = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )

    with pytest.raises(CredentialInvalid):
        service.authenticate(issued.session_token.get_secret_value())


def test_switch_rechecks_source_membership_inside_commit_and_preserves_source() -> None:
    service, sessions, authority = _service()
    source = service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(authority),
    )

    def suspend_source_at_commit() -> None:
        assert sessions.memberships is not None
        sessions.memberships.memberships = (
            _membership("tenant-a", "principal-a", status="suspended", revision=2),
            _membership("tenant-b", "principal-b"),
        )

    sessions.before_rotation_commit = suspend_source_at_commit
    with pytest.raises(CredentialInvalid):
        service.switch_tenant(
            source.session_token.get_secret_value(),
            source.csrf_token.get_secret_value(),
            "tenant-b",
        )
    assert sessions.sessions[source.session.session_id].revoked_at is None
    assert len(sessions.sessions) == 1


def test_session_exact_idle_and_absolute_boundaries_are_expired() -> None:
    idle_service, idle_store, idle_authority = _service()
    idle = idle_service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(idle_authority),
    )
    idle_clock = idle_store.evidence_store._clock  # noqa: SLF001
    assert isinstance(idle_clock, MutableClock)
    idle_clock.now = NOW + timedelta(days=7)
    with pytest.raises(CredentialInvalid):
        idle_service.authenticate(idle.session_token.get_secret_value())

    absolute_service, absolute_store, absolute_authority = _service()
    absolute = absolute_service.create(
        "tenant-a",
        "subject-1",
        "principal-a",
        _evidence(absolute_authority),
    )
    absolute_store.sessions[absolute.session.session_id] = absolute.session.model_copy(
        update={
            "last_seen_at": NOW + timedelta(days=29),
            "idle_expires_at": NOW + timedelta(days=30),
            "revision": 2,
        }
    )
    absolute_clock = absolute_store.evidence_store._clock  # noqa: SLF001
    assert isinstance(absolute_clock, MutableClock)
    absolute_clock.now = NOW + timedelta(days=30)
    with pytest.raises(CredentialInvalid):
        absolute_service.authenticate(absolute.session_token.get_secret_value())


@pytest.mark.parametrize("failure", ("malformed", "error"))
def test_every_session_public_path_normalizes_trusted_clock_failure(
    failure: str,
) -> None:
    service, store, authority = _service()
    issued = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    pending = _evidence(authority)

    def broken_clock() -> object:
        if failure == "error":
            raise RuntimeError("secret-clock-detail")
        return object()

    store.evidence_store._clock = broken_clock  # noqa: SLF001
    calls = (
        lambda: service.create("tenant-a", "subject-1", "principal-a", pending),
        lambda: service.authenticate(issued.session_token.get_secret_value()),
        lambda: service.refresh_csrf(issued.session_token.get_secret_value()),
        lambda: service.switch_tenant(
            issued.session_token.get_secret_value(),
            issued.csrf_token.get_secret_value(),
            "tenant-b",
        ),
        lambda: service.verify_csrf(
            issued.session, issued.csrf_token.get_secret_value()
        ),
    )
    for call in calls:
        with pytest.raises(
            IdentityUnavailable, match="^identity service is unavailable$"
        ) as caught:
            call()
        assert "secret-clock-detail" not in str(caught.value)


def test_refresh_csrf_keeps_session_cookie_and_rotates_only_csrf_revision() -> None:
    service, _, authority = _service()
    issued = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )

    refreshed = service.refresh_csrf(issued.session_token.get_secret_value())

    assert (
        refreshed.session_token.get_secret_value()
        == issued.session_token.get_secret_value()
    )
    assert (
        refreshed.csrf_token.get_secret_value() != issued.csrf_token.get_secret_value()
    )
    assert refreshed.session.session_id == issued.session.session_id
    assert refreshed.session.session_token_digest == issued.session.session_token_digest
    assert refreshed.session.csrf_token_digest != issued.session.csrf_token_digest
    assert refreshed.session.revision == issued.session.revision
    assert refreshed.session.created_at == issued.session.created_at
    assert refreshed.session.absolute_expires_at == issued.session.absolute_expires_at
    with pytest.raises(CredentialInvalid):
        service.verify_csrf(refreshed.session, issued.csrf_token.get_secret_value())
    service.verify_csrf(refreshed.session, refreshed.csrf_token.get_secret_value())


def test_revoke_requires_bound_csrf_and_persists_one_terminal_revision() -> None:
    service, store, authority = _service()
    issued = service.create(
        "tenant-a", "subject-1", "principal-a", _evidence(authority)
    )
    with pytest.raises(CredentialInvalid):
        service.revoke(
            issued.session_token.get_secret_value(),
            "wrong-csrf",
        )

    revoked = service.revoke(
        issued.session_token.get_secret_value(),
        issued.csrf_token.get_secret_value(),
    )

    assert revoked.session_id == issued.session.session_id
    assert revoked.revoked_at == NOW
    assert revoked.revision == issued.session.revision + 1
    assert store.sessions[revoked.session_id] == revoked
    with pytest.raises(CredentialInvalid):
        service.authenticate(issued.session_token.get_secret_value())


def test_rotation_port_contract_requires_membership_recheck_in_same_transaction() -> (
    None
):
    contract = inspect.getdoc(BrowserSessionRepository.rotate_session) or ""
    assert "same transaction" in contract
    assert "active membership" in contract
    assert "source Session unchanged" in contract
    create_contract = inspect.getdoc(BrowserSessionRepository.create_session) or ""
    touch_contract = inspect.getdoc(BrowserSessionRepository.touch_session) or ""
    assert "same transaction" in create_contract
    assert "active membership" in create_contract
    assert "same transaction" in touch_contract
    assert "active membership" in touch_contract
    assert "source and target" in contract
    assert "failure at any stage" in contract
    assert "rolls back source and replacement" in contract
    for binding in (
        "Subject",
        "credential",
        "Application",
        "provider",
        "authentication methods",
        "absolute expiry",
    ):
        assert binding in contract


def test_session_ports_freeze_backend_global_atomic_unique_arbitration() -> None:
    uow_contract = inspect.getdoc(SessionCreationUnitOfWork) or ""
    create_contract = inspect.getdoc(BrowserSessionRepository.create_session) or ""
    rotate_contract = inspect.getdoc(BrowserSessionRepository.rotate_session) or ""
    for contract in (uow_contract, create_contract, rotate_contract):
        assert "backend-global unique constraints" in contract
        assert "atomic arbiter" in contract
    assert "session_id" in rotate_contract
    assert "Session token digest" in rotate_contract


def test_session_creation_uow_exposes_no_standalone_evidence_consume_port() -> None:
    assert set(AuthenticationEvidenceSessionStore.__dict__) >= {
        "current_time",
        "issue",
        "inspect",
    }
    assert "consume" not in AuthenticationEvidenceSessionStore.__dict__
    assert AuthenticationEvidenceSessionStore in SessionCreationUnitOfWork.__bases__
    assert AuthenticationEvidenceStore not in SessionCreationUnitOfWork.__bases__
    assert AuthenticationEvidenceSessionStore in AuthenticationEvidenceStore.__bases__
    assert "consume_for_session" not in (
        evidence_module.AuthenticationEvidenceAuthority.__dict__
    )
    assert "consume_for_session" in AuthenticationEvidenceAuthority.__dict__
    contract = inspect.getdoc(SessionCreationUnitOfWork) or ""
    assert "Restricted Evidence is valid for a restricted Session" in contract
    assert "must exactly equal the Evidence flag" in contract


def test_token_only_lookup_ports_freeze_global_digest_uniqueness() -> None:
    from eios.identity.ports import InvitationRepository, PasswordResetRepository

    for method in (
        BrowserSessionRepository.find_by_token_digest,
        InvitationRepository.find_by_token_digest,
        PasswordResetRepository.find_by_token_digest,
    ):
        contract = inspect.getdoc(method) or ""
        assert "globally unique" in contract
    evidence_contract = inspect.getdoc(AuthenticationEvidenceStore) or ""
    assert "globally unique" in evidence_contract


def test_evidence_port_freezes_bounded_replay_retention_not_lifetime_uniqueness() -> (
    None
):
    contract = inspect.getdoc(AuthenticationEvidenceStore) or ""
    assert "active record and its original replay-retention TTL" in contract
    assert "cleanup after that TTL is intentional and bounded" in contract
    assert "Persistent adapters may enforce lifetime uniqueness" in contract


def test_session_internal_commands_carry_application_and_credential_tenant() -> None:
    assert {
        "application_revision",
        "credential_tenant_id",
        "new_csrf_token_digest",
    } <= set(TouchBrowserSessionCommand.model_fields)
    assert {
        "source_application_revision",
        "source_credential_tenant_id",
        "source_provider_id",
    } <= set(RotateBrowserSessionCommand.model_fields)


def test_seven_day_activity_rolls_idle_but_never_absolute_deadline():
    service, store, authority = _service()
    issued = service.create("tenant-a", "subject-1", "principal-a", _evidence(authority))
    clock = store.evidence_store._clock
    assert isinstance(clock, MutableClock)
    token = issued.session_token.get_secret_value()
    absolute = NOW + timedelta(days=30)
    for day in (6, 12, 18, 24, 29):
        clock.now = NOW + timedelta(days=day)
        touched = service.authenticate(token)
        assert touched.idle_expires_at == min(clock.now+timedelta(days=7), absolute)
        assert touched.absolute_expires_at == absolute
    clock.now = absolute
    with pytest.raises(CredentialInvalid):
        service.authenticate(token)


def test_old_expired_session_is_not_revived_by_new_idle_policy():
    service, store, authority = _service()
    issued = service.create("tenant-a", "subject-1", "principal-a", _evidence(authority))
    store.sessions[issued.session.session_id] = issued.session.model_copy(update={
        "idle_expires_at": NOW+timedelta(minutes=30),
        "absolute_expires_at": NOW+timedelta(hours=8),
    })
    clock = store.evidence_store._clock
    assert isinstance(clock, MutableClock)
    clock.now = NOW+timedelta(minutes=31)
    with pytest.raises(CredentialInvalid):
        service.authenticate(issued.session_token.get_secret_value())


@pytest.mark.parametrize("operation", ["lookup", "authenticate", "refresh_csrf", "switch_tenant", "revoke"])
@pytest.mark.parametrize("denied", [True, False])
def test_repository_credential_denial_stays_invalid_while_fault_stays_unavailable(
    monkeypatch, operation, denied
):
    service, store, authority = _service()
    issued = service.create("tenant-a", "subject-1", "principal-a", _evidence(authority))
    token = issued.session_token.get_secret_value()
    csrf = issued.csrf_token.get_secret_value()
    before = dict(store.sessions)
    boundary = {
        "lookup": "find_by_token_digest", "authenticate": "touch_session",
        "refresh_csrf": "touch_session", "switch_tenant": "rotate_session",
        "revoke": "revoke_session",
    }[operation]
    def reject(*args, **kwargs):
        error = CredentialInvalid if denied else RuntimeError
        raise error("private-repository-detail")
    monkeypatch.setattr(store, boundary, reject)
    error = CredentialInvalid if denied else IdentityUnavailable
    expected = "session is invalid or expired" if denied else "identity service is unavailable"
    with pytest.raises(error, match="^"+expected+"$") as captured:
        if operation == "lookup":
            service.inspect(token)
        elif operation == "switch_tenant":
            service.switch_tenant(token, csrf, "tenant-b")
        elif operation == "revoke":
            service.revoke(token, csrf)
        else:
            getattr(service, operation)(token)
    assert "private-repository-detail" not in str(captured.value)
    assert store.sessions == before

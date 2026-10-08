"""Single-use trusted authentication evidence for Session creation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from heapq import heapify, heappop, heappush
from hmac import compare_digest
from secrets import token_bytes
from threading import Lock
from typing import Literal, Protocol

from .errors import CredentialInvalid, IdentityUnavailable
from .models import LocalAccount, Subject, SubjectKind
from .ports import ResolvedExternalIdentity


_FAILURE = "authentication evidence is invalid or expired"
_PROOF_DOMAIN = b"nex-eios/authentication-evidence/proof/v1\x00"
_OIDC_METHOD_ALLOWLIST = frozenset(
    {"oidc", "pwd", "otp", "sms", "hwk", "swk", "face", "fpt", "pin"}
)


class AuthenticationEvidence:
    """Opaque bearer handle; all authority facts live in the evidence store."""

    __slots__ = ("evidence_id", "_proof")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("authentication evidence requires a trusted authority")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("authentication evidence is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("authentication evidence is immutable")

    def __repr__(self) -> str:
        return (
            "AuthenticationEvidence("
            f"evidence_id={self.evidence_id!r}, proof=<redacted>)"
        )

    def _export_for_trusted_transport(self) -> tuple[str, str]:
        """Export the bearer values for an authenticated trusted transport."""

        return self.evidence_id, self._proof


@dataclass(frozen=True, slots=True)
class AuthenticationEvidenceRecord:
    """Canonical server-side facts bound to one bearer proof."""

    evidence_id: str
    proof_digest: bytes = field(repr=False)
    tenant_id: str
    subject_id: str
    subject_kind: Literal["human"]
    subject_revision: int
    subject_status: Literal["active"]
    credential_kind: Literal["local_account", "external_identity"]
    credential_tenant_id: str
    credential_id: str
    credential_revision: int
    credential_session_epoch: int
    application_id: str
    application_revision: int
    authentication_methods: tuple[str, ...]
    purpose: Literal["session.create", "password.change"]
    issued_at: datetime
    expires_at: datetime
    restricted: bool
    authentication_issued_at: datetime | None = None
    authentication_not_before: datetime | None = None
    authentication_expires_at: datetime | None = None
    provider_id: str | None = None
    provider_revision: int | None = None
    provider_configuration_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticationEvidenceFacts:
    """Canonical authority facts accepted by an Evidence persistence boundary."""

    tenant_id: str
    subject_id: str
    subject_kind: Literal["human"]
    subject_revision: int
    subject_status: Literal["active"]
    credential_kind: Literal["local_account", "external_identity"]
    credential_tenant_id: str
    credential_id: str
    credential_revision: int
    credential_session_epoch: int
    application_id: str
    application_revision: int
    authentication_methods: tuple[str, ...]
    purpose: Literal["session.create", "password.change"]
    restricted: bool
    authentication_issued_at: datetime | None
    authentication_not_before: datetime | None
    authentication_expires_at: datetime | None
    provider_id: str | None
    provider_revision: int | None
    provider_configuration_fingerprint: str | None


# Compatibility alias for older in-package callers. Production adapters and
# composition code must import the public contract above.
_EvidenceFacts = AuthenticationEvidenceFacts


@dataclass(frozen=True, slots=True)
class _StoredEvidence:
    record: AuthenticationEvidenceRecord
    proof_digest: bytes


@dataclass(frozen=True, slots=True)
class _ReplayTombstone:
    proof_digest: bytes
    tenant_id: str
    application_id: str
    expires_at: datetime


class AuthenticationEvidenceSessionStore(Protocol):
    """Evidence operations required to issue and atomically create Sessions.

    Evidence IDs and proof digests are globally unique across tenants while an
    active record and its original replay-retention TTL remain authoritative,
    because lookup uses only the opaque ID and digest. Tombstone cleanup after
    that TTL is intentional and bounded.
    Persistent adapters may enforce lifetime uniqueness conservatively.

    Deliberately excludes standalone consume: durable Session creation consumes
    Evidence only inside ``BrowserSessionRepository.create_session``.
    """

    def current_time(self) -> datetime:
        """Return the transaction authority's trusted UTC clock."""
        ...

    def issue(
        self,
        evidence_id: str,
        proof_digest: bytes,
        facts: AuthenticationEvidenceFacts,
    ) -> AuthenticationEvidenceRecord:
        """Use store time and atomically store canonical facts in shared storage."""
        ...

    def inspect(
        self,
        evidence_id: str,
        proof_digest: bytes,
        *,
        tenant_id: str,
        application_id: str,
        purpose: Literal["session.create"],
    ) -> AuthenticationEvidenceRecord | None:
        """Compare proof and expected scope without consuming the record."""
        ...


class AuthenticationEvidenceStore(AuthenticationEvidenceSessionStore, Protocol):
    """Complete shared store used by authorities that explicitly consume Evidence.

    Evidence IDs and proof digests are globally unique across tenants while an
    active record and its original replay-retention TTL remain authoritative.
    Tombstone cleanup after that TTL is intentional and bounded.
    Persistent adapters may enforce lifetime uniqueness conservatively.
    """

    def consume(
        self,
        evidence_id: str,
        proof_digest: bytes,
    ) -> AuthenticationEvidenceRecord | None:
        """Atomically compare a proof and consume one shared single-use record."""
        ...


class AuthenticationEvidenceRejected(Exception):
    """Canonical authentication facts became invalid at atomic issuance."""


class InMemoryAuthenticationEvidenceStore:
    """Process-local reference implementation of the shared store contract."""

    def __init__(
        self,
        *,
        capacity: int = 10_000,
        global_capacity: int = 100_000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise ValueError("authentication evidence capacity is invalid")
        if type(global_capacity) is not int or not 1 <= global_capacity <= 1_000_000:
            raise ValueError("authentication evidence global capacity is invalid")
        self._records: dict[str, _StoredEvidence] = {}
        self._proof_index: dict[bytes, str] = {}
        self._scope_counts: dict[tuple[str, str], int] = {}
        self._expirations: list[tuple[datetime, str]] = []
        self._issuances: list[tuple[float, str]] = []
        self._replay_tombstones: dict[str, _ReplayTombstone] = {}
        self._proof_tombstones: dict[bytes, str] = {}
        self._replay_expirations: list[tuple[datetime, str]] = []
        self._lock = Lock()
        self._capacity = capacity
        self._global_capacity = global_capacity
        self._clock = clock

    def issue(
        self,
        evidence_id: str,
        proof_digest: bytes,
        facts: AuthenticationEvidenceFacts,
    ) -> AuthenticationEvidenceRecord:
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            _validate_authentication_window(facts, now)
            digest = bytes(proof_digest)
            if (
                evidence_id in self._records
                or evidence_id in self._replay_tombstones
                or digest in self._proof_index
                or digest in self._proof_tombstones
            ):
                raise _unavailable()
            scope = (facts.tenant_id, facts.application_id)
            if (
                self._scope_counts.get(scope, 0) >= self._capacity
                or len(self._records) + len(self._replay_tombstones)
                >= self._global_capacity
            ):
                raise _unavailable()
            expires_at = min(
                now + timedelta(minutes=5),
                facts.authentication_expires_at or now + timedelta(minutes=5),
            )
            record = AuthenticationEvidenceRecord(
                evidence_id=evidence_id,
                proof_digest=digest,
                tenant_id=facts.tenant_id,
                subject_id=facts.subject_id,
                subject_kind=facts.subject_kind,
                subject_revision=facts.subject_revision,
                subject_status=facts.subject_status,
                credential_kind=facts.credential_kind,
                credential_tenant_id=facts.credential_tenant_id,
                credential_id=facts.credential_id,
                credential_revision=facts.credential_revision,
                credential_session_epoch=facts.credential_session_epoch,
                application_id=facts.application_id,
                application_revision=facts.application_revision,
                authentication_methods=facts.authentication_methods,
                purpose=facts.purpose,
                issued_at=now,
                expires_at=expires_at,
                restricted=facts.restricted,
                authentication_issued_at=facts.authentication_issued_at,
                authentication_not_before=facts.authentication_not_before,
                authentication_expires_at=facts.authentication_expires_at,
                provider_id=facts.provider_id,
                provider_revision=facts.provider_revision,
                provider_configuration_fingerprint=(
                    facts.provider_configuration_fingerprint
                ),
            )
            self._records[evidence_id] = _StoredEvidence(
                record=record, proof_digest=digest
            )
            self._proof_index[digest] = evidence_id
            self._scope_counts[scope] = self._scope_counts.get(scope, 0) + 1
            heappush(self._expirations, (record.expires_at, evidence_id))
            heappush(self._issuances, (-record.issued_at.timestamp(), evidence_id))
            self._compact_indexes()
            return record

    def current_time(self) -> datetime:
        return self._now()

    def inspect(
        self,
        evidence_id: str,
        proof_digest: bytes,
        *,
        tenant_id: str,
        application_id: str,
        purpose: Literal["session.create"],
    ) -> AuthenticationEvidenceRecord | None:
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            stored = self._records.get(evidence_id)
            if stored is None:
                return None
            if now < stored.record.issued_at or now >= stored.record.expires_at:
                self._delete_active(evidence_id)
                self._compact_indexes()
                return None
            if not compare_digest(stored.proof_digest, proof_digest):
                return None
            if (
                stored.record.tenant_id != tenant_id
                or stored.record.application_id != application_id
                or stored.record.purpose != purpose
            ):
                return None
            return stored.record

    def consume(
        self,
        evidence_id: str,
        proof_digest: bytes,
    ) -> AuthenticationEvidenceRecord | None:
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            stored = self._records.get(evidence_id)
            if stored is None:
                return None
            if now < stored.record.issued_at or now >= stored.record.expires_at:
                self._delete_active(evidence_id)
                self._compact_indexes()
                return None
            if not compare_digest(stored.proof_digest, proof_digest):
                return None
            self._consume_active(evidence_id)
            self._compact_indexes()
            return stored.record

    def _now(self) -> datetime:
        return _utc_time(self._clock())

    def _purge_expired(self, now: datetime) -> None:
        while self._expirations and self._expirations[0][0] <= now:
            _, evidence_id = heappop(self._expirations)
            stored = self._records.get(evidence_id)
            if stored is not None and stored.record.expires_at <= now:
                self._delete_active(evidence_id)
        now_timestamp = now.timestamp()
        while self._issuances and -self._issuances[0][0] > now_timestamp:
            _, evidence_id = heappop(self._issuances)
            stored = self._records.get(evidence_id)
            if stored is not None and stored.record.issued_at > now:
                self._delete_active(evidence_id)
        while self._replay_expirations and self._replay_expirations[0][0] <= now:
            expires_at, evidence_id = heappop(self._replay_expirations)
            tombstone = self._replay_tombstones.get(evidence_id)
            if tombstone is not None and tombstone.expires_at == expires_at:
                self._remove_tombstone(evidence_id)
        self._compact_indexes()

    def _delete_active(self, evidence_id: str) -> None:
        stored = self._records.pop(evidence_id)
        self._proof_index.pop(stored.proof_digest, None)
        self._decrement_scope(stored.record.tenant_id, stored.record.application_id)

    def _consume_active(self, evidence_id: str) -> None:
        stored = self._records.pop(evidence_id)
        self._proof_index.pop(stored.proof_digest, None)
        tombstone = _ReplayTombstone(
            proof_digest=stored.proof_digest,
            tenant_id=stored.record.tenant_id,
            application_id=stored.record.application_id,
            expires_at=stored.record.expires_at,
        )
        self._replay_tombstones[evidence_id] = tombstone
        self._proof_tombstones[stored.proof_digest] = evidence_id
        heappush(self._replay_expirations, (tombstone.expires_at, evidence_id))

    def _remove_tombstone(self, evidence_id: str) -> None:
        tombstone = self._replay_tombstones.pop(evidence_id)
        self._proof_tombstones.pop(tombstone.proof_digest, None)
        self._decrement_scope(tombstone.tenant_id, tombstone.application_id)

    def _decrement_scope(self, tenant_id: str, application_id: str) -> None:
        scope = (tenant_id, application_id)
        remaining = self._scope_counts[scope] - 1
        if remaining:
            self._scope_counts[scope] = remaining
        else:
            del self._scope_counts[scope]

    def _compact_indexes(self) -> None:
        if len(self._expirations) > 2 * len(self._records) + 4:
            self._expirations = [
                (stored.record.expires_at, evidence_id)
                for evidence_id, stored in self._records.items()
            ]
            heapify(self._expirations)
        if len(self._issuances) > 2 * len(self._records) + 4:
            self._issuances = [
                (-stored.record.issued_at.timestamp(), evidence_id)
                for evidence_id, stored in self._records.items()
            ]
            heapify(self._issuances)
        if len(self._replay_expirations) > 2 * len(self._replay_tombstones) + 4:
            self._replay_expirations = [
                (tombstone.expires_at, evidence_id)
                for evidence_id, tombstone in self._replay_tombstones.items()
            ]
            heapify(self._replay_expirations)


class AuthenticationEvidenceAuthority:
    """Trusted issuer/consumer that never treats a Python object as authority."""

    def __init__(
        self,
        *,
        store: AuthenticationEvidenceSessionStore,
        random_bytes: Callable[[int], bytes] = token_bytes,
    ) -> None:
        self._random_bytes = random_bytes
        self._store = store

    def issue_password(
        self,
        account: LocalAccount,
        subject: Subject,
        *,
        application_id: str,
        application_revision: int = 1,
    ) -> AuthenticationEvidence:
        checked = LocalAccount.model_validate(account)
        checked_subject = Subject.model_validate(subject)
        if (
            checked.status != "active"
            or checked.password_hash is None
            or checked_subject.subject_id != checked.subject_id
            or checked_subject.kind is not SubjectKind.HUMAN
            or checked_subject.status != "active"
        ):
            raise _invalid()
        return self._issue(
            tenant_id=checked.tenant_id,
            subject_id=checked.subject_id,
            subject_revision=checked_subject.revision,
            credential_kind="local_account",
            credential_id=checked.local_account_id,
            credential_revision=checked.revision,
            credential_session_epoch=checked.session_epoch,
            application_id=application_id,
            application_revision=application_revision,
            authentication_methods=("password",),
            restricted=checked.must_change_password,
            authentication_issued_at=None,
            authentication_not_before=None,
            authentication_expires_at=None,
            provider_id=None,
            provider_revision=None,
            provider_configuration_fingerprint=None,
        )

    def issue_oidc(
        self,
        *,
        resolved_identity: ResolvedExternalIdentity,
        authentication_methods: tuple[str, ...],
        application_id: str,
        application_revision: int = 1,
        token_issued_at: datetime,
        token_not_before: datetime | None,
        token_expires_at: datetime,
    ) -> AuthenticationEvidence:
        checked = ResolvedExternalIdentity.model_validate(resolved_identity)
        checked_issued_at = _utc_time(token_issued_at)
        checked_not_before = (
            None if token_not_before is None else _utc_time(token_not_before)
        )
        checked_expires_at = _utc_time(token_expires_at)
        methods = tuple(dict.fromkeys(authentication_methods))
        if (
            not methods
            or methods[0] != "oidc"
            or not set(methods) <= _OIDC_METHOD_ALLOWLIST
            or "mfa" in methods
        ):
            raise _invalid()
        return self._issue(
            tenant_id=checked.tenant_id,
            subject_id=checked.subject_id,
            subject_revision=checked.subject_revision,
            credential_kind="external_identity",
            credential_id=checked.external_identity_id,
            credential_revision=checked.external_identity_revision,
            credential_session_epoch=checked.external_identity_session_epoch,
            application_id=application_id,
            application_revision=application_revision,
            authentication_methods=methods,
            restricted=False,
            authentication_issued_at=checked_issued_at,
            authentication_not_before=checked_not_before,
            authentication_expires_at=checked_expires_at,
            provider_id=checked.provider_id,
            provider_revision=checked.provider_revision,
            provider_configuration_fingerprint=(
                checked.provider_configuration_fingerprint
            ),
        )

    def current_time(self) -> datetime:
        """Read time only from the Evidence/Session transaction authority."""

        try:
            return _utc_time(self._store.current_time())
        except Exception:
            raise _unavailable() from None

    def inspect_for_session(
        self,
        evidence: AuthenticationEvidence,
        *,
        tenant_id: str,
        subject_id: str,
        application_id: str,
    ) -> AuthenticationEvidenceRecord:
        evidence_id, proof_digest = _handle_values(evidence)
        try:
            record = self._store.inspect(
                evidence_id,
                proof_digest,
                tenant_id=tenant_id,
                application_id=application_id,
                purpose="session.create",
            )
        except Exception:
            raise _unavailable() from None
        return _validated_record(
            record,
            evidence_id,
            proof_digest,
            tenant_id,
            subject_id,
            application_id,
            self.current_time(),
        )

    def _issue(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        subject_revision: int,
        credential_kind: Literal["local_account", "external_identity"],
        credential_id: str,
        credential_revision: int,
        credential_session_epoch: int,
        application_id: str,
        application_revision: int,
        authentication_methods: tuple[str, ...],
        restricted: bool,
        authentication_issued_at: datetime | None,
        authentication_not_before: datetime | None,
        authentication_expires_at: datetime | None,
        provider_id: str | None,
        provider_revision: int | None,
        provider_configuration_fingerprint: str | None,
    ) -> AuthenticationEvidence:
        if (
            not _identifier(tenant_id)
            or not _identifier(subject_id)
            or type(subject_revision) is not int
            or subject_revision < 1
            or not _identifier(credential_id)
            or type(credential_revision) is not int
            or credential_revision < 1
            or type(credential_session_epoch) is not int
            or credential_session_epoch < 1
            or not _identifier(application_id)
            or type(application_revision) is not int
            or application_revision < 1
        ):
            raise _invalid()
        evidence_id_bytes = self._secure_random(18)
        proof_bytes = self._secure_random(32)
        evidence_id = "aev_" + evidence_id_bytes.hex()
        proof = proof_bytes.hex()
        facts = AuthenticationEvidenceFacts(
            tenant_id=tenant_id,
            subject_id=subject_id,
            subject_kind="human",
            subject_revision=subject_revision,
            subject_status="active",
            credential_kind=credential_kind,
            credential_tenant_id=tenant_id,
            credential_id=credential_id,
            credential_revision=credential_revision,
            credential_session_epoch=credential_session_epoch,
            application_id=application_id,
            application_revision=application_revision,
            authentication_methods=authentication_methods,
            purpose="session.create",
            restricted=restricted,
            authentication_issued_at=authentication_issued_at,
            authentication_not_before=authentication_not_before,
            authentication_expires_at=authentication_expires_at,
            provider_id=provider_id,
            provider_revision=provider_revision,
            provider_configuration_fingerprint=provider_configuration_fingerprint,
        )
        proof_digest = authentication_evidence_proof_digest(proof)
        try:
            record = self._store.issue(
                evidence_id,
                proof_digest,
                facts,
            )
        except AuthenticationEvidenceRejected:
            raise _invalid() from None
        except Exception:
            raise _unavailable() from None
        try:
            exact = authentication_evidence_record_is_exact(
                record,
                evidence_id,
                proof_digest,
                facts,
            )
        except Exception:
            raise _unavailable() from None
        if not exact:
            raise _unavailable()
        return restore_authentication_evidence_for_trusted_transport(evidence_id, proof)

    def _secure_random(self, size: int) -> bytes:
        try:
            value = self._random_bytes(size)
        except Exception:
            raise _unavailable() from None
        if type(value) is not bytes or len(value) != size:
            raise _unavailable()
        return value


class AuthenticationEvidenceConsumerAuthority(AuthenticationEvidenceAuthority):
    """Explicit complete-store consumer used outside durable Session creation."""

    def __init__(
        self,
        *,
        store: AuthenticationEvidenceStore,
        random_bytes: Callable[[int], bytes] = token_bytes,
    ) -> None:
        super().__init__(store=store, random_bytes=random_bytes)
        self._consumer_store = store

    def consume_for_session(
        self,
        evidence: AuthenticationEvidence,
        *,
        tenant_id: str,
        subject_id: str,
        application_id: str,
    ) -> AuthenticationEvidenceRecord:
        evidence_id, proof_digest = _handle_values(evidence)
        try:
            record = self._consumer_store.consume(evidence_id, proof_digest)
        except Exception:
            raise _unavailable() from None
        return _validated_record(
            record,
            evidence_id,
            proof_digest,
            tenant_id,
            subject_id,
            application_id,
            self.current_time(),
        )


def restore_authentication_evidence_for_trusted_transport(
    evidence_id: str, proof: str
) -> AuthenticationEvidence:
    """Reconstruct a handle received through a trusted authenticated channel."""

    if (
        type(evidence_id) is not str
        or not evidence_id.startswith("aev_")
        or len(evidence_id) != 4 + 36
        or type(proof) is not str
        or len(proof) != 64
    ):
        raise _invalid()
    try:
        bytes.fromhex(evidence_id[4:])
        bytes.fromhex(proof)
    except ValueError:
        raise _invalid() from None
    evidence = object.__new__(AuthenticationEvidence)
    object.__setattr__(evidence, "evidence_id", evidence_id)
    object.__setattr__(evidence, "_proof", proof)
    return evidence


def _handle_values(evidence: object) -> tuple[str, bytes]:
    if type(evidence) is not AuthenticationEvidence:
        raise _invalid()
    try:
        evidence_id, proof = evidence._export_for_trusted_transport()
        return evidence_id, authentication_evidence_proof_digest(proof)
    except (AttributeError, TypeError, ValueError):
        raise _invalid() from None


def authentication_evidence_proof_digest(proof: str) -> bytes:
    """Derive the canonical store digest from one opaque Evidence proof."""

    if type(proof) is not str or len(proof) != 64:
        raise _invalid()
    try:
        proof_bytes = bytes.fromhex(proof)
    except ValueError:
        raise _invalid() from None
    return sha256(_PROOF_DOMAIN + proof_bytes).digest()


def _validated_record(
    record: object,
    evidence_id: str,
    proof_digest: bytes,
    tenant_id: str,
    subject_id: str,
    application_id: str,
    now: datetime,
) -> AuthenticationEvidenceRecord:
    try:
        invalid = (
            type(record) is not AuthenticationEvidenceRecord
            or record.evidence_id != evidence_id
            or type(record.proof_digest) is not bytes
            or len(record.proof_digest) != 32
            or not compare_digest(record.proof_digest, proof_digest)
            or record.tenant_id != tenant_id
            or record.credential_tenant_id != tenant_id
            or record.subject_id != subject_id
            or record.application_id != application_id
            or record.subject_kind != "human"
            or record.subject_status != "active"
            or record.purpose != "session.create"
            or "mfa" in record.authentication_methods
            or not _record_schema_is_exact(record, now)
        )
    except Exception:
        raise _invalid() from None
    if invalid:
        raise _invalid()
    return record


def _record_schema_is_exact(
    record: AuthenticationEvidenceRecord,
    now: datetime,
) -> bool:
    revisions = (
        record.subject_revision,
        record.credential_revision,
        record.credential_session_epoch,
        record.application_revision,
    )
    if (
        not all(type(value) is int and value >= 1 for value in revisions)
        or not all(
            _identifier(value)
            for value in (
                record.evidence_id,
                record.tenant_id,
                record.subject_id,
                record.credential_tenant_id,
                record.credential_id,
                record.application_id,
            )
        )
        or record.credential_tenant_id != record.tenant_id
        or record.subject_kind != "human"
        or record.subject_status != "active"
        or record.purpose != "session.create"
        or type(record.restricted) is not bool
        or type(record.authentication_methods) is not tuple
        or not record.authentication_methods
        or len(record.authentication_methods) != len(set(record.authentication_methods))
        or any(not _identifier(value) for value in record.authentication_methods)
        or not _canonical_utc(record.issued_at)
        or not _canonical_utc(record.expires_at)
        or not record.issued_at <= now < record.expires_at
        or not record.issued_at < record.expires_at
        or record.expires_at > record.issued_at + timedelta(minutes=5)
    ):
        return False
    if record.credential_kind == "local_account":
        return (
            record.authentication_methods == ("password",)
            and record.provider_id is None
            and record.provider_revision is None
            and record.provider_configuration_fingerprint is None
            and record.authentication_issued_at is None
            and record.authentication_not_before is None
            and record.authentication_expires_at is None
            and record.expires_at == record.issued_at + timedelta(minutes=5)
        )
    if record.credential_kind != "external_identity":
        return False
    return bool(
        record.authentication_methods[0] == "oidc"
        and set(record.authentication_methods) <= _OIDC_METHOD_ALLOWLIST
        and _identifier(record.provider_id)
        and type(record.provider_revision) is int
        and record.provider_revision >= 1
        and _fingerprint(record.provider_configuration_fingerprint)
        and _canonical_utc(record.authentication_issued_at)
        and (
            record.authentication_not_before is None
            or _canonical_utc(record.authentication_not_before)
        )
        and _canonical_utc(record.authentication_expires_at)
        and record.authentication_issued_at <= record.issued_at
        and record.authentication_issued_at < record.authentication_expires_at
        and (
            record.authentication_not_before is None
            or record.authentication_not_before <= record.issued_at
        )
        and (
            record.authentication_not_before is None
            or record.authentication_not_before < record.authentication_expires_at
        )
        and record.authentication_expires_at >= record.expires_at
        and record.authentication_expires_at > record.issued_at
        and record.expires_at
        == min(
            record.issued_at + timedelta(minutes=5),
            record.authentication_expires_at,
        )
    )


def _canonical_utc(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _fingerprint(value: object) -> bool:
    return bool(
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def authentication_evidence_record_is_exact(
    record: object,
    evidence_id: str,
    proof_digest: bytes,
    facts: AuthenticationEvidenceFacts,
) -> bool:
    """Reject a store that acknowledges different canonical authority facts."""

    return bool(
        type(record) is AuthenticationEvidenceRecord
        and record.evidence_id == evidence_id
        and type(record.proof_digest) is bytes
        and len(record.proof_digest) == 32
        and compare_digest(record.proof_digest, proof_digest)
        and record.tenant_id == facts.tenant_id
        and record.subject_id == facts.subject_id
        and record.subject_kind == facts.subject_kind
        and record.subject_revision == facts.subject_revision
        and record.subject_status == facts.subject_status
        and record.credential_kind == facts.credential_kind
        and record.credential_tenant_id == facts.credential_tenant_id
        and record.credential_id == facts.credential_id
        and record.credential_revision == facts.credential_revision
        and record.credential_session_epoch == facts.credential_session_epoch
        and record.application_id == facts.application_id
        and record.application_revision == facts.application_revision
        and record.authentication_methods == facts.authentication_methods
        and record.purpose == facts.purpose
        and record.restricted is facts.restricted
        and record.authentication_issued_at == facts.authentication_issued_at
        and record.authentication_not_before == facts.authentication_not_before
        and record.authentication_expires_at == facts.authentication_expires_at
        and record.provider_id == facts.provider_id
        and record.provider_revision == facts.provider_revision
        and record.provider_configuration_fingerprint
        == facts.provider_configuration_fingerprint
        and _canonical_utc(record.issued_at)
        and _canonical_utc(record.expires_at)
        and record.expires_at
        == min(
            record.issued_at + timedelta(minutes=5),
            facts.authentication_expires_at or record.issued_at + timedelta(minutes=5),
        )
    )


def _validate_authentication_window(
    facts: AuthenticationEvidenceFacts, now: datetime
) -> None:
    if facts.purpose not in {"session.create", "password.change"}:
        raise AuthenticationEvidenceRejected
    values = (
        facts.authentication_issued_at,
        facts.authentication_not_before,
        facts.authentication_expires_at,
    )
    if facts.credential_kind == "local_account":
        if any(value is not None for value in values) or any(
            value is not None
            for value in (
                facts.provider_id,
                facts.provider_revision,
                facts.provider_configuration_fingerprint,
            )
        ):
            raise AuthenticationEvidenceRejected
        return
    if (
        not _identifier(facts.provider_id)
        or type(facts.provider_revision) is not int
        or facts.provider_revision < 1
        or not _fingerprint(facts.provider_configuration_fingerprint)
        or facts.authentication_issued_at is None
        or facts.authentication_expires_at is None
        or facts.authentication_issued_at > now
        or (
            facts.authentication_not_before is not None
            and facts.authentication_not_before > now
        )
        or facts.authentication_expires_at <= now
    ):
        raise AuthenticationEvidenceRejected


def _utc_time(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _invalid()
    return value.astimezone(UTC)


def _identifier(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and bool(value.strip())


def _invalid() -> CredentialInvalid:
    return CredentialInvalid(_FAILURE)


def _unavailable() -> IdentityUnavailable:
    return IdentityUnavailable("authentication evidence is unavailable")


# Backward-compatible private aliases for existing in-package callers.
_issued_record_is_exact = authentication_evidence_record_is_exact
_proof_digest = authentication_evidence_proof_digest
_restore_authentication_evidence = restore_authentication_evidence_for_trusted_transport


__all__ = [
    "AuthenticationEvidence",
    "AuthenticationEvidenceAuthority",
    "AuthenticationEvidenceConsumerAuthority",
    "AuthenticationEvidenceFacts",
    "AuthenticationEvidenceRecord",
    "AuthenticationEvidenceRejected",
    "AuthenticationEvidenceSessionStore",
    "AuthenticationEvidenceStore",
    "InMemoryAuthenticationEvidenceStore",
    "authentication_evidence_proof_digest",
    "authentication_evidence_record_is_exact",
    "restore_authentication_evidence_for_trusted_transport",
]

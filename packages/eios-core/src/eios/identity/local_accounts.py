"""Local enterprise account authentication services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import pbkdf2_hmac
from hmac import compare_digest
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .errors import (
    CredentialInvalid,
    IdentityConflict,
    IdentityUnavailable,
    PasswordPolicyViolation,
)
from .evidence import AuthenticationEvidence, AuthenticationEvidenceAuthority
from .models import EncodedPasswordHash, LocalAccount, Subject, SubjectKind
from .ports import (
    LocalAccountRepository,
    RecordLocalAccountFailureCommand,
    SaveLocalAccountCommand,
    SubjectRepository,
    TrustedIdentityOperator,
)
from .settings import MINIMUM_NEW_PASSWORD_LENGTH


_MAX_TENANT_ID_CHARS = 255
_MAX_USERNAME_CHARS = 320
_MAX_PASSWORD_BYTES = 4096
_GENERIC_FAILURE = "credentials are invalid"


@dataclass(frozen=True, slots=True)
class PasswordAuthentication:
    tenant_id: str
    local_account_id: str
    subject_id: str
    evidence: AuthenticationEvidence
    authentication_methods: tuple[str, ...] = ("password",)
    must_change_password: bool = False


class LocalAccountService:
    """Verify local credentials while persisting bounded lockout state."""

    def __init__(
        self,
        repository: LocalAccountRepository,
        evidence_authority: AuthenticationEvidenceAuthority,
        *,
        subject_repository: SubjectRepository,
        tenant_id: str,
        application_id: str,
        application_revision: int = 1,
        operator: TrustedIdentityOperator,
        password_hasher: Any | None = None,
        failure_result_verifier: Callable[[LocalAccount, LocalAccount, datetime, TrustedIdentityOperator], bool] | None = None,
    ) -> None:
        self._repository = repository
        self._failure_result_verifier = failure_result_verifier
        self._evidence = evidence_authority
        self._subjects = subject_repository
        if not _bounded_text(tenant_id, _MAX_TENANT_ID_CHARS):
            raise ValueError("tenant_id is invalid")
        self._tenant_id = tenant_id
        if not _bounded_text(application_id, _MAX_TENANT_ID_CHARS):
            raise ValueError("application_id is invalid")
        self._application_id = application_id
        if type(application_revision) is not int or application_revision < 1:
            raise ValueError("application_revision is invalid")
        self._application_revision = application_revision
        self._operator = TrustedIdentityOperator.model_validate(operator)
        self._hasher = password_hasher or PasswordHasher()
        self._dummy_hash = EncodedPasswordHash(
            self._hasher.hash("nex-eios-dummy-password-verification")
        )

    def authenticate(
        self,
        tenant_id: str,
        username: str,
        password: str,
    ) -> PasswordAuthentication:
        current_time = self._current_time()
        if tenant_id != self._tenant_id or not _bounded_text(
            username, _MAX_USERNAME_CHARS
        ):
            self._dummy_verify(password)
            raise _invalid()
        if not _bounded_password(password):
            raise _invalid()

        try:
            candidate = self._repository.find_by_username(tenant_id, username)
        except Exception:
            raise _unavailable() from None
        if candidate is None:
            self._dummy_verify(password)
            raise _invalid()
        try:
            account = LocalAccount.model_validate(candidate)
        except (TypeError, ValueError):
            self._dummy_verify(password)
            raise _invalid() from None
        if (
            account.tenant_id != tenant_id
            or account.username != username
            or account.status != "active"
            or account.password_hash is None
        ):
            self._dummy_verify(password)
            raise _invalid()
        if account.locked_until is not None and current_time < account.locked_until:
            self._dummy_verify(password)
            raise _invalid()

        verified, requires_rehash = self._verify(account.password_hash, password)
        if not verified:
            self._record_failure(account, current_time)
            raise _invalid()

        replacement_hash = (
            EncodedPasswordHash(self._hasher.hash(password))
            if requires_rehash
            else account.password_hash
        )
        if (
            replacement_hash != account.password_hash
            or account.failed_attempts
            or account.lockout_level
            or account.locked_until is not None
        ):
            account = self._save(
                account,
                now=current_time,
                password_hash=replacement_hash,
                failed_attempts=0,
                lockout_level=0,
                locked_until=None,
            )
        try:
            candidate_subject = self._subjects.get_subject(account.subject_id)
        except Exception:
            raise _unavailable() from None
        try:
            subject = (
                Subject.model_validate(candidate_subject)
                if candidate_subject is not None
                else None
            )
        except (TypeError, ValueError):
            raise _invalid() from None
        if (
            subject is None
            or subject.subject_id != account.subject_id
            or subject.kind is not SubjectKind.HUMAN
            or subject.status != "active"
        ):
            raise _invalid()
        try:
            evidence = self._evidence.issue_password(
                account,
                subject,
                application_id=self._application_id,
                application_revision=self._application_revision,
            )
        except CredentialInvalid:
            raise _invalid() from None
        return PasswordAuthentication(
            tenant_id=account.tenant_id,
            local_account_id=account.local_account_id,
            subject_id=account.subject_id,
            must_change_password=account.must_change_password,
            evidence=evidence,
        )

    def verify_current_password(
        self,
        account: LocalAccount,
        password: str,
    ) -> None:
        current_time = self._current_time()
        try:
            checked = LocalAccount.model_validate(account)
        except (TypeError, ValueError):
            raise _invalid() from None
        if (
            not _bounded_password(password)
            or checked.status != "active"
            or checked.password_hash is None
            or (
                checked.locked_until is not None and current_time < checked.locked_until
            )
        ):
            raise _invalid()
        if not self._verify(checked.password_hash, password)[0]:
            self._record_failure(checked, current_time)
            raise _invalid()

    def hash_password(self, password: str) -> EncodedPasswordHash:
        if not _bounded_password(password):
            raise _password_policy()
        return EncodedPasswordHash(self._hasher.hash(password))

    def hash_password_for_account(
        self, account: LocalAccount, password: str
    ) -> EncodedPasswordHash:
        """Hash a new credential after bounded length-policy and reuse checks."""
        try:
            checked = LocalAccount.model_validate(account)
        except (TypeError, ValueError):
            raise _invalid() from None
        if not _bounded_password(password):
            raise _password_policy()
        if len(password) < MINIMUM_NEW_PASSWORD_LENGTH:
            raise _password_policy()
        candidate_hashes = (
            () if checked.password_hash is None else (checked.password_hash,)
        ) + checked.password_history
        reused = False
        for encoded_hash in candidate_hashes:
            reused = self._verify(encoded_hash, password)[0] or reused
        if reused:
            raise _password_policy()
        return EncodedPasswordHash(self._hasher.hash(password))

    def _dummy_verify(self, password: str) -> None:
        bounded = (
            password if _bounded_password(password) else "invalid-bounded-password"
        )
        try:
            self._hasher.verify(self._dummy_hash.get_secret_value(), bounded)
        except (InvalidHashError, VerificationError, ValueError):
            pass

    def _current_time(self) -> datetime:
        try:
            return _utc_time(self._evidence.current_time())
        except Exception:
            raise _unavailable() from None

    def _verify(
        self, encoded_hash: EncodedPasswordHash, password: str
    ) -> tuple[bool, bool]:
        encoded = encoded_hash.get_secret_value()
        if encoded.startswith("$argon2id$"):
            try:
                verified = bool(self._hasher.verify(encoded, password))
            except (InvalidHashError, VerificationError, ValueError):
                return False, False
            return verified, verified and bool(self._hasher.check_needs_rehash(encoded))
        parts = encoded.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False, False
        try:
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
            derived = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        except (UnicodeError, ValueError):
            return False, False
        return compare_digest(derived, expected), True

    def _record_failure(self, account: LocalAccount, now: datetime) -> None:
        command = RecordLocalAccountFailureCommand(
            tenant_id=account.tenant_id,
            local_account_id=account.local_account_id,
            failed_at=now,
        )
        for _ in range(3):
            try:
                recorded = self._repository.record_authentication_failure(
                    command,
                    operator=self._operator,
                )
            except IdentityConflict:
                continue
            except Exception:
                raise _unavailable() from None
            try:
                checked = LocalAccount.model_validate(recorded)
            except (TypeError, ValueError):
                continue
            if self._failure_result_verifier is not None:
                try:
                    verified = self._failure_result_verifier(account, checked, now, self._operator)
                except Exception:
                    raise _unavailable() from None
                if verified is not True:
                    raise _unavailable()
                return
            if not _valid_recorded_failure(account, checked, now):
                continue
            return
        raise _invalid()

    def _save(
        self, account: LocalAccount, *, now: datetime, **changes: object
    ) -> LocalAccount:
        desired = account.model_copy(update={**changes, "updated_at": now})
        try:
            saved = self._repository.save_local_account(
                SaveLocalAccountCommand(
                    local_account=desired,
                    expected_revision=account.revision,
                ),
                operator=self._operator,
            )
        except Exception:
            raise _unavailable() from None
        try:
            checked = LocalAccount.model_validate(saved)
        except (TypeError, ValueError):
            raise _invalid() from None
        expected = desired.model_copy(update={"revision": account.revision + 1})
        if checked != expected:
            raise _invalid()
        return checked


def _utc_time(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _invalid()
    return value.astimezone(UTC)


def _bounded_text(value: object, maximum: int) -> bool:
    return type(value) is str and 0 < len(value) <= maximum and bool(value.strip())


def _bounded_password(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_PASSWORD_BYTES
    except UnicodeError:
        return False


def _valid_recorded_failure(
    before: LocalAccount, after: LocalAccount, failed_at: datetime
) -> bool:
    if before.locked_until is not None and failed_at < before.locked_until:
        return after == before
    attempts = before.failed_attempts + 1
    changes: dict[str, object] = {
        "failed_attempts": attempts,
        "locked_until": None,
        "updated_at": failed_at,
        "revision": before.revision + 1,
    }
    if attempts == 5:
        level = min(before.lockout_level + 1, 3)
        changes.update(
            failed_attempts=0,
            lockout_level=level,
            locked_until=failed_at
            + (
                timedelta(minutes=15),
                timedelta(minutes=30),
                timedelta(minutes=60),
            )[level - 1],
        )
    return after == before.model_copy(update=changes)


def _invalid() -> CredentialInvalid:
    return CredentialInvalid(_GENERIC_FAILURE)


def _password_policy() -> PasswordPolicyViolation:
    return PasswordPolicyViolation("password does not satisfy policy")


def _unavailable() -> IdentityUnavailable:
    return IdentityUnavailable("identity service is unavailable")


__all__ = ["LocalAccountService", "PasswordAuthentication"]

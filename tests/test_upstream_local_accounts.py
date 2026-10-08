from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import inspect
from threading import Barrier, Lock, Thread

from argon2 import PasswordHasher
import pytest

from eios.identity.errors import (
    CredentialInvalid,
    IdentityConflict,
    IdentityUnavailable,
    PasswordPolicyViolation,
)
from eios.identity.evidence import (
    AuthenticationEvidence,
    AuthenticationEvidenceConsumerAuthority as AuthenticationEvidenceAuthority,
    InMemoryAuthenticationEvidenceStore,
)
from eios.identity.local_accounts import LocalAccountService, _valid_recorded_failure
from eios.identity.models import EncodedPasswordHash, LocalAccount, Subject, SubjectKind
from eios.identity.ports import (
    RecordLocalAccountFailureCommand,
    SaveLocalAccountCommand,
    TrustedIdentityOperator,
)


NOW = datetime(2026, 7, 20, 8, tzinfo=UTC)
VALID_PASSWORD = "correct horse battery staple"


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def current_time(self) -> datetime:
        return self.now


def _hasher() -> PasswordHasher:
    return PasswordHasher(memory_cost=19_456, time_cost=2, parallelism=1)


def _legacy_hash(password: str) -> EncodedPasswordHash:
    salt = bytes.fromhex("00" * 16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return EncodedPasswordHash(f"pbkdf2_sha256$100000${salt.hex()}${digest.hex()}")


def _account(*, password_hash: EncodedPasswordHash, **changes: object) -> LocalAccount:
    values: dict[str, object] = {
        "local_account_id": "account-1",
        "tenant_id": "tenant-a",
        "subject_id": "subject-1",
        "username": "alice",
        "verified_email": "alice@example.com",
        "password_hash": password_hash,
        "password_history": (),
        "status": "active",
        "failed_attempts": 0,
        "lockout_level": 0,
        "locked_until": None,
        "must_change_password": False,
        "session_epoch": 1,
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(days=1),
        "revision": 1,
    }
    values.update(changes)
    return LocalAccount(**values)


class AccountStore:
    def __init__(self, account: LocalAccount | None) -> None:
        self.account = account
        self.save_count = 0
        self.find_calls: list[tuple[str, str]] = []
        self.failure_lock = Lock()
        self.failure_conflicts = 0
        self.failure_calls = 0
        self.read_barrier: Barrier | None = None
        self.subject = Subject(
            subject_id="subject-1",
            kind=SubjectKind.HUMAN,
            status="active",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
            revision=1,
        )

    def get_subject(self, subject_id: str) -> Subject | None:
        return self.subject if subject_id == self.subject.subject_id else None

    def find_by_username(self, tenant_id: str, username: str) -> LocalAccount | None:
        self.find_calls.append((tenant_id, username))
        if self.account is None:
            return None
        if (tenant_id, username) != (self.account.tenant_id, self.account.username):
            return None
        snapshot = self.account
        if self.read_barrier is not None:
            self.read_barrier.wait()
        return snapshot

    def save_local_account(
        self,
        command: SaveLocalAccountCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> LocalAccount:
        assert operator.operator_principal_id == "identity-service"
        assert self.account is not None
        assert command.expected_revision == self.account.revision
        assert command.local_account.revision == self.account.revision
        self.account = command.local_account.model_copy(
            update={"revision": self.account.revision + 1}
        )
        self.save_count += 1
        return self.account

    def record_authentication_failure(
        self,
        command: RecordLocalAccountFailureCommand,
        *,
        operator: TrustedIdentityOperator,
    ) -> LocalAccount:
        del operator
        with self.failure_lock:
            self.failure_calls += 1
            if self.failure_conflicts:
                self.failure_conflicts -= 1
                raise IdentityConflict("failure revision changed")
            assert self.account is not None
            assert command.local_account_id == self.account.local_account_id
            if (
                self.account.locked_until is not None
                and command.failed_at < self.account.locked_until
            ):
                return self.account
            attempts = self.account.failed_attempts + 1
            changes: dict[str, object] = {
                "updated_at": command.failed_at,
                "revision": self.account.revision + 1,
            }
            if attempts == 5:
                level = min(self.account.lockout_level + 1, 3)
                changes.update(
                    failed_attempts=0,
                    lockout_level=level,
                    locked_until=command.failed_at
                    + (
                        timedelta(minutes=15),
                        timedelta(minutes=30),
                        timedelta(minutes=60),
                    )[level - 1],
                )
            else:
                changes.update(failed_attempts=attempts, locked_until=None)
            self.account = self.account.model_copy(update=changes)
            return self.account


class CountingHasher:
    def __init__(self) -> None:
        self.delegate = _hasher()
        self.verify_count = 0

    def hash(self, password: str) -> str:
        return self.delegate.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        self.verify_count += 1
        return self.delegate.verify(encoded, password)

    def check_needs_rehash(self, encoded: str) -> bool:
        return self.delegate.check_needs_rehash(encoded)


def _operator() -> TrustedIdentityOperator:
    return TrustedIdentityOperator(
        operator_principal_id="identity-service",
        request_id="request-1",
        trace_id="trace-1",
    )


def _service(
    store: AccountStore,
    hasher: PasswordHasher | CountingHasher | None = None,
    *,
    application_id: str = "portal",
    clock: MutableClock | None = None,
) -> LocalAccountService:
    trusted_clock = clock or MutableClock()
    return LocalAccountService(
        store,
        AuthenticationEvidenceAuthority(
            store=InMemoryAuthenticationEvidenceStore(clock=trusted_clock)
        ),
        subject_repository=store,
        tenant_id="tenant-a",
        application_id=application_id,
        operator=_operator(),
        password_hasher=hasher or _hasher(),
    )


def test_lockout_is_persistent_generic_and_escalates() -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    clock = MutableClock()
    service = _service(store, clock=clock)

    for _ in range(5):
        with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
            service.authenticate("tenant-a", "alice", "wrong")
    assert store.account is not None
    assert store.account.locked_until == NOW + timedelta(minutes=15)

    for lock_minutes in (30, 60, 60):
        after_expiry = store.account.locked_until
        assert after_expiry is not None
        clock.now = after_expiry
        for _ in range(5):
            with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
                service.authenticate("tenant-a", "alice", "wrong")
        assert store.account.locked_until == after_expiry + timedelta(
            minutes=lock_minutes
        )


def test_pbkdf2_success_rehashes_to_argon2_and_clears_failures() -> None:
    store = AccountStore(
        _account(
            password_hash=_legacy_hash(VALID_PASSWORD),
            failed_attempts=3,
            lockout_level=2,
        )
    )

    authenticated = _service(store).authenticate("tenant-a", "alice", VALID_PASSWORD)

    assert authenticated.authentication_methods == ("password",)
    assert type(authenticated.evidence) is AuthenticationEvidence
    assert store.account is not None
    assert store.account.password_hash is not None
    assert store.account.password_hash.startswith("$argon2id$")
    assert store.account.failed_attempts == 0
    assert store.account.lockout_level == 0
    assert store.account.locked_until is None


def test_local_authentication_requires_trusted_active_human_subject_resolution() -> (
    None
):
    assert "subject_repository" in inspect.signature(LocalAccountService).parameters
    assert "tenant_id" in inspect.signature(LocalAccountService).parameters

    for changes in (
        {"status": "disabled", "revision": 2},
        {"kind": SubjectKind.AGENT, "revision": 2},
        {"kind": SubjectKind.SERVICE, "revision": 2},
    ):
        store = AccountStore(
            _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
        )
        store.subject = store.subject.model_copy(update=changes)
        with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
            _service(store).authenticate("tenant-a", "alice", VALID_PASSWORD)


def test_local_evidence_binds_exact_subject_and_credential_revisions() -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    store.subject = store.subject.model_copy(update={"revision": 7})
    evidence_store = InMemoryAuthenticationEvidenceStore()
    authority = AuthenticationEvidenceAuthority(store=evidence_store)
    authenticated = LocalAccountService(
        store,
        authority,
        subject_repository=store,
        tenant_id="tenant-a",
        application_id="portal",
        operator=_operator(),
        password_hasher=_hasher(),
    ).authenticate("tenant-a", "alice", VALID_PASSWORD)
    record = authority.consume_for_session(
        authenticated.evidence,
        tenant_id="tenant-a",
        subject_id="subject-1",
        application_id="portal",
    )
    assert record.subject_revision == 7
    assert record.credential_revision == 1
    assert record.credential_session_epoch == 1


def test_local_authentication_rejects_caller_tenant_outside_trusted_context() -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    service = _service(store)

    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        service.authenticate("tenant-b", "alice", VALID_PASSWORD)

    assert store.find_calls == []


def test_new_password_rejects_current_or_historical_reuse_with_generic_error() -> None:
    hasher = _hasher()
    current = EncodedPasswordHash(hasher.hash(VALID_PASSWORD))
    historical_password = "historical correct horse battery staple"
    history = EncodedPasswordHash(hasher.hash(historical_password))
    service = _service(AccountStore(None), hasher)
    account = _account(password_hash=current, password_history=(history,))

    for reused in (VALID_PASSWORD, historical_password):
        with pytest.raises(
            PasswordPolicyViolation, match="^password does not satisfy policy$"
        ):
            service.hash_password_for_account(account, reused)


def test_new_password_verification_work_is_bounded_by_current_plus_ten_history() -> (
    None
):
    counting = CountingHasher()
    current = EncodedPasswordHash(counting.hash("current unique password value"))
    history = tuple(
        EncodedPasswordHash(counting.hash(f"historical unique password {index}"))
        for index in range(10)
    )
    account = _account(password_hash=current, password_history=history)
    service = _service(AccountStore(None), counting)

    encoded = service.hash_password_for_account(account, "brand new password value")

    assert encoded.startswith("$argon2id$")
    assert counting.verify_count == 11


def test_missing_user_performs_dummy_argon2_verification() -> None:
    counting = CountingHasher()
    service = _service(AccountStore(None), counting)

    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        service.authenticate("tenant-a", "missing", "wrong")

    assert counting.verify_count == 1


def test_locked_account_and_malformed_repository_record_fail_generically() -> None:
    valid = _account(
        password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)),
        updated_at=NOW,
        locked_until=NOW + timedelta(minutes=1),
    )
    store = AccountStore(valid)
    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store).authenticate("tenant-a", "alice", VALID_PASSWORD)

    forged_values = {
        field_name: getattr(valid, field_name)
        for field_name in LocalAccount.model_fields
    }
    forged_values["tenant_id"] = "tenant-b"
    forged = LocalAccount.model_construct(**forged_values)
    store.account = forged
    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store).authenticate("tenant-a", "alice", VALID_PASSWORD)


def test_external_password_work_is_bounded_and_redacted() -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    hasher = CountingHasher()

    with pytest.raises(CredentialInvalid, match="^credentials are invalid$") as caught:
        _service(store, hasher).authenticate(
            "tenant-a", "alice", "secret-needle" * 1000
        )

    assert "secret-needle" not in str(caught.value)
    assert hasher.verify_count == 0
    assert store.save_count == 0


def test_repository_failures_are_bounded_and_do_not_leak_credentials() -> None:
    class FailingStore(AccountStore):
        def find_by_username(
            self, tenant_id: str, username: str
        ) -> LocalAccount | None:
            del tenant_id, username
            raise RuntimeError("database secret-needle password")

    service = _service(FailingStore(None))
    with pytest.raises(
        IdentityUnavailable, match="^identity service is unavailable$"
    ) as caught:
        service.authenticate("tenant-a", "alice", "secret-needle")
    assert "secret-needle" not in str(caught.value)


@pytest.mark.parametrize("concurrency", (5, 6, 20))
def test_concurrent_bad_passwords_never_clear_fifth_failure_lock(
    concurrency: int,
) -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    service = _service(store)
    barrier = Barrier(concurrency)
    store.read_barrier = barrier
    outcomes: list[str] = []

    def fail() -> None:
        try:
            service.authenticate("tenant-a", "alice", "wrong")
        except CredentialInvalid as error:
            outcomes.append(str(error))

    threads = [Thread(target=fail) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes == ["credentials are invalid"] * concurrency
    assert store.account is not None
    assert store.account.locked_until == NOW + timedelta(minutes=15)
    assert store.account.revision == 6


def test_already_locked_account_stays_locked_without_failure_state_regression() -> None:
    locked = _account(
        password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)),
        failed_attempts=0,
        lockout_level=1,
        locked_until=NOW + timedelta(minutes=15),
        updated_at=NOW,
        revision=6,
    )
    store = AccountStore(locked)
    clock = MutableClock(NOW + timedelta(minutes=1))
    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store, clock=clock).authenticate("tenant-a", "alice", "wrong")
    assert store.account == locked


def test_bad_password_revision_conflicts_retry_bounded_and_remain_generic() -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    store.failure_conflicts = 2
    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store).authenticate("tenant-a", "alice", "wrong")
    assert store.failure_conflicts == 0
    assert store.account is not None and store.account.failed_attempts == 1

    store.failure_conflicts = 10
    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store).authenticate("tenant-a", "alice", "wrong")


def test_single_bad_password_uses_one_atomic_failure_call() -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )

    with pytest.raises(CredentialInvalid):
        _service(store).authenticate("tenant-a", "alice", "wrong")

    assert store.failure_calls == 1


@pytest.mark.parametrize("failure", ("malformed", "error"))
def test_local_auth_normalizes_trusted_clock_failure(failure: str) -> None:
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)))
    )
    clock = MutableClock()
    service = _service(store, clock=clock)

    if failure == "malformed":
        clock.now = object()  # type: ignore[assignment]
    else:

        def broken_clock() -> datetime:
            raise RuntimeError("secret-clock-detail")

        service._evidence._store._clock = broken_clock  # noqa: SLF001

    with pytest.raises(
        IdentityUnavailable, match="^identity service is unavailable$"
    ) as caught:
        service.authenticate("tenant-a", "alice", VALID_PASSWORD)
    assert "secret-clock-detail" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    (
        {"locked_until": NOW + timedelta(seconds=1)},
        {"revision": 3},
        {"failed_attempts": 1},
        {"lockout_level": 0},
        {"updated_at": NOW + timedelta(seconds=1)},
    ),
)
def test_failure_postcondition_rejects_every_non_exact_lock_transition(
    changes: dict[str, object],
) -> None:
    before = _account(
        password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)),
        failed_attempts=4,
    )
    expected = before.model_copy(
        update={
            "failed_attempts": 0,
            "lockout_level": 1,
            "locked_until": NOW + timedelta(minutes=15),
            "updated_at": NOW,
            "revision": 2,
        }
    )
    malformed = expected.model_copy(update=changes)

    assert not _valid_recorded_failure(before, malformed, NOW)


@pytest.mark.parametrize(
    "mutation",
    (
        "revision_jump",
        "lock_duration",
        "lockout_level",
        "updated_at",
        "missing_field",
    ),
)
def test_public_authenticate_rejects_malicious_failure_transition(
    mutation: str,
) -> None:
    class MaliciousFailureStore(AccountStore):
        def record_authentication_failure(
            self,
            command: RecordLocalAccountFailureCommand,
            *,
            operator: TrustedIdentityOperator,
        ) -> object:
            recorded = super().record_authentication_failure(
                command,
                operator=operator,
            )
            if mutation == "missing_field":
                returned = recorded.model_dump()
                del returned["revision"]
                return returned
            field, value = {
                "revision_jump": ("revision", recorded.revision + 1),
                "lock_duration": (
                    "locked_until",
                    NOW + timedelta(minutes=16),
                ),
                "lockout_level": ("lockout_level", 2),
                "updated_at": ("updated_at", NOW + timedelta(seconds=1)),
            }[mutation]
            return recorded.model_copy(update={field: value})

    store = MaliciousFailureStore(
        _account(
            password_hash=EncodedPasswordHash(_hasher().hash(VALID_PASSWORD)),
            failed_attempts=4,
        )
    )

    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store).authenticate("tenant-a", "alice", "wrong")
    assert store.failure_calls == 3


@pytest.mark.parametrize(
    "mutation",
    ("revision_jump", "old_password", "uncleared_failure", "epoch_change"),
)
def test_successful_login_rejects_non_exact_repository_save(
    mutation: str,
) -> None:
    class MaliciousSaveStore(AccountStore):
        def save_local_account(
            self,
            command: SaveLocalAccountCommand,
            *,
            operator: TrustedIdentityOperator,
        ) -> LocalAccount:
            del operator
            assert self.account is not None
            assert command.expected_revision is not None
            result = command.local_account.model_copy(
                update={"revision": command.expected_revision + 1}
            )
            if mutation == "revision_jump":
                result = result.model_copy(update={"revision": result.revision + 1})
            elif mutation == "old_password":
                result = result.model_copy(
                    update={"password_hash": self.account.password_hash}
                )
            elif mutation == "uncleared_failure":
                result = result.model_copy(update={"failed_attempts": 1})
            else:
                result = result.model_copy(
                    update={"session_epoch": result.session_epoch + 1}
                )
            self.account = result
            return result

    store = MaliciousSaveStore(
        _account(
            password_hash=_legacy_hash(VALID_PASSWORD),
            failed_attempts=3,
            lockout_level=2,
        )
    )

    with pytest.raises(CredentialInvalid, match="^credentials are invalid$"):
        _service(store).authenticate("tenant-a", "alice", VALID_PASSWORD)


def test_new_passwords_below_the_minimum_length_are_rejected_at_the_hash_gate() -> None:
    from eios.identity.settings import MINIMUM_NEW_PASSWORD_LENGTH

    assert MINIMUM_NEW_PASSWORD_LENGTH == 12
    hasher = _hasher()
    account = _account(password_hash=EncodedPasswordHash(hasher.hash(VALID_PASSWORD)))
    service = _service(AccountStore(None), hasher)

    for short in ("x", "x" * (MINIMUM_NEW_PASSWORD_LENGTH - 1)):
        with pytest.raises(
            PasswordPolicyViolation, match="^password does not satisfy policy$"
        ):
            service.hash_password_for_account(account, short)

    encoded = service.hash_password_for_account(
        account, "y" * MINIMUM_NEW_PASSWORD_LENGTH
    )
    assert encoded.startswith("$argon2id$")


def test_legacy_short_password_still_authenticates_but_must_change_to_policy() -> None:
    hasher = _hasher()
    legacy_short = "legacy-pw"
    store = AccountStore(
        _account(password_hash=EncodedPasswordHash(hasher.hash(legacy_short)))
    )
    service = _service(store, hasher)

    authenticated = service.authenticate("tenant-a", "alice", legacy_short)
    assert authenticated.local_account_id == "account-1"

    assert store.account is not None
    service.verify_current_password(store.account, legacy_short)

    with pytest.raises(
        PasswordPolicyViolation, match="^password does not satisfy policy$"
    ):
        service.hash_password_for_account(store.account, "elevenchars")

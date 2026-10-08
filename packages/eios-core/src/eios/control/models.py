from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
import re
import secrets
import threading
from typing import Any, Iterable, NoReturn
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .errors import ApiKeySecretConsumed, InvalidApiKey


_API_KEY_PATTERN = re.compile(r"^eios_[A-Za-z0-9_-]{43}$", re.ASCII)
_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$", re.ASCII)


class PolicyReasonCode(str, Enum):
    ALLOWED = "allowed"
    AUTHENTICATION_REQUIRED = "authentication_required"
    CREDENTIAL_INACTIVE = "credential_inactive"
    TENANT_INACTIVE = "tenant_inactive"
    PRINCIPAL_INACTIVE = "principal_inactive"
    ROUTE_SCOPE_MISSING = "route_scope_missing"
    CAPABILITY_SCOPE_MISSING = "capability_scope_missing"
    TENANT_MISMATCH = "tenant_mismatch"


_TENANT_BOUND_AUDIT_REASONS = frozenset(
    {
        PolicyReasonCode.ROUTE_SCOPE_MISSING,
        PolicyReasonCode.CAPABILITY_SCOPE_MISSING,
        PolicyReasonCode.TENANT_MISMATCH,
    }
)


def requires_tenant_bound_audit(
    reason_code: PolicyReasonCode, *, has_trusted_tenant: bool
) -> bool:
    return bool(has_trusted_tenant and reason_code in _TENANT_BOUND_AUDIT_REASONS)


class ApiKeyStatus(str, Enum):
    ACTIVE = "active"
    RETIRING = "retiring"
    REVOKED = "revoked"


class SubjectStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


def _identifier(value: str) -> str:
    if not value or value != value.strip() or not value.isprintable():
        raise ValueError("identifier must be non-empty, trimmed, and control-free")
    return value


def _display_name(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("display name must be 1-128 trimmed control-free characters")
    return value


def _revoke_reason(value: str) -> str:
    if (
        not value
        or len(value) > 256
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("revoke reason must be 1-256 trimmed control-free characters")
    return value


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be timezone-aware UTC")
    return value


def _command_id(value: str) -> str:
    value = _identifier(value)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("command_id must be a UUID") from None
    if str(parsed) != value:
        raise ValueError("command_id must be a canonical UUID")
    return value


def _sha256_hex(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("command_digest must be 64 lowercase hexadecimal characters")
    return value


def _token_digest(value: bytes) -> bytes:
    if len(value) != 32:
        raise ValueError("token_digest must contain 32 bytes")
    return value


def normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    if isinstance(scopes, (str, bytes)):
        raise ValueError("scopes must be a collection")
    try:
        iterator = iter(scopes)
    except TypeError:
        raise ValueError("scopes must be a collection") from None
    normalized: set[str] = set()
    for scope in iterator:
        if (
            not isinstance(scope, str)
            or not _SCOPE_PATTERN.fullmatch(scope)
            or scope == "*"
        ):
            raise ValueError("scope is invalid")
        normalized.add(scope)
    return tuple(sorted(normalized))


def validate_api_key(token: str) -> str:
    if not isinstance(token, str) or not _API_KEY_PATTERN.fullmatch(token):
        raise InvalidApiKey("API key format is invalid")
    return token


def api_key_digest(token: str) -> bytes:
    return hashlib.sha256(validate_api_key(token).encode("ascii")).digest()


class ApiKeySecret:
    __slots__ = ("_lock", "_secret", "_revealed")

    def __init__(self, token: str) -> None:
        self._lock = threading.Lock()
        self._secret: str | None = validate_api_key(token)
        self._revealed = False

    def reveal(self) -> str:
        with self._lock:
            if self._revealed or self._secret is None:
                raise ApiKeySecretConsumed("API key secret is no longer available")
            token = self._secret
            self._secret = None
            self._revealed = True
            return token

    def destroy(self) -> None:
        with self._lock:
            self._secret = None
            self._revealed = True

    def __copy__(self) -> NoReturn:
        raise TypeError("API key secret cannot be copied or serialized")

    def __deepcopy__(self, memo: dict[int, Any]) -> NoReturn:
        del memo
        raise TypeError("API key secret cannot be copied or serialized")

    def __reduce__(self) -> NoReturn:
        raise TypeError("API key secret cannot be copied or serialized")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del protocol
        raise TypeError("API key secret cannot be copied or serialized")

    def model_dump(self) -> dict[str, str]:
        return {"secret": "<redacted>"}

    def __repr__(self) -> str:
        return "ApiKeySecret(<redacted>)"

    __str__ = __repr__


def generate_api_key() -> ApiKeySecret:
    token = f"eios_{secrets.token_urlsafe(32)}"
    return ApiKeySecret(token)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ApiKeyIdentity(_FrozenModel):
    tenant_id: str
    principal_id: str
    api_key_id: str
    scopes: tuple[str, ...]
    # Stage 9: clearances returned by control.authenticate_api_key.
    markings: tuple[str, ...] = ()

    _validate_identifiers = field_validator("tenant_id", "principal_id", "api_key_id")(
        _identifier
    )

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if normalized != value:
            raise ValueError("scopes must be normalized")
        return value


class AuthenticationResult(_FrozenModel):
    identity: ApiKeyIdentity | None
    reason_code: PolicyReasonCode

    @model_validator(mode="after")
    def _validate_identity_boundary(self) -> AuthenticationResult:
        allowed = self.reason_code is PolicyReasonCode.ALLOWED
        if allowed != (self.identity is not None):
            raise ValueError("authentication identity and reason are inconsistent")
        return self


class PolicyDecision(_FrozenModel):
    allowed: bool
    reason_code: PolicyReasonCode
    required_scopes: tuple[str, ...] = ()
    missing_scopes: tuple[str, ...] = ()

    @field_validator("required_scopes", "missing_scopes", mode="before")
    @classmethod
    def _normalize_policy_scopes(cls, value) -> tuple[str, ...]:
        return normalize_scopes(value)

    @model_validator(mode="after")
    def _validate_decision(self) -> PolicyDecision:
        if self.allowed != (self.reason_code is PolicyReasonCode.ALLOWED):
            raise ValueError("allowed flag and reason_code are inconsistent")
        if self.allowed and self.missing_scopes:
            raise ValueError("allowed decision cannot have missing scopes")
        if not set(self.missing_scopes).issubset(self.required_scopes):
            raise ValueError("missing scopes must be required scopes")
        return self


class BrowserSessionProof(_FrozenModel):
    """Server-only identity proof selecting one exact browser device session."""

    session_id: str
    session_revision: int = Field(ge=1)

    _validate_session_id = field_validator("session_id")(_identifier)


class TenantContext(_FrozenModel):
    """Internal process trust boundary minted from authenticated identity data.

    External adapters must obtain this context from ``AccessService.authenticate``.
    Direct construction is reserved for trusted in-process callers and tests.
    """

    tenant_id: str
    principal_id: str
    api_key_id: str
    scopes: frozenset[str]
    request_id: str
    trace_id: str
    correlation_id: str
    causation_id: str = ""
    browser_session: BrowserSessionProof | None = None
    # Stage 9: mandatory-access clearances. Orthogonal to scopes — markings
    # only ever restrict; an empty set is the least-cleared session.
    markings: frozenset[str] = frozenset()

    _validate_identifiers = field_validator(
        "tenant_id",
        "principal_id",
        "api_key_id",
        "request_id",
        "trace_id",
        "correlation_id",
    )(_identifier)

    @field_validator("causation_id")
    @classmethod
    def _validate_causation_id(cls, value: str) -> str:
        return value if value == "" else _identifier(value)

    @field_validator("scopes")
    @classmethod
    def _validate_scope_set(cls, value: frozenset[str]) -> frozenset[str]:
        normalize_scopes(value)
        return value


class TrustedOperatorContext(_FrozenModel):
    actor_id: str

    _validate_actor = field_validator("actor_id")(_identifier)


def _canonical_command_digest(action: str, intent: dict[str, Any]) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, datetime):
            checked = _aware_utc(value)
            assert checked is not None
            return checked.isoformat()
        if isinstance(value, tuple):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    payload = {
        "contract": "eios-access-command/v1",
        "action": action,
        "intent": normalize(intent),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def bootstrap_command_digest(
    *,
    tenant_id: str,
    tenant_display_name: str,
    principal_id: str,
    principal_display_name: str,
    scopes: Iterable[str],
    expires_at: datetime | None,
) -> str:
    return _canonical_command_digest(
        "bootstrap",
        {
            "tenant_id": _identifier(tenant_id),
            "tenant_display_name": _display_name(tenant_display_name),
            "principal_id": _identifier(principal_id),
            "principal_display_name": _display_name(principal_display_name),
            "scopes": normalize_scopes(scopes),
            "expires_at": _aware_utc(expires_at),
        },
    )


def create_principal_command_digest(
    *,
    tenant_id: str,
    principal_id: str,
    principal_display_name: str,
    scopes: Iterable[str],
    expires_at: datetime | None,
) -> str:
    return _canonical_command_digest(
        "create_principal",
        {
            "tenant_id": _identifier(tenant_id),
            "principal_id": _identifier(principal_id),
            "principal_display_name": _display_name(principal_display_name),
            "scopes": normalize_scopes(scopes),
            "expires_at": _aware_utc(expires_at),
        },
    )


def issue_command_digest(
    *,
    tenant_id: str,
    principal_id: str,
    scopes: Iterable[str],
    expires_at: datetime | None,
) -> str:
    return _canonical_command_digest(
        "issue",
        {
            "tenant_id": _identifier(tenant_id),
            "principal_id": _identifier(principal_id),
            "scopes": normalize_scopes(scopes),
            "expires_at": _aware_utc(expires_at),
        },
    )


def rotate_command_digest(
    *,
    tenant_id: str,
    predecessor_api_key_id: str,
    overlap_seconds: int,
    expires_at: datetime | None,
) -> str:
    if type(overlap_seconds) is not int or not 0 <= overlap_seconds <= 86_400:
        raise ValueError("overlap_seconds must be between 0 and 86400")
    return _canonical_command_digest(
        "rotate",
        {
            "tenant_id": _identifier(tenant_id),
            "predecessor_api_key_id": _identifier(predecessor_api_key_id),
            "overlap_seconds": overlap_seconds,
            "expires_at": _aware_utc(expires_at),
        },
    )


def revoke_command_digest(*, tenant_id: str, api_key_id: str, reason: str) -> str:
    return _canonical_command_digest(
        "revoke",
        {
            "tenant_id": _identifier(tenant_id),
            "api_key_id": _identifier(api_key_id),
            "reason": _revoke_reason(reason),
        },
    )


def update_scopes_command_digest(
    *, tenant_id: str, api_key_id: str, scopes: Iterable[str]
) -> str:
    return _canonical_command_digest(
        "update_scopes",
        {
            "tenant_id": _identifier(tenant_id),
            "api_key_id": _identifier(api_key_id),
            "scopes": normalize_scopes(scopes),
        },
    )


class _LifecycleCommand(_FrozenModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )

    command_id: str
    command_digest: str

    _validate_command_id = field_validator("command_id")(_command_id)
    _validate_command_digest = field_validator("command_digest")(_sha256_hex)


class BootstrapApiKeyCommand(_LifecycleCommand):
    tenant_id: str
    tenant_display_name: str
    principal_id: str
    principal_display_name: str
    api_key_id: str
    token_digest: bytes = Field(repr=False, exclude=True)
    scopes: tuple[str, ...]
    expires_at: datetime | None

    _validate_identifiers = field_validator("tenant_id", "principal_id", "api_key_id")(
        _identifier
    )
    _validate_display_names = field_validator(
        "tenant_display_name", "principal_display_name"
    )(_display_name)
    _validate_token_digest = field_validator("token_digest")(_token_digest)
    _validate_expires_at = field_validator("expires_at")(_aware_utc)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if not normalized or normalized != value:
            raise ValueError("scopes must be non-empty and normalized")
        return value

    @model_validator(mode="after")
    def _validate_digest(self) -> BootstrapApiKeyCommand:
        expected = bootstrap_command_digest(
            tenant_id=self.tenant_id,
            tenant_display_name=self.tenant_display_name,
            principal_id=self.principal_id,
            principal_display_name=self.principal_display_name,
            scopes=self.scopes,
            expires_at=self.expires_at,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match bootstrap intent")
        return self


class CreatePrincipalCommand(_LifecycleCommand):
    tenant_id: str
    principal_id: str
    principal_display_name: str
    api_key_id: str
    token_digest: bytes = Field(repr=False, exclude=True)
    scopes: tuple[str, ...]
    expires_at: datetime | None

    _validate_identifiers = field_validator("tenant_id", "principal_id", "api_key_id")(
        _identifier
    )
    _validate_display_name = field_validator("principal_display_name")(_display_name)
    _validate_token_digest = field_validator("token_digest")(_token_digest)
    _validate_expires_at = field_validator("expires_at")(_aware_utc)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if not normalized or normalized != value:
            raise ValueError("scopes must be non-empty and normalized")
        return value

    @model_validator(mode="after")
    def _validate_digest(self) -> CreatePrincipalCommand:
        expected = create_principal_command_digest(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            principal_display_name=self.principal_display_name,
            scopes=self.scopes,
            expires_at=self.expires_at,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match create_principal intent")
        return self


class IssueApiKeyCommand(_LifecycleCommand):
    tenant_id: str
    principal_id: str
    api_key_id: str
    token_digest: bytes = Field(repr=False, exclude=True)
    scopes: tuple[str, ...]
    expires_at: datetime | None

    _validate_identifiers = field_validator("tenant_id", "principal_id", "api_key_id")(
        _identifier
    )
    _validate_token_digest = field_validator("token_digest")(_token_digest)
    _validate_expires_at = field_validator("expires_at")(_aware_utc)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if not normalized or normalized != value:
            raise ValueError("scopes must be non-empty and normalized")
        return value

    @model_validator(mode="after")
    def _validate_digest(self) -> IssueApiKeyCommand:
        expected = issue_command_digest(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            scopes=self.scopes,
            expires_at=self.expires_at,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match issue intent")
        return self


class RotateApiKeyCommand(_LifecycleCommand):
    tenant_id: str
    predecessor_api_key_id: str
    successor_api_key_id: str
    successor_token_digest: bytes = Field(repr=False, exclude=True)
    overlap_seconds: int = 300
    expires_at: datetime | None

    _validate_identifiers = field_validator(
        "tenant_id", "predecessor_api_key_id", "successor_api_key_id"
    )(_identifier)
    _validate_token_digest = field_validator("successor_token_digest")(_token_digest)
    _validate_expires_at = field_validator("expires_at")(_aware_utc)

    @field_validator("overlap_seconds")
    @classmethod
    def _validate_overlap(cls, value: int) -> int:
        if not 0 <= value <= 86_400:
            raise ValueError("overlap_seconds must be between 0 and 86400")
        return value

    @model_validator(mode="after")
    def _validate_digest(self) -> RotateApiKeyCommand:
        expected = rotate_command_digest(
            tenant_id=self.tenant_id,
            predecessor_api_key_id=self.predecessor_api_key_id,
            overlap_seconds=self.overlap_seconds,
            expires_at=self.expires_at,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match rotate intent")
        return self


class RevokeApiKeyCommand(_LifecycleCommand):
    tenant_id: str
    api_key_id: str
    reason: str

    _validate_identifiers = field_validator("tenant_id", "api_key_id")(_identifier)
    _validate_reason = field_validator("reason")(_revoke_reason)

    @model_validator(mode="after")
    def _validate_digest(self) -> RevokeApiKeyCommand:
        expected = revoke_command_digest(
            tenant_id=self.tenant_id,
            api_key_id=self.api_key_id,
            reason=self.reason,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match revoke intent")
        return self


class UpdateApiKeyScopesCommand(_LifecycleCommand):
    tenant_id: str
    api_key_id: str
    scopes: tuple[str, ...]

    _validate_identifiers = field_validator("tenant_id", "api_key_id")(_identifier)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if not normalized or normalized != value:
            raise ValueError("scopes must be non-empty and normalized")
        return value

    @model_validator(mode="after")
    def _validate_digest(self) -> UpdateApiKeyScopesCommand:
        expected = update_scopes_command_digest(
            tenant_id=self.tenant_id,
            api_key_id=self.api_key_id,
            scopes=self.scopes,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match scope update intent")
        return self


class ApiKeyRecord(_FrozenModel):
    actor_id: str
    tenant_id: str
    principal_id: str
    api_key_id: str
    status: ApiKeyStatus
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None
    retire_at: datetime | None
    revoked_at: datetime | None
    command_id: str

    _validate_identifiers = field_validator(
        "actor_id", "tenant_id", "principal_id", "api_key_id"
    )(_identifier)
    _validate_command_id = field_validator("command_id")(_command_id)
    _validate_datetimes = field_validator(
        "created_at", "expires_at", "retire_at", "revoked_at"
    )(_aware_utc)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = normalize_scopes(value)
        if not normalized or normalized != value:
            raise ValueError("scopes must be non-empty and normalized")
        return value


class ApiKeyMutationResult(_FrozenModel):
    record: ApiKeyRecord
    applied: bool


class ApiKeyLifecycleResult(_FrozenModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
    )

    record: ApiKeyRecord
    secret: ApiKeySecret | None

    @field_serializer("secret")
    def _serialize_secret(self, value: ApiKeySecret | None) -> str | None:
        return None if value is None else "<redacted>"

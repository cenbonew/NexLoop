from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from hmac import compare_digest
import math
import re
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypeAlias

from argon2 import extract_parameters
from argon2.exceptions import InvalidHashError
from argon2.low_level import Type
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    PlainSerializer,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema


FrozenJsonValue: TypeAlias = Any
_ARGON2ID_HASH = re.compile(
    r"^\$argon2id\$v=19\$m=([1-9][0-9]*),t=([1-9][0-9]*),p=([1-9][0-9]*)"
    r"\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)$"
)
_LEGACY_PBKDF2_HASH = re.compile(
    r"^pbkdf2_sha256\$([1-9][0-9]*)\$([0-9a-f]{32})\$([0-9a-f]{64})$"
)
_MAX_PASSWORD_HASH_LENGTH = 512
_ARGON2_MEMORY_COST_RANGE = range(19_456, 262_145)
_ARGON2_TIME_COST_RANGE = range(2, 11)
_ARGON2_PARALLELISM_RANGE = range(1, 9)
_RESERVED_IDENTITY_CLAIMS = frozenset(
    {
        "acr",
        "amr",
        "application_id",
        "aud",
        "auth_time",
        "azp",
        "exp",
        "iat",
        "iss",
        "jti",
        "nbf",
        "nonce",
        "principal_id",
        "provider_id",
        "session_id",
        "sub",
        "subject_id",
        "tenant_id",
    }
)
_ARGON2_SALT_LENGTH_RANGE = range(16, 65)
_ARGON2_DIGEST_LENGTH_RANGE = range(16, 65)
_PBKDF2_ITERATIONS_RANGE = range(100_000, 2_000_001)


class FrozenJsonMap(Mapping[str, FrozenJsonValue]):
    """A recursively immutable JSON object for trusted identity facts."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        frozen = _freeze_json({} if value is None else value)
        if not isinstance(frozen, FrozenJsonMap):
            raise TypeError("JSON object must be a mapping")
        object.__setattr__(self, "_data", frozen._data)

    @classmethod
    def _from_frozen(cls, value: dict[str, FrozenJsonValue]) -> FrozenJsonMap:
        instance = object.__new__(cls)
        copied: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            copied[key] = _freeze_json(item)
        object.__setattr__(instance, "_data", MappingProxyType(copied))
        return instance

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("FrozenJsonMap is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("FrozenJsonMap is immutable")

    def __getitem__(self, key: str) -> FrozenJsonValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return NotImplemented
        try:
            frozen_other = FrozenJsonMap(other)
        except (TypeError, ValueError):
            return False
        return self._data == frozen_other._data

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __repr__(self) -> str:
        return f"FrozenJsonMap({dict(self._data)!r})"


def _freeze_json(value: Any) -> FrozenJsonValue:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, FrozenJsonMap):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return FrozenJsonMap._from_frozen(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _freeze_json_object(value: Any) -> FrozenJsonMap:
    if not isinstance(value, Mapping):
        raise ValueError("JSON object must be a mapping")
    frozen = _freeze_json(value)
    if not isinstance(frozen, FrozenJsonMap):
        raise ValueError("JSON object must be a mapping")
    return frozen


def _thaw_json(value: FrozenJsonValue) -> Any:
    if isinstance(value, FrozenJsonMap):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


FrozenJsonObject = Annotated[
    FrozenJsonMap,
    BeforeValidator(_freeze_json_object),
    PlainSerializer(_thaw_json, return_type=dict[str, Any], when_used="always"),
]


def _non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must be non-empty")
    return clean


def _optional_non_blank(value: str | None, field_name: str) -> str | None:
    return None if value is None else _non_blank(value, field_name)


def _aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


class _Digest32:
    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        if type(value) is not bytes:
            raise TypeError("digest value must be bytes")
        if len(value) != 32:
            raise ValueError("digest value must contain exactly 32 bytes")
        object.__setattr__(self, "_value", bytes(value))

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash((type(self), self._value))

    def __repr__(self) -> str:
        return f"{type(self).__name__}('<redacted>')"

    def __str__(self) -> str:
        return "<redacted>"

    def get_secret_value(self) -> bytes:
        """Return bytes only at an explicit trusted adapter boundary."""

        return self._value


class SecretDigest32(_Digest32):
    __slots__ = ()


class KeyedDigest32(_Digest32):
    __slots__ = ()


def _unique_non_blank(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    cleaned = tuple(_non_blank(value, field_name) for value in values)
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return cleaned


def _claim_name(value: object, *, maximum: int, canonical: bool) -> bool:
    if type(value) is not str or not 0 < len(value) <= maximum or not value.isascii():
        return False
    if canonical:
        return value[0].islower() and all(
            character.islower() or character.isdigit() or character == "_"
            for character in value
        )
    return (value[0].isalpha() or value[0] == "_") and all(
        character.isalnum() or character in "_.-" for character in value
    )


def _canonical_unpadded_base64(value: str) -> bytes | None:
    if len(value) % 4 == 1:
        return None
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except ValueError:
        return None
    canonical = base64.b64encode(decoded).decode("ascii").rstrip("=")
    return decoded if canonical == value else None


def _is_canonical_argon2id_hash(value: str) -> bool:
    matched = _ARGON2ID_HASH.fullmatch(value)
    if matched is None:
        return False
    memory_cost, time_cost, parallelism = map(int, matched.group(1, 2, 3))
    if (
        memory_cost not in _ARGON2_MEMORY_COST_RANGE
        or time_cost not in _ARGON2_TIME_COST_RANGE
        or parallelism not in _ARGON2_PARALLELISM_RANGE
    ):
        return False
    salt = _canonical_unpadded_base64(matched.group(4))
    digest = _canonical_unpadded_base64(matched.group(5))
    if (
        salt is None
        or digest is None
        or len(salt) not in _ARGON2_SALT_LENGTH_RANGE
        or len(digest) not in _ARGON2_DIGEST_LENGTH_RANGE
    ):
        return False
    try:
        parameters = extract_parameters(value)
    except (InvalidHashError, ValueError):
        return False
    return (
        parameters.type is Type.ID
        and parameters.version == 19
        and parameters.memory_cost == memory_cost
        and parameters.time_cost == time_cost
        and parameters.parallelism == parallelism
        and parameters.salt_len == len(salt)
        and parameters.hash_len == len(digest)
    )


def _stored_password_hash(value: str, field_name: str) -> str:
    if len(value) > _MAX_PASSWORD_HASH_LENGTH:
        raise ValueError(f"{field_name} must be a supported encoded password hash")
    legacy = _LEGACY_PBKDF2_HASH.fullmatch(value)
    if legacy is not None:
        iterations = int(legacy.group(1))
        if iterations in _PBKDF2_ITERATIONS_RANGE:
            return value
    if _is_canonical_argon2id_hash(value):
        return value
    raise ValueError(f"{field_name} must be a supported encoded password hash")


class EncodedPasswordHash(SecretStr):
    """Validated password encoding with explicit access and masked surfaces."""

    __slots__ = ()

    def __init__(self, value: str) -> None:
        validated = _stored_password_hash(value, "encoded password hash")
        object.__setattr__(self, "_secret_value", validated)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("EncodedPasswordHash is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("EncodedPasswordHash is immutable")

    def startswith(self, prefix: str | tuple[str, ...], *args: int) -> bool:
        return self.get_secret_value().startswith(prefix, *args)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        del source_type
        secret_schema = handler.generate_schema(SecretStr)

        def validated_secret(value: SecretStr) -> EncodedPasswordHash:
            return cls(value.get_secret_value())

        return core_schema.chain_schema(
            [
                secret_schema,
                core_schema.no_info_plain_validator_function(validated_secret),
            ]
        )


def _mask_password_input(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value
    if isinstance(value, str):
        return SecretStr(value)
    if isinstance(value, tuple):
        return tuple(_mask_password_input(item) for item in value)
    if isinstance(value, list):
        return [_mask_password_input(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _mask_password_input(item) for key, item in value.items()}
    return value


EncodedPasswordValue = Annotated[
    EncodedPasswordHash | None,
    BeforeValidator(_mask_password_input),
]
PasswordHashHistory = Annotated[
    tuple[EncodedPasswordHash, ...],
    BeforeValidator(_mask_password_input),
    Field(max_length=10),
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class SubjectKind(str, Enum):
    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"
    SYSTEM = "system"


class MembershipKind(str, Enum):
    HOME = "home"
    GUEST = "guest"


class SessionRotationKind(str, Enum):
    TENANT_SWITCH = "tenant_switch"
    REAUTHENTICATION = "reauthentication"
    CREDENTIAL_CHANGE = "credential_change"
    RISK = "risk"


class Subject(_FrozenModel):
    subject_id: str
    kind: SubjectKind
    status: Literal["active", "disabled"]
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)

    @field_validator("subject_id")
    @classmethod
    def _validate_subject_id(cls, value: str) -> str:
        return _non_blank(value, "subject_id")

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        checked = _aware_utc(value, info.field_name)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def _validate_time_order(self) -> Subject:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot predate created_at")
        return self


class TenantMembership(_FrozenModel):
    tenant_id: str
    subject_id: str
    principal_id: str
    kind: MembershipKind
    status: Literal["invited", "active", "suspended", "revoked"]
    valid_from: datetime
    valid_until: datetime | None
    trusted_attributes: FrozenJsonObject = Field(
        default_factory=lambda: FrozenJsonMap({})
    )
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "subject_id", "principal_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_validity(self) -> TenantMembership:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class IdentityProvider(_FrozenModel):
    provider_id: str
    tenant_id: str
    issuer: str
    client_id: str
    audience: str
    client_secret_ref: str | None
    allowed_signing_algorithms: tuple[str, ...]
    trusted_jwks_uris: tuple[str, ...]
    claim_mappings: FrozenJsonObject = Field(default_factory=lambda: FrozenJsonMap({}))
    status: Literal["active", "disabled"]
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)

    @field_validator("provider_id", "tenant_id", "issuer", "client_id", "audience")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("client_secret_ref")
    @classmethod
    def _validate_secret_ref(cls, value: str | None) -> str | None:
        return _optional_non_blank(value, "client_secret_ref")

    @field_validator("allowed_signing_algorithms")
    @classmethod
    def _validate_algorithms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        value = _unique_non_blank(value, "allowed_signing_algorithms")
        if not value or len(value) > 8:
            raise ValueError(
                "allowed_signing_algorithms must contain 1 to 8 unique algorithms"
            )
        return value

    @field_validator("trusted_jwks_uris")
    @classmethod
    def _validate_trusted_jwks_uris(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        value = _unique_non_blank(value, "trusted_jwks_uris")
        if not value or len(value) > 8:
            raise ValueError("trusted_jwks_uris must contain 1 to 8 unique URIs")
        return value

    @field_validator("claim_mappings")
    @classmethod
    def _validate_claim_mappings(cls, value: FrozenJsonObject) -> FrozenJsonObject:
        items = tuple(value.items())
        if len(items) > 32:
            raise ValueError("claim_mappings cannot contain more than 32 entries")
        sources: set[str] = set()
        for canonical_name, source_name in items:
            if (
                not _claim_name(canonical_name, maximum=64, canonical=True)
                or not _claim_name(source_name, maximum=128, canonical=False)
                or canonical_name in _RESERVED_IDENTITY_CLAIMS
                or source_name in _RESERVED_IDENTITY_CLAIMS
                or source_name in sources
            ):
                raise ValueError("claim_mappings must contain unique ASCII identifiers")
            sources.add(source_name)
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime, info: Any) -> datetime:
        checked = _aware_utc(value, info.field_name)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def _validate_time_order(self) -> IdentityProvider:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot predate created_at")
        return self


class ExternalIdentity(_FrozenModel):
    external_identity_id: str
    tenant_id: str
    provider_id: str
    issuer: str
    subject: str
    subject_id: str
    status: Literal["active", "disabled"]
    session_epoch: int = Field(ge=1)
    email: str | None
    created_at: datetime
    updated_at: datetime
    last_authenticated_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator(
        "external_identity_id",
        "tenant_id",
        "provider_id",
        "issuer",
        "subject",
        "subject_id",
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str | None) -> str | None:
        return _optional_non_blank(value, "email")

    @field_validator("created_at", "updated_at", "last_authenticated_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_time_order(self) -> ExternalIdentity:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot predate created_at")
        if (
            self.last_authenticated_at is not None
            and self.last_authenticated_at < self.created_at
        ):
            raise ValueError("last_authenticated_at cannot predate created_at")
        return self

    @classmethod
    def link_key_fields(cls) -> tuple[str, str, str]:
        return ("provider_id", "issuer", "subject")

    @property
    def link_key(self) -> tuple[str, str, str]:
        return (self.provider_id, self.issuer, self.subject)


class LocalAccount(_FrozenModel):
    local_account_id: str
    tenant_id: str
    subject_id: str
    username: str
    verified_email: str
    password_hash: EncodedPasswordValue = Field(repr=False)
    password_history: PasswordHashHistory = Field(default=(), repr=False)
    status: Literal["invited", "active", "disabled"]
    failed_attempts: int = Field(ge=0, le=4)
    lockout_level: int = Field(ge=0, le=3)
    locked_until: datetime | None
    must_change_password: bool
    session_epoch: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)

    @field_validator(
        "local_account_id", "tenant_id", "subject_id", "username", "verified_email"
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("locked_until", "created_at", "updated_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_account(self) -> LocalAccount:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot predate created_at")
        if self.status == "active" and self.password_hash is None:
            raise ValueError("active account requires password_hash")
        if self.locked_until is not None and self.locked_until <= self.updated_at:
            raise ValueError("locked_until must be later than updated_at")
        return self


#: 浏览器会话的两道水位。**抽成常量,因为这两个数此前写死在五处**
#: (models 不变量 ×2、sessions 默认值 ×2、sessions 校验期望 ×2),
#: 2026-09-07 改 7 天时才发现派单只列到其中三处 —— 少改一处就会让写入端与
#: 校验端各按各的水位判,touch 全线 `_invalid`。
#: 🔴 库侧还有两处独立的水位(0053 的 CHECK 谓词、0062 的 definer 函数体),
#:    它们**不在 Python 里**,改这两个常量拦不住库;见同车的迁移。
SESSION_IDLE_LIMIT = timedelta(days=7)
#: 绝对上限是**唯一能保证凭据最终失效**的闸(运营 2026-09-07 拍板 30 天):
#: 去掉它,只要有人一直用就永不重登。
SESSION_ABSOLUTE_LIMIT = timedelta(days=30)


class BrowserSession(_FrozenModel):
    """Human browser session with a globally unique opaque session_id."""

    session_id: str
    tenant_id: str
    subject_id: str
    principal_id: str
    application_id: str
    application_revision: int = Field(ge=1)
    subject_kind: SubjectKind
    subject_revision: int = Field(ge=1)
    credential_kind: Literal["local_account", "external_identity"]
    credential_tenant_id: str
    credential_id: str
    credential_revision: int = Field(ge=1)
    credential_session_epoch: int = Field(ge=1)
    provider_id: str | None = None
    provider_revision: int | None = Field(default=None, ge=1)
    provider_configuration_fingerprint: str | None = None
    session_token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    csrf_token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    authentication_methods: tuple[str, ...]
    restricted: bool
    membership_revision: int = Field(ge=1)
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator(
        "session_id",
        "tenant_id",
        "subject_id",
        "principal_id",
        "application_id",
        "credential_tenant_id",
        "credential_id",
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("provider_id")
    @classmethod
    def _validate_provider_id(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank(value, "provider_id")

    @field_validator("authentication_methods")
    @classmethod
    def _validate_authentication_methods(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        value = _unique_non_blank(value, "authentication_methods")
        if not value:
            raise ValueError("authentication_methods must be non-empty")
        return value

    @field_validator("provider_configuration_fingerprint")
    @classmethod
    def _validate_provider_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(
                "provider_configuration_fingerprint must be a lowercase SHA-256 digest"
            )
        return value

    @field_validator(
        "created_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
        "revoked_at",
    )
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_lifetimes(self) -> BrowserSession:
        if self.subject_kind is not SubjectKind.HUMAN:
            raise ValueError("browser session subject must be human")
        if self.credential_kind == "external_identity":
            if (
                self.provider_id is None
                or self.provider_revision is None
                or self.provider_configuration_fingerprint is None
                or not self.provider_configuration_fingerprint.strip()
            ):
                raise ValueError("external credential requires provider binding")
        elif (
            self.provider_id is not None
            or self.provider_revision is not None
            or self.provider_configuration_fingerprint is not None
        ):
            raise ValueError("local credential cannot carry provider binding")
        if self.last_seen_at < self.created_at:
            raise ValueError("last_seen_at cannot predate created_at")
        if not self.last_seen_at < self.idle_expires_at:
            raise ValueError("idle_expires_at must be later than last_seen_at")
        if self.idle_expires_at - self.last_seen_at > SESSION_IDLE_LIMIT:
            raise ValueError("idle session lifetime exceeds the configured limit")
        if self.absolute_expires_at - self.created_at > SESSION_ABSOLUTE_LIMIT:
            raise ValueError("absolute session lifetime exceeds the configured limit")
        if self.absolute_expires_at <= self.created_at:
            raise ValueError("absolute_expires_at must be later than created_at")
        if self.idle_expires_at > self.absolute_expires_at:
            raise ValueError("idle_expires_at cannot exceed absolute_expires_at")
        if self.revoked_at is not None and self.revoked_at < self.last_seen_at:
            raise ValueError("revoked_at cannot predate last_seen_at")
        return self


class Invitation(_FrozenModel):
    """Local-account invitation with a globally unique opaque invitation_id."""

    invitation_id: str
    tenant_id: str
    email: str
    membership_kind: MembershipKind
    subject_id: str
    principal_id: str
    membership_revision: int = Field(ge=1)
    token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    status: Literal["pending", "consumed", "revoked"]
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator(
        "invitation_id", "tenant_id", "email", "subject_id", "principal_id"
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("created_at", "expires_at", "consumed_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Invitation:
        if not self.created_at < self.expires_at:
            raise ValueError("expires_at must be later than created_at")
        if self.expires_at - self.created_at > timedelta(hours=24):
            raise ValueError("invitation lifetime cannot exceed 24 hours")
        _validate_consumption(
            status=self.status,
            consumed_at=self.consumed_at,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )
        return self


class PasswordReset(_FrozenModel):
    """Password reset with a globally unique opaque password_reset_id."""

    password_reset_id: str
    tenant_id: str
    local_account_id: str
    token_digest: SecretDigest32 = Field(repr=False, exclude=True)
    status: Literal["pending", "consumed", "revoked"]
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator("password_reset_id", "tenant_id", "local_account_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("created_at", "expires_at", "consumed_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> PasswordReset:
        if not self.created_at < self.expires_at:
            raise ValueError("expires_at must be later than created_at")
        if self.expires_at - self.created_at > timedelta(minutes=30):
            raise ValueError("password reset lifetime cannot exceed 30 minutes")
        _validate_consumption(
            status=self.status,
            consumed_at=self.consumed_at,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )
        return self


class AccountRecoveryCode(_FrozenModel):
    """Recovery code with a globally unique opaque recovery_code_id."""

    recovery_code_id: str
    tenant_id: str
    local_account_id: str
    code_set_id: str
    keyed_digest: KeyedDigest32 = Field(repr=False, exclude=True)
    digest_key_id: str
    created_at: datetime
    consumed_at: datetime | None
    revision: int = Field(ge=1)

    @field_validator(
        "recovery_code_id",
        "tenant_id",
        "local_account_id",
        "code_set_id",
        "digest_key_id",
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("created_at", "consumed_at")
    @classmethod
    def _validate_datetimes(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_consumed_at(self) -> AccountRecoveryCode:
        if self.consumed_at is not None and self.consumed_at < self.created_at:
            raise ValueError("consumed_at cannot predate created_at")
        return self


def _validate_consumption(
    *,
    status: str,
    consumed_at: datetime | None,
    created_at: datetime,
    expires_at: datetime,
) -> None:
    if (status == "consumed") != (consumed_at is not None):
        raise ValueError("consumed_at must be set exactly when status is consumed")
    if consumed_at is not None and not created_at <= consumed_at <= expires_at:
        raise ValueError("consumed_at must be within the credential validity interval")


__all__ = [
    "AccountRecoveryCode",
    "BrowserSession",
    "EncodedPasswordHash",
    "ExternalIdentity",
    "FrozenJsonMap",
    "FrozenJsonObject",
    "IdentityProvider",
    "Invitation",
    "KeyedDigest32",
    "LocalAccount",
    "MembershipKind",
    "PasswordReset",
    "SecretDigest32",
    "SessionRotationKind",
    "Subject",
    "SubjectKind",
    "TenantMembership",
]

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from json import dumps
import re
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from .errors import AuthorizationValidationError
from .operations import Operation


FROZEN_RESOURCE_TYPE_VALUES = (
    "tenant",
    "project",
    "folder",
    "dataset",
    "connector",
    "object_type",
    "link_type",
    "interface",
    "object_view",
    "object_set",
    "function",
    "action",
    "capability",
    "api_operation",
    "artifact",
    "agent",
    "application",
    "external_effect",
    # NexLoop service instance/property/Relation authority extension.
    "object",
    "property",
    "relation",
)
MAX_BREAK_GLASS_LIFETIME = timedelta(minutes=15)

_FROZEN_RESOURCE_TYPES = frozenset(FROZEN_RESOURCE_TYPE_VALUES)
_CANONICAL_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}")
_CANONICAL_RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~:{}-]{0,254}")
_CANONICAL_STABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~{}-]*")
_CANONICAL_VERSION_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*")
_CANONICAL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,254}")
_CANONICAL_PLACEHOLDER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CANONICAL_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/~-]{0,254}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_BREAK_GLASS_RESOURCE_TYPES = frozenset(
    {"tenant", "project", "folder", "application", "agent", "api_operation"}
)
_BREAK_GLASS_OPERATIONS = frozenset(
    {
        Operation.DISCOVER,
        Operation.VIEW_METADATA,
        Operation.MANAGE_PERMISSIONS,
        Operation.MANAGE_POLICY,
    }
)


class ApplicationRestrictionError(AuthorizationValidationError):
    code = "application_restriction_invalid"


class ApplicationVersionImmutable(ApplicationRestrictionError):
    code = "application_version_immutable"


class ApplicationReleaseInvalid(ApplicationRestrictionError):
    code = "application_release_invalid"


@runtime_checkable
class ApplicationRepositoryAttestationVerifier(Protocol):
    """0038 DB-witness port implemented only by the trusted repository.

    Composition will bind the exact Postgres Application repository to this
    verify-only contract.  The Authz domain must never expose a signer, key, or
    first-writer installer for it.
    """

    def verify_application_version(
        self,
        *,
        frozen_authority_digest: str,
        frozen_record_digest: str,
        repository_attestation: str,
    ) -> bool: ...

    def verify_agent_release(
        self,
        *,
        frozen_authority_digest: str,
        frozen_release_digest: str,
        repository_attestation: str,
    ) -> bool: ...


class ApplicationMode(str, Enum):
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


class ApplicationVersionState(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    REVOKED = "revoked"


class ApplicationStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class AgentStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class AgentReleaseStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ApplicationCeilingReason(str, Enum):
    APPLICATION_CEILING_MATCH = "application_ceiling_match"
    APPLICATION_CEILING_DENIED = "application_ceiling_denied"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )


class ApplicationCeilingDecision(_StrictFrozenModel):
    allowed: bool
    reason_codes: tuple[ApplicationCeilingReason, ...]

    @field_validator("reason_codes")
    @classmethod
    def _validate_reason_codes(
        cls,
        value: tuple[ApplicationCeilingReason, ...],
    ) -> tuple[ApplicationCeilingReason, ...]:
        if len(value) != 1:
            raise ValueError("reason_codes must contain exactly one bounded value")
        return value

    @model_validator(mode="after")
    def _validate_outcome(self) -> ApplicationCeilingDecision:
        match = ApplicationCeilingReason.APPLICATION_CEILING_MATCH
        if self.allowed != (self.reason_codes == (match,)):
            raise ValueError("allowed must correspond to the ceiling-match reason")
        return self


class Application(_StrictFrozenModel):
    tenant_id: str
    application_id: str
    display_name: str
    status: ApplicationStatus = ApplicationStatus.ACTIVE
    revision: int = Field(default=1, ge=1)

    _validated_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _tenant_id(value)

    @field_validator("application_id")
    @classmethod
    def _validate_application_id(cls, value: str) -> str:
        return _typed_resource_id(value, "application", "application_id")

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return _bounded_non_blank(value, "display_name")

    @model_validator(mode="after")
    def _seal_application(self) -> Application:
        self._validated_seal = self._seal_value()
        return self

    def _seal_value(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            self.application_id,
            self.display_name,
            self.status,
            self.revision,
        )

    def _ensure_validated(self) -> None:
        if self._validated_seal != self._seal_value():
            raise ApplicationRestrictionError("application is not validated")

    def disable(self) -> Application:
        self._ensure_validated()
        if self.status is not ApplicationStatus.ACTIVE:
            raise ApplicationRestrictionError(
                "only an active application can be disabled"
            )
        return Application(
            **{
                **self.model_dump(),
                "status": ApplicationStatus.DISABLED,
                "revision": self.revision + 1,
            }
        )

    def revoke(self) -> Application:
        self._ensure_validated()
        if self.status is ApplicationStatus.REVOKED:
            raise ApplicationRestrictionError("application is already revoked")
        return Application(
            **{
                **self.model_dump(),
                "status": ApplicationStatus.REVOKED,
                "revision": self.revision + 1,
            }
        )


class ResourceRestriction(_StrictFrozenModel):
    tenant_id: str
    resource_type: str
    # None = 该类型的任意资源(类型级通配,DB 0159)。授权目标 external_effect 是
    # 运行时铸造的,发布时无法枚举;与 scope_authorities.resource_id 同一语义。
    resource_id: str | None
    restriction_digest: str | None = None

    _validated_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _tenant_id(value)

    @field_validator("resource_type", mode="before")
    @classmethod
    def _normalize_resource_type(cls, value: object) -> object:
        # Task 7 owns ResourceType. Accept its str-Enum shape without importing or
        # copying the resource registry into this independently mergeable domain.
        if isinstance(value, Enum) and type(value.value) is str:
            return value.value
        return value

    @field_validator("resource_type")
    @classmethod
    def _validate_resource_type(cls, value: str) -> str:
        if value not in _FROZEN_RESOURCE_TYPES:
            raise ValueError("resource_type is not in the frozen vocabulary")
        return value

    @field_validator("restriction_digest")
    @classmethod
    def _validate_restriction_digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_HEX.fullmatch(value) is None:
            raise ValueError("restriction_digest must be a full SHA-256")
        return value

    @model_validator(mode="after")
    def _validate_resource_id(self) -> ResourceRestriction:
        if self.resource_id is not None:
            _typed_resource_id(self.resource_id, self.resource_type, "resource_id")
        expected_digest = _canonical_digest(
            {
                "tenant_id": self.tenant_id,
                "resource_type": self.resource_type,
                "resource_id": self.resource_id,
            }
        )
        if (
            self.restriction_digest is not None
            and self.restriction_digest != expected_digest
        ):
            raise ValueError("restriction_digest does not match resource restriction")
        object.__setattr__(self, "restriction_digest", expected_digest)
        self._validated_seal = self._seal_value()
        return self

    def _seal_value(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            self.resource_type,
            self.resource_id,
            self.restriction_digest,
        )

    def _ensure_validated(self) -> None:
        if self._validated_seal != self._seal_value():
            raise ApplicationRestrictionError("resource restriction is not validated")

    @property
    def key(self) -> tuple[str, str, str]:
        self._ensure_validated()
        return (self.tenant_id, self.resource_type, self.resource_id)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._ensure_validated()
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        self._ensure_validated()
        return super().model_dump_json(*args, **kwargs)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> ResourceRestriction:
        self._ensure_validated()
        if update:
            raise ApplicationRestrictionError("resource restriction is immutable")
        return self.__deepcopy__() if deep else self.__copy__()

    def copy(self, **kwargs: Any) -> ResourceRestriction:
        self._ensure_validated()
        deep = bool(kwargs.pop("deep", False))
        if kwargs:
            raise ApplicationRestrictionError("resource restriction is immutable")
        return self.__deepcopy__() if deep else self.__copy__()

    def __iter__(self) -> Any:
        self._ensure_validated()
        return super().__iter__()

    def __copy__(self) -> ResourceRestriction:
        self._ensure_validated()
        return ResourceRestriction.model_validate(self.model_dump())

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> ResourceRestriction:
        del memo
        return self.__copy__()

    def __reduce_ex__(self, protocol: int) -> tuple[object, tuple[object, ...]]:
        del protocol
        self._ensure_validated()
        return (_restore_resource_restriction, (self.model_dump(),))


class OperationRestriction(_StrictFrozenModel):
    operation: Operation
    restriction_digest: str | None = None

    _validated_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _seal_operation(self) -> OperationRestriction:
        expected_digest = _canonical_digest({"operation": self.operation.value})
        if (
            self.restriction_digest is not None
            and self.restriction_digest != expected_digest
        ):
            raise ValueError("restriction_digest does not match operation restriction")
        object.__setattr__(self, "restriction_digest", expected_digest)
        self._validated_seal = self._seal_value()
        return self

    def _seal_value(self) -> tuple[object, ...]:
        return (self.operation, self.restriction_digest)

    @field_validator("restriction_digest")
    @classmethod
    def _validate_restriction_digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_HEX.fullmatch(value) is None:
            raise ValueError("restriction_digest must be a full SHA-256")
        return value

    def _ensure_validated(self) -> None:
        if self._validated_seal != self._seal_value():
            raise ApplicationRestrictionError("operation restriction is not validated")

    @property
    def validated_operation(self) -> Operation:
        self._ensure_validated()
        return self.operation

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._ensure_validated()
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        self._ensure_validated()
        return super().model_dump_json(*args, **kwargs)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> OperationRestriction:
        self._ensure_validated()
        if update:
            raise ApplicationRestrictionError("operation restriction is immutable")
        return self.__deepcopy__() if deep else self.__copy__()

    def copy(self, **kwargs: Any) -> OperationRestriction:
        self._ensure_validated()
        deep = bool(kwargs.pop("deep", False))
        if kwargs:
            raise ApplicationRestrictionError("operation restriction is immutable")
        return self.__deepcopy__() if deep else self.__copy__()

    def __iter__(self) -> Any:
        self._ensure_validated()
        return super().__iter__()

    def __copy__(self) -> OperationRestriction:
        self._ensure_validated()
        return OperationRestriction.model_validate(self.model_dump())

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> OperationRestriction:
        del memo
        return self.__copy__()

    def __reduce_ex__(self, protocol: int) -> tuple[object, tuple[object, ...]]:
        del protocol
        self._ensure_validated()
        return (_restore_operation_restriction, (self.model_dump(),))


_AUTHORITY_ATTESTATION_ISSUER = object()
_RELEASE_BINDING_ISSUER = object()
_REPOSITORY_ATTESTATION_VERIFIER_ISSUER = object()


class _ApplicationAuthorityAttestation:
    __slots__ = ("authority_digest", "record_digest")

    def __init__(
        self,
        authority_digest: str,
        record_digest: str,
        issuer: object,
    ) -> None:
        if issuer is not _AUTHORITY_ATTESTATION_ISSUER:
            raise ApplicationRestrictionError(
                "application authority attestation has an invalid issuer"
            )
        self.authority_digest = authority_digest
        self.record_digest = record_digest


@runtime_checkable
class ResourceLike(Protocol):
    tenant_id: str
    resource_type: object
    resource_id: str


class ApplicationVersion(_StrictFrozenModel):
    tenant_id: str
    application_id: str
    version: str
    state: ApplicationVersionState = ApplicationVersionState.DRAFT
    resources: tuple[ResourceRestriction, ...] = ()
    operations: tuple[OperationRestriction, ...] = ()
    mode: ApplicationMode = ApplicationMode.RESTRICTED
    break_glass: bool = False
    approved_governance_reference: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    published_at: datetime | None = None
    revoked_at: datetime | None = None
    revision: int = Field(default=1, ge=1)
    authority_digest: str | None = None
    record_digest: str | None = None

    _content_digest: str | None = PrivateAttr(default=None)
    _validated_seal: tuple[object, ...] | None = PrivateAttr(default=None)
    _authority_attestation: _ApplicationAuthorityAttestation | None = PrivateAttr(
        default=None
    )
    _resource_index: Mapping[tuple[str, str, str], ResourceRestriction] | None = (
        PrivateAttr(default=None)
    )
    _operation_index: Mapping[Operation, OperationRestriction] | None = PrivateAttr(
        default=None
    )

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _tenant_id(value)

    @field_validator("application_id")
    @classmethod
    def _validate_application_id(cls, value: str) -> str:
        return _typed_resource_id(value, "application", "application_id")

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if _CANONICAL_VERSION.fullmatch(value) is None:
            raise ValueError("version must be a canonical bounded identifier")
        return value

    @field_validator("resources")
    @classmethod
    def _canonicalize_resources(
        cls,
        value: tuple[ResourceRestriction, ...],
    ) -> tuple[ResourceRestriction, ...]:
        keys = tuple(item.key for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("resources must not contain duplicates")
        return tuple(sorted(value, key=lambda item: item.key))

    @field_validator("operations")
    @classmethod
    def _canonicalize_operations(
        cls,
        value: tuple[OperationRestriction, ...],
    ) -> tuple[OperationRestriction, ...]:
        operations = tuple(item.validated_operation for item in value)
        if len(operations) != len(set(operations)):
            raise ValueError("operations must not contain duplicates")
        return tuple(sorted(value, key=lambda item: item.validated_operation.value))

    @field_validator("approved_governance_reference")
    @classmethod
    def _validate_governance_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_non_blank(value, "approved_governance_reference")

    @field_validator("authority_digest", "record_digest")
    @classmethod
    def _validate_digest(cls, value: str | None, info: Any) -> str | None:
        if value is not None and _SHA256_HEX.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be a full SHA-256")
        return value

    @field_validator("approved_at", "expires_at", "published_at", "revoked_at")
    @classmethod
    def _validate_timestamp(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_version_contract(self) -> ApplicationVersion:
        if any(item.tenant_id != self.tenant_id for item in self.resources):
            raise ValueError("application restrictions must use the same tenant")
        if self.state is ApplicationVersionState.DRAFT:
            if self.published_at is not None or self.revoked_at is not None:
                raise ValueError("draft version cannot have lifecycle timestamps")
        elif self.state is ApplicationVersionState.PUBLISHED:
            if self.published_at is None or self.revoked_at is not None:
                raise ValueError("published version requires only published_at")
        elif self.published_at is None or self.revoked_at is None:
            raise ValueError("revoked version requires lifecycle timestamps")
        if self.published_at is not None:
            if self.expires_at is not None and self.published_at >= self.expires_at:
                raise ValueError("publication must precede expiry")
            if self.revoked_at is not None and self.revoked_at < self.published_at:
                raise ValueError("revocation cannot precede publication")

        if self.mode is ApplicationMode.RESTRICTED:
            if self.break_glass:
                raise ValueError("restricted version cannot use break_glass")
            if (
                self.approved_governance_reference is not None
                or self.approved_at is not None
            ):
                raise ValueError("restricted version cannot carry break-glass approval")
        else:
            self._validate_break_glass_contract()

        content_digest = _application_version_content_digest(self)
        if (
            self.authority_digest is not None
            and self.authority_digest != content_digest
        ):
            raise ValueError("authority_digest does not match application authority")
        object.__setattr__(self, "authority_digest", content_digest)
        record_digest = _application_version_record_digest(self)
        if self.record_digest is not None and self.record_digest != record_digest:
            raise ValueError("record_digest does not match application version record")
        object.__setattr__(self, "record_digest", record_digest)
        self._resource_index = MappingProxyType(
            {item.key: item for item in self.resources}
        )
        self._operation_index = MappingProxyType(
            {item.validated_operation: item for item in self.operations}
        )
        self._content_digest = content_digest
        self._validated_seal = self._seal_value(content_digest)
        return self

    @classmethod
    def create_draft(cls, **data: Any) -> ApplicationVersion:
        """Create authority through the explicit configuration command boundary."""
        if data.get("state", ApplicationVersionState.DRAFT) is not (
            ApplicationVersionState.DRAFT
        ):
            raise ApplicationVersionImmutable(
                "create_draft only creates draft versions"
            )
        version = cls(**data)
        version._mark_authority_attested(_AUTHORITY_ATTESTATION_ISSUER)
        return version

    def _validate_break_glass_contract(self) -> None:
        if self.break_glass is not True:
            raise ValueError("break-glass version requires break_glass=True")
        if self.approved_governance_reference is None or self.approved_at is None:
            raise ValueError("break-glass version requires approved governance")
        if self.expires_at is None or not (
            self.approved_at
            < self.expires_at
            <= self.approved_at + MAX_BREAK_GLASS_LIFETIME
        ):
            raise ValueError("break-glass expiry must be within fifteen minutes")
        if not self.resources or not self.operations:
            raise ValueError("break-glass version requires non-empty exact allowlists")
        if any(
            item.resource_type not in _BREAK_GLASS_RESOURCE_TYPES
            for item in self.resources
        ):
            raise ValueError("break-glass resources must be platform resources")
        if any(
            item.validated_operation not in _BREAK_GLASS_OPERATIONS
            for item in self.operations
        ):
            raise ValueError(
                "break-glass operations must be audited platform operations"
            )
        if self.published_at is not None and not (
            self.approved_at <= self.published_at < self.expires_at
        ):
            raise ValueError("break-glass publication must be inside approval lifetime")

    def _seal_value(self, content_digest: str | None) -> tuple[object, ...]:
        return (
            self.tenant_id,
            self.application_id,
            self.version,
            self.state,
            id(self.resources),
            id(self.operations),
            id(self._resource_index),
            id(self._operation_index),
            self.mode,
            self.break_glass,
            self.approved_governance_reference,
            self.approved_at,
            self.expires_at,
            self.published_at,
            self.revoked_at,
            self.revision,
            self.authority_digest,
            self.record_digest,
            content_digest,
        )

    def _ensure_validated(self) -> None:
        if (
            self._content_digest is None
            or self._resource_index is None
            or self._operation_index is None
            or self._validated_seal != self._seal_value(self._content_digest)
        ):
            raise ApplicationRestrictionError("application version is not validated")

    def _ensure_authority_items_validated(self) -> None:
        for item in self.resources:
            item._ensure_validated()
        for item in self.operations:
            item._ensure_validated()

    def _mark_authority_attested(self, issuer: object) -> None:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        content_digest = self._content_digest
        if content_digest is None:
            raise ApplicationRestrictionError("application version is not validated")
        self._authority_attestation = _ApplicationAuthorityAttestation(
            content_digest,
            self.record_digest,
            issuer,
        )

    def _authority_is_attested(self) -> bool:
        self._ensure_validated()
        attestation = self._authority_attestation
        if attestation is None:
            return False
        if (
            type(attestation) is not _ApplicationAuthorityAttestation
            or attestation.authority_digest != self._content_digest
            or attestation.record_digest != self.record_digest
        ):
            raise ApplicationRestrictionError(
                "application version authority attestation is invalid"
            )
        return True

    def _ensure_authority_attested(self) -> None:
        if not self._authority_is_attested():
            raise ApplicationRestrictionError(
                "application version authority is not attested"
            )

    @property
    def version_digest(self) -> str:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        content_digest = self._content_digest
        if content_digest is None:
            raise ApplicationRestrictionError("application version is not validated")
        return content_digest

    @property
    def reference(self) -> tuple[str, str, str, str]:
        return (
            self.tenant_id,
            self.application_id,
            self.version,
            self.version_digest,
        )

    def publish(self, *, at: datetime) -> ApplicationVersion:
        self._ensure_authority_attested()
        if self.state is not ApplicationVersionState.DRAFT:
            raise ApplicationVersionImmutable("only a draft version can be published")
        published_at = _aware_utc(at, "at")
        if self.expires_at is not None and published_at >= self.expires_at:
            raise ValueError("publication must precede expiry")
        published = ApplicationVersion(
            **{
                **self.model_dump(),
                "state": ApplicationVersionState.PUBLISHED,
                "published_at": published_at,
                "revision": self.revision + 1,
                "record_digest": None,
            }
        )
        published._mark_authority_attested(_AUTHORITY_ATTESTATION_ISSUER)
        return published

    def revoke(self, *, at: datetime) -> ApplicationVersion:
        self._ensure_authority_attested()
        if self.state is not ApplicationVersionState.PUBLISHED:
            raise ApplicationVersionImmutable("only a published version can be revoked")
        revoked_at = _aware_utc(at, "at")
        published_at = self.published_at
        if published_at is None:
            raise ApplicationVersionImmutable("application version was not published")
        if revoked_at < published_at:
            raise ValueError("revocation cannot precede publication")
        revoked = ApplicationVersion(
            **{
                **self.model_dump(),
                "state": ApplicationVersionState.REVOKED,
                "revoked_at": revoked_at,
                "revision": self.revision + 1,
                "record_digest": None,
            }
        )
        revoked._mark_authority_attested(_AUTHORITY_ATTESTATION_ISSUER)
        return revoked

    def with_authority(
        self,
        *,
        resources: tuple[ResourceRestriction, ...],
        operations: tuple[OperationRestriction, ...],
    ) -> ApplicationVersion:
        self._ensure_authority_attested()
        if self.state is not ApplicationVersionState.DRAFT:
            raise ApplicationVersionImmutable(
                "published application version is immutable"
            )
        updated = ApplicationVersion(
            **{
                **self.model_dump(),
                "resources": resources,
                "operations": operations,
                "revision": self.revision + 1,
                "authority_digest": None,
                "record_digest": None,
            }
        )
        updated._mark_authority_attested(_AUTHORITY_ATTESTATION_ISSUER)
        return updated

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        return super().model_dump_json(*args, **kwargs)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> ApplicationVersion:
        validated = super().model_validate(obj, **kwargs)
        validated._authority_attestation = None
        return validated

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        **kwargs: Any,
    ) -> ApplicationVersion:
        validated = super().model_validate_json(json_data, **kwargs)
        validated._authority_attestation = None
        return validated

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> ApplicationVersion:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        if update:
            raise ApplicationVersionImmutable(
                "application versions use lifecycle methods"
            )
        return self.__deepcopy__() if deep else self.__copy__()

    def copy(self, **kwargs: Any) -> ApplicationVersion:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        deep = bool(kwargs.pop("deep", False))
        if kwargs:
            raise ApplicationVersionImmutable(
                "application versions use lifecycle methods"
            )
        return self.__deepcopy__() if deep else self.__copy__()

    def __iter__(self) -> Any:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        return super().__iter__()

    def __copy__(self) -> ApplicationVersion:
        authority_is_attested = self._authority_is_attested()
        copied = ApplicationVersion.model_validate(self.model_dump())
        if authority_is_attested:
            copied._mark_authority_attested(_AUTHORITY_ATTESTATION_ISSUER)
        return copied

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> ApplicationVersion:
        del memo
        return self.__copy__()

    def __reduce_ex__(self, protocol: int) -> tuple[object, tuple[object, ...]]:
        del protocol
        self._ensure_validated()
        self._ensure_authority_items_validated()
        return (_restore_application_version_data, (self.model_dump(),))

    def __getstate__(self) -> dict[str, Any]:
        self._ensure_validated()
        self._ensure_authority_items_validated()
        state = super().__getstate__()
        private_state = dict(state.get("__pydantic_private__") or {})
        private_state["_authority_attestation"] = None
        state["__pydantic_private__"] = private_state
        return state

    def is_eligible(self, *, at: datetime) -> bool:
        self._ensure_validated()
        now = _aware_utc(at, "at")
        return (
            self.state is ApplicationVersionState.PUBLISHED
            and self.published_at is not None
            and self.published_at <= now
            and (self.expires_at is None or now < self.expires_at)
        )

    def evaluate(
        self,
        operation: Operation,
        resource: ResourceLike,
        *,
        at: datetime,
    ) -> ApplicationCeilingDecision:
        self._ensure_validated()
        if type(operation) is not Operation:
            raise TypeError("operation must be an Operation")
        if not self.is_eligible(at=at):
            return _denied()
        self._ensure_authority_attested()
        resource_key = _resource_like_key(resource)
        resource_index = self._resource_index
        operation_index = self._operation_index
        if resource_index is None or operation_index is None:
            raise ApplicationRestrictionError("application version is not validated")
        matched_resource = resource_index.get(resource_key)
        matched_operation = operation_index.get(operation)
        if (
            resource_key[0] != self.tenant_id
            or matched_operation is None
            or matched_resource is None
        ):
            return _denied()
        matched_operation._ensure_validated()
        matched_resource._ensure_validated()
        return _allowed()

    def allows(
        self,
        operation: Operation,
        resource: ResourceLike,
        *,
        at: datetime,
    ) -> bool:
        return self.evaluate(operation, resource, at=at).allowed

    def is_narrower_than(
        self,
        parent: ApplicationVersion,
        *,
        at: datetime,
    ) -> bool:
        self._ensure_authority_attested()
        parent._ensure_authority_attested()
        now = _aware_utc(at, "at")
        if self.tenant_id != parent.tenant_id:
            return False
        if not self.is_eligible(at=now) or not parent.is_eligible(at=now):
            return False
        if not _expiry_is_narrower(self.expires_at, parent.expires_at):
            return False
        child_resources = frozenset(item.key for item in self.resources)
        parent_resources = frozenset(item.key for item in parent.resources)
        child_operations = frozenset(
            item.validated_operation for item in self.operations
        )
        parent_operations = frozenset(
            item.validated_operation for item in parent.operations
        )
        return (
            child_resources <= parent_resources
            and child_operations <= parent_operations
        )


class Agent(_StrictFrozenModel):
    tenant_id: str
    agent_id: str
    application_id: str
    display_name: str
    parent_agent_id: str | None = None
    status: AgentStatus = AgentStatus.ACTIVE
    revision: int = Field(default=1, ge=1)

    _validated_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _tenant_id(value)

    @field_validator("agent_id", "parent_agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _typed_resource_id(value, "agent", info.field_name)

    @field_validator("application_id")
    @classmethod
    def _validate_application_id(cls, value: str) -> str:
        return _typed_resource_id(value, "application", "application_id")

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return _bounded_non_blank(value, "display_name")

    @model_validator(mode="after")
    def _validate_parent(self) -> Agent:
        if self.parent_agent_id == self.agent_id:
            raise ValueError("agent cannot be its own parent")
        self._validated_seal = self._seal_value()
        return self

    def _seal_value(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            self.agent_id,
            self.application_id,
            self.display_name,
            self.parent_agent_id,
            self.status,
            self.revision,
        )

    def _ensure_validated(self) -> None:
        if self._validated_seal != self._seal_value():
            raise ApplicationRestrictionError("agent is not validated")

    def disable(self) -> Agent:
        self._ensure_validated()
        if self.status is not AgentStatus.ACTIVE:
            raise ApplicationRestrictionError("only an active agent can be disabled")
        return Agent(
            **{
                **self.model_dump(),
                "status": AgentStatus.DISABLED,
                "revision": self.revision + 1,
            }
        )

    def revoke(self) -> Agent:
        self._ensure_validated()
        if self.status is AgentStatus.REVOKED:
            raise ApplicationRestrictionError("agent is already revoked")
        return Agent(
            **{
                **self.model_dump(),
                "status": AgentStatus.REVOKED,
                "revision": self.revision + 1,
            }
        )


class AgentRelease(_StrictFrozenModel):
    tenant_id: str
    release_id: str
    agent_id: str
    application_id: str
    application_version: str
    application_version_digest: str
    released_at: datetime
    parent_release_id: str | None = None
    status: AgentReleaseStatus = AgentReleaseStatus.ACTIVE
    revision: int = Field(default=1, ge=1)
    revoked_at: datetime | None = None
    release_digest: str | None = None

    _bound_seal: tuple[object, ...] | None = PrivateAttr(default=None)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _tenant_id(value)

    @field_validator("release_id", "parent_release_id")
    @classmethod
    def _validate_release_id(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        if _CANONICAL_RELEASE_ID.fullmatch(value) is None:
            raise ValueError(
                f"{info.field_name} must be a canonical bounded identifier"
            )
        return value

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return _typed_resource_id(value, "agent", "agent_id")

    @field_validator("application_id")
    @classmethod
    def _validate_application_id(cls, value: str) -> str:
        return _typed_resource_id(value, "application", "application_id")

    @field_validator("application_version")
    @classmethod
    def _validate_application_version(cls, value: str) -> str:
        if _CANONICAL_VERSION.fullmatch(value) is None:
            raise ValueError("application_version must be canonical and bounded")
        return value

    @field_validator("application_version_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if _SHA256_HEX.fullmatch(value) is None:
            raise ValueError("application_version_digest must be a full SHA-256")
        return value

    @field_validator("release_digest")
    @classmethod
    def _validate_release_digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_HEX.fullmatch(value) is None:
            raise ValueError("release_digest must be a full SHA-256")
        return value

    @field_validator("released_at", "revoked_at")
    @classmethod
    def _validate_release_timestamp(
        cls, value: datetime | None, info: Any
    ) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_release_lifecycle(self) -> AgentRelease:
        if self.status is AgentReleaseStatus.ACTIVE and self.revoked_at is not None:
            raise ValueError("active release cannot have revoked_at")
        if self.status is AgentReleaseStatus.REVOKED:
            if self.revoked_at is None:
                raise ValueError("revoked release requires revoked_at")
            if self.revoked_at < self.released_at:
                raise ValueError("revocation cannot precede released_at")
        expected_digest = _agent_release_record_digest(self)
        if self.release_digest is not None and self.release_digest != expected_digest:
            raise ValueError("release_digest does not match agent release record")
        object.__setattr__(self, "release_digest", expected_digest)
        return self

    def _seal_value(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            self.release_id,
            self.agent_id,
            self.application_id,
            self.application_version,
            self.application_version_digest,
            self.released_at,
            self.parent_release_id,
            self.status,
            self.revision,
            self.revoked_at,
            self.release_digest,
        )

    def _ensure_record_validated(self) -> None:
        if (
            self.release_digest is None
            or self.release_digest != _agent_release_record_digest(self)
        ):
            raise ApplicationReleaseInvalid("agent release record is not validated")

    def _mark_bound(self, issuer: object) -> None:
        self._ensure_record_validated()
        if issuer is not _RELEASE_BINDING_ISSUER:
            raise ApplicationReleaseInvalid(
                "agent release binding has an invalid issuer"
            )
        self._bound_seal = self._seal_value()

    def _ensure_bound(self) -> None:
        self._ensure_record_validated()
        if self._bound_seal != self._seal_value():
            raise ApplicationReleaseInvalid("agent release is not validated and bound")

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._ensure_record_validated()
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        self._ensure_record_validated()
        return super().model_dump_json(*args, **kwargs)

    def __iter__(self) -> Any:
        self._ensure_record_validated()
        return super().__iter__()

    def __reduce_ex__(self, protocol: int) -> tuple[object, tuple[object, ...]]:
        del protocol
        return (_restore_agent_release_data, (self.model_dump(),))

    def __getstate__(self) -> dict[str, Any]:
        self._ensure_record_validated()
        state = super().__getstate__()
        private_state = dict(state.get("__pydantic_private__") or {})
        private_state["_bound_seal"] = None
        state["__pydantic_private__"] = private_state
        return state

    def revoke(self, *, at: datetime) -> AgentRelease:
        self._ensure_bound()
        if self.status is AgentReleaseStatus.REVOKED:
            raise ApplicationReleaseInvalid("agent release is already revoked")
        revoked_at = _aware_utc(at, "at")
        if revoked_at < self.released_at:
            raise ApplicationReleaseInvalid("revoked_at cannot precede released_at")
        revoked = AgentRelease(
            **{
                **self.model_dump(),
                "status": AgentReleaseStatus.REVOKED,
                "revision": self.revision + 1,
                "revoked_at": revoked_at,
                "release_digest": None,
            }
        )
        revoked._mark_bound(_RELEASE_BINDING_ISSUER)
        return revoked

    @classmethod
    def bind(
        cls,
        *,
        release_id: str,
        application: Application,
        agent: Agent,
        application_version: ApplicationVersion,
        released_at: datetime,
        parent_release: AgentRelease | None = None,
        parent_application_version: ApplicationVersion | None = None,
    ) -> AgentRelease:
        when = _aware_utc(released_at, "released_at")
        if (
            type(application) is not Application
            or type(agent) is not Agent
            or type(application_version) is not ApplicationVersion
        ):
            raise ApplicationReleaseInvalid(
                "release inputs must use exact domain types"
            )
        try:
            application._ensure_validated()
            agent._ensure_validated()
            application_version._ensure_authority_attested()
            application_version._ensure_authority_items_validated()
        except (ApplicationRestrictionError, AttributeError) as error:
            raise ApplicationReleaseInvalid("release input is not validated") from error
        if (
            application.status is not ApplicationStatus.ACTIVE
            or agent.status is not AgentStatus.ACTIVE
        ):
            raise ApplicationReleaseInvalid(
                "agent release requires an active application and agent"
            )
        if application_version.mode is ApplicationMode.UNRESTRICTED:
            raise ApplicationReleaseInvalid(
                "unrestricted application version cannot bind an ordinary agent"
            )
        if not application_version.is_eligible(at=when):
            raise ApplicationReleaseInvalid(
                "agent release requires an eligible published application version"
            )
        if not (
            application.tenant_id == agent.tenant_id == application_version.tenant_id
        ):
            raise ApplicationReleaseInvalid("agent and application tenant do not match")
        if not (
            application.application_id
            == agent.application_id
            == application_version.application_id
        ):
            raise ApplicationReleaseInvalid(
                "agent and application identity do not match"
            )

        parent_release_id: str | None = None
        if agent.parent_agent_id is None:
            if parent_release is not None or parent_application_version is not None:
                raise ApplicationReleaseInvalid(
                    "root agent cannot bind a parent release"
                )
        else:
            if parent_release is None or parent_application_version is None:
                raise ApplicationReleaseInvalid(
                    "child agent requires its parent release"
                )
            if (
                type(parent_release) is not AgentRelease
                or type(parent_application_version) is not ApplicationVersion
            ):
                raise ApplicationReleaseInvalid(
                    "parent inputs must use exact domain types"
                )
            try:
                parent_release._ensure_bound()
                parent_application_version._ensure_authority_attested()
                parent_application_version._ensure_authority_items_validated()
            except (ApplicationRestrictionError, AttributeError) as error:
                raise ApplicationReleaseInvalid(
                    "parent release or application version is not validated"
                ) from error
            if parent_release.status is not AgentReleaseStatus.ACTIVE:
                raise ApplicationReleaseInvalid("parent release must be active")
            if parent_release.tenant_id != agent.tenant_id:
                raise ApplicationReleaseInvalid("parent release tenant does not match")
            if parent_release.agent_id != agent.parent_agent_id:
                raise ApplicationReleaseInvalid(
                    "parent release does not bind parent agent"
                )
            if not (
                parent_release.tenant_id == parent_application_version.tenant_id
                and parent_release.application_id
                == parent_application_version.application_id
                and parent_release.application_version
                == parent_application_version.version
                and parent_release.application_version_digest
                == parent_application_version.version_digest
            ):
                raise ApplicationReleaseInvalid(
                    "parent release does not bind the supplied application version"
                )
            if not parent_application_version.is_eligible(at=when):
                raise ApplicationReleaseInvalid(
                    "parent application version must be eligible"
                )
            if when < parent_release.released_at:
                raise ApplicationReleaseInvalid("child released before parent release")
            if not application_version.is_narrower_than(
                parent_application_version,
                at=when,
            ):
                raise ApplicationReleaseInvalid(
                    "child release cannot expand parent ceiling"
                )
            parent_release_id = parent_release.release_id

        release = cls(
            tenant_id=agent.tenant_id,
            release_id=release_id,
            agent_id=agent.agent_id,
            application_id=agent.application_id,
            application_version=application_version.version,
            application_version_digest=application_version.version_digest,
            released_at=when,
            parent_release_id=parent_release_id,
        )
        release._mark_bound(_RELEASE_BINDING_ISSUER)
        return release


def _tenant_id(value: str) -> str:
    if _CANONICAL_TENANT_ID.fullmatch(value) is None:
        raise ValueError("tenant_id must be a canonical bounded identifier")
    return value


def _bounded_non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 255:
        raise ValueError(f"{field_name} must be non-empty and bounded")
    if clean != value:
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    return value


def _typed_resource_id(value: str, resource_type: str, field_name: str) -> str:
    if _CANONICAL_RESOURCE_ID.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a canonical bounded resource identifier"
        )
    prefix = f"eios:{resource_type}:"
    if not value.startswith(prefix) or len(value) == len(prefix):
        raise ValueError(f"{field_name} must use the {resource_type} canonical prefix")
    suffix = value[len(prefix) :]
    if suffix.count(":") > 1:
        raise ValueError(f"{field_name} must contain at most one version separator")
    stable_name, separator, version = suffix.partition(":")
    try:
        _stable_name(stable_name)
        if separator:
            _version_part(version)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} suffix is not canonical") from error
    return value


def _stable_name(value: str) -> str:
    if type(value) is not str or _CANONICAL_STABLE_NAME.fullmatch(value) is None:
        raise ValueError("stable_name must be a canonical non-empty identifier part")
    position = 0
    while position < len(value):
        character = value[position]
        if character == "}":
            raise ValueError("stable_name contains an unpaired route brace")
        if character != "{":
            position += 1
            continue
        close = value.find("}", position + 1)
        if close < 0:
            raise ValueError("stable_name contains an unpaired route brace")
        placeholder = value[position + 1 : close]
        if _CANONICAL_PLACEHOLDER.fullmatch(placeholder) is None or "{" in placeholder:
            raise ValueError("stable_name contains an invalid route placeholder")
        position = close + 1
    return value


def _version_part(value: str) -> str:
    if type(value) is not str or _CANONICAL_VERSION_PART.fullmatch(value) is None:
        raise ValueError("version must be a canonical non-empty identifier part")
    return value


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _canonical_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _application_version_content_digest(version: ApplicationVersion) -> str:
    payload = {
        "tenant_id": version.tenant_id,
        "application_id": version.application_id,
        "version": version.version,
        "mode": version.mode.value,
        "resources": [list(item.key) for item in version.resources],
        "operations": [item.validated_operation.value for item in version.operations],
        "break_glass": version.break_glass,
        "approved_governance_reference": version.approved_governance_reference,
        "approved_at": _canonical_timestamp(version.approved_at),
        "expires_at": _canonical_timestamp(version.expires_at),
    }
    return _canonical_digest(payload)


def _application_version_record_digest(version: ApplicationVersion) -> str:
    payload = {
        "tenant_id": version.tenant_id,
        "application_id": version.application_id,
        "version": version.version,
        "state": version.state.value,
        "authority_digest": version.authority_digest,
        "published_at": _canonical_timestamp(version.published_at),
        "revoked_at": _canonical_timestamp(version.revoked_at),
        "revision": version.revision,
    }
    return _canonical_digest(payload)


def _agent_release_record_digest(release: AgentRelease) -> str:
    payload = {
        "tenant_id": release.tenant_id,
        "release_id": release.release_id,
        "agent_id": release.agent_id,
        "application_id": release.application_id,
        "application_version": release.application_version,
        "application_version_digest": release.application_version_digest,
        "released_at": _canonical_timestamp(release.released_at),
        "parent_release_id": release.parent_release_id,
        "status": release.status.value,
        "revision": release.revision,
        "revoked_at": _canonical_timestamp(release.revoked_at),
    }
    return _canonical_digest(payload)


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _restore_resource_restriction(payload: Mapping[str, Any]) -> ResourceRestriction:
    return ResourceRestriction.model_validate(payload)


def _restore_operation_restriction(payload: Mapping[str, Any]) -> OperationRestriction:
    return OperationRestriction.model_validate(payload)


def _restore_application_version_data(
    payload: Mapping[str, Any],
) -> ApplicationVersion:
    return ApplicationVersion.model_validate(payload)


def restore_attested_application_version(
    payload: Mapping[str, Any],
    *,
    frozen_authority_digest: str,
    frozen_record_digest: str,
    repository_attestation: str,
    repository_verifier: ApplicationRepositoryAttestationVerifier,
) -> ApplicationVersion:
    """Restore an attested version from exact trusted repository records.

    This public port is reserved for the Postgres Application repository and
    remains fail-closed in F8.  In 0038, trusted composition will bind the exact
    owner to the verify-only ``repository_verifier`` port and this witness
    parameter; only successful DB-backed verification may precede marking.
    Serialized payload digests alone never restore trust, and F8 does not claim
    that persisted authority is restorable yet.
    """
    if _SHA256_HEX.fullmatch(frozen_authority_digest) is None:
        raise ApplicationRestrictionError(
            "repository frozen authority digest must be a full SHA-256"
        )
    version = ApplicationVersion.model_validate(payload)
    if version.version_digest != frozen_authority_digest:
        raise ApplicationRestrictionError(
            "repository frozen authority digest does not match application authority"
        )
    if (
        _SHA256_HEX.fullmatch(frozen_record_digest) is None
        or version.record_digest != frozen_record_digest
    ):
        raise ApplicationRestrictionError(
            "repository frozen record digest does not match application version"
        )
    if (
        getattr(repository_verifier, "_repository_issuer", None)
        is not _REPOSITORY_ATTESTATION_VERIFIER_ISSUER
        or not repository_verifier.verify_application_version(
            frozen_authority_digest=frozen_authority_digest,
            frozen_record_digest=frozen_record_digest,
            repository_attestation=repository_attestation,
        )
    ):
        raise ApplicationRestrictionError(
            "repository attestation verifier is unavailable"
        )
    version._mark_authority_attested(_AUTHORITY_ATTESTATION_ISSUER)
    return version


def _restore_agent_release_data(payload: Mapping[str, Any]) -> AgentRelease:
    return AgentRelease.model_validate(payload)


def restore_bound_agent_release(
    payload: Mapping[str, Any],
    *,
    application_version: ApplicationVersion,
    frozen_authority_digest: str,
    frozen_release_digest: str,
    repository_attestation: str,
    repository_verifier: ApplicationRepositoryAttestationVerifier,
) -> AgentRelease:
    """Restore a release bound to an exact attested Application ceiling.

    This public port is reserved for the Postgres Application repository and
    remains fail-closed in F8.  In 0038, trusted composition will bind the exact
    owner to the verify-only ``repository_verifier`` port and this witness
    parameter; only successful DB-backed verification may precede marking.
    Frozen authority and release digests alone never restore trust, and F8 does
    not claim that persisted releases are restorable yet.
    """
    if type(application_version) is not ApplicationVersion:
        raise ApplicationReleaseInvalid(
            "repository application version must use the exact domain type"
        )
    try:
        application_version._ensure_authority_attested()
        application_version._ensure_authority_items_validated()
    except ApplicationRestrictionError as error:
        raise ApplicationReleaseInvalid(
            "repository application version authority is not attested"
        ) from error
    if (
        _SHA256_HEX.fullmatch(frozen_authority_digest) is None
        or application_version.version_digest != frozen_authority_digest
    ):
        raise ApplicationReleaseInvalid(
            "repository frozen authority digest does not match application authority"
        )
    release = AgentRelease.model_validate(payload)
    if (
        _SHA256_HEX.fullmatch(frozen_release_digest) is None
        or release.release_digest != frozen_release_digest
    ):
        raise ApplicationReleaseInvalid(
            "repository frozen release digest does not match agent release"
        )
    if not (
        release.tenant_id == application_version.tenant_id
        and release.application_id == application_version.application_id
        and release.application_version == application_version.version
        and release.application_version_digest == frozen_authority_digest
    ):
        raise ApplicationReleaseInvalid(
            "repository release does not bind the attested application authority"
        )
    if (
        getattr(repository_verifier, "_repository_issuer", None)
        is not _REPOSITORY_ATTESTATION_VERIFIER_ISSUER
        or not repository_verifier.verify_agent_release(
            frozen_authority_digest=frozen_authority_digest,
            frozen_release_digest=frozen_release_digest,
            repository_attestation=repository_attestation,
        )
    ):
        raise ApplicationReleaseInvalid(
            "repository attestation verifier is unavailable"
        )
    release._mark_bound(_RELEASE_BINDING_ISSUER)
    return release


def _resource_type_value(value: object) -> str:
    if isinstance(value, Enum) and type(value.value) is str:
        candidate = value.value
    elif type(value) is str:
        candidate = value
    else:
        raise TypeError("resource.resource_type must be a string Enum")
    if candidate not in _FROZEN_RESOURCE_TYPES:
        raise ValueError("resource.resource_type is not in the frozen vocabulary")
    return candidate


def _resource_like_key(resource: ResourceLike) -> tuple[str, str, str]:
    if isinstance(resource, Mapping):
        raise TypeError("resource must be a trusted structured reference")
    try:
        tenant_id = resource.tenant_id
        resource_type = _resource_type_value(resource.resource_type)
        resource_id = resource.resource_id
    except AttributeError as error:
        raise TypeError("resource must expose tenant, type, and identifier") from error
    if type(tenant_id) is not str:
        raise TypeError("resource.tenant_id must be a string")
    if type(resource_id) is not str:
        raise TypeError("resource.resource_id must be a string")
    checked_tenant = _tenant_id(tenant_id)
    checked_resource_id = _typed_resource_id(
        resource_id,
        resource_type,
        "resource.resource_id",
    )
    return (checked_tenant, resource_type, checked_resource_id)


def _expiry_is_narrower(
    child: datetime | None,
    parent: datetime | None,
) -> bool:
    if parent is None:
        return True
    return child is not None and child <= parent


def _allowed() -> ApplicationCeilingDecision:
    return ApplicationCeilingDecision(
        allowed=True,
        reason_codes=(ApplicationCeilingReason.APPLICATION_CEILING_MATCH,),
    )


def _denied() -> ApplicationCeilingDecision:
    return ApplicationCeilingDecision(
        allowed=False,
        reason_codes=(ApplicationCeilingReason.APPLICATION_CEILING_DENIED,),
    )


__all__ = [
    "FROZEN_RESOURCE_TYPE_VALUES",
    "MAX_BREAK_GLASS_LIFETIME",
    "Agent",
    "AgentRelease",
    "AgentReleaseStatus",
    "AgentStatus",
    "Application",
    "ApplicationCeilingDecision",
    "ApplicationCeilingReason",
    "ApplicationMode",
    "ApplicationReleaseInvalid",
    "ApplicationRepositoryAttestationVerifier",
    "ApplicationRestrictionError",
    "ApplicationStatus",
    "ApplicationVersion",
    "ApplicationVersionImmutable",
    "ApplicationVersionState",
    "OperationRestriction",
    "ResourceLike",
    "ResourceRestriction",
    "restore_attested_application_version",
    "restore_bound_agent_release",
]

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import re
from typing import Annotated, Literal, Protocol, TypeVar
from weakref import ReferenceType, ref

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from .plans import PolicyEvaluationResult


MAX_REGISTRY_IDENTIFIER_BYTES = 255
MAX_REGISTRY_TEXT_BYTES = 1_024
MAX_REGISTRY_DIGEST_FIELDS = 4_096
MAX_REGISTRY_DIGEST_VALUE_BYTES = 1_048_576
MAX_REGISTRY_DIGEST_CANONICAL_BYTES = 8_388_608
# Registry metadata counters and revisions use one stable signed 64-bit domain.
MIN_REGISTRY_DIGEST_INTEGER = -(2**63)
MAX_REGISTRY_DIGEST_INTEGER = 2**63 - 1
MAX_POLICY_DEPENDENCIES = 256
MAX_POLICY_BINDINGS = 256
_FULL_DIGEST = re.compile(r"[0-9a-f]{64}")
_REGISTRY_DIGEST_MAGIC = b"EIOS-POLICY-REGISTRY\x00\x01"


class PolicyRegistryError(ValueError):
    """Bounded domain error for invalid registry data or transitions."""


class PolicyKind(str, Enum):
    RESOURCE_ACCESS = "resource_access"
    OBJECT_SECURITY = "object_security"
    PROPERTY_SECURITY = "property_security"
    FUNCTION_EXECUTION = "function_execution"
    ACTION_SUBMISSION = "action_submission"
    ACTION_APPROVAL = "action_approval"
    EXPORT_CONTROL = "export_control"
    DELEGATION = "delegation"


class PolicyLifecycle(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class DraftState(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ABANDONED = "abandoned"


class VersionState(str, Enum):
    PUBLISHED = "published"
    REVOKED = "revoked"


class PolicyEffect(str, Enum):
    DENY_IF = "DENY_IF"
    REQUIRE = "REQUIRE"
    OBLIGATION_IF = "OBLIGATION_IF"


class PolicyObligationKind(str, Enum):
    APPROVAL = "approval"
    MASK = "mask"
    DENY_EXPORT = "deny_export"
    WATERMARK = "watermark"
    RESULT_CONTROL = "result_control"
    MAX_CACHE_TTL = "max_cache_ttl"


class PolicyEffectReason(str, Enum):
    DENY_MATCHED = "policy_deny_matched"
    REQUIREMENT_UNSATISFIED = "policy_requirement_unsatisfied"
    EVALUATION_ERROR = "policy_evaluation_error"


class PolicyDependencyKind(str, Enum):
    FACT = "fact"
    SCHEMA = "schema"
    RESOURCE = "resource"
    POLICY = "policy"


class ActivationEventKind(str, Enum):
    ACTIVATE = "activate"
    ROLLBACK = "rollback"
    EMERGENCY_REVOKE = "emergency_revoke"


def _snapshot(value: object) -> object:
    if isinstance(value, BaseModel):
        return (
            type(value),
            tuple(
                (name, _snapshot(value.__dict__[name]))
                for name in type(value).model_fields
            ),
        )
    if type(value) is tuple:
        return (tuple, tuple(_snapshot(item) for item in value))
    return (type(value), value)


class _StrictFrozenRegistryModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )
    _validated_snapshot: object = PrivateAttr(default=None)

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        self.__pydantic_private__["_validated_snapshot"] = self._field_snapshot()

    def _field_snapshot(self) -> tuple[tuple[str, object], ...]:
        return tuple(
            (name, _snapshot(self.__dict__[name])) for name in type(self).model_fields
        )

    def _construction_is_pristine(self) -> bool:
        return (
            set(self.__dict__) == set(type(self).model_fields)
            and self.__pydantic_extra__ is None
            and self.__pydantic_private__
            == {"_validated_snapshot": self._field_snapshot()}
            and type(self.__pydantic_fields_set__) is set
            and self.__pydantic_fields_set__ <= set(type(self).model_fields)
        )


def _bounded_identifier(value: str, field_name: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except (AttributeError, UnicodeError):
        raise ValueError(f"{field_name} must be bounded non-blank text") from None
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(encoded) > MAX_REGISTRY_IDENTIFIER_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-blank text")
    return value


def _utc_timestamp(value: datetime, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_text(value: str, field_name: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except (AttributeError, UnicodeError):
        raise ValueError(f"{field_name} must be bounded non-blank text") from None
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(encoded) > MAX_REGISTRY_TEXT_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-blank text")
    return value


def _full_digest(value: str, field_name: str) -> str:
    if type(value) is not str or _FULL_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase digest")
    return value


RegistryIdentifier = Annotated[
    str,
    AfterValidator(lambda value: _bounded_identifier(value, "identifier")),
]
RegistryText = Annotated[
    str,
    AfterValidator(lambda value: _bounded_text(value, "text")),
]
FullDigest = Annotated[
    str,
    AfterValidator(lambda value: _full_digest(value, "digest")),
]
UtcTimestamp = Annotated[
    datetime,
    AfterValidator(lambda value: _utc_timestamp(value, "timestamp")),
]


def _checked_digest_text_bytes(value: str, maximum_encoded_bytes: int) -> bytes:
    if (
        type(value) is not str
        or maximum_encoded_bytes < 0
        or len(value) > maximum_encoded_bytes
    ):
        raise PolicyRegistryError("registry digest field is invalid")
    encoded_length = 0
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x7F:
            encoded_length += 1
        elif codepoint <= 0x7FF:
            encoded_length += 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise PolicyRegistryError("registry digest field is invalid")
        elif codepoint <= 0xFFFF:
            encoded_length += 3
        else:
            encoded_length += 4
        if encoded_length > maximum_encoded_bytes:
            raise PolicyRegistryError("registry digest field is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise PolicyRegistryError("registry digest field is invalid") from None
    if len(encoded) != encoded_length:
        raise PolicyRegistryError("registry digest field is invalid")
    return encoded


def _registry_value_parts(
    value: object,
    maximum_encoded_bytes: int,
) -> tuple[bytes, tuple[bytes, ...], int]:
    if not 0 <= maximum_encoded_bytes <= MAX_REGISTRY_DIGEST_VALUE_BYTES:
        raise PolicyRegistryError("registry digest field is invalid")
    if type(value) is str:
        encoded = _checked_digest_text_bytes(value, maximum_encoded_bytes)
        return b"s", (encoded,), len(encoded)
    if type(value) is bytes:
        if len(value) > maximum_encoded_bytes:
            raise PolicyRegistryError("registry digest field is invalid")
        return b"b", (value,), len(value)
    if type(value) is bool:
        encoded = b"1" if value else b"0"
        if len(encoded) > maximum_encoded_bytes:
            raise PolicyRegistryError("registry digest field is invalid")
        return b"o", (encoded,), len(encoded)
    if type(value) is int:
        try:
            if not MIN_REGISTRY_DIGEST_INTEGER <= value <= MAX_REGISTRY_DIGEST_INTEGER:
                raise ValueError
            encoded = str(value).encode("ascii")
        except (ValueError, OverflowError):
            raise PolicyRegistryError("registry digest field is invalid") from None
        if len(encoded) > maximum_encoded_bytes:
            raise PolicyRegistryError("registry digest field is invalid")
        return b"i", (encoded,), len(encoded)
    if type(value) is datetime:
        try:
            normalized = _utc_timestamp(value, "digest timestamp")
            encoded = normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode("ascii")
        except (UnicodeError, ValueError):
            raise PolicyRegistryError("registry digest field is invalid") from None
        if len(encoded) > maximum_encoded_bytes:
            raise PolicyRegistryError("registry digest field is invalid")
        return b"t", (encoded,), len(encoded)
    if isinstance(value, Enum):
        enum_type = type(value)
        enum_value = value.value
        if (
            type(enum_value) is not str
            or type(enum_type.__module__) is not str
            or type(enum_type.__qualname__) is not str
        ):
            raise PolicyRegistryError("registry digest field is invalid")
        parts: list[bytes] = []
        encoded_length = 0
        for tag, component in (
            (b"m", enum_type.__module__),
            (b"q", enum_type.__qualname__),
            (b"v", enum_value),
        ):
            remaining_component_bytes = maximum_encoded_bytes - encoded_length - 1 - 8
            encoded = _checked_digest_text_bytes(
                component,
                remaining_component_bytes,
            )
            component_length = 1 + 8 + len(encoded)
            encoded_length += component_length
            if encoded_length > maximum_encoded_bytes:
                raise PolicyRegistryError("registry digest field is invalid")
            parts.extend((tag, len(encoded).to_bytes(8, "big"), encoded))
        return b"e", tuple(parts), encoded_length
    raise PolicyRegistryError("registry digest field is invalid")


def _preflight_registry_digest(
    fields: tuple[tuple[str, object], ...],
) -> tuple[tuple[tuple[str, object, bytes, int], ...], int]:
    if (
        type(fields) is not tuple
        or not fields
        or len(fields) > MAX_REGISTRY_DIGEST_FIELDS
    ):
        raise PolicyRegistryError("registry digest fields are invalid")
    canonical_bytes = len(_REGISTRY_DIGEST_MAGIC) + 4
    if canonical_bytes > MAX_REGISTRY_DIGEST_CANONICAL_BYTES:
        raise PolicyRegistryError("registry digest fields are invalid")
    names: set[str] = set()
    prepared: list[tuple[str, object, bytes, int]] = []
    for field in fields:
        if type(field) is not tuple or len(field) != 2:
            raise PolicyRegistryError("registry digest fields are invalid")
        name, value = field
        try:
            if type(name) is not str or len(name) > MAX_REGISTRY_IDENTIFIER_BYTES:
                raise ValueError
            checked_name = _bounded_identifier(name, "digest field name")
        except Exception:
            raise PolicyRegistryError("registry digest fields are invalid") from None
        if checked_name in names:
            raise PolicyRegistryError("registry digest fields are invalid")
        names.add(checked_name)
        encoded_name = checked_name.encode("utf-8")
        value_budget = min(
            MAX_REGISTRY_DIGEST_VALUE_BYTES,
            MAX_REGISTRY_DIGEST_CANONICAL_BYTES
            - canonical_bytes
            - 4
            - len(encoded_name)
            - 1
            - 8,
        )
        try:
            type_tag, value_parts, encoded_value_length = _registry_value_parts(
                value,
                value_budget,
            )
        except PolicyRegistryError:
            raise PolicyRegistryError("registry digest fields are invalid") from None
        field_bytes = 4 + len(encoded_name) + 1 + 8 + encoded_value_length
        if canonical_bytes + field_bytes > MAX_REGISTRY_DIGEST_CANONICAL_BYTES:
            raise PolicyRegistryError("registry digest fields are invalid")
        canonical_bytes += field_bytes
        prepared.append((checked_name, value, type_tag, encoded_value_length))
        del value_parts
    return tuple(prepared), canonical_bytes


def registry_digest(fields: tuple[tuple[str, object], ...]) -> str:
    """Digest a fully preflighted typed field sequence with bounded streaming."""

    prepared, canonical_bytes = _preflight_registry_digest(fields)
    digest = sha256()
    hashed_bytes = 0

    def update(value: bytes) -> None:
        nonlocal hashed_bytes
        hashed_bytes += len(value)
        digest.update(value)

    update(_REGISTRY_DIGEST_MAGIC)
    update(len(prepared).to_bytes(4, "big"))
    for checked_name, value, expected_type_tag, expected_value_length in prepared:
        encoded_name = checked_name.encode("utf-8")
        try:
            type_tag, value_parts, encoded_value_length = _registry_value_parts(
                value,
                expected_value_length,
            )
        except PolicyRegistryError:
            raise PolicyRegistryError("registry digest fields are invalid") from None
        if (
            type_tag != expected_type_tag
            or encoded_value_length != expected_value_length
        ):
            raise PolicyRegistryError("registry digest fields are invalid")
        update(len(encoded_name).to_bytes(4, "big"))
        update(encoded_name)
        update(type_tag)
        update(encoded_value_length.to_bytes(8, "big"))
        for value_part in value_parts:
            update(value_part)
    if hashed_bytes != canonical_bytes:
        raise PolicyRegistryError("registry digest fields are invalid")
    return digest.hexdigest()


class PolicySet(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    kind: PolicyKind
    lifecycle: PolicyLifecycle
    revision: int = Field(ge=1)
    created_by: RegistryIdentifier
    created_at: UtcTimestamp


class PolicySetLifecycleTransition(_StrictFrozenRegistryModel):
    """Immutable evidence for one adjacent, irreversible lifecycle change."""

    previous_policy_set: PolicySet
    policy_set: PolicySet
    updated_by: RegistryIdentifier
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _validate_transition(self) -> PolicySetLifecycleTransition:
        previous = self.previous_policy_set
        current = self.policy_set
        allowed = {
            PolicyLifecycle.ACTIVE: PolicyLifecycle.DISABLED,
            PolicyLifecycle.DISABLED: PolicyLifecycle.RETIRED,
        }
        if (
            allowed.get(previous.lifecycle) is not current.lifecycle
            or current.tenant_id != previous.tenant_id
            or current.policy_set_id != previous.policy_set_id
            or current.kind is not previous.kind
            or current.revision != previous.revision + 1
            or current.created_by != previous.created_by
            or current.created_at != previous.created_at
            or self.updated_at < previous.created_at
        ):
            raise ValueError("policy set lifecycle transition is invalid")
        return self


class ObligationConfigEntry(_StrictFrozenRegistryModel):
    name: RegistryIdentifier
    value: str | int | bool

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> object:
        if type(value) is str:
            return _bounded_identifier(value, "value")
        if type(value) is int:
            if not -(2**63) <= value <= 2**63 - 1:
                raise ValueError("config integer is outside the supported range")
            return value
        if type(value) is bool:
            return value
        raise ValueError("config value type is not supported")


class PolicyObligation(_StrictFrozenRegistryModel):
    kind: PolicyObligationKind
    config: tuple[ObligationConfigEntry, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _validate_closed_config(self) -> PolicyObligation:
        entries = {entry.name: entry.value for entry in self.config}
        if len(entries) != len(self.config):
            raise ValueError("obligation config names must be unique")
        if tuple(entry.name for entry in self.config) != tuple(sorted(entries)):
            raise ValueError("obligation config must use canonical key order")
        expected_names = {
            PolicyObligationKind.APPROVAL: {"approval_policy_id", "quorum"},
            PolicyObligationKind.MASK: {"mask_profile_id"},
            PolicyObligationKind.DENY_EXPORT: set(),
            PolicyObligationKind.WATERMARK: {"template_id"},
            PolicyObligationKind.RESULT_CONTROL: {"control_id"},
            PolicyObligationKind.MAX_CACHE_TTL: {"seconds"},
        }[self.kind]
        if set(entries) != expected_names:
            raise ValueError("obligation config does not match its closed schema")
        for name, value in entries.items():
            if (
                name
                in {
                    "approval_policy_id",
                    "mask_profile_id",
                    "template_id",
                    "control_id",
                }
                and type(value) is not str
            ):
                raise ValueError("obligation identifier config must be text")
        quorum = entries.get("quorum")
        if quorum is not None and (type(quorum) is not int or not 1 <= quorum <= 64):
            raise ValueError("approval quorum is outside the supported range")
        seconds = entries.get("seconds")
        if seconds is not None and (
            type(seconds) is not int or not 1 <= seconds <= 300
        ):
            raise ValueError("cache lifetime is outside the supported range")
        return self


class PolicyEffectDecision(_StrictFrozenRegistryModel):
    """Internal narrowing result; absence of a deny is not an Authorization Allow."""

    must_deny: bool
    obligations: tuple[PolicyObligation, ...] = Field(default=(), max_length=64)
    reason_codes: tuple[PolicyEffectReason, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def _validate_bounded_result(self) -> PolicyEffectDecision:
        if len({obligation.kind for obligation in self.obligations}) != len(
            self.obligations
        ):
            raise ValueError("effect obligations must be unique")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("effect reasons must be unique")
        if self.reason_codes and not self.must_deny:
            raise ValueError("non-deny effect result cannot contain deny reasons")
        return self


class PolicyDependencyReference(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    kind: PolicyDependencyKind
    dependency_id: RegistryIdentifier
    revision: int = Field(ge=1)
    digest: FullDigest


class PolicyBinding(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    binding_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    resource_type: RegistryIdentifier
    resource_id: RegistryIdentifier
    operation: RegistryIdentifier
    binding_digest: FullDigest


def create_policy_binding(
    *,
    tenant_id: str,
    binding_id: str,
    policy_set_id: str,
    resource_type: str,
    resource_id: str,
    operation: str,
) -> PolicyBinding:
    """Create one strict binding with the DB-recomputable canonical digest."""
    try:
        binding_digest = registry_digest(
            (
                ("record_type", "policy_binding"),
                ("tenant_id", tenant_id),
                ("binding_id", binding_id),
                ("policy_set_id", policy_set_id),
                ("resource_type", resource_type),
                ("resource_id", resource_id),
                ("operation", operation),
            )
        )
        return PolicyBinding(
            tenant_id=tenant_id,
            binding_id=binding_id,
            policy_set_id=policy_set_id,
            resource_type=resource_type,
            resource_id=resource_id,
            operation=operation,
            binding_digest=binding_digest,
        )
    except Exception:
        raise PolicyRegistryError("policy binding is invalid") from None


def _obligation_fields(
    obligations: tuple[PolicyObligation, ...],
) -> tuple[tuple[str, object], ...]:
    fields: list[tuple[str, object]] = [("obligation_count", len(obligations))]
    for index, obligation in enumerate(obligations):
        fields.append((f"obligation_{index}_kind", obligation.kind))
        fields.append((f"obligation_{index}_config_count", len(obligation.config)))
        for config_index, entry in enumerate(obligation.config):
            fields.extend(
                (
                    (f"obligation_{index}_config_{config_index}_name", entry.name),
                    (f"obligation_{index}_config_{config_index}_value", entry.value),
                )
            )
    return tuple(fields)


def _dependency_fields(
    dependencies: tuple[PolicyDependencyReference, ...],
) -> tuple[tuple[str, object], ...]:
    fields: list[tuple[str, object]] = [("dependency_count", len(dependencies))]
    for index, dependency in enumerate(dependencies):
        fields.extend(
            (
                (f"dependency_{index}_tenant_id", dependency.tenant_id),
                (f"dependency_{index}_kind", dependency.kind),
                (f"dependency_{index}_id", dependency.dependency_id),
                (f"dependency_{index}_revision", dependency.revision),
                (f"dependency_{index}_digest", dependency.digest),
            )
        )
    return tuple(fields)


def _draft_digest(
    *,
    tenant_id: str,
    policy_set_id: str,
    draft_id: str,
    revision: int,
    previous_revision_digest: str | None,
    state: DraftState,
    effect: PolicyEffect,
    canonical_ast_utf8: bytes,
    canonical_ast_sha256: str,
    obligations: tuple[PolicyObligation, ...],
    dependencies: tuple[PolicyDependencyReference, ...],
    author: str,
    created_at: datetime,
) -> str:
    return registry_digest(
        (
            ("record_type", "draft_revision"),
            ("tenant_id", tenant_id),
            ("policy_set_id", policy_set_id),
            ("draft_id", draft_id),
            ("revision", revision),
            (
                "previous_revision_digest",
                previous_revision_digest or "initial",
            ),
            ("state", state),
            ("effect", effect),
            ("canonical_ast_utf8", canonical_ast_utf8),
            ("canonical_ast_sha256", canonical_ast_sha256),
            *_obligation_fields(obligations),
            *_dependency_fields(dependencies),
            ("author", author),
            ("created_at", created_at),
        )
    )


def _validate_policy_content(
    *,
    tenant_id: str,
    effect: PolicyEffect,
    canonical_ast_utf8: bytes,
    canonical_ast_sha256: str,
    obligations: tuple[PolicyObligation, ...],
    dependencies: tuple[PolicyDependencyReference, ...],
) -> None:
    from .canonical import verify_canonical_policy_bytes

    verify_canonical_policy_bytes(canonical_ast_utf8, canonical_ast_sha256)
    if type(obligations) is not tuple or len(obligations) > 64:
        raise ValueError
    checked_obligations = tuple(
        validate_registry_model(obligation, PolicyObligation)
        for obligation in obligations
    )
    if len({obligation.kind for obligation in checked_obligations}) != len(
        checked_obligations
    ):
        raise ValueError
    if tuple(obligation.kind.value for obligation in checked_obligations) != tuple(
        sorted(obligation.kind.value for obligation in checked_obligations)
    ):
        raise ValueError
    if effect is PolicyEffect.OBLIGATION_IF:
        if not checked_obligations:
            raise ValueError
    elif checked_obligations:
        raise ValueError
    if type(dependencies) is not tuple or len(dependencies) > MAX_POLICY_DEPENDENCIES:
        raise ValueError
    checked_dependencies = tuple(
        validate_registry_model(dependency, PolicyDependencyReference)
        for dependency in dependencies
    )
    identities = tuple(
        (dependency.kind, dependency.dependency_id)
        for dependency in checked_dependencies
    )
    if len(identities) != len(set(identities)):
        raise ValueError
    if identities != tuple(
        sorted(identities, key=lambda item: (item[0].value, item[1]))
    ):
        raise ValueError
    if any(dependency.tenant_id != tenant_id for dependency in checked_dependencies):
        raise ValueError


class DraftRevision(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    draft_id: RegistryIdentifier
    revision: int = Field(ge=1)
    previous_revision_digest: FullDigest | None
    state: DraftState
    effect: PolicyEffect
    canonical_ast_utf8: bytes
    canonical_ast_sha256: FullDigest
    obligations: tuple[PolicyObligation, ...] = Field(default=(), max_length=64)
    dependencies: tuple[PolicyDependencyReference, ...] = Field(
        default=(),
        max_length=MAX_POLICY_DEPENDENCIES,
    )
    author: RegistryIdentifier
    created_at: UtcTimestamp
    draft_digest: FullDigest

    @model_validator(mode="after")
    def _validate_revision(self) -> DraftRevision:
        if (self.revision == 1) != (self.previous_revision_digest is None):
            raise ValueError("draft revision chain is invalid")
        _validate_policy_content(
            tenant_id=self.tenant_id,
            effect=self.effect,
            canonical_ast_utf8=self.canonical_ast_utf8,
            canonical_ast_sha256=self.canonical_ast_sha256,
            obligations=self.obligations,
            dependencies=self.dependencies,
        )
        expected = _draft_digest(
            tenant_id=self.tenant_id,
            policy_set_id=self.policy_set_id,
            draft_id=self.draft_id,
            revision=self.revision,
            previous_revision_digest=self.previous_revision_digest,
            state=self.state,
            effect=self.effect,
            canonical_ast_utf8=self.canonical_ast_utf8,
            canonical_ast_sha256=self.canonical_ast_sha256,
            obligations=self.obligations,
            dependencies=self.dependencies,
            author=self.author,
            created_at=self.created_at,
        )
        if expected != self.draft_digest:
            raise ValueError("draft digest does not match revision")
        return self


def _version_digest(
    *,
    tenant_id: str,
    policy_set_id: str,
    version: int,
    effect: PolicyEffect,
    canonical_ast_utf8: bytes,
    canonical_ast_sha256: str,
    obligations: tuple[PolicyObligation, ...],
    dependencies: tuple[PolicyDependencyReference, ...],
    source_draft_id: str,
    source_draft_revision: int,
    source_draft_digest: str,
    published_by: str,
    published_at: datetime,
) -> str:
    return registry_digest(
        (
            ("record_type", "policy_version"),
            ("tenant_id", tenant_id),
            ("policy_set_id", policy_set_id),
            ("version", version),
            ("effect", effect),
            ("canonical_ast_utf8", canonical_ast_utf8),
            ("canonical_ast_sha256", canonical_ast_sha256),
            *_obligation_fields(obligations),
            *_dependency_fields(dependencies),
            ("source_draft_id", source_draft_id),
            ("source_draft_revision", source_draft_revision),
            ("source_draft_digest", source_draft_digest),
            ("published_by", published_by),
            ("published_at", published_at),
        )
    )


class PolicyVersion(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    version: int = Field(ge=1)
    effect: PolicyEffect
    canonical_ast_utf8: bytes
    canonical_ast_sha256: FullDigest
    obligations: tuple[PolicyObligation, ...] = Field(default=(), max_length=64)
    dependencies: tuple[PolicyDependencyReference, ...] = Field(
        default=(),
        max_length=MAX_POLICY_DEPENDENCIES,
    )
    source_draft_id: RegistryIdentifier
    source_draft_revision: int = Field(ge=1)
    source_draft_digest: FullDigest
    published_by: RegistryIdentifier
    published_at: UtcTimestamp
    version_digest: FullDigest

    @model_validator(mode="after")
    def _validate_version(self) -> PolicyVersion:
        _validate_policy_content(
            tenant_id=self.tenant_id,
            effect=self.effect,
            canonical_ast_utf8=self.canonical_ast_utf8,
            canonical_ast_sha256=self.canonical_ast_sha256,
            obligations=self.obligations,
            dependencies=self.dependencies,
        )
        expected = _version_digest(
            tenant_id=self.tenant_id,
            policy_set_id=self.policy_set_id,
            version=self.version,
            effect=self.effect,
            canonical_ast_utf8=self.canonical_ast_utf8,
            canonical_ast_sha256=self.canonical_ast_sha256,
            obligations=self.obligations,
            dependencies=self.dependencies,
            source_draft_id=self.source_draft_id,
            source_draft_revision=self.source_draft_revision,
            source_draft_digest=self.source_draft_digest,
            published_by=self.published_by,
            published_at=self.published_at,
        )
        if expected != self.version_digest:
            raise ValueError("version digest does not match immutable fields")
        return self


def _eligibility_witness_digest(
    *,
    tenant_id: str,
    policy_set_id: str,
    version: int,
    version_digest: str,
    record_digest: str,
    published: bool,
    not_revoked: bool,
    registry_revision: int,
    issuer_identity: str,
) -> str:
    return registry_digest(
        (
            ("record_type", "policy_version_eligibility_witness"),
            ("tenant_id", tenant_id),
            ("policy_set_id", policy_set_id),
            ("version", version),
            ("version_digest", version_digest),
            ("record_digest", record_digest),
            ("published", published),
            ("not_revoked", not_revoked),
            ("registry_revision", registry_revision),
            ("issuer_identity", issuer_identity),
        )
    )


class PolicyVersionEligibilityWitness(_StrictFrozenRegistryModel):
    """Inert 0039 repository-attestation envelope.

    The model is immutable and fully bound, but construction never grants trust.
    F10A has no repository issuer or verifier, so every public authority-changing
    transition remains fail-closed until 0039 supplies the concrete DB boundary.
    """

    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    version: int = Field(ge=1)
    version_digest: FullDigest
    record_digest: FullDigest
    published: Literal[True]
    not_revoked: Literal[True]
    repository_witness: FullDigest
    registry_revision: int = Field(ge=0)
    issuer_identity: Literal["eios.authz.policy-registry.repository.0057"]

    @model_validator(mode="after")
    def _validate_repository_witness(self) -> PolicyVersionEligibilityWitness:
        expected = _eligibility_witness_digest(
            tenant_id=self.tenant_id,
            policy_set_id=self.policy_set_id,
            version=self.version,
            version_digest=self.version_digest,
            record_digest=self.record_digest,
            published=self.published,
            not_revoked=self.not_revoked,
            registry_revision=self.registry_revision,
            issuer_identity=self.issuer_identity,
        )
        if self.repository_witness != expected:
            raise ValueError("repository witness does not match eligibility fields")
        return self


class _PolicyRepositoryEligibilityVerifier(Protocol):
    _repository_issuer: object

    def _verify_repository_eligibility_witness(
        self,
        witness: PolicyVersionEligibilityWitness,
        *,
        head_digest: str,
    ) -> bool: ...


_POLICY_REPOSITORY_ISSUER = object()
_REPOSITORY_WITNESS_BINDINGS: dict[
    int,
    tuple[
        ReferenceType[PolicyVersionEligibilityWitness],
        ReferenceType[_PolicyRepositoryEligibilityVerifier],
        str,
    ],
] = {}


def _bind_repository_eligibility_witness(
    witness: PolicyVersionEligibilityWitness,
    verifier: _PolicyRepositoryEligibilityVerifier,
    *,
    head_digest: str,
) -> None:
    """Bind one exact DB-issued envelope to its exact trusted verifier instance."""
    if (
        type(witness) is not PolicyVersionEligibilityWitness
        or getattr(verifier, "_repository_issuer", None)
        is not _POLICY_REPOSITORY_ISSUER
        or type(head_digest) is not str
        or _FULL_DIGEST.fullmatch(head_digest) is None
    ):
        raise PolicyRegistryError("repository eligibility witness is invalid")
    identity = id(witness)

    def discard(_: object) -> None:
        _REPOSITORY_WITNESS_BINDINGS.pop(identity, None)

    _REPOSITORY_WITNESS_BINDINGS[identity] = (
        ref(witness, discard),
        ref(verifier, discard),
        head_digest,
    )


class PolicyPublication(_StrictFrozenRegistryModel):
    published_draft: DraftRevision
    version: PolicyVersion

    @model_validator(mode="after")
    def _validate_publication(self) -> PolicyPublication:
        draft = self.published_draft
        version = self.version
        if (
            draft.state is not DraftState.PUBLISHED
            or draft.previous_revision_digest is None
            or draft.tenant_id != version.tenant_id
            or draft.policy_set_id != version.policy_set_id
            or draft.draft_id != version.source_draft_id
            or draft.revision != version.source_draft_revision + 1
            or draft.previous_revision_digest != version.source_draft_digest
            or draft.effect is not version.effect
            or draft.canonical_ast_utf8 != version.canonical_ast_utf8
            or draft.canonical_ast_sha256 != version.canonical_ast_sha256
            or draft.obligations != version.obligations
            or draft.dependencies != version.dependencies
            or draft.author != version.published_by
            or draft.created_at != version.published_at
        ):
            raise ValueError("publication facts are inconsistent")
        return self


def _revocation_digest(
    *,
    tenant_id: str,
    policy_set_id: str,
    version: int,
    version_digest: str,
    reason: str,
    revoked_by: str,
    revoked_at: datetime,
) -> str:
    return registry_digest(
        (
            ("record_type", "policy_version_revocation"),
            ("tenant_id", tenant_id),
            ("policy_set_id", policy_set_id),
            ("version", version),
            ("version_digest", version_digest),
            ("reason", reason),
            ("revoked_by", revoked_by),
            ("revoked_at", revoked_at),
        )
    )


class PolicyVersionRevocation(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    version: int = Field(ge=1)
    version_digest: FullDigest
    reason: RegistryText
    revoked_by: RegistryIdentifier
    revoked_at: UtcTimestamp
    revocation_digest: FullDigest

    @model_validator(mode="after")
    def _validate_revocation(self) -> PolicyVersionRevocation:
        expected = _revocation_digest(
            tenant_id=self.tenant_id,
            policy_set_id=self.policy_set_id,
            version=self.version,
            version_digest=self.version_digest,
            reason=self.reason,
            revoked_by=self.revoked_by,
            revoked_at=self.revoked_at,
        )
        if expected != self.revocation_digest:
            raise ValueError("revocation digest does not match immutable fields")
        return self


def _binding_fields(
    bindings: tuple[PolicyBinding, ...],
) -> tuple[tuple[str, object], ...]:
    fields: list[tuple[str, object]] = [("binding_count", len(bindings))]
    for index, binding in enumerate(bindings):
        fields.extend(
            (
                (f"binding_{index}_tenant_id", binding.tenant_id),
                (f"binding_{index}_id", binding.binding_id),
                (f"binding_{index}_policy_set_id", binding.policy_set_id),
                (f"binding_{index}_resource_type", binding.resource_type),
                (f"binding_{index}_resource_id", binding.resource_id),
                (f"binding_{index}_operation", binding.operation),
                (f"binding_{index}_digest", binding.binding_digest),
            )
        )
    return tuple(fields)


def _activation_head_digest(
    *,
    tenant_id: str,
    policy_set_id: str,
    active_version: int | None,
    active_version_digest: str | None,
    bindings: tuple[PolicyBinding, ...],
    revision: int,
) -> str:
    return registry_digest(
        (
            ("record_type", "activation_head"),
            ("tenant_id", tenant_id),
            ("policy_set_id", policy_set_id),
            (
                "active_version",
                active_version if active_version is not None else "system_default_deny",
            ),
            (
                "active_version_digest",
                active_version_digest or "system_default_deny",
            ),
            *_binding_fields(bindings),
            ("revision", revision),
        )
    )


class ActivationHead(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    active_version: int | None = Field(default=None, ge=1)
    active_version_digest: FullDigest | None = None
    bindings: tuple[PolicyBinding, ...] = Field(
        default=(),
        max_length=MAX_POLICY_BINDINGS,
    )
    revision: int = Field(ge=0)
    head_digest: FullDigest

    @model_validator(mode="after")
    def _validate_head(self) -> ActivationHead:
        if (self.active_version is None) != (self.active_version_digest is None):
            raise ValueError("active version and digest must be present together")
        checked_bindings = tuple(
            validate_registry_model(binding, PolicyBinding) for binding in self.bindings
        )
        identities = tuple(binding.binding_id for binding in checked_bindings)
        if (
            len(identities) != len(set(identities))
            or identities != tuple(sorted(identities))
            or any(
                binding.tenant_id != self.tenant_id
                or binding.policy_set_id != self.policy_set_id
                for binding in checked_bindings
            )
        ):
            raise ValueError("activation bindings are invalid")
        expected = _activation_head_digest(
            tenant_id=self.tenant_id,
            policy_set_id=self.policy_set_id,
            active_version=self.active_version,
            active_version_digest=self.active_version_digest,
            bindings=self.bindings,
            revision=self.revision,
        )
        if expected != self.head_digest:
            raise ValueError("activation head digest does not match fields")
        return self

    @property
    def uses_system_default_deny(self) -> bool:
        return self.active_version is None


class ActivationRevisionWitness(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    expected_active_version: int | None = Field(default=None, ge=1)
    expected_revision: int = Field(ge=0)
    expected_head_digest: FullDigest


def _event_digest(
    *,
    tenant_id: str,
    policy_set_id: str,
    kind: ActivationEventKind,
    prior_version: int | None,
    prior_version_digest: str | None,
    new_version: int | None,
    new_version_digest: str | None,
    head_revision: int,
    actor: str,
    occurred_at: datetime,
) -> str:
    return registry_digest(
        (
            ("record_type", "activation_event"),
            ("tenant_id", tenant_id),
            ("policy_set_id", policy_set_id),
            ("kind", kind),
            ("prior_version", prior_version or "system_default_deny"),
            (
                "prior_version_digest",
                prior_version_digest or "system_default_deny",
            ),
            ("new_version", new_version or "system_default_deny"),
            ("new_version_digest", new_version_digest or "system_default_deny"),
            ("head_revision", head_revision),
            ("actor", actor),
            ("occurred_at", occurred_at),
        )
    )


class PolicyActivationEvent(_StrictFrozenRegistryModel):
    tenant_id: RegistryIdentifier
    policy_set_id: RegistryIdentifier
    kind: ActivationEventKind
    prior_version: int | None = Field(default=None, ge=1)
    prior_version_digest: FullDigest | None = None
    new_version: int | None = Field(default=None, ge=1)
    new_version_digest: FullDigest | None = None
    head_revision: int = Field(ge=1)
    actor: RegistryIdentifier
    occurred_at: UtcTimestamp
    event_digest: FullDigest

    @model_validator(mode="after")
    def _validate_event(self) -> PolicyActivationEvent:
        if (self.prior_version is None) != (self.prior_version_digest is None):
            raise ValueError("prior version event evidence is invalid")
        if (self.new_version is None) != (self.new_version_digest is None):
            raise ValueError("new version event evidence is invalid")
        if self.kind is ActivationEventKind.ACTIVATE and (
            self.new_version is None
            or (
                self.prior_version is not None
                and self.new_version <= self.prior_version
            )
        ):
            raise ValueError("activation event action is inconsistent")
        if self.kind is ActivationEventKind.ROLLBACK and (
            self.prior_version is None
            or self.new_version is None
            or self.new_version >= self.prior_version
        ):
            raise ValueError("rollback event action is inconsistent")
        if self.kind is ActivationEventKind.EMERGENCY_REVOKE and (
            self.prior_version is None or self.new_version == self.prior_version
        ):
            raise ValueError("emergency event action is inconsistent")
        expected = _event_digest(
            tenant_id=self.tenant_id,
            policy_set_id=self.policy_set_id,
            kind=self.kind,
            prior_version=self.prior_version,
            prior_version_digest=self.prior_version_digest,
            new_version=self.new_version,
            new_version_digest=self.new_version_digest,
            head_revision=self.head_revision,
            actor=self.actor,
            occurred_at=self.occurred_at,
        )
        if expected != self.event_digest:
            raise ValueError("activation event digest does not match fields")
        return self


class PolicyActivationTransition(_StrictFrozenRegistryModel):
    new_head: ActivationHead
    event: PolicyActivationEvent

    @model_validator(mode="after")
    def _validate_transition(self) -> PolicyActivationTransition:
        head = self.new_head
        event = self.event
        if (
            event.kind
            not in (ActivationEventKind.ACTIVATE, ActivationEventKind.ROLLBACK)
            or head.uses_system_default_deny
            or head.tenant_id != event.tenant_id
            or head.policy_set_id != event.policy_set_id
            or head.active_version != event.new_version
            or head.active_version_digest != event.new_version_digest
            or head.revision != event.head_revision
        ):
            raise ValueError("activation facts are inconsistent")
        return self


class PolicyEmergencyRevocation(_StrictFrozenRegistryModel):
    new_head: ActivationHead
    event: PolicyActivationEvent
    revocation: PolicyVersionRevocation

    @model_validator(mode="after")
    def _validate_emergency(self) -> PolicyEmergencyRevocation:
        head = self.new_head
        event = self.event
        revocation = self.revocation
        if (
            event.kind is not ActivationEventKind.EMERGENCY_REVOKE
            or head.tenant_id != event.tenant_id
            or head.policy_set_id != event.policy_set_id
            or head.active_version != event.new_version
            or head.active_version_digest != event.new_version_digest
            or head.revision != event.head_revision
            or revocation.tenant_id != event.tenant_id
            or revocation.policy_set_id != event.policy_set_id
            or revocation.version != event.prior_version
            or revocation.version_digest != event.prior_version_digest
            or revocation.revoked_by != event.actor
            or revocation.revoked_at != event.occurred_at
        ):
            raise ValueError("emergency facts are inconsistent")
        return self


RegistryModelT = TypeVar("RegistryModelT", bound=_StrictFrozenRegistryModel)


def validate_registry_model(
    value: object,
    expected_type: type[RegistryModelT],
) -> RegistryModelT:
    """Reject forged or mutated instances and return a defensive copy."""

    try:
        if type(value) is not expected_type or not value._construction_is_pristine():
            raise PolicyRegistryError("registry model is invalid")
        payload = value.model_dump(mode="python", round_trip=True)
        validated = expected_type.model_validate(payload, strict=True)
        if not validated._construction_is_pristine():
            raise PolicyRegistryError("registry model is invalid")
        return validated
    except PolicyRegistryError:
        raise
    except Exception:
        raise PolicyRegistryError("registry model is invalid") from None


def transition_policy_set_lifecycle(
    policy_set: PolicySet,
    *,
    expected_revision: int,
    lifecycle: PolicyLifecycle,
    updated_by: str,
    updated_at: datetime,
) -> PolicySetLifecycleTransition:
    """Apply one CAS-bound, adjacent and irreversible policy-set transition."""

    checked = validate_registry_model(policy_set, PolicySet)
    if type(expected_revision) is not int or expected_revision != checked.revision:
        raise PolicyRegistryError("policy set lifecycle CAS mismatch")
    allowed = {
        PolicyLifecycle.ACTIVE: PolicyLifecycle.DISABLED,
        PolicyLifecycle.DISABLED: PolicyLifecycle.RETIRED,
    }
    if (
        type(lifecycle) is not PolicyLifecycle
        or allowed.get(checked.lifecycle) is not lifecycle
    ):
        raise PolicyRegistryError("policy set lifecycle transition is invalid")
    try:
        next_policy_set = PolicySet(
            tenant_id=checked.tenant_id,
            policy_set_id=checked.policy_set_id,
            kind=checked.kind,
            lifecycle=lifecycle,
            revision=checked.revision + 1,
            created_by=checked.created_by,
            created_at=checked.created_at,
        )
        return PolicySetLifecycleTransition(
            previous_policy_set=checked,
            policy_set=next_policy_set,
            updated_by=updated_by,
            updated_at=updated_at,
        )
    except Exception:
        raise PolicyRegistryError(
            "policy set lifecycle transition is invalid"
        ) from None


def _require_repository_eligibility_witness(
    witness: object,
    version: PolicyVersion,
    *,
    registry_revision: int,
    head_digest: str,
) -> None:
    """Validate exact envelope binding and its live 0039 DB witness."""
    try:
        checked = validate_registry_model(
            witness,
            PolicyVersionEligibilityWitness,
        )
        if (
            checked.tenant_id != version.tenant_id
            or checked.policy_set_id != version.policy_set_id
            or checked.version != version.version
            or checked.version_digest != version.version_digest
            or checked.record_digest != version.version_digest
            or checked.registry_revision != registry_revision
        ):
            raise ValueError
    except Exception:
        raise PolicyRegistryError("repository eligibility witness is invalid") from None
    binding = _REPOSITORY_WITNESS_BINDINGS.get(id(witness))
    if binding is None:
        raise PolicyRegistryError("repository eligibility verifier is unavailable")
    bound_witness = binding[0]()
    verifier = binding[1]()
    bound_head_digest = binding[2]
    if (
        bound_witness is not witness
        or verifier is None
        or bound_head_digest != head_digest
        or getattr(verifier, "_repository_issuer", None)
        is not _POLICY_REPOSITORY_ISSUER
    ):
        raise PolicyRegistryError("repository eligibility witness is invalid")
    try:
        verified = verifier._verify_repository_eligibility_witness(
            checked,
            head_digest=bound_head_digest,
        )
    except Exception:
        raise PolicyRegistryError(
            "repository eligibility verifier is unavailable"
        ) from None
    if verified is not True:
        raise PolicyRegistryError("repository eligibility witness is invalid")


def _canonical_parts(canonical_ast: object) -> tuple[bytes, str]:
    from .canonical import CanonicalPolicyAst, verify_canonical_policy_bytes

    try:
        checked = validate_registry_model(canonical_ast, CanonicalPolicyAst)
        verify_canonical_policy_bytes(checked.utf8, checked.sha256)
        return checked.utf8, checked.sha256
    except Exception:
        raise PolicyRegistryError("canonical policy AST is invalid") from None


def create_draft_revision(
    *,
    tenant_id: str,
    policy_set_id: str,
    draft_id: str,
    effect: PolicyEffect,
    canonical_ast: object,
    obligations: tuple[PolicyObligation, ...],
    dependencies: tuple[PolicyDependencyReference, ...],
    author: str,
    created_at: datetime,
) -> DraftRevision:
    try:
        canonical_ast_utf8, canonical_ast_sha256 = _canonical_parts(canonical_ast)
        digest = _draft_digest(
            tenant_id=tenant_id,
            policy_set_id=policy_set_id,
            draft_id=draft_id,
            revision=1,
            previous_revision_digest=None,
            state=DraftState.DRAFT,
            effect=effect,
            canonical_ast_utf8=canonical_ast_utf8,
            canonical_ast_sha256=canonical_ast_sha256,
            obligations=obligations,
            dependencies=dependencies,
            author=author,
            created_at=created_at,
        )
        return DraftRevision(
            tenant_id=tenant_id,
            policy_set_id=policy_set_id,
            draft_id=draft_id,
            revision=1,
            previous_revision_digest=None,
            state=DraftState.DRAFT,
            effect=effect,
            canonical_ast_utf8=canonical_ast_utf8,
            canonical_ast_sha256=canonical_ast_sha256,
            obligations=obligations,
            dependencies=dependencies,
            author=author,
            created_at=created_at,
            draft_digest=digest,
        )
    except Exception:
        raise PolicyRegistryError("draft revision is invalid") from None


def _append_draft_revision(
    current: DraftRevision,
    *,
    expected_revision: int,
    expected_digest: str,
    canonical_ast_utf8: bytes,
    canonical_ast_sha256: str,
    effect: PolicyEffect,
    obligations: tuple[PolicyObligation, ...],
    dependencies: tuple[PolicyDependencyReference, ...],
    author: str,
    created_at: datetime,
    state: DraftState,
) -> DraftRevision:
    checked = validate_registry_model(current, DraftRevision)
    if checked.state is not DraftState.DRAFT:
        raise PolicyRegistryError("draft is terminal")
    try:
        _full_digest(expected_digest, "expected_digest")
    except Exception:
        raise PolicyRegistryError("draft CAS mismatch") from None
    if (
        type(expected_revision) is not int
        or expected_revision != checked.revision
        or expected_digest != checked.draft_digest
    ):
        raise PolicyRegistryError("draft CAS mismatch")
    revision = checked.revision + 1
    digest = _draft_digest(
        tenant_id=checked.tenant_id,
        policy_set_id=checked.policy_set_id,
        draft_id=checked.draft_id,
        revision=revision,
        previous_revision_digest=checked.draft_digest,
        state=state,
        effect=effect,
        canonical_ast_utf8=canonical_ast_utf8,
        canonical_ast_sha256=canonical_ast_sha256,
        obligations=obligations,
        dependencies=dependencies,
        author=author,
        created_at=created_at,
    )
    return DraftRevision(
        tenant_id=checked.tenant_id,
        policy_set_id=checked.policy_set_id,
        draft_id=checked.draft_id,
        revision=revision,
        previous_revision_digest=checked.draft_digest,
        state=state,
        effect=effect,
        canonical_ast_utf8=canonical_ast_utf8,
        canonical_ast_sha256=canonical_ast_sha256,
        obligations=obligations,
        dependencies=dependencies,
        author=author,
        created_at=created_at,
        draft_digest=digest,
    )


def append_draft_revision(
    current: DraftRevision,
    *,
    expected_revision: int,
    expected_digest: str,
    canonical_ast: object,
    effect: PolicyEffect,
    obligations: tuple[PolicyObligation, ...],
    dependencies: tuple[PolicyDependencyReference, ...],
    author: str,
    created_at: datetime,
) -> DraftRevision:
    canonical_ast_utf8, canonical_ast_sha256 = _canonical_parts(canonical_ast)
    try:
        return _append_draft_revision(
            current,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            canonical_ast_utf8=canonical_ast_utf8,
            canonical_ast_sha256=canonical_ast_sha256,
            effect=effect,
            obligations=obligations,
            dependencies=dependencies,
            author=author,
            created_at=created_at,
            state=DraftState.DRAFT,
        )
    except PolicyRegistryError:
        raise
    except Exception:
        raise PolicyRegistryError("draft revision is invalid") from None


def abandon_draft_revision(
    draft: DraftRevision,
    *,
    expected_revision: int,
    expected_digest: str,
    abandoned_by: str,
    abandoned_at: datetime,
) -> DraftRevision:
    try:
        checked = validate_registry_model(draft, DraftRevision)
        return _append_draft_revision(
            checked,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            canonical_ast_utf8=checked.canonical_ast_utf8,
            canonical_ast_sha256=checked.canonical_ast_sha256,
            effect=checked.effect,
            obligations=checked.obligations,
            dependencies=checked.dependencies,
            author=abandoned_by,
            created_at=abandoned_at,
            state=DraftState.ABANDONED,
        )
    except PolicyRegistryError as error:
        if str(error) in {"draft CAS mismatch", "draft is terminal"}:
            raise
        raise PolicyRegistryError("draft revision is invalid") from None
    except Exception:
        raise PolicyRegistryError("draft revision is invalid") from None


def publish_draft_revision(
    draft: DraftRevision,
    *,
    expected_revision: int,
    expected_digest: str,
    version: int,
    published_by: str,
    published_at: datetime,
) -> PolicyPublication:
    checked = validate_registry_model(draft, DraftRevision)
    try:
        published_draft = _append_draft_revision(
            checked,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            canonical_ast_utf8=checked.canonical_ast_utf8,
            canonical_ast_sha256=checked.canonical_ast_sha256,
            effect=checked.effect,
            obligations=checked.obligations,
            dependencies=checked.dependencies,
            author=published_by,
            created_at=published_at,
            state=DraftState.PUBLISHED,
        )
        digest = _version_digest(
            tenant_id=checked.tenant_id,
            policy_set_id=checked.policy_set_id,
            version=version,
            effect=checked.effect,
            canonical_ast_utf8=checked.canonical_ast_utf8,
            canonical_ast_sha256=checked.canonical_ast_sha256,
            obligations=checked.obligations,
            dependencies=checked.dependencies,
            source_draft_id=checked.draft_id,
            source_draft_revision=checked.revision,
            source_draft_digest=checked.draft_digest,
            published_by=published_by,
            published_at=published_at,
        )
        policy_version = PolicyVersion(
            tenant_id=checked.tenant_id,
            policy_set_id=checked.policy_set_id,
            version=version,
            effect=checked.effect,
            canonical_ast_utf8=checked.canonical_ast_utf8,
            canonical_ast_sha256=checked.canonical_ast_sha256,
            obligations=checked.obligations,
            dependencies=checked.dependencies,
            source_draft_id=checked.draft_id,
            source_draft_revision=checked.revision,
            source_draft_digest=checked.draft_digest,
            published_by=published_by,
            published_at=published_at,
            version_digest=digest,
        )
        return PolicyPublication(
            published_draft=published_draft,
            version=policy_version,
        )
    except PolicyRegistryError:
        raise
    except Exception:
        raise PolicyRegistryError("draft publication is invalid") from None


def _checked_revocations(
    tenant_id: str,
    policy_set_id: str,
    revocations: tuple[PolicyVersionRevocation, ...],
) -> tuple[PolicyVersionRevocation, ...]:
    if type(revocations) is not tuple or len(revocations) > 1_024:
        raise PolicyRegistryError("version revocations are invalid")
    try:
        checked = tuple(
            validate_registry_model(revocation, PolicyVersionRevocation)
            for revocation in revocations
        )
    except Exception:
        raise PolicyRegistryError("version revocations are invalid") from None
    identities = tuple(
        (revocation.version, revocation.version_digest) for revocation in checked
    )
    if len(identities) != len(set(identities)) or any(
        revocation.tenant_id != tenant_id or revocation.policy_set_id != policy_set_id
        for revocation in checked
    ):
        raise PolicyRegistryError("version revocations are invalid")
    return checked


def policy_version_state(
    version: PolicyVersion,
    revocations: tuple[PolicyVersionRevocation, ...] = (),
) -> VersionState:
    checked_version = validate_registry_model(version, PolicyVersion)
    checked_revocations = _checked_revocations(
        checked_version.tenant_id,
        checked_version.policy_set_id,
        revocations,
    )
    for revocation in checked_revocations:
        if revocation.version != checked_version.version:
            continue
        if revocation.version_digest != checked_version.version_digest:
            raise PolicyRegistryError("version revocations are invalid")
        return VersionState.REVOKED
    return VersionState.PUBLISHED


def create_activation_head(
    *,
    tenant_id: str,
    policy_set_id: str,
    bindings: tuple[PolicyBinding, ...],
) -> ActivationHead:
    try:
        digest = _activation_head_digest(
            tenant_id=tenant_id,
            policy_set_id=policy_set_id,
            active_version=None,
            active_version_digest=None,
            bindings=bindings,
            revision=0,
        )
        return ActivationHead(
            tenant_id=tenant_id,
            policy_set_id=policy_set_id,
            active_version=None,
            active_version_digest=None,
            bindings=bindings,
            revision=0,
            head_digest=digest,
        )
    except Exception:
        raise PolicyRegistryError("activation head is invalid") from None


def activation_revision_witness(head: ActivationHead) -> ActivationRevisionWitness:
    checked = validate_registry_model(head, ActivationHead)
    return ActivationRevisionWitness(
        tenant_id=checked.tenant_id,
        policy_set_id=checked.policy_set_id,
        expected_active_version=checked.active_version,
        expected_revision=checked.revision,
        expected_head_digest=checked.head_digest,
    )


def _check_activation_cas(
    head: ActivationHead,
    *,
    expected_active_version: int | None,
    expected_revision: int,
    expected_head_digest: str,
) -> ActivationHead:
    checked = validate_registry_model(head, ActivationHead)
    try:
        _full_digest(expected_head_digest, "expected_head_digest")
    except Exception:
        raise PolicyRegistryError("activation CAS mismatch") from None
    if (
        (
            expected_active_version is not None
            and type(expected_active_version) is not int
        )
        or type(expected_revision) is not int
        or checked.active_version != expected_active_version
        or checked.revision != expected_revision
        or checked.head_digest != expected_head_digest
    ):
        raise PolicyRegistryError("activation CAS mismatch")
    return checked


def _activation_event(
    *,
    prior: ActivationHead,
    new: ActivationHead,
    kind: ActivationEventKind,
    actor: str,
    occurred_at: datetime,
) -> PolicyActivationEvent:
    digest = _event_digest(
        tenant_id=prior.tenant_id,
        policy_set_id=prior.policy_set_id,
        kind=kind,
        prior_version=prior.active_version,
        prior_version_digest=prior.active_version_digest,
        new_version=new.active_version,
        new_version_digest=new.active_version_digest,
        head_revision=new.revision,
        actor=actor,
        occurred_at=occurred_at,
    )
    return PolicyActivationEvent(
        tenant_id=prior.tenant_id,
        policy_set_id=prior.policy_set_id,
        kind=kind,
        prior_version=prior.active_version,
        prior_version_digest=prior.active_version_digest,
        new_version=new.active_version,
        new_version_digest=new.active_version_digest,
        head_revision=new.revision,
        actor=actor,
        occurred_at=occurred_at,
        event_digest=digest,
    )


def _next_activation_head(
    current: ActivationHead,
    version: PolicyVersion | None,
) -> ActivationHead:
    active_version = version.version if version else None
    active_digest = version.version_digest if version else None
    revision = current.revision + 1
    values = {
        "tenant_id": current.tenant_id,
        "policy_set_id": current.policy_set_id,
        "active_version": active_version,
        "active_version_digest": active_digest,
        "bindings": current.bindings,
        "revision": revision,
    }
    return ActivationHead(
        **values,
        head_digest=_activation_head_digest(**values),
    )


def _transition_to_version_internal(
    head: ActivationHead,
    version: PolicyVersion,
    *,
    expected_active_version: int | None,
    expected_revision: int,
    expected_head_digest: str,
    activated_by: str,
    activated_at: datetime,
    event_kind: ActivationEventKind,
) -> PolicyActivationTransition:
    checked_head = _check_activation_cas(
        head,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
    )
    checked_version = validate_registry_model(version, PolicyVersion)
    if (
        checked_version.tenant_id != checked_head.tenant_id
        or checked_version.policy_set_id != checked_head.policy_set_id
    ):
        raise PolicyRegistryError("policy version is not eligible for activation")
    if event_kind is ActivationEventKind.ACTIVATE and (
        checked_head.active_version is not None
        and checked_version.version <= checked_head.active_version
    ):
        raise PolicyRegistryError("activation target must be newer")
    if event_kind is ActivationEventKind.ROLLBACK and (
        checked_head.active_version is None
        or checked_version.version >= checked_head.active_version
    ):
        raise PolicyRegistryError("rollback target must be older")
    new_head = _next_activation_head(checked_head, checked_version)
    return PolicyActivationTransition(
        new_head=new_head,
        event=_activation_event(
            prior=checked_head,
            new=new_head,
            kind=event_kind,
            actor=activated_by,
            occurred_at=activated_at,
        ),
    )


def activate_policy_version(
    head: ActivationHead,
    version: PolicyVersion,
    *,
    eligibility_witness: PolicyVersionEligibilityWitness,
    expected_active_version: int | None,
    expected_revision: int,
    expected_head_digest: str,
    activated_by: str,
    activated_at: datetime,
) -> PolicyActivationTransition:
    """Reserve activation for a 0039-verified repository witness; fail closed now."""
    checked_head = _check_activation_cas(
        head,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
    )
    checked_version = validate_registry_model(version, PolicyVersion)
    _require_repository_eligibility_witness(
        eligibility_witness,
        checked_version,
        registry_revision=checked_head.revision,
        head_digest=checked_head.head_digest,
    )
    return _transition_to_version_internal(
        head,
        version,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
        activated_by=activated_by,
        activated_at=activated_at,
        event_kind=ActivationEventKind.ACTIVATE,
    )


def rollback_policy_version(
    head: ActivationHead,
    version: PolicyVersion,
    *,
    eligibility_witness: PolicyVersionEligibilityWitness,
    expected_active_version: int | None,
    expected_revision: int,
    expected_head_digest: str,
    activated_by: str,
    activated_at: datetime,
) -> PolicyActivationTransition:
    """Reserve rollback for a 0039-verified repository witness; fail closed now."""
    checked_head = _check_activation_cas(
        head,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
    )
    checked_version = validate_registry_model(version, PolicyVersion)
    _require_repository_eligibility_witness(
        eligibility_witness,
        checked_version,
        registry_revision=checked_head.revision,
        head_digest=checked_head.head_digest,
    )
    return _transition_to_version_internal(
        head,
        version,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
        activated_by=activated_by,
        activated_at=activated_at,
        event_kind=ActivationEventKind.ROLLBACK,
    )


def _emergency_revoke_active_policy_internal(
    head: ActivationHead,
    active_version: PolicyVersion,
    *,
    expected_active_version: int,
    expected_revision: int,
    expected_head_digest: str,
    reason: str,
    revoked_by: str,
    revoked_at: datetime,
    replacement: PolicyVersion | None = None,
) -> PolicyEmergencyRevocation:
    checked_head = _check_activation_cas(
        head,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
    )
    checked_active = validate_registry_model(active_version, PolicyVersion)
    if (
        checked_active.tenant_id != checked_head.tenant_id
        or checked_active.policy_set_id != checked_head.policy_set_id
        or checked_active.version != checked_head.active_version
        or checked_active.version_digest != checked_head.active_version_digest
    ):
        raise PolicyRegistryError("active version evidence is invalid")
    checked_replacement: PolicyVersion | None = None
    if replacement is not None:
        try:
            candidate = validate_registry_model(replacement, PolicyVersion)
            if (
                candidate.tenant_id != checked_head.tenant_id
                or candidate.policy_set_id != checked_head.policy_set_id
                or candidate.version == checked_active.version
            ):
                raise ValueError
            checked_replacement = candidate
        except Exception:
            raise PolicyRegistryError("replacement is not eligible") from None
    revocation_digest = _revocation_digest(
        tenant_id=checked_active.tenant_id,
        policy_set_id=checked_active.policy_set_id,
        version=checked_active.version,
        version_digest=checked_active.version_digest,
        reason=reason,
        revoked_by=revoked_by,
        revoked_at=revoked_at,
    )
    revocation = PolicyVersionRevocation(
        tenant_id=checked_active.tenant_id,
        policy_set_id=checked_active.policy_set_id,
        version=checked_active.version,
        version_digest=checked_active.version_digest,
        reason=reason,
        revoked_by=revoked_by,
        revoked_at=revoked_at,
        revocation_digest=revocation_digest,
    )
    new_head = _next_activation_head(checked_head, checked_replacement)
    return PolicyEmergencyRevocation(
        new_head=new_head,
        event=_activation_event(
            prior=checked_head,
            new=new_head,
            kind=ActivationEventKind.EMERGENCY_REVOKE,
            actor=revoked_by,
            occurred_at=revoked_at,
        ),
        revocation=revocation,
    )


def emergency_revoke_active_policy(
    head: ActivationHead,
    active_version: PolicyVersion,
    *,
    eligibility_witness: PolicyVersionEligibilityWitness,
    expected_active_version: int,
    expected_revision: int,
    expected_head_digest: str,
    reason: str,
    revoked_by: str,
    revoked_at: datetime,
    replacement: PolicyVersion | None = None,
    replacement_eligibility_witness: PolicyVersionEligibilityWitness | None = None,
) -> PolicyEmergencyRevocation:
    """Reserve emergency mutation for 0039 repository verification; fail closed."""
    checked_head = _check_activation_cas(
        head,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
    )
    checked_active = validate_registry_model(active_version, PolicyVersion)
    _require_repository_eligibility_witness(
        eligibility_witness,
        checked_active,
        registry_revision=checked_head.revision,
        head_digest=checked_head.head_digest,
    )
    if replacement is None:
        if replacement_eligibility_witness is not None:
            raise PolicyRegistryError("replacement eligibility witness is invalid")
    else:
        checked_replacement = validate_registry_model(replacement, PolicyVersion)
        if replacement_eligibility_witness is None:
            raise PolicyRegistryError("replacement eligibility witness is required")
        _require_repository_eligibility_witness(
            replacement_eligibility_witness,
            checked_replacement,
            registry_revision=checked_head.revision,
            head_digest=checked_head.head_digest,
        )
    return _emergency_revoke_active_policy_internal(
        head,
        active_version,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
        reason=reason,
        revoked_by=revoked_by,
        revoked_at=revoked_at,
        replacement=replacement,
    )


def _validated_evaluation(value: object) -> PolicyEvaluationResult:
    try:
        if (
            type(value) is not PolicyEvaluationResult
            or not value._construction_is_pristine()
        ):
            raise ValueError
        validated = PolicyEvaluationResult.model_validate(
            value.model_dump(mode="python", round_trip=True),
            strict=True,
        )
        if not validated._construction_is_pristine():
            raise ValueError
        return validated
    except Exception:
        raise PolicyRegistryError("policy evaluation is invalid") from None


def _validated_obligations(
    effect: PolicyEffect,
    declared_obligations: tuple[PolicyObligation, ...],
) -> tuple[PolicyObligation, ...]:
    try:
        if type(declared_obligations) is not tuple or len(declared_obligations) > 64:
            raise ValueError
        validated = tuple(
            validate_registry_model(obligation, PolicyObligation)
            for obligation in declared_obligations
        )
        if len({obligation.kind for obligation in validated}) != len(validated):
            raise ValueError
        if effect is PolicyEffect.OBLIGATION_IF:
            if not validated:
                raise ValueError
        elif validated:
            raise ValueError
        return validated
    except Exception:
        raise PolicyRegistryError("obligations are invalid") from None


def apply_policy_effect(
    *,
    effect: PolicyEffect,
    evaluation: PolicyEvaluationResult,
    declared_obligations: tuple[PolicyObligation, ...] = (),
) -> PolicyEffectDecision:
    """Apply one fail-closed effect without manufacturing positive authority."""

    if type(effect) is not PolicyEffect:
        raise PolicyRegistryError("policy effect is invalid")
    checked = _validated_evaluation(evaluation)
    obligations = _validated_obligations(effect, declared_obligations)
    if checked.had_error:
        return PolicyEffectDecision(
            must_deny=True,
            reason_codes=(PolicyEffectReason.EVALUATION_ERROR,),
        )
    if effect is PolicyEffect.DENY_IF:
        return PolicyEffectDecision(
            must_deny=checked.matched,
            reason_codes=(PolicyEffectReason.DENY_MATCHED,) if checked.matched else (),
        )
    if effect is PolicyEffect.REQUIRE:
        return PolicyEffectDecision(
            must_deny=not checked.matched,
            reason_codes=(PolicyEffectReason.REQUIREMENT_UNSATISFIED,)
            if not checked.matched
            else (),
        )
    return PolicyEffectDecision(
        must_deny=False,
        obligations=obligations if checked.matched else (),
    )


__all__ = [
    "MAX_REGISTRY_DIGEST_INTEGER",
    "MIN_REGISTRY_DIGEST_INTEGER",
    "MAX_REGISTRY_DIGEST_CANONICAL_BYTES",
    "MAX_REGISTRY_DIGEST_FIELDS",
    "MAX_REGISTRY_DIGEST_VALUE_BYTES",
    "ActivationEventKind",
    "ActivationHead",
    "ActivationRevisionWitness",
    "DraftState",
    "DraftRevision",
    "ObligationConfigEntry",
    "PolicyActivationEvent",
    "PolicyActivationTransition",
    "PolicyBinding",
    "PolicyDependencyKind",
    "PolicyDependencyReference",
    "PolicyEffect",
    "PolicyEffectDecision",
    "PolicyEffectReason",
    "PolicyEmergencyRevocation",
    "PolicyKind",
    "PolicyLifecycle",
    "PolicyObligation",
    "PolicyObligationKind",
    "PolicyPublication",
    "PolicyRegistryError",
    "PolicySet",
    "PolicySetLifecycleTransition",
    "PolicyVersion",
    "PolicyVersionEligibilityWitness",
    "PolicyVersionRevocation",
    "VersionState",
    "activate_policy_version",
    "activation_revision_witness",
    "abandon_draft_revision",
    "append_draft_revision",
    "apply_policy_effect",
    "create_activation_head",
    "create_draft_revision",
    "create_policy_binding",
    "emergency_revoke_active_policy",
    "policy_version_state",
    "publish_draft_revision",
    "registry_digest",
    "rollback_policy_version",
    "transition_policy_set_lifecycle",
    "validate_registry_model",
]

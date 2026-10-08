from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from json import dumps
import math
import re
from typing import Any, Generic, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .context import AuthorizationContext
from .errors import (
    AuthorizationError,
    AuthorizationUnavailable,
    AuthorizationValidationError,
)
from .operations import Operation
from .resources import ResourceLifecycle, ResourceReference


MAX_GROUP_NESTING_DEPTH = 16
MAX_PAGE_SIZE = 100
MAX_IDENTIFIER_BYTES = 255
MAX_DISPLAY_NAME_BYTES = 255
MAX_ATTRIBUTE_JSON_BYTES = 16_384
MAX_ATTRIBUTE_STRING_BYTES = 255
MAX_ATTRIBUTE_STRING_SET_ITEMS = 100
SENSITIVE_ATTRIBUTE_REDACTION = "<redacted>"
_CANONICAL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_CANONICAL_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,254}$")
_CANONICAL_RESOURCE_STABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~{}-]*$")
_CANONICAL_RESOURCE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")
_CANONICAL_ROUTE_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FROZEN_RESOURCE_TYPES = frozenset(
    {
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
        "identity_user",
        "external_effect",
        # NexLoop instance/property/Relation scope vocabulary.
        "object",
        "property",
        "relation",
    }
)


class GrantSubjectKind(str, Enum):
    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"
    GROUP = "group"


class GrantStatus(str, Enum):
    """Task 7 lifecycle after the atomic pending-resource migration."""

    ACTIVE = "active"
    REVOKED = "revoked"


class AttributeSource(str, Enum):
    OIDC = "oidc"
    SCIM = "scim"
    LOCAL_ADMIN = "local_admin"


class AttributeValueType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_SET = "string_set"


class GrantDomainError(AuthorizationValidationError):
    code = "grant_invalid"


class GrantConflict(AuthorizationError):
    code = "grant_conflict"


class GrantNotFound(AuthorizationError):
    code = "grant_not_found"


class GrantUnavailable(AuthorizationUnavailable):
    code = "grant_unavailable"


class GrantCycleError(GrantDomainError):
    code = "group_cycle"


class GroupNestingDepthError(GrantDomainError):
    code = "group_nesting_depth_exceeded"


class CrossTenantReferenceError(GrantDomainError):
    code = "cross_tenant_reference"


def effective_grant_subject_kind(value: object) -> GrantSubjectKind:
    """Return an exact principal kind accepted by effective-grant reads."""

    if type(value) is not GrantSubjectKind or value is GrantSubjectKind.GROUP:
        raise GrantDomainError("effective grant subject kind is invalid")
    return value


StrictModel = TypeVar("StrictModel", bound=BaseModel)


def strict_model_copy(
    model_type: type[StrictModel],
    value: object,
    *,
    error_message: str,
) -> StrictModel:
    """Revalidate exact Pydantic instances and return an unaliased copy."""

    if type(value) is not model_type:
        raise GrantDomainError(error_message)
    try:
        return model_type.model_validate(value, strict=True)
    except Exception:
        raise GrantDomainError(error_message) from None


def _canonical_identifier(value: str, field_name: str) -> str:
    if (
        len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES
        or _CANONICAL_IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a canonical bounded identifier")
    return value


def _attribute_name(value: str, field_name: str) -> str:
    if (
        len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES
        or _CANONICAL_ATTRIBUTE_NAME.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a canonical bounded attribute name")
    return value


def _canonical_resource_id(value: str, resource_type: str) -> str:
    prefix = f"eios:{resource_type}:"
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES or not value.startswith(
        prefix
    ):
        raise ValueError(
            "resource_id must be an exact canonical eios resource identifier"
        )
    suffix = value[len(prefix) :]
    if suffix.count(":") > 1:
        raise ValueError("resource_id allows at most one version separator")
    stable, separator, version = suffix.partition(":")
    if not stable or _CANONICAL_RESOURCE_STABLE.fullmatch(stable) is None:
        raise ValueError("resource_id must contain a canonical stable name")
    if separator and not version:
        raise ValueError("resource_id version must be non-empty")
    if version and (not version[0].isascii() or not version[0].isalnum()):
        raise ValueError("resource_id version must start with an alphanumeric")
    if version and _CANONICAL_RESOURCE_VERSION.fullmatch(version) is None:
        raise ValueError("resource_id version must be canonical")
    _validate_resource_braces(stable)
    return value


def _validate_resource_braces(stable: str) -> None:
    cursor = 0
    while cursor < len(stable):
        char = stable[cursor]
        if char == "}":
            raise ValueError("resource_id contains an unmatched route brace")
        if char != "{":
            cursor += 1
            continue
        closing = stable.find("}", cursor + 1)
        if closing == -1:
            raise ValueError("resource_id contains an unmatched route brace")
        parameter = stable[cursor + 1 : closing]
        if _CANONICAL_ROUTE_PARAMETER.fullmatch(parameter) is None:
            raise ValueError("resource_id contains an invalid route parameter")
        cursor = closing + 1


def _display_name(value: str, field_name: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-blank text")
    return value


def _aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )


class PendingResourceKey(_StrictFrozenModel):
    """Strict Task 6 placeholder for the Task 7 ResourceReference ABI."""

    tenant_id: str
    resource_type: str
    resource_id: str

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _canonical_identifier(value, "tenant_id")

    @field_validator("resource_type")
    @classmethod
    def _validate_resource_type(cls, value: str) -> str:
        if value not in _FROZEN_RESOURCE_TYPES:
            raise ValueError("resource_type must use the frozen resource vocabulary")
        return value

    @field_validator("resource_id")
    @classmethod
    def _validate_resource_id(cls, value: str, info: Any) -> str:
        resource_type = info.data.get("resource_type")
        if not isinstance(resource_type, str):
            raise ValueError("resource_id requires a valid resource_type")
        return _canonical_resource_id(value, resource_type)


class TrustedGrantOperator(_StrictFrozenModel):
    """Caller claim plus the database-session operator for grant mutations."""

    tenant_id: str
    claimed_actor_principal_id: str
    operator_principal_id: str
    request_id: str
    trace_id: str

    @field_validator(
        "tenant_id",
        "claimed_actor_principal_id",
        "operator_principal_id",
        "request_id",
        "trace_id",
    )
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _canonical_identifier(value, info.field_name)


class Group(_StrictFrozenModel):
    tenant_id: str
    group_id: str
    display_name: str
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "group_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _canonical_identifier(value, info.field_name)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return _display_name(value, "display_name")


class GroupMembership(_StrictFrozenModel):
    tenant_id: str
    group_id: str
    member_kind: GrantSubjectKind
    member_id: str
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "group_id", "member_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _canonical_identifier(value, info.field_name)


class Role(_StrictFrozenModel):
    tenant_id: str
    role_id: str
    display_name: str
    operations: frozenset[Operation]
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "role_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _canonical_identifier(value, info.field_name)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return _display_name(value, "display_name")


class RoleGrant(_StrictFrozenModel):
    tenant_id: str
    grant_id: str
    subject_kind: GrantSubjectKind
    subject_id: str
    role_id: str
    resource: ResourceReference
    valid_from: datetime
    valid_until: datetime | None
    inherits: bool
    status: GrantStatus = GrantStatus.ACTIVE
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "grant_id", "subject_id", "role_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _canonical_identifier(value, info.field_name)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _validate_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_binding(self) -> RoleGrant:
        if self.resource.tenant_id != self.tenant_id:
            raise ValueError("resource and grant must reference the same tenant")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class EffectiveRoleGrant(RoleGrant):
    """Grant returned by the trusted effective-read path with current facts."""

    tenant_status: str
    resource_lifecycle: ResourceLifecycle
    evaluated_resource_security_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_effective_evidence(self) -> EffectiveRoleGrant:
        if self.status is not GrantStatus.ACTIVE:
            raise ValueError("effective grant must be active")
        if self.tenant_status != "active":
            raise ValueError("effective grant tenant must be active")
        if self.resource_lifecycle is not ResourceLifecycle.ACTIVE:
            raise ValueError("effective grant resource must be active")
        if self.evaluated_resource_security_revision != self.resource.security_revision:
            raise ValueError("effective grant resource revision is stale")
        return self

    def is_effective_at(self, at: datetime) -> bool:
        """Recheck the validity interval of an already trusted effective row."""

        checked = strict_model_copy(
            EffectiveRoleGrant,
            self,
            error_message="effective role grant is invalid",
        )
        checked_at = _aware_utc(at, "at")
        assert checked_at is not None
        return (
            checked.status is GrantStatus.ACTIVE
            and checked.valid_from <= checked_at
            and (checked.valid_until is None or checked_at < checked.valid_until)
        )


class AttributeSchema(_StrictFrozenModel):
    tenant_id: str
    name: str
    value_type: AttributeValueType
    source: AttributeSource
    sensitive: bool
    revision: int = Field(ge=1)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _canonical_identifier(value, "tenant_id")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _attribute_name(value, "name")

    def assert_matches(self, attribute: SubjectAttribute) -> None:
        checked_schema = strict_model_copy(
            AttributeSchema,
            self,
            error_message="attribute schema is invalid",
        )
        checked_attribute = strict_model_copy(
            SubjectAttribute,
            attribute,
            error_message="subject attribute is invalid",
        )
        expected = (
            checked_schema.tenant_id,
            checked_schema.name,
            checked_schema.value_type,
            checked_schema.source,
            checked_schema.sensitive,
            checked_schema.revision,
        )
        actual = (
            checked_attribute.tenant_id,
            checked_attribute.name,
            checked_attribute.value_type,
            checked_attribute.source,
            checked_attribute.sensitive,
            checked_attribute.schema_revision,
        )
        if actual != expected:
            raise ValueError("subject attribute does not match its registered schema")


TrustedAttributeValue = str | int | float | bool | frozenset[str]


class SubjectAttribute(_StrictFrozenModel):
    """Trusted value model whose default serialization redacts sensitive values.

    Domain and persistence code may read ``value`` directly after strict
    revalidation. Logging and transport code must use Pydantic serialization,
    which emits the fixed redaction marker when ``sensitive`` is true.
    """

    tenant_id: str
    subject_principal_id: str
    name: str
    value_type: AttributeValueType
    source: AttributeSource
    sensitive: bool
    value: TrustedAttributeValue = Field(repr=False)
    schema_revision: int = Field(ge=1)
    valid_from: datetime
    valid_until: datetime | None
    revision: int = Field(ge=1)

    @field_validator("tenant_id", "subject_principal_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _canonical_identifier(value, info.field_name)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _attribute_name(value, "name")

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _validate_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_value_and_interval(self) -> SubjectAttribute:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if not _value_matches_type(self.value_type, self.value):
            raise ValueError("value does not match value_type")
        if self.value_type is AttributeValueType.STRING:
            if len(self.value.encode("utf-8")) > MAX_ATTRIBUTE_STRING_BYTES:
                raise ValueError("string value exceeds the UTF-8 byte limit")
        if isinstance(self.value, frozenset):
            if len(self.value) > MAX_ATTRIBUTE_STRING_SET_ITEMS:
                raise ValueError("string_set contains too many items")
            for item in self.value:
                if len(item.encode("utf-8")) > MAX_ATTRIBUTE_STRING_BYTES:
                    raise ValueError("string_set item exceeds the UTF-8 byte limit")
                _display_name(item, "value")
        if _attribute_json_size(self.value_type, self.value) > MAX_ATTRIBUTE_JSON_BYTES:
            raise ValueError("value exceeds the canonical JSON byte limit")
        return self

    @field_serializer("value")
    def _serialize_value(self, value: TrustedAttributeValue) -> object:
        if self.sensitive:
            return SENSITIVE_ATTRIBUTE_REDACTION
        return value


def _value_matches_type(value_type: AttributeValueType, value: object) -> bool:
    if value_type is AttributeValueType.STRING:
        return type(value) is str
    if value_type is AttributeValueType.INTEGER:
        return type(value) is int
    if value_type is AttributeValueType.NUMBER:
        return type(value) in (int, float) and (
            type(value) is int or math.isfinite(value)
        )
    if value_type is AttributeValueType.BOOLEAN:
        return type(value) is bool
    return type(value) is frozenset and all(type(item) is str for item in value)


def _attribute_json_size(
    value_type: AttributeValueType,
    value: TrustedAttributeValue,
) -> int:
    json_value: TrustedAttributeValue | list[str] = value
    if value_type is AttributeValueType.STRING_SET:
        assert isinstance(value, frozenset)
        json_value = sorted(value)
    canonical = dumps(
        json_value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(canonical.encode("utf-8"))


class PageRequest(_StrictFrozenModel):
    """Unbounded collection contract: limit applies to one page, not the total."""

    limit: int = Field(gt=0, le=MAX_PAGE_SIZE)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return _canonical_identifier(value, "cursor")
        except ValueError:
            pass
        member_kind, separator, member_id = value.partition(":")
        if separator and member_kind in {kind.value for kind in GrantSubjectKind}:
            return f"{member_kind}:{_canonical_identifier(member_id, 'cursor')}"
        return _canonical_identifier(value, "cursor")


PageItem = TypeVar("PageItem")


class CollectionPage(_StrictFrozenModel, Generic[PageItem]):
    items: tuple[PageItem, ...]
    next_cursor: str | None


class GroupDirectory:
    """Framework-free group graph used to enforce Task 6 graph invariants."""

    __slots__ = ("_groups", "_members", "_tenant_id")

    def __init__(self, *, tenant_id: str) -> None:
        self._tenant_id = _canonical_identifier(tenant_id, "tenant_id")
        self._groups: dict[str, Group] = {}
        self._members: dict[str, set[str]] = {}

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def add_group(self, group: Group) -> None:
        checked_group = strict_model_copy(
            Group,
            group,
            error_message="group is invalid",
        )
        if checked_group.tenant_id != self.tenant_id:
            raise CrossTenantReferenceError("group belongs to another tenant")
        if checked_group.group_id in self._groups:
            raise ValueError("group already exists")
        self._groups[checked_group.group_id] = checked_group
        self._members[checked_group.group_id] = set()

    def add_group_member(self, parent_group_id: str, member_group_id: str) -> None:
        parent = _canonical_identifier(parent_group_id, "parent_group_id")
        member = _canonical_identifier(member_group_id, "member_group_id")
        self._require_group(parent)
        self._require_group(member)
        if parent == member or self._reachable(member, parent):
            raise GrantCycleError("nested group membership would create a cycle")
        if member in self._members[parent]:
            return
        if self._depth_with_edge(parent, member) > MAX_GROUP_NESTING_DEPTH:
            raise GroupNestingDepthError("nested group membership exceeds depth 16")
        self._members[parent].add(member)

    def remove_group_member(self, parent_group_id: str, member_group_id: str) -> None:
        parent = _canonical_identifier(parent_group_id, "parent_group_id")
        member = _canonical_identifier(member_group_id, "member_group_id")
        self._require_group(parent)
        self._require_group(member)
        if member not in self._members[parent]:
            raise ValueError("unknown group membership")
        self._members[parent].remove(member)

    def groups(self, request: PageRequest) -> CollectionPage[Group]:
        checked_request = strict_model_copy(
            PageRequest,
            request,
            error_message="grant page is invalid",
        )
        groups = tuple(
            strict_model_copy(
                Group,
                self._groups[group_id],
                error_message="stored group is invalid",
            )
            for group_id in sorted(self._groups)
        )
        return _page(groups, checked_request)

    def group_members(
        self, group_id: str, request: PageRequest
    ) -> CollectionPage[Group]:
        group_id = _canonical_identifier(group_id, "group_id")
        self._require_group(group_id)
        checked_request = strict_model_copy(
            PageRequest,
            request,
            error_message="grant page is invalid",
        )
        members = tuple(
            strict_model_copy(
                Group,
                self._groups[member_id],
                error_message="stored group is invalid",
            )
            for member_id in sorted(self._members[group_id])
        )
        return _page(members, checked_request)

    def _require_group(self, group_id: str) -> Group:
        try:
            return self._groups[group_id]
        except KeyError:
            raise ValueError("unknown group reference") from None

    def _reachable(self, start: str, target: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self._members[current])
        return False

    def _depth_with_edge(self, parent: str, member: str) -> int:
        memo: dict[str, int] = {}

        def depth(group_id: str) -> int:
            if group_id in memo:
                return memo[group_id]
            children = self._members[group_id]
            if group_id == parent:
                children = children | {member}
            value = 0 if not children else 1 + max(depth(child) for child in children)
            memo[group_id] = value
            return value

        return max((depth(group_id) for group_id in self._groups), default=0)


def _page(items: Sequence[PageItem], request: PageRequest) -> CollectionPage[PageItem]:
    keys = tuple(_page_item_key(item) for item in items)
    start = 0 if request.cursor is None else _keyset_start(keys, request.cursor)
    stop = min(start + request.limit, len(items))
    page_items = tuple(items[start:stop])
    next_cursor = _page_item_key(page_items[-1]) if stop < len(items) else None
    return CollectionPage(items=page_items, next_cursor=next_cursor)


def _keyset_start(keys: tuple[str, ...], cursor: str) -> int:
    low = 0
    high = len(keys)
    while low < high:
        middle = (low + high) // 2
        if keys[middle] <= cursor:
            low = middle + 1
        else:
            high = middle
    return low


def _page_item_key(item: PageItem) -> str:
    if not isinstance(item, Group):
        raise TypeError("group page contains an unsupported item")
    return item.group_id


def effective_grants(
    grants: Iterable[EffectiveRoleGrant],
    *,
    at: datetime,
    inherited: bool = False,
    cache_input: bool = False,
) -> tuple[EffectiveRoleGrant, ...]:
    """Filter trusted effective-read rows by time and inheritance context."""

    del cache_input
    checked_at = _aware_utc(at, "at")
    assert checked_at is not None
    checked_grants = tuple(
        strict_model_copy(
            EffectiveRoleGrant,
            grant,
            error_message="effective role grant is invalid",
        )
        for grant in grants
    )
    return tuple(
        grant
        for grant in checked_grants
        if grant.is_effective_at(checked_at) and (not inherited or grant.inherits)
    )


def effective_operations(
    role: Role,
    grant: EffectiveRoleGrant,
    *,
    at: datetime,
) -> frozenset[Operation]:
    checked_role = strict_model_copy(Role, role, error_message="role is invalid")
    checked_grant = strict_model_copy(
        EffectiveRoleGrant,
        grant,
        error_message="effective role grant is invalid",
    )
    if checked_role.tenant_id != checked_grant.tenant_id:
        raise CrossTenantReferenceError("role and grant belong to different tenants")
    if checked_role.role_id != checked_grant.role_id:
        raise ValueError("role_id does not match grant")
    return (
        frozenset(checked_role.operations)
        if checked_grant.is_effective_at(at)
        else frozenset()
    )


def trusted_attribute(
    context: AuthorizationContext,
    request: Mapping[str, object],
    name: str,
) -> object | None:
    """Read only server-built context facts; request bodies are never a source."""

    if not isinstance(request, Mapping):
        raise GrantDomainError("trusted attribute request is invalid")
    checked_context = strict_model_copy(
        AuthorizationContext,
        context,
        error_message="authorization context is invalid",
    )
    try:
        checked_name = _attribute_name(name, "name")
    except (TypeError, ValueError):
        raise GrantDomainError("trusted attribute name is invalid") from None
    return _defensive_attribute_copy(checked_context.attributes.get(checked_name))


def _defensive_attribute_copy(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _defensive_attribute_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_defensive_attribute_copy(item) for item in value)
    return value


__all__ = [
    "MAX_GROUP_NESTING_DEPTH",
    "MAX_PAGE_SIZE",
    "MAX_IDENTIFIER_BYTES",
    "MAX_DISPLAY_NAME_BYTES",
    "MAX_ATTRIBUTE_JSON_BYTES",
    "MAX_ATTRIBUTE_STRING_BYTES",
    "MAX_ATTRIBUTE_STRING_SET_ITEMS",
    "SENSITIVE_ATTRIBUTE_REDACTION",
    "AttributeSchema",
    "AttributeSource",
    "AttributeValueType",
    "CollectionPage",
    "CrossTenantReferenceError",
    "EffectiveRoleGrant",
    "GrantCycleError",
    "GrantConflict",
    "GrantDomainError",
    "GrantNotFound",
    "GrantStatus",
    "GrantSubjectKind",
    "GrantUnavailable",
    "Group",
    "GroupDirectory",
    "GroupMembership",
    "GroupNestingDepthError",
    "PageRequest",
    "PendingResourceKey",
    "Role",
    "RoleGrant",
    "SubjectAttribute",
    "TrustedGrantOperator",
    "effective_grants",
    "effective_grant_subject_kind",
    "effective_operations",
    "trusted_attribute",
    "strict_model_copy",
]

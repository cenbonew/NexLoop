from __future__ import annotations

import base64
from collections.abc import Iterable
from enum import Enum
from hmac import compare_digest, digest
from json import dumps
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import from_json

from .errors import AuthorizationValidationError


MAX_RESOURCE_PARENT_DEPTH = 32
MAX_RESOURCE_DEPENDENCY_DEPTH = 32

_MAX_IDENTIFIER_LENGTH = 255
_MAX_CURSOR_LENGTH = 1024
_MAX_CURSOR_PAYLOAD_BYTES = 768
_CANONICAL_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}")
_CANONICAL_RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~:{}-]{0,254}")
_CANONICAL_STABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~{}-]*")
_CANONICAL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*")
_CANONICAL_PLACEHOLDER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_FIELDS = frozenset({"tenant", "query", "last_type", "last_id"})


class ResourceType(str, Enum):
    TENANT = "tenant"
    PROJECT = "project"
    FOLDER = "folder"
    DATASET = "dataset"
    CONNECTOR = "connector"
    OBJECT = "object"
    PROPERTY = "property"
    RELATION = "relation"
    OBJECT_TYPE = "object_type"
    LINK_TYPE = "link_type"
    INTERFACE = "interface"
    OBJECT_VIEW = "object_view"
    OBJECT_SET = "object_set"
    FUNCTION = "function"
    ACTION = "action"
    CAPABILITY = "capability"
    API_OPERATION = "api_operation"
    ARTIFACT = "artifact"
    AGENT = "agent"
    APPLICATION = "application"
    IDENTITY_USER = "identity_user"
    EXTERNAL_EFFECT = "external_effect"


class ResourceLifecycle(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class ResourceError(AuthorizationValidationError):
    code = "resource_invalid"


class ResourceAlreadyRegisteredError(ResourceError):
    code = "resource_already_registered"


class ResourceNotFoundError(ResourceError):
    code = "resource_not_found"


class ResourceRevisionMismatchError(ResourceError):
    code = "resource_revision_mismatch"


class CrossTenantResourceError(ResourceError):
    code = "resource_cross_tenant"


class ResourceCycleError(ResourceError):
    code = "resource_cycle"


class ResourceDepthError(ResourceError):
    code = "resource_depth_exceeded"


class ResourceVisibilityError(ResourceError):
    code = "resource_not_visible"


class ResourceUnavailableError(ResourceError):
    code = "resource_unavailable"


class ResourceCursorError(ResourceError):
    code = "resource_cursor_invalid"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )


class ResourceReference(_StrictFrozenModel):
    tenant_id: str
    resource_id: str
    resource_type: ResourceType
    security_revision: int = Field(ge=1)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _tenant_id(value)

    @field_validator("resource_id")
    @classmethod
    def _validate_resource_id(cls, value: str) -> str:
        if _CANONICAL_RESOURCE_ID.fullmatch(value) is None:
            raise ValueError("resource_id must be a canonical bounded identifier")
        return value

    @model_validator(mode="after")
    def _validate_type_prefix(self) -> ResourceReference:
        prefix = f"eios:{self.resource_type.value}:"
        if not self.resource_id.startswith(prefix) or len(self.resource_id) == len(
            prefix
        ):
            raise ValueError("resource_id must use its resource_type canonical prefix")
        suffix = self.resource_id[len(prefix) :]
        if suffix.count(":") > 1:
            raise ValueError("resource_id must contain at most one version separator")
        stable_name, separator, version = suffix.partition(":")
        try:
            _stable_name(stable_name)
            if separator:
                _version(version)
        except (TypeError, ValueError) as error:
            raise ValueError("resource_id suffix is not canonical") from error
        if (
            resource_id(
                self.resource_type,
                stable_name,
                version if separator else None,
            )
            != self.resource_id
        ):
            raise ValueError("resource_id suffix is not canonical")
        return self


class ResourcePageRequest(_StrictFrozenModel):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or len(value) > _MAX_CURSOR_LENGTH:
            raise ValueError("cursor must be non-empty and bounded")
        return value


class ResourcePage(_StrictFrozenModel):
    items: tuple[ResourceReference, ...]
    next_cursor: str | None


class PublicResourceProjection(_StrictFrozenModel):
    resource: ResourceReference
    parent: ResourceReference | None
    dependencies: tuple[ResourceReference, ...]


ResourceKey = tuple[str, str, str]
ResourceSortKey = tuple[str, str]


class _IndexNode:
    __slots__ = ("height", "key", "left", "right")

    def __init__(self, key: ResourceSortKey) -> None:
        self.key = key
        self.left: _IndexNode | None = None
        self.right: _IndexNode | None = None
        self.height = 1


class _SortedResourceIndex:
    __slots__ = ("_root", "_size")

    def __init__(self) -> None:
        self._root: _IndexNode | None = None
        self._size = 0

    def add(self, key: ResourceSortKey) -> None:
        self._root = _index_insert(self._root, key)
        self._size += 1

    def page_after(
        self,
        after: ResourceSortKey | None,
        *,
        limit: int,
    ) -> tuple[ResourceSortKey, ...]:
        result: list[ResourceSortKey] = []

        def collect(node: _IndexNode | None) -> None:
            if node is None or len(result) >= limit:
                return
            if after is not None and node.key <= after:
                collect(node.right)
                return
            collect(node.left)
            if len(result) < limit:
                result.append(node.key)
            collect(node.right)

        collect(self._root)
        return tuple(result)

    def __len__(self) -> int:
        return self._size


def resource_id(
    kind: ResourceType,
    stable_name: str,
    version: str | int | None = None,
) -> str:
    """Build the sole canonical resource identifier form."""

    if type(kind) is not ResourceType:
        raise TypeError("kind must be a ResourceType")
    stable = _stable_name(stable_name)
    if version is None:
        suffix = stable
    elif type(version) is int:
        if version < 0:
            raise ValueError("version must be non-negative")
        suffix = f"{stable}:{version}"
    elif type(version) is str:
        suffix = f"{stable}:{_version(version)}"
    else:
        raise TypeError("version must be a string, integer, or None")
    result = f"eios:{kind.value}:{suffix}"
    if (
        len(result) > _MAX_IDENTIFIER_LENGTH
        or _CANONICAL_RESOURCE_ID.fullmatch(result) is None
    ):
        raise ValueError("resource_id must be a canonical bounded identifier")
    return result


class _ResourceCursorSigner:
    __slots__ = ("__key",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes:
            raise TypeError("cursor_signing_key must be bytes")
        if len(key) < 32:
            raise ValueError("cursor_signing_key must contain at least 32 bytes")
        self.__key = bytes(key)

    def sign(self, signing_input: bytes) -> str:
        return _encode_base64url(digest(self.__key, signing_input, "sha256"))

    def verify(self, signing_input: bytes, encoded_tag: str) -> bool:
        try:
            supplied = _decode_base64url(encoded_tag, maximum_bytes=32)
        except ValueError:
            return False
        expected = digest(self.__key, signing_input, "sha256")
        return len(supplied) == 32 and compare_digest(supplied, expected)

    def __repr__(self) -> str:
        return "_ResourceCursorSigner(<redacted>)"


class ResourceRegistry:
    """Fail-closed in-memory resource graph domain contract.

    Migration 0037 persists the resource graph and active resource-bound grants
    in PostgreSQL. This class remains the in-memory graph implementation and has
    no persistence or active-grant semantics. Migrations 0038 and 0039 are not
    delivered; Application/Agent ceilings and policy evaluation therefore remain
    outside this class. Candidate mutations validate before publishing their
    edges, so a rejected edge cannot leave a partial graph.
    """

    def __init__(self, *, cursor_signing_key: bytes) -> None:
        self._cursor_signer = _ResourceCursorSigner(cursor_signing_key)
        self._resources: dict[ResourceKey, ResourceReference] = {}
        self._parents: dict[ResourceKey, ResourceKey] = {}
        self._children: dict[ResourceKey, set[ResourceKey]] = {}
        self._dependencies: dict[ResourceKey, set[ResourceKey]] = {}
        self._dependents: dict[ResourceKey, set[ResourceKey]] = {}
        self._dependency_depths: dict[ResourceKey, int] = {}
        self._tenant_index: dict[str, _SortedResourceIndex] = {}
        self._tenant_type_index: dict[tuple[str, str], _SortedResourceIndex] = {}

    def register(
        self,
        resource: ResourceReference,
        *,
        parent: ResourceReference | None = None,
    ) -> None:
        checked = _reference(resource)
        key = _key(checked)
        if key in self._resources:
            raise ResourceAlreadyRegisteredError("resource is already registered")

        parent_key: ResourceKey | None = None
        if parent is not None:
            checked_parent = self._current(parent)
            _same_tenant(checked, checked_parent)
            parent_key = _key(checked_parent)
            if _parent_depth(parent_key, self._parents) + 1 > MAX_RESOURCE_PARENT_DEPTH:
                raise ResourceDepthError("parent graph exceeds maximum depth")

        self._resources[key] = checked
        self._children[key] = set()
        self._dependencies[key] = set()
        self._dependents[key] = set()
        self._dependency_depths[key] = 0
        sort_key = _sort_key(checked)
        tenant_index = self._tenant_index.get(checked.tenant_id)
        if tenant_index is None:
            tenant_index = _SortedResourceIndex()
            self._tenant_index[checked.tenant_id] = tenant_index
        tenant_index.add(sort_key)
        typed_index_key = (checked.tenant_id, checked.resource_type.value)
        typed_index = self._tenant_type_index.get(typed_index_key)
        if typed_index is None:
            typed_index = _SortedResourceIndex()
            self._tenant_type_index[typed_index_key] = typed_index
        typed_index.add(sort_key)
        if parent_key is not None:
            self._parents[key] = parent_key
            self._children[parent_key].add(key)

    def reparent(
        self,
        resource: ResourceReference,
        *,
        parent: ResourceReference | None,
    ) -> None:
        checked = self._current(resource)
        key = _key(checked)
        parent_key: ResourceKey | None = None
        new_parent_depth = 0
        if parent is not None:
            checked_parent = self._current(parent)
            _same_tenant(checked, checked_parent)
            parent_key = _key(checked_parent)
            current = parent_key
            visited: set[ResourceKey] = set()
            while True:
                if current == key:
                    raise ResourceCycleError("parent graph contains a cycle")
                if current in visited:
                    raise ResourceCycleError("parent graph contains a cycle")
                visited.add(current)
                if current not in self._parents:
                    break
                current = self._parents[current]
            new_parent_depth = len(visited)

        descendant_depth = _longest_path(key, self._children)
        if new_parent_depth + descendant_depth > MAX_RESOURCE_PARENT_DEPTH:
            raise ResourceDepthError("parent graph exceeds maximum depth")

        old_parent = self._parents.get(key)
        if old_parent == parent_key:
            return
        if old_parent is not None:
            self._children[old_parent].remove(key)
            del self._parents[key]
        if parent_key is not None:
            self._parents[key] = parent_key
            self._children[parent_key].add(key)

    def bind_dependency(
        self,
        resource: ResourceReference,
        dependency: ResourceReference,
    ) -> None:
        checked = self._current(resource)
        checked_dependency = self._current(dependency)
        _same_tenant(checked, checked_dependency)
        source_key = _key(checked)
        dependency_key = _key(checked_dependency)
        if dependency_key in self._dependencies[source_key]:
            return
        updates = _dependency_depth_updates(
            source_key,
            dependency_key,
            1 + self._dependency_depths[dependency_key],
            dependents=self._dependents,
            depths=self._dependency_depths,
        )

        self._dependencies[source_key].add(dependency_key)
        self._dependents[dependency_key].add(source_key)
        self._dependency_depths.update(updates)

    def parent_of(self, resource: ResourceReference) -> ResourceReference | None:
        checked = self._current(resource)
        parent = self._parents.get(_key(checked))
        return None if parent is None else _reference(self._resources[parent])

    def dependencies_of(
        self, resource: ResourceReference
    ) -> tuple[ResourceReference, ...]:
        checked = self._current(resource)
        return tuple(
            _reference(self._resources[key])
            for key in sorted(self._dependencies[_key(checked)], key=_sort_key_from_key)
        )

    def ancestors(self, resource: ResourceReference) -> tuple[ResourceReference, ...]:
        current = _key(self._current(resource))
        result: list[ResourceReference] = []
        visited = {current}
        while current in self._parents:
            current = self._parents[current]
            if current in visited:
                raise ResourceCycleError("parent graph contains a cycle")
            visited.add(current)
            result.append(_reference(self._resources[current]))
            if len(result) > MAX_RESOURCE_PARENT_DEPTH:
                raise ResourceDepthError("parent graph exceeds maximum depth")
        return tuple(result)

    def dependency_closure(
        self, resource: ResourceReference
    ) -> tuple[ResourceReference, ...]:
        root = _key(self._current(resource))
        result: list[ResourceReference] = []
        visited = {root}

        def visit(key: ResourceKey, depth: int, path: frozenset[ResourceKey]) -> None:
            if depth > MAX_RESOURCE_DEPENDENCY_DEPTH:
                raise ResourceDepthError("dependency graph exceeds maximum depth")
            for dependency in sorted(self._dependencies[key], key=_sort_key_from_key):
                if dependency in path:
                    raise ResourceCycleError("dependency graph contains a cycle")
                if dependency not in visited:
                    visited.add(dependency)
                    result.append(_reference(self._resources[dependency]))
                    visit(dependency, depth + 1, path | {dependency})

        visit(root, 0, frozenset({root}))
        return tuple(result)

    def public_projection(
        self,
        resource: ResourceReference,
        *,
        visible: Iterable[ResourceReference],
    ) -> PublicResourceProjection:
        try:
            root = self._current(resource)
            visible_keys: set[ResourceKey] = set()
            for item in visible:
                checked_visible = self._current(item)
                _same_tenant(root, checked_visible)
                visible_keys.add(_key(checked_visible))
            root_key = _key(root)
            if root_key not in visible_keys:
                raise ResourceVisibilityError("resource is not visible")
        except (
            CrossTenantResourceError,
            ResourceNotFoundError,
            ResourceRevisionMismatchError,
            ResourceVisibilityError,
        ):
            raise ResourceVisibilityError("resource is not visible") from None

        visible_dependencies: list[ResourceReference] = []
        visited = {root_key}

        def visit(key: ResourceKey, depth: int) -> None:
            if depth > MAX_RESOURCE_DEPENDENCY_DEPTH:
                raise ResourceDepthError("dependency graph exceeds maximum depth")
            for dependency in sorted(self._dependencies[key], key=_sort_key_from_key):
                if dependency not in visible_keys or dependency in visited:
                    continue
                visited.add(dependency)
                visible_dependencies.append(_reference(self._resources[dependency]))
                visit(dependency, depth + 1)

        visit(root_key, 0)
        parent_key = self._parents.get(root_key)
        parent = (
            _reference(self._resources[parent_key])
            if parent_key is not None and parent_key in visible_keys
            else None
        )
        return PublicResourceProjection(
            resource=_reference(root),
            parent=parent,
            dependencies=tuple(visible_dependencies),
        )

    def list_resources(
        self,
        *,
        tenant_id: str,
        page: ResourcePageRequest,
        resource_type: ResourceType | None = None,
    ) -> ResourcePage:
        tenant = _tenant_id(tenant_id)
        checked_page = _page_request(page)
        if resource_type is not None and type(resource_type) is not ResourceType:
            raise TypeError("resource_type must be a ResourceType or None")

        index = (
            self._tenant_index.get(tenant)
            if resource_type is None
            else self._tenant_type_index.get((tenant, resource_type.value))
        )
        after: ResourceSortKey | None = None
        if checked_page.cursor is not None:
            after = _decode_cursor(
                checked_page.cursor,
                tenant_id=tenant,
                resource_type=resource_type,
                signer=self._cursor_signer,
            )
        window = (
            ()
            if index is None
            else index.page_after(after, limit=checked_page.limit + 1)
        )
        page_keys = window[: checked_page.limit]
        items = tuple(
            _reference(self._resources[(tenant, item_type, item_id)])
            for item_type, item_id in page_keys
        )
        next_cursor = None
        if len(window) > checked_page.limit:
            next_cursor = _encode_cursor(
                page_keys[-1],
                tenant_id=tenant,
                resource_type=resource_type,
                signer=self._cursor_signer,
            )
        return ResourcePage(items=items, next_cursor=next_cursor)

    def _current(self, resource: ResourceReference) -> ResourceReference:
        checked = _reference(resource)
        current = self._resources.get(_key(checked))
        if current is None:
            raise ResourceNotFoundError("resource is not registered")
        current = _reference(current)
        if current != checked:
            raise ResourceRevisionMismatchError("resource security revision is stale")
        return current


def _reference(value: ResourceReference) -> ResourceReference:
    if type(value) is not ResourceReference:
        raise TypeError("resource must be a ResourceReference")
    try:
        values = vars(value)
        extra = getattr(value, "__pydantic_extra__", None)
        private = getattr(value, "__pydantic_private__", None)
        if set(values) != {
            "tenant_id",
            "resource_id",
            "resource_type",
            "security_revision",
        }:
            raise ValueError
        if extra not in (None, {}) or private not in (None, {}):
            raise ValueError
        return ResourceReference.model_validate(dict(values), strict=True)
    except Exception:
        raise ResourceError("resource reference is invalid") from None


def _page_request(value: ResourcePageRequest) -> ResourcePageRequest:
    if type(value) is not ResourcePageRequest:
        raise TypeError("page must be a ResourcePageRequest")
    try:
        values = vars(value)
        extra = getattr(value, "__pydantic_extra__", None)
        private = getattr(value, "__pydantic_private__", None)
        if set(values) != {"limit", "cursor"}:
            raise ValueError
        if extra not in (None, {}) or private not in (None, {}):
            raise ValueError
        return ResourcePageRequest.model_validate(dict(values), strict=True)
    except Exception:
        raise ResourceError("resource page request is invalid") from None


def _tenant_id(value: str) -> str:
    if type(value) is not str or _CANONICAL_TENANT_ID.fullmatch(value) is None:
        raise ValueError("tenant_id must be a canonical bounded identifier")
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


def _version(value: str) -> str:
    if type(value) is not str or _CANONICAL_VERSION.fullmatch(value) is None:
        raise ValueError("version must be a canonical non-empty identifier part")
    return value


def _key(resource: ResourceReference) -> ResourceKey:
    return (
        resource.tenant_id,
        resource.resource_type.value,
        resource.resource_id,
    )


def _sort_key(resource: ResourceReference) -> ResourceSortKey:
    return resource.resource_type.value, resource.resource_id


def _sort_key_from_key(key: ResourceKey) -> ResourceSortKey:
    return key[1], key[2]


def _index_height(node: _IndexNode | None) -> int:
    return 0 if node is None else node.height


def _update_index_height(node: _IndexNode) -> None:
    node.height = 1 + max(_index_height(node.left), _index_height(node.right))


def _rotate_index_left(node: _IndexNode) -> _IndexNode:
    pivot = node.right
    if pivot is None:
        raise ResourceError("resource index rotation is invalid")
    node.right = pivot.left
    pivot.left = node
    _update_index_height(node)
    _update_index_height(pivot)
    return pivot


def _rotate_index_right(node: _IndexNode) -> _IndexNode:
    pivot = node.left
    if pivot is None:
        raise ResourceError("resource index rotation is invalid")
    node.left = pivot.right
    pivot.right = node
    _update_index_height(node)
    _update_index_height(pivot)
    return pivot


def _index_insert(
    node: _IndexNode | None,
    key: ResourceSortKey,
) -> _IndexNode:
    if node is None:
        return _IndexNode(key)
    if key < node.key:
        node.left = _index_insert(node.left, key)
    elif key > node.key:
        node.right = _index_insert(node.right, key)
    else:
        raise ResourceAlreadyRegisteredError("resource index key already exists")

    _update_index_height(node)
    balance = _index_height(node.left) - _index_height(node.right)
    if balance > 1:
        if node.left is None:
            raise ResourceError("resource index balance is invalid")
        if key > node.left.key:
            node.left = _rotate_index_left(node.left)
        return _rotate_index_right(node)
    if balance < -1:
        if node.right is None:
            raise ResourceError("resource index balance is invalid")
        if key < node.right.key:
            node.right = _rotate_index_right(node.right)
        return _rotate_index_left(node)
    return node


def _same_tenant(left: ResourceReference, right: ResourceReference) -> None:
    if left.tenant_id != right.tenant_id:
        raise CrossTenantResourceError("resource edges must stay in one tenant")


def _parent_depth(
    resource: ResourceKey,
    parents: dict[ResourceKey, ResourceKey],
) -> int:
    current = resource
    visited = {current}
    depth = 0
    while current in parents:
        current = parents[current]
        if current in visited:
            raise ResourceCycleError("parent graph contains a cycle")
        visited.add(current)
        depth += 1
        if depth > MAX_RESOURCE_PARENT_DEPTH:
            raise ResourceDepthError("parent graph exceeds maximum depth")
    return depth


def _dependency_depth_updates(
    source: ResourceKey,
    dependency: ResourceKey,
    proposed_depth: int,
    *,
    dependents: dict[ResourceKey, set[ResourceKey]],
    depths: dict[ResourceKey, int],
) -> dict[ResourceKey, int]:
    updates: dict[ResourceKey, int] = {}
    pending = [(source, proposed_depth)]
    while pending:
        current, candidate = pending.pop()
        effective = updates.get(current, depths[current])
        if candidate <= effective:
            continue
        if current == dependency:
            raise ResourceCycleError("dependency graph contains a cycle")
        updates[current] = candidate
        pending.extend((dependent, candidate + 1) for dependent in dependents[current])
    if any(depth > MAX_RESOURCE_DEPENDENCY_DEPTH for depth in updates.values()):
        raise ResourceDepthError("dependency graph exceeds maximum depth")
    return updates


def _longest_path(
    start: ResourceKey,
    edges: dict[ResourceKey, set[ResourceKey]],
) -> int:
    visiting: set[ResourceKey] = set()
    memo: dict[ResourceKey, int] = {}

    def visit(current: ResourceKey) -> int:
        if current in visiting:
            raise ResourceCycleError("resource graph contains a cycle")
        if current in memo:
            return memo[current]
        visiting.add(current)
        depth = max((1 + visit(child) for child in edges[current]), default=0)
        visiting.remove(current)
        memo[current] = depth
        return depth

    return visit(start)


def _query_name(resource_type: ResourceType | None) -> str:
    return "*" if resource_type is None else resource_type.value


def _encode_cursor(
    last: ResourceSortKey,
    *,
    tenant_id: str,
    resource_type: ResourceType | None,
    signer: _ResourceCursorSigner,
) -> str:
    payload = {
        "last_id": last[1],
        "last_type": last[0],
        "query": _query_name(resource_type),
        "tenant": tenant_id,
    }
    raw = dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    encoded = _encode_base64url(raw)
    signing_input = f"v1.{encoded}".encode("ascii")
    cursor = f"v1.{encoded}.{signer.sign(signing_input)}"
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise ResourceCursorError("cursor is too large")
    return cursor


def _decode_cursor(
    cursor: str,
    *,
    tenant_id: str,
    resource_type: ResourceType | None,
    signer: _ResourceCursorSigner,
) -> ResourceSortKey:
    try:
        parts = cursor.split(".")
        if len(parts) != 3:
            raise ValueError
        version, encoded, encoded_tag = parts
        if version != "v1" or _BASE64URL.fullmatch(encoded) is None:
            raise ValueError
        signing_input = f"v1.{encoded}".encode("ascii")
        if not signer.verify(signing_input, encoded_tag):
            raise ValueError
        raw = _decode_base64url(
            encoded,
            maximum_bytes=_MAX_CURSOR_PAYLOAD_BYTES,
        )
        payload = from_json(raw, allow_partial=False)
        if not isinstance(payload, dict) or frozenset(payload) != _CURSOR_FIELDS:
            raise ValueError
        if not all(type(payload[field]) is str for field in _CURSOR_FIELDS):
            raise ValueError
        canonical = _encode_cursor(
            (payload["last_type"], payload["last_id"]),
            tenant_id=payload["tenant"],
            resource_type=(
                None if payload["query"] == "*" else ResourceType(payload["query"])
            ),
            signer=signer,
        )
        if canonical != cursor:
            raise ValueError
        last_type = ResourceType(payload["last_type"])
        if _CANONICAL_RESOURCE_ID.fullmatch(payload["last_id"]) is None:
            raise ValueError
        if not payload["last_id"].startswith(f"eios:{last_type.value}:"):
            raise ValueError
        ResourceReference(
            tenant_id=payload["tenant"],
            resource_id=payload["last_id"],
            resource_type=last_type,
            security_revision=1,
        )
    except (UnicodeDecodeError, ValueError):
        raise ResourceCursorError("cursor is invalid") from None
    if payload["tenant"] != tenant_id:
        raise ResourceCursorError("cursor belongs to another tenant")
    if payload["query"] != _query_name(resource_type):
        raise ResourceCursorError("cursor belongs to another query")
    return last_type.value, payload["last_id"]


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str, *, maximum_bytes: int) -> bytes:
    if _BASE64URL.fullmatch(value) is None:
        raise ValueError("base64url value is invalid")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
    if len(decoded) > maximum_bytes or _encode_base64url(decoded) != value:
        raise ValueError("base64url value is invalid")
    return decoded


__all__ = [
    "MAX_RESOURCE_DEPENDENCY_DEPTH",
    "MAX_RESOURCE_PARENT_DEPTH",
    "CrossTenantResourceError",
    "PublicResourceProjection",
    "ResourceAlreadyRegisteredError",
    "ResourceCursorError",
    "ResourceCycleError",
    "ResourceDepthError",
    "ResourceError",
    "ResourceLifecycle",
    "ResourceNotFoundError",
    "ResourcePage",
    "ResourcePageRequest",
    "ResourceReference",
    "ResourceRegistry",
    "ResourceRevisionMismatchError",
    "ResourceType",
    "ResourceUnavailableError",
    "ResourceVisibilityError",
    "resource_id",
]

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import core_schema


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
_DIGEST_NAMESPACE = "nex-eios.definition.v1"
_MAX_JSON_DEPTH = 64
_LIFECYCLE_FIELDS = frozenset(
    {
        "tenant_id",
        "version",
        "status",
        "contract_digest",
        "created_by",
        "created_at",
        "previous_version",
        "source_lineage",
    }
)

# Additive digest fields: non-lifecycle fields introduced after the frozen
# `nex-eios.definition.v1` contract. To keep every already-published
# contract_digest stable (hydration recomputes and rejects on mismatch), an
# additive field contributes to the canonical payload ONLY when its value
# differs from the field default. A field equal to its default is omitted, so a
# definition that does not use the new feature keeps the exact digest it had
# before the field existed; a definition that does use it gets a new, correct
# digest that survives the create -> store -> hydrate round trip because the
# non-default value persists. Registration is (class name, field name) — a
# field name like "input_schema" can be frozen-v1 on one Definition class and
# additive on another. Every field added by the ontology-depth plan MUST be
# registered here — never bump `_DIGEST_NAMESPACE`, which would rewrite all
# published digests, and never add a field outside this set without a default.
_DIGEST_ADDITIVE_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        # Stage 4: parameterized Action input contract.
        ("ActionDefinition", "input_schema"),
        ("ActionDefinition", "parameters"),
        # Stage 7: Object Set aggregations.
        ("ObjectSetDefinition", "aggregations"),
        # Stage 12-C: Object Set semantic-search node.
        ("ObjectSetDefinition", "semantic"),
        # Stage 9: marking floor on every Definition type.
        ("FunctionDefinition", "required_markings"),
        ("ActionDefinition", "required_markings"),
        ("ObjectSetDefinition", "required_markings"),
        ("ObjectViewDefinition", "required_markings"),
        ("InterfaceDefinition", "required_markings"),
    }
)


class FrozenJsonList(tuple[Any, ...]):
    """Tuple-compatible JSON sequence with explicit immutable mutator errors."""

    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("frozen JSON list cannot be mutated")

    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __delitem__(self, _key: object) -> None:
        self._immutable()

    def __setitem__(self, _key: object, _value: object) -> None:
        self._immutable()


class FrozenJsonMap(Mapping[str, Any]):
    """Copied immutable mapping used for JSON contract fragments."""

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_items", MappingProxyType(dict(items)))

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: object,
        _handler: object,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            lambda value: _freeze_json_map(value, "JSON mapping"),
            serialization=core_schema.plain_serializer_function_ser_schema(
                _thaw_json,
                when_used="json",
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: object,
        _handler: object,
    ) -> dict[str, object]:
        return {"type": "object", "additionalProperties": True}

    def __getitem__(self, key: str) -> Any:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenJsonMap({self._items!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return NotImplemented

    def __copy__(self) -> FrozenJsonMap:
        return self

    def __deepcopy__(self, _memo: object) -> FrozenJsonMap:
        return self

    def __setattr__(self, _name: str, _value: object) -> None:
        self._immutable()

    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("frozen JSON mapping cannot be mutated")

    __delitem__ = _immutable
    __setitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def thaw_json(value: object) -> object:
    """把冻结过的 JSON 片段还原成纯 dict / list，**供跨模块使用的公共出口**。

    🔴 2026-09-18 立它的原因(生产 P1):
    `FrozenJsonMap` 自带 pydantic serializer,但 API 投影的返回类型是 `dict[str, Any]`
    —— **`Any` 不会走到那个 serializer** ⇒ pydantic 退回通用序列化 ⇒
    `PydanticSerializationError: Unable to serialize unknown type: FrozenJsonMap`。
    而投影里写的是 `dict(run.args)`,**只解最外层**:嵌套的值(生产上是 `source_origin`)
    原样留在响应里 ⇒ `/api/orchestration/runs` 500。
    (09-11 出现第一条带嵌套的 run,09-12 起该端点就开始 500,而它**时好时坏** ——
     取决于 limit 窗口里有没有毒行,所以一周里一直被当成偶发。)

    ⚠️ 为什么加这个名字,而不是让调用方 import `_thaw_json`:
    仓里此前有**三份**各自实现的解冻函数(本模块 / identity/models.py / runtime/runs/models.py),
    且**两种实现**(判 `Mapping` vs 判 `FrozenJsonMap`)、**无跨模块复用先例**。
    再写第四份会让「哪一份是对的」彻底无解;而 import 私有名没有先例、也不该有。
    ⇒ 这里**只加一个薄委托**:函数体不重写,`_thaw_json` 与它现有的全部调用点**逐字不变**。
    **本改动可以被描述成「只新增,不修改」。**

    📌 已验(2026-09-18,改代码之前):`_thaw_json` 是**递归**的 ——
    深嵌套 + 列表内嵌的样本冻结后有 7 处 `FrozenJsonMap`,解冻后 0 处。
    (这一步不能靠"它是 serializer 用的那份所以理应递归"——同一个模块里
     `FrozenJsonMap.__init__` 就**不**递归,递归的是 `_freeze_json`。**别假设冻结与解冻对称。**)
    """

    return _thaw_json(value)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def _non_blank(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must be non-empty")
    return clean


def _stable_name(value: str, field_name: str) -> str:
    clean = _non_blank(value, field_name)
    if not _STABLE_NAME_RE.fullmatch(clean):
        raise ValueError(f"{field_name} must be a stable dotted name")
    return clean


def _freeze_json(
    value: object,
    *,
    path: str = "JSON",
    active_ids: frozenset[int] = frozenset(),
    depth: int = 0,
) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{path} exceeds maximum JSON nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError(f"{path} contains a cyclic JSON reference")
        child_ids = active_ids | {identity}
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be JSON strings")
            frozen[key] = _freeze_json(
                item,
                path=f"{path}.{key}",
                active_ids=child_ids,
                depth=depth + 1,
            )
        return FrozenJsonMap(frozen)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_ids:
            raise ValueError(f"{path} contains a cyclic JSON reference")
        child_ids = active_ids | {identity}
        return FrozenJsonList(
            _freeze_json(
                item,
                path=f"{path}[]",
                active_ids=child_ids,
                depth=depth + 1,
            )
            for item in value
        )
    raise ValueError(f"{path} contains a non-JSON value")


def _freeze_json_map(value: object, field_name: str) -> FrozenJsonMap:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    frozen = _freeze_json(value, path=f"JSON {field_name}")
    if not isinstance(frozen, FrozenJsonMap):  # pragma: no cover - defensive
        raise ValueError(f"{field_name} must be a JSON object")
    return frozen


# ---------------------------------------------------------------------------
# Display-vocabulary validation for ObjectView display_hints and Interface
# channel_mappings. Known keys are validated strictly; unknown keys pass
# through untouched — both fields are part of frozen published digests, so the
# vocabulary can only reject malformed values, never rewrite or extend stored
# payloads. Normalization for UIs happens in the resolved read projection.
# ---------------------------------------------------------------------------

_HINT_CONTROLS = frozenset(
    {"text", "number", "date", "select", "checkbox", "textarea", "json"}
)
_HINT_FORMATS = frozenset({"plain", "email", "url", "currency", "percent"})
_HINT_SORT_DIRECTIONS = frozenset({"asc", "desc"})
_HINT_ACTION_PLACEMENTS = frozenset({"primary", "secondary", "menu"})


def _hint_stable_name(value: object, path: str) -> None:
    if not isinstance(value, str) or not _STABLE_NAME_RE.fullmatch(value):
        raise ValueError(f"{path} must be a stable name")


def _hint_label(value: object, path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")


def _hint_token_map(value: object, allowed: frozenset[str], path: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object")
    for key, item in value.items():
        _hint_stable_name(key, f"{path} key")
        # isinstance guard first: frozen JSON objects are unhashable, so a bare
        # membership test would raise TypeError, which pydantic does not
        # convert into a ValidationError.
        if not isinstance(item, str) or item not in allowed:
            raise ValueError(f"{path}.{key} must be one of {sorted(allowed)}")


def _validate_display_hints(hints: Mapping[str, Any]) -> None:
    for key in ("title_property", "subtitle_property"):
        if key in hints:
            _hint_stable_name(hints[key], f"display_hints.{key}")
    if "property_groups" in hints:
        groups = hints["property_groups"]
        if not isinstance(groups, (list, tuple)):
            raise ValueError("display_hints.property_groups must be a JSON array")
        for index, group in enumerate(groups):
            path = f"display_hints.property_groups[{index}]"
            if not isinstance(group, Mapping):
                raise ValueError(f"{path} must be a JSON object")
            _hint_stable_name(group.get("group_name"), f"{path}.group_name")
            if "display_name" in group:
                _hint_label(group["display_name"], f"{path}.display_name")
            members = group.get("property_names")
            if not isinstance(members, (list, tuple)) or not members:
                raise ValueError(f"{path}.property_names must be a non-empty array")
            for member in members:
                _hint_stable_name(member, f"{path}.property_names[]")
    if "controls" in hints:
        _hint_token_map(hints["controls"], _HINT_CONTROLS, "display_hints.controls")
    if "formats" in hints:
        _hint_token_map(hints["formats"], _HINT_FORMATS, "display_hints.formats")
    if "sort" in hints:
        sort = hints["sort"]
        if not isinstance(sort, (list, tuple)):
            raise ValueError("display_hints.sort must be a JSON array")
        for index, entry in enumerate(sort):
            path = f"display_hints.sort[{index}]"
            if not isinstance(entry, Mapping):
                raise ValueError(f"{path} must be a JSON object")
            _hint_stable_name(entry.get("property_name"), f"{path}.property_name")
            direction = entry.get("direction")
            if not isinstance(direction, str) or direction not in _HINT_SORT_DIRECTIONS:
                raise ValueError(f"{path}.direction must be one of ['asc', 'desc']")
    if "relation_labels" in hints:
        labels = hints["relation_labels"]
        if not isinstance(labels, Mapping):
            raise ValueError("display_hints.relation_labels must be a JSON object")
        for key, item in labels.items():
            _hint_stable_name(key, "display_hints.relation_labels key")
            _hint_label(item, f"display_hints.relation_labels.{key}")
    if "action_placement" in hints:
        _hint_token_map(
            hints["action_placement"],
            _HINT_ACTION_PLACEMENTS,
            "display_hints.action_placement",
        )


def _validate_channel_mappings(mappings: Mapping[str, Any]) -> None:
    for channel, target in mappings.items():
        _hint_stable_name(channel, "channel_mappings key")
        if isinstance(target, str):
            _hint_label(target, f"channel_mappings.{channel}")
            continue
        if not isinstance(target, Mapping):
            raise ValueError(
                f"channel_mappings.{channel} must be a string or JSON object"
            )
        for key, item in target.items():
            if key in ("route", "operation_id", "tool_name", "command"):
                _hint_label(item, f"channel_mappings.{channel}.{key}")


def _include_additive_in_digest(
    owner: type, field_name: str, value: object, field: Any
) -> bool:
    """Decide whether a non-lifecycle field contributes to the digest payload.

    Legacy `nex-eios.definition.v1` fields always contribute (so every already
    published digest is preserved). An additive field contributes only when it
    holds a non-default value, so a definition that does not use the new
    feature keeps its original digest while one that does gets a new digest that
    round-trips through create -> store -> hydrate.
    """
    if (owner.__name__, field_name) not in _DIGEST_ADDITIVE_FIELDS:
        return True
    try:
        default = field.get_default(call_default_factory=True)
    except Exception:  # pragma: no cover - defensive: no usable default
        return True
    return value != default


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return {
            name: _canonical_value(getattr(value, name))
            for name in type(value).model_fields
            if name != "tenant_id"
        }
    if isinstance(value, Mapping):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical contract must contain finite JSON numbers")
        return value
    raise ValueError("canonical contract contains a non-JSON value")


def _canonical_key(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_duplicates(values: Sequence[object], field_name: str) -> None:
    keys = [_canonical_key(value) for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {field_name}")


def _canonical_set(
    values: Sequence[object],
    field_name: str,
) -> tuple[Any, ...]:
    keyed = [(_canonical_key(value), value) for value in values]
    keys = [key for key, _value in keyed]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {field_name}")
    return tuple(value for _key, value in sorted(keyed, key=lambda item: item[0]))


def _reference_tenants(value: object) -> Iterator[str]:
    if isinstance(value, (DefinitionReference, OntologySchemaReference)):
        yield value.tenant_id
        return
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield from _reference_tenants(getattr(value, field_name))
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _reference_tenants(item)
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _reference_tenants(item)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class FrozenContract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        payload = self.model_dump(
            mode="python",
            round_trip=True,
            exclude_unset=True,
        )
        if deep:
            payload = deepcopy(payload)
        if update:
            payload.update(update)
        return type(self).model_validate(payload)


class DefinitionType(str, Enum):
    FUNCTION = "function"
    ACTION = "action"
    OBJECT_SET = "object_set"
    OBJECT_VIEW = "object_view"
    INTERFACE = "interface"


class DefinitionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    INACTIVE = "inactive"


class OntologySchemaType(str, Enum):
    OBJECT_TYPE = "object_type"
    RELATION_TYPE = "relation_type"


class FunctionCacheMode(str, Enum):
    NONE = "none"
    PER_OBJECT = "per_object"
    PER_OBJECT_SET = "per_object_set"


class ActionRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionApprovalMode(str, Enum):
    NONE = "none"
    REQUIRED = "required"


class ActionCompensationMode(str, Enum):
    NONE = "none"
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class FilterOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


class LinkDirection(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
    BOTH = "both"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class DefinitionReference(FrozenContract):
    tenant_id: str
    definition_type: DefinitionType
    stable_name: str
    version: int = Field(gt=0)
    contract_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _non_blank(value, "tenant_id")

    @field_validator("stable_name")
    @classmethod
    def _validate_stable_name(cls, value: str) -> str:
        return _stable_name(value, "stable_name")


class CapabilityBinding(FrozenContract):
    capability_name: str
    capability_version: str
    schema_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("capability_name", "capability_version")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)


class OntologySchemaReference(FrozenContract):
    tenant_id: str
    schema_type: OntologySchemaType
    stable_name: str
    version: int = Field(gt=0)
    schema_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _non_blank(value, "tenant_id")

    @field_validator("stable_name")
    @classmethod
    def _validate_stable_name(cls, value: str) -> str:
        return _stable_name(value, "stable_name")


class PropertyReference(FrozenContract):
    object_type: OntologySchemaReference
    property_name: str

    @field_validator("property_name")
    @classmethod
    def _validate_property_name(cls, value: str) -> str:
        return _stable_name(value, "property_name")

    @model_validator(mode="after")
    def _validate_object_type(self) -> PropertyReference:
        if self.object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
            raise ValueError("object_type must reference an object type")
        return self


class ObjectReference(FrozenContract):
    object_type: OntologySchemaReference
    object_id: str

    @field_validator("object_id")
    @classmethod
    def _validate_object_id(cls, value: str) -> str:
        return _non_blank(value, "object_id")

    @model_validator(mode="after")
    def _validate_object_type(self) -> ObjectReference:
        if self.object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
            raise ValueError("object_type must reference an object type")
        return self


class FunctionTarget(FrozenContract):
    object_type: OntologySchemaReference | None = None
    object_set: DefinitionReference | None = None

    @model_validator(mode="after")
    def _validate_target(self) -> FunctionTarget:
        if (self.object_type is None) == (self.object_set is None):
            raise ValueError("FunctionTarget must contain exactly one target")
        if (
            self.object_type is not None
            and self.object_type.schema_type is not OntologySchemaType.OBJECT_TYPE
        ):
            raise ValueError("object_type must reference an object type")
        if (
            self.object_set is not None
            and self.object_set.definition_type is not DefinitionType.OBJECT_SET
        ):
            raise ValueError("object_set must reference an object_set definition")
        return self


class FunctionCachePolicy(FrozenContract):
    mode: FunctionCacheMode = FunctionCacheMode.NONE
    ttl_seconds: int | None = Field(default=None, gt=0)
    vary_by: tuple[PropertyReference, ...] = ()

    @field_validator("vary_by")
    @classmethod
    def _canonicalize_vary_by(
        cls,
        values: tuple[PropertyReference, ...],
    ) -> tuple[PropertyReference, ...]:
        return _canonical_set(values, "cache vary_by property")

    @model_validator(mode="after")
    def _validate_cache_policy(self) -> FunctionCachePolicy:
        if self.mode is FunctionCacheMode.NONE:
            if self.ttl_seconds is not None:
                raise ValueError("ttl_seconds must be omitted when cache mode is none")
            if self.vary_by:
                raise ValueError("vary_by must be empty when cache mode is none")
        elif self.ttl_seconds is None:
            raise ValueError("ttl_seconds is required when caching is enabled")
        return self


class ActionPrecondition(FrozenContract):
    name: str
    expression: FrozenJsonMap
    property_dependencies: tuple[PropertyReference, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _stable_name(value, "name")

    @field_validator("expression", mode="before")
    @classmethod
    def _freeze_expression(cls, value: object) -> FrozenJsonMap:
        return _freeze_json_map(value, "expression")

    @field_validator("property_dependencies")
    @classmethod
    def _canonicalize_dependencies(
        cls,
        values: tuple[PropertyReference, ...],
    ) -> tuple[PropertyReference, ...]:
        return _canonical_set(values, "precondition dependency")


class ActionChangeScope(FrozenContract):
    object_types: tuple[OntologySchemaReference, ...] = ()
    properties: tuple[PropertyReference, ...] = ()
    target_systems: tuple[str, ...] = ()

    @field_validator("object_types")
    @classmethod
    def _canonicalize_object_types(
        cls,
        values: tuple[OntologySchemaReference, ...],
    ) -> tuple[OntologySchemaReference, ...]:
        return _canonical_set(values, "change-scope object type")

    @field_validator("properties")
    @classmethod
    def _canonicalize_properties(
        cls,
        values: tuple[PropertyReference, ...],
    ) -> tuple[PropertyReference, ...]:
        return _canonical_set(values, "change-scope property")

    @field_validator("target_systems")
    @classmethod
    def _validate_target_systems(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_non_blank(value, "target_systems") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate target_systems")
        return tuple(sorted(normalized))

    @model_validator(mode="after")
    def _validate_change_scope(self) -> ActionChangeScope:
        if not (self.object_types or self.properties or self.target_systems):
            raise ValueError("change_scope must not be empty")
        for object_type in self.object_types:
            if object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
                raise ValueError("object_types must reference object types")
        for property_ref in self.properties:
            if property_ref.object_type not in self.object_types:
                raise ValueError(
                    "change_scope properties must belong to declared object_types"
                )
        return self


class ActionIdempotencyPolicy(FrozenContract):
    required: bool = True
    key_fields: tuple[str, ...]

    @field_validator("key_fields")
    @classmethod
    def _validate_key_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_stable_name(value, "key_fields") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate idempotency key_fields")
        if not normalized:
            raise ValueError("key_fields must not be empty")
        return normalized


class ActionGovernanceContract(FrozenContract):
    change_scope: ActionChangeScope
    risk_level: ActionRiskLevel
    policy_refs: tuple[str, ...] = ()
    approval_mode: ActionApprovalMode = ActionApprovalMode.NONE
    idempotency: ActionIdempotencyPolicy
    compensation_mode: ActionCompensationMode = ActionCompensationMode.NONE
    receipt_required: Literal[True] = True

    @field_validator("receipt_required", mode="before")
    @classmethod
    def _validate_receipt_required(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("receipt_required must be boolean true")
        return True

    @field_validator("policy_refs")
    @classmethod
    def _validate_policy_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_stable_name(value, "policy_refs") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate policy_refs")
        return tuple(sorted(normalized))

    @model_validator(mode="after")
    def _validate_idempotency(self) -> ActionGovernanceContract:
        if not self.idempotency.required:
            raise ValueError("idempotency must be required for an Action")
        return self


class QueryParameter(FrozenContract):
    name: str
    parameter_schema: FrozenJsonMap = Field(
        validation_alias=AliasChoices("parameter_schema", "schema"),
        serialization_alias="schema",
    )
    required: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _stable_name(value, "name")

    @field_validator("parameter_schema", mode="before")
    @classmethod
    def _freeze_schema(cls, value: object) -> FrozenJsonMap:
        return _freeze_json_map(value, "parameter_schema")


_ACTION_PARAMETER_TYPES = frozenset(
    {"string", "number", "integer", "boolean", "object_reference"}
)
_ACTION_STRING_CONSTRAINTS = ("minLength", "maxLength", "pattern", "format")
_ACTION_NUMBER_CONSTRAINTS = ("minimum", "maximum")
_MAX_ACTION_PATTERN_LENGTH = 256
_MAX_PATTERNED_INPUT_LENGTH = 4096
_MAX_RECEIPT_SCHEMA_DEPTH = 16
_RECEIPT_SCHEMA_TYPES = frozenset(
    {
        "object",
        "object_reference",
        "string",
        "integer",
        "number",
        "boolean",
        "array",
        "null",
    }
)
_RFC3339_DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _validate_safe_action_pattern(pattern: str) -> None:
    """Accept only a bounded linear regex subset suitable for request gates."""

    if len(pattern) > _MAX_ACTION_PATTERN_LENGTH:
        raise ValueError("parameter_schema.pattern must not exceed 256 characters")
    try:
        parsed = re._parser.parse(pattern, 0)  # type: ignore[attr-defined]
    except re.error:
        raise ValueError(
            "parameter_schema.pattern must be a valid regular expression"
        ) from None

    escaped = False
    in_class = False
    for character in pattern:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "[":
            in_class = True
        elif character == "]":
            in_class = False
        elif character == "|" and not in_class:
            raise ValueError(
                "parameter_schema.pattern uses an unsafe regular expression"
            )

    scalar_operations = {"LITERAL", "NOT_LITERAL", "ANY", "CATEGORY", "AT"}
    class_operations = {"LITERAL", "RANGE", "CATEGORY", "NEGATE"}
    repeats = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
    variable_repeat_count = 0

    def validate_atom(node: object) -> None:
        if not isinstance(node, tuple) or len(node) != 2:
            raise ValueError(
                "parameter_schema.pattern uses an unsafe regular expression"
            )
        operation, argument = node
        name = getattr(operation, "name", str(operation))
        if name in scalar_operations:
            return
        if name == "IN" and all(
            getattr(class_operation, "name", str(class_operation)) in class_operations
            for class_operation, _class_argument in argument
        ):
            return
        raise ValueError("parameter_schema.pattern uses an unsafe regular expression")

    def walk(nodes: object) -> None:
        nonlocal variable_repeat_count
        if hasattr(nodes, "data"):
            nodes = nodes.data
        if not isinstance(nodes, (list, tuple)):
            raise ValueError(
                "parameter_schema.pattern uses an unsafe regular expression"
            )
        previous_was_repeat = False
        for node in nodes:
            if not isinstance(node, tuple) or len(node) != 2:
                raise ValueError(
                    "parameter_schema.pattern uses an unsafe regular expression"
                )
            operation, argument = node
            name = getattr(operation, "name", str(operation))
            if name in scalar_operations:
                previous_was_repeat = False
                continue
            if name == "IN":
                if any(
                    getattr(class_operation, "name", str(class_operation))
                    not in class_operations
                    for class_operation, _class_argument in argument
                ):
                    raise ValueError(
                        "parameter_schema.pattern uses an unsafe regular expression"
                    )
                previous_was_repeat = False
                continue
            if name in repeats:
                if previous_was_repeat:
                    raise ValueError(
                        "parameter_schema.pattern uses an unsafe regular expression"
                    )
                minimum, maximum, repeated = argument
                repeated_nodes = (
                    repeated.data if hasattr(repeated, "data") else repeated
                )
                if (
                    not isinstance(repeated_nodes, (list, tuple))
                    or len(repeated_nodes) != 1
                ):
                    raise ValueError(
                        "parameter_schema.pattern uses an unsafe regular expression"
                    )
                validate_atom(repeated_nodes[0])
                if minimum != maximum:
                    variable_repeat_count += 1
                    if variable_repeat_count > 1:
                        raise ValueError(
                            "parameter_schema.pattern uses an unsafe regular expression"
                        )
                elif maximum > _MAX_PATTERNED_INPUT_LENGTH:
                    raise ValueError(
                        "parameter_schema.pattern uses an unsafe regular expression"
                    )
                previous_was_repeat = True
                continue
            raise ValueError(
                "parameter_schema.pattern uses an unsafe regular expression"
            )

    walk(parsed)


def _validate_action_parameter_schema_constraints(
    declared_type: object,
    parameter_schema: Mapping[str, Any],
) -> None:
    string_constraints = tuple(
        name for name in _ACTION_STRING_CONSTRAINTS if name in parameter_schema
    )
    if string_constraints and declared_type != "string":
        raise ValueError("parameter_schema string constraints require type string")
    number_constraints = tuple(
        name for name in _ACTION_NUMBER_CONSTRAINTS if name in parameter_schema
    )
    if number_constraints and declared_type not in {"integer", "number"}:
        raise ValueError(
            "parameter_schema numeric constraints require type integer or number"
        )

    for name in ("minLength", "maxLength"):
        if name not in parameter_schema:
            continue
        value = parameter_schema[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"parameter_schema.{name} must be a non-negative integer")
    min_length = parameter_schema.get("minLength")
    max_length = parameter_schema.get("maxLength")
    if (
        isinstance(min_length, int)
        and not isinstance(min_length, bool)
        and isinstance(max_length, int)
        and not isinstance(max_length, bool)
        and min_length > max_length
    ):
        raise ValueError("parameter_schema.minLength must not exceed maxLength")

    pattern = parameter_schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise ValueError("parameter_schema.pattern must be a string")
        _validate_safe_action_pattern(pattern)

    for name in ("minimum", "maximum"):
        if name not in parameter_schema:
            continue
        value = parameter_schema[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"parameter_schema.{name} must be a number")
    minimum = parameter_schema.get("minimum")
    maximum = parameter_schema.get("maximum")
    if (
        isinstance(minimum, (int, float))
        and not isinstance(minimum, bool)
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and minimum > maximum
    ):
        raise ValueError("parameter_schema.minimum must not exceed maximum")


def _validate_action_receipt_schema(
    schema: Mapping[str, Any], *, depth: int = 0
) -> None:
    """Validate the bounded schema subset enforced by the receipt matcher."""

    if depth > _MAX_RECEIPT_SCHEMA_DEPTH:
        raise ValueError("receipt_schema exceeds maximum schema depth")
    declared_type = schema.get("type")
    if not isinstance(declared_type, str) or declared_type not in _RECEIPT_SCHEMA_TYPES:
        raise ValueError("receipt_schema.type is unsupported")

    common_keys = {"type", "const", "enum"}
    type_keys = {
        "object": {"properties", "required", "additionalProperties"},
        "object_reference": {"object_type"},
        "string": {"minLength", "maxLength", "pattern"},
        "integer": {"minimum", "maximum"},
        "number": {"minimum", "maximum"},
        "boolean": set(),
        "array": {"items"},
        "null": set(),
    }
    unsupported = set(schema) - common_keys - type_keys[declared_type]
    if unsupported:
        raise ValueError("receipt_schema contains unsupported constraints")

    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, Sequence) or isinstance(enum, (str, bytes)) or not enum
    ):
        raise ValueError("receipt_schema.enum must be a non-empty JSON array")

    if declared_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError("receipt_schema.properties must be a JSON object")
        required = schema.get("required", ())
        if (
            not isinstance(required, Sequence)
            or isinstance(required, (str, bytes))
            or any(not isinstance(item, str) or not item for item in required)
            or len(required) != len(set(required))
        ):
            raise ValueError("receipt_schema.required must contain unique strings")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise ValueError("receipt_schema.additionalProperties must be a boolean")
        if additional is False and any(item not in properties for item in required):
            raise ValueError(
                "receipt_schema.required fields must be declared in properties"
            )
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError("receipt_schema property names must be non-empty")
            if not isinstance(child, Mapping):
                raise ValueError("receipt_schema properties must contain schemas")
            _validate_action_receipt_schema(child, depth=depth + 1)
        return

    if declared_type == "array":
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, Mapping):
                raise ValueError("receipt_schema.items must be a schema")
            _validate_action_receipt_schema(items, depth=depth + 1)
        return

    if declared_type == "object_reference":
        object_type = schema.get("object_type")
        if object_type is not None and (
            not isinstance(object_type, str) or not object_type.strip()
        ):
            raise ValueError("receipt_schema.object_type must be a non-empty string")
        return

    try:
        _validate_action_parameter_schema_constraints(declared_type, schema)
    except ValueError as error:
        raise ValueError(
            str(error).replace("parameter_schema", "receipt_schema")
        ) from None


def _action_parameter_constraint_violation(
    declared_type: object,
    parameter_schema: Mapping[str, Any],
    value: object,
) -> str | None:
    if declared_type == "string" and isinstance(value, str):
        min_length = parameter_schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            return "minLength"
        max_length = parameter_schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            return "maxLength"
        pattern = parameter_schema.get("pattern")
        if isinstance(pattern, str):
            if len(value) > _MAX_PATTERNED_INPUT_LENGTH:
                return "pattern"
            try:
                matches = re.search(pattern, value) is not None
            except re.error:  # pragma: no cover - model validation prevents this
                matches = False
            if not matches:
                return "pattern"
        if parameter_schema.get("format") == "date-time":
            if _RFC3339_DATE_TIME_RE.fullmatch(value) is None:
                return "format"
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return "format"
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return "format"

    if (
        declared_type in {"integer", "number"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        minimum = parameter_schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return "minimum"
        maximum = parameter_schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            return "maximum"
    return None


class ActionParameter(FrozenContract):
    """One declared, typed input of a parameterized Action.

    ``parameter_schema`` must declare a primitive ``type`` (string / number /
    integer / boolean) with an optional ``enum`` of same-typed values. String
    inputs may additionally declare JSON-Schema ``minLength``, ``maxLength``,
    ``pattern``, and timezone-aware ``format: date-time`` constraints; numeric
    inputs may declare inclusive ``minimum`` / ``maximum`` bounds. Alternatively,
    ``type: object_reference`` carries an ``object_type`` stable name that the
    owning Action validates against its declared ``object_types``. ``default``
    (when present) and enum values must satisfy every declared constraint, so a
    generated form can always prefill a valid value.
    """

    name: str
    parameter_schema: FrozenJsonMap = Field(
        validation_alias=AliasChoices("parameter_schema", "schema"),
        serialization_alias="schema",
    )
    required: bool = False
    default: Any = None
    display_hints: FrozenJsonMap = Field(default_factory=lambda: FrozenJsonMap({}))

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _stable_name(value, "name")

    @field_validator("parameter_schema", "display_hints", mode="before")
    @classmethod
    def _freeze_maps(cls, value: object, info: Any) -> FrozenJsonMap:
        return _freeze_json_map(value, info.field_name)

    @field_validator("default", mode="before")
    @classmethod
    def _freeze_default(cls, value: object) -> object:
        return _freeze_json(value, path="JSON parameter default")

    @field_serializer("default", when_used="json")
    def _serialize_default(self, value: object) -> object:
        return _thaw_json(value)

    @model_validator(mode="after")
    def _validate_parameter(self) -> ActionParameter:
        declared_type = self.parameter_schema.get("type")
        if declared_type not in _ACTION_PARAMETER_TYPES:
            raise ValueError(
                "parameter_schema.type must be one of "
                f"{sorted(_ACTION_PARAMETER_TYPES)}"
            )
        if declared_type == "object_reference":
            target = self.parameter_schema.get("object_type")
            if not isinstance(target, str):
                raise ValueError("parameter_schema.object_type must be a stable name")
            _stable_name(target, "parameter_schema.object_type")
            if "enum" in self.parameter_schema:
                raise ValueError("object_reference parameters cannot declare an enum")
        _validate_action_parameter_schema_constraints(
            declared_type, self.parameter_schema
        )
        enum = self.parameter_schema.get("enum")
        if enum is not None:
            if not isinstance(enum, (list, tuple)) or not enum:
                raise ValueError("parameter_schema.enum must be a non-empty JSON array")
            for item in enum:
                if not _matches_action_parameter_type(declared_type, item):
                    raise ValueError("parameter_schema.enum values must match the type")
                violation = _action_parameter_constraint_violation(
                    declared_type, self.parameter_schema, item
                )
                if violation is not None:
                    raise ValueError(
                        "parameter_schema.enum values must satisfy "
                        f"parameter_schema.{violation}"
                    )
        if self.default is not None:
            if not _matches_action_parameter_type(declared_type, self.default):
                raise ValueError("default must satisfy parameter_schema.type")
            if enum is not None and self.default not in tuple(enum):
                raise ValueError("default must be one of parameter_schema.enum")
            violation = _action_parameter_constraint_violation(
                declared_type, self.parameter_schema, self.default
            )
            if violation is not None:
                raise ValueError(f"default must satisfy parameter_schema.{violation}")
            if self.required:
                raise ValueError("required parameters cannot carry a default")
        return self


def _matches_action_parameter_type(declared_type: object, value: object) -> bool:
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if declared_type == "object_reference":
        return isinstance(value, str) and bool(value.strip())
    return False


class ActionInputError(ValueError):
    """A request does not satisfy the Action's declared input contract."""

    code = "action_input_invalid"


def validate_action_input(
    parameters: tuple[ActionParameter, ...],
    input_schema: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    """Fail-closed gate for a parameterized Action's request payload.

    Runs BEFORE the request digest and claim are computed, so an invalid
    request never burns an idempotency binding. Actions declaring neither
    parameters nor an input schema keep their pre-stage-4 behavior (any
    payload passes). ``additionalProperties: false`` in the input schema
    turns on strict key checking against the declared parameters.
    """

    if not parameters and not input_schema:
        return
    if not isinstance(request, Mapping):
        raise ActionInputError("Action request must be a JSON object")
    declared = {parameter.name: parameter for parameter in parameters}
    for name, parameter in declared.items():
        if name not in request:
            if parameter.required:
                raise ActionInputError(f"required action parameter is missing: {name}")
            continue
        value = request[name]
        declared_type = parameter.parameter_schema.get("type")
        if not _matches_action_parameter_type(declared_type, value):
            raise ActionInputError(f"action parameter value has the wrong type: {name}")
        enum = parameter.parameter_schema.get("enum")
        if enum is not None and value not in tuple(enum):
            raise ActionInputError(
                f"action parameter value is not in the declared enum: {name}"
            )
        violation = _action_parameter_constraint_violation(
            declared_type, parameter.parameter_schema, value
        )
        if violation is not None:
            raise ActionInputError(
                f"action parameter value violates the declared {violation}: {name}"
            )
    for name in input_schema.get("required", ()):
        if isinstance(name, str) and name not in request:
            raise ActionInputError(f"required action input is missing: {name}")
    if input_schema.get("additionalProperties") is False:
        allowed = set(declared)
        properties = input_schema.get("properties")
        if isinstance(properties, Mapping):
            allowed.update(properties)
        unknown = sorted(set(request) - allowed)
        if unknown:
            raise ActionInputError(f"action input key is not declared: {unknown[0]}")


class PropertyFilter(FrozenContract):
    property: PropertyReference
    operator: FilterOperator
    parameter_name: str | None = None
    value: Any = None

    @model_validator(mode="after")
    def _validate_operand_presence(self) -> PropertyFilter:
        fields = self.model_fields_set
        operand_count = int("parameter_name" in fields) + int("value" in fields)
        if operand_count != 1:
            raise ValueError(
                "PropertyFilter must provide exactly one of parameter_name or value"
            )
        if "parameter_name" in fields:
            if self.parameter_name is None:
                raise ValueError("parameter_name must be non-empty when provided")
            if self.value is not None:
                raise ValueError(
                    "PropertyFilter parameter operand cannot hide a value operand"
                )
        elif self.parameter_name is not None:
            raise ValueError(
                "PropertyFilter value operand cannot hide a parameter operand"
            )
        return self

    @field_serializer("value", when_used="json")
    def _serialize_value(self, value: object) -> object:
        return _thaw_json(value)

    @model_serializer(mode="wrap")
    def _serialize_operand(self, handler: Any) -> dict[str, Any]:
        serialized = handler(self)
        fields = self.model_fields_set
        if "parameter_name" in fields and "value" not in fields:
            serialized.pop("value", None)
        elif "value" in fields and "parameter_name" not in fields:
            serialized.pop("parameter_name", None)
        else:  # pragma: no cover - validation invariant
            raise ValueError("PropertyFilter operand selection is invalid")
        return serialized

    @field_validator("parameter_name")
    @classmethod
    def _validate_parameter_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _stable_name(value, "parameter_name")

    @field_validator("value", mode="before")
    @classmethod
    def _freeze_value(cls, value: object) -> object:
        return _freeze_json(value, path="JSON filter value")


class LinkTraversal(FrozenContract):
    relation: OntologySchemaReference
    direction: LinkDirection
    target_object_type: OntologySchemaReference
    max_depth: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_schema_types(self) -> LinkTraversal:
        if self.relation.schema_type is not OntologySchemaType.RELATION_TYPE:
            raise ValueError("relation must reference a relation type")
        if self.target_object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
            raise ValueError("target_object_type must reference an object type")
        return self


class AggregationFunction(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class Aggregation(FrozenContract):
    """One declared metric over an Object Set population (stage 7).

    COUNT needs no property; every other function aggregates one numeric
    property. group_by buckets the population by property values before the
    metric is computed, so consumers get indicators without materializing
    the full population.
    """

    name: str
    function: AggregationFunction
    property: PropertyReference | None = None
    group_by: tuple[PropertyReference, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _stable_name(value, "name")

    @model_validator(mode="after")
    def _validate_aggregation(self) -> Aggregation:
        if self.function is AggregationFunction.COUNT:
            if self.property is not None:
                raise ValueError("count aggregations cannot declare a property")
        elif self.property is None:
            raise ValueError("metric aggregations require a property")
        keys = tuple(
            (item.object_type.stable_name, item.property_name) for item in self.group_by
        )
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate group_by property")
        return self


class SortField(FrozenContract):
    property: PropertyReference
    direction: SortDirection = SortDirection.ASC


class ViewSource(FrozenContract):
    object_type: OntologySchemaReference | None = None
    object_set: DefinitionReference | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> ViewSource:
        if (self.object_type is None) == (self.object_set is None):
            raise ValueError("ViewSource must contain exactly one source")
        if (
            self.object_type is not None
            and self.object_type.schema_type is not OntologySchemaType.OBJECT_TYPE
        ):
            raise ValueError("object_type must reference an object type")
        if (
            self.object_set is not None
            and self.object_set.definition_type is not DefinitionType.OBJECT_SET
        ):
            raise ValueError("object_set must reference an object_set definition")
        return self


class ObjectSetRevision(FrozenContract):
    tenant_id: str
    object_set: DefinitionReference
    revision: str
    source_revision: str
    parameters_digest: str = Field(pattern=_SHA256_PATTERN)
    objects: tuple[ObjectReference, ...]
    created_at: datetime

    @field_validator("tenant_id", "revision", "source_revision")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "created_at")

    @model_validator(mode="after")
    def _validate_snapshot(self) -> ObjectSetRevision:
        if self.object_set.definition_type is not DefinitionType.OBJECT_SET:
            raise ValueError("object_set must reference an object_set definition")
        if self.object_set.tenant_id != self.tenant_id:
            raise ValueError("object_set tenant must match snapshot tenant")
        for item in self.objects:
            if item.object_type.tenant_id != self.tenant_id:
                raise ValueError("object tenant must match snapshot tenant")
        _reject_duplicates(self.objects, "snapshot object")
        return self


class Definition(FrozenContract):
    tenant_id: str
    definition_type: DefinitionType
    stable_name: str
    version: int = Field(gt=0)
    status: DefinitionStatus = DefinitionStatus.DRAFT
    contract_digest: str | None = None
    required_scopes: tuple[str, ...] = ()
    # Stage 9 (additive, digest-registered per concrete class): the marking
    # floor a Definition declares. A referencing Definition's markings must
    # contain those of every object/definition it exposes, so a Restricted
    # View can only narrow — never widen — the underlying marking visibility.
    required_markings: tuple[str, ...] = ()
    created_by: str
    created_at: datetime
    previous_version: DefinitionReference | None = None
    source_lineage: tuple[str, ...] = ()

    @field_validator("tenant_id", "created_by")
    @classmethod
    def _validate_non_blank(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("stable_name")
    @classmethod
    def _validate_stable_name(cls, value: str) -> str:
        return _stable_name(value, "stable_name")

    @field_validator("contract_digest")
    @classmethod
    def _validate_contract_digest(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(_SHA256_PATTERN, value) is None:
            raise ValueError("contract_digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("required_scopes", "required_markings", mode="before")
    @classmethod
    def _normalize_required_scopes(cls, value: object, info: Any) -> tuple[str, ...]:
        if not isinstance(value, tuple) and not (
            info.mode == "json" and isinstance(value, list)
        ):
            raise ValueError(f"{info.field_name} must be a tuple")
        scopes: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{info.field_name} must contain strings")
            scopes.append(_non_blank(item, info.field_name))
        return tuple(sorted(set(scopes)))

    @field_validator("source_lineage")
    @classmethod
    def _validate_source_lineage(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_non_blank(value, "source_lineage") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate source_lineage")
        return normalized

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, "created_at")

    @model_validator(mode="after")
    def _validate_definition(self) -> Definition:
        previous = self.previous_version
        if previous is not None and (
            previous.tenant_id != self.tenant_id
            or previous.definition_type is not self.definition_type
            or previous.stable_name != self.stable_name
            or previous.version >= self.version
        ):
            raise ValueError(
                "previous_version must be a lower exact reference to the same definition"
            )

        for field_name in type(self).model_fields:
            if field_name in _LIFECYCLE_FIELDS:
                continue
            for referenced_tenant in _reference_tenants(getattr(self, field_name)):
                if referenced_tenant != self.tenant_id:
                    raise ValueError(f"{field_name} contains a cross-tenant reference")

        model_fields = type(self).model_fields
        semantic_payload: dict[str, object] = {"schema_namespace": _DIGEST_NAMESPACE}
        for field_name, field in model_fields.items():
            if field_name in _LIFECYCLE_FIELDS:
                continue
            value = getattr(self, field_name)
            if not _include_additive_in_digest(type(self), field_name, value, field):
                continue
            semantic_payload[field_name] = _canonical_value(value)
        encoded = json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected_digest = hashlib.sha256(encoded).hexdigest()
        if self.contract_digest is not None and self.contract_digest != expected_digest:
            raise ValueError("contract_digest mismatch")
        object.__setattr__(self, "contract_digest", expected_digest)
        return self

    def reference(self) -> DefinitionReference:
        if self.contract_digest is None:  # pragma: no cover - validator invariant
            raise RuntimeError("contract_digest is unavailable")
        return DefinitionReference(
            tenant_id=self.tenant_id,
            definition_type=self.definition_type,
            stable_name=self.stable_name,
            version=self.version,
            contract_digest=self.contract_digest,
        )


class FunctionDefinition(Definition):
    definition_type: Literal[DefinitionType.FUNCTION] = DefinitionType.FUNCTION
    capability_binding: CapabilityBinding
    applies_to: tuple[FunctionTarget, ...]
    input_schema: FrozenJsonMap
    output_schema: FrozenJsonMap
    property_dependencies: tuple[PropertyReference, ...] = ()
    link_dependencies: tuple[OntologySchemaReference, ...] = ()
    cache_policy: FunctionCachePolicy = FunctionCachePolicy()

    @field_validator("definition_type", mode="before")
    @classmethod
    def _validate_definition_type(cls, value: object, info: Any) -> object:
        if info.mode == "python" and value is not DefinitionType.FUNCTION:
            raise ValueError("definition_type must be DefinitionType.FUNCTION")
        return value

    @field_validator("applies_to")
    @classmethod
    def _canonicalize_targets(
        cls,
        values: tuple[FunctionTarget, ...],
    ) -> tuple[FunctionTarget, ...]:
        return _canonical_set(values, "function target")

    @field_validator("property_dependencies")
    @classmethod
    def _canonicalize_property_dependencies(
        cls,
        values: tuple[PropertyReference, ...],
    ) -> tuple[PropertyReference, ...]:
        return _canonical_set(values, "property dependency")

    @field_validator("link_dependencies")
    @classmethod
    def _canonicalize_link_dependencies(
        cls,
        values: tuple[OntologySchemaReference, ...],
    ) -> tuple[OntologySchemaReference, ...]:
        return _canonical_set(values, "link dependency")

    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def _freeze_schemas(cls, value: object, info: Any) -> FrozenJsonMap:
        return _freeze_json_map(value, info.field_name)

    @model_validator(mode="after")
    def _validate_function(self) -> FunctionDefinition:
        if not self.applies_to:
            raise ValueError("applies_to must not be empty")
        for relation in self.link_dependencies:
            if relation.schema_type is not OntologySchemaType.RELATION_TYPE:
                raise ValueError("link_dependencies must reference relation types")
        return self


class ActionDefinition(Definition):
    definition_type: Literal[DefinitionType.ACTION] = DefinitionType.ACTION
    capability_binding: CapabilityBinding
    object_types: tuple[OntologySchemaReference, ...]
    preconditions: tuple[ActionPrecondition, ...] = ()
    governance: ActionGovernanceContract
    receipt_schema: FrozenJsonMap
    # Additive (stage 4, registered in _DIGEST_ADDITIVE_FIELDS): defaults are
    # omitted from the digest so pre-stage-4 published digests stay stable.
    input_schema: FrozenJsonMap = Field(default_factory=lambda: FrozenJsonMap({}))
    parameters: tuple[ActionParameter, ...] = ()

    @field_validator("definition_type", mode="before")
    @classmethod
    def _validate_definition_type(cls, value: object, info: Any) -> object:
        if info.mode == "python" and value is not DefinitionType.ACTION:
            raise ValueError("definition_type must be DefinitionType.ACTION")
        return value

    @field_validator("input_schema", mode="before")
    @classmethod
    def _freeze_input_schema(cls, value: object) -> FrozenJsonMap:
        return _freeze_json_map(value, "input_schema")

    @field_validator("object_types")
    @classmethod
    def _canonicalize_object_types(
        cls,
        values: tuple[OntologySchemaReference, ...],
    ) -> tuple[OntologySchemaReference, ...]:
        return _canonical_set(values, "action object_types")

    @field_validator("preconditions")
    @classmethod
    def _canonicalize_preconditions(
        cls,
        values: tuple[ActionPrecondition, ...],
    ) -> tuple[ActionPrecondition, ...]:
        return _canonical_set(values, "action precondition")

    @field_validator("receipt_schema", mode="before")
    @classmethod
    def _freeze_receipt_schema(cls, value: object) -> FrozenJsonMap:
        frozen = _freeze_json_map(value, "receipt_schema")
        _validate_action_receipt_schema(frozen)
        return frozen

    @model_validator(mode="after")
    def _validate_action(self) -> ActionDefinition:
        if not self.object_types:
            raise ValueError("object_types must not be empty")
        for object_type in self.object_types:
            if object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
                raise ValueError("object_types must reference object types")
        names = tuple(item.name for item in self.preconditions)
        if len(names) != len(set(names)):
            raise ValueError("duplicate action precondition")
        for object_type in self.governance.change_scope.object_types:
            if object_type not in self.object_types:
                raise ValueError(
                    "governance change_scope object_types must belong to Action object_types"
                )
        for property_ref in self.governance.change_scope.properties:
            if property_ref.object_type not in self.object_types:
                raise ValueError(
                    "governance change_scope properties must belong to Action object_types"
                )
        for precondition in self.preconditions:
            for dependency in precondition.property_dependencies:
                if dependency.object_type not in self.object_types:
                    raise ValueError(
                        "precondition property dependency escapes Action object_types"
                    )
        parameter_names = tuple(item.name for item in self.parameters)
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("duplicate action parameter")
        declared_types = {reference.stable_name for reference in self.object_types}
        for parameter in self.parameters:
            if parameter.parameter_schema.get("type") != "object_reference":
                continue
            target = parameter.parameter_schema.get("object_type")
            if target not in declared_types:
                raise ValueError(
                    "object_reference parameter must target a declared"
                    " Action object_type"
                )
        return self


class SemanticSearch(FrozenContract):
    """A semantic-search node over an Object Set (stage 12-C).

    Declares that the Object Set may be resolved by vector similarity against a
    Memory index: the runtime query text is carried by a declared query
    parameter, embedded, and matched by KNN (optionally fused with keyword
    search). The object type must equal the Object Set's own object type — the
    hits resolve back to objects of that type and are then narrowed by the same
    markings/filter/traversal predicates as any other membership.
    """

    object_type: OntologySchemaReference
    index_name: str
    query_parameter: str
    top_k: int = Field(default=8, ge=1, le=1000)
    rrf_k: int = Field(default=60, ge=1)
    include_keyword: bool = False

    @field_validator("index_name")
    @classmethod
    def _validate_index_name(cls, value: str) -> str:
        return _stable_name(value, "index_name")

    @field_validator("query_parameter")
    @classmethod
    def _validate_query_parameter(cls, value: str) -> str:
        return _stable_name(value, "query_parameter")

    @model_validator(mode="after")
    def _validate_semantic(self) -> SemanticSearch:
        if self.object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
            raise ValueError("semantic object_type must reference an object type")
        return self


class ObjectSetDefinition(Definition):
    definition_type: Literal[DefinitionType.OBJECT_SET] = DefinitionType.OBJECT_SET
    # Additive (stage 7, registered in _DIGEST_ADDITIVE_FIELDS).
    aggregations: tuple[Aggregation, ...] = ()
    # Additive (stage 12-C, registered in _DIGEST_ADDITIVE_FIELDS).
    semantic: SemanticSearch | None = None
    object_type: OntologySchemaReference
    parameters: tuple[QueryParameter, ...] = ()
    filters: tuple[PropertyFilter, ...] = ()
    link_traversals: tuple[LinkTraversal, ...] = ()
    sort: tuple[SortField, ...] = ()
    projection: tuple[PropertyReference, ...] = ()
    snapshots_allowed: bool = True

    @field_validator("definition_type", mode="before")
    @classmethod
    def _validate_definition_type(cls, value: object, info: Any) -> object:
        if info.mode == "python" and value is not DefinitionType.OBJECT_SET:
            raise ValueError("definition_type must be DefinitionType.OBJECT_SET")
        return value

    @model_validator(mode="after")
    def _validate_object_set(self) -> ObjectSetDefinition:
        if self.object_type.schema_type is not OntologySchemaType.OBJECT_TYPE:
            raise ValueError("object_type must reference an object type")
        parameter_names = tuple(item.name for item in self.parameters)
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("duplicate query parameter")
        for item in self.filters:
            if (
                item.parameter_name is not None
                and item.parameter_name not in parameter_names
            ):
                raise ValueError(
                    f"filter parameter {item.parameter_name!r} is not declared"
                )
            if item.property.object_type != self.object_type:
                raise ValueError(
                    "filter property must reference the object_set object_type"
                )
        for item in self.sort:
            if item.property.object_type != self.object_type:
                raise ValueError(
                    "sort property must reference the object_set object_type"
                )
        for item in self.projection:
            if item.object_type != self.object_type:
                raise ValueError(
                    "projection property must reference the object_set object_type"
                )
        _reject_duplicates(self.filters, "object_set filter")
        _reject_duplicates(self.link_traversals, "link traversal")
        _reject_duplicates(self.sort, "sort field")
        _reject_duplicates(self.projection, "projection property")
        aggregation_names = tuple(item.name for item in self.aggregations)
        if len(aggregation_names) != len(set(aggregation_names)):
            raise ValueError("duplicate aggregation name")
        for aggregation in self.aggregations:
            if (
                aggregation.property is not None
                and aggregation.property.object_type != self.object_type
            ):
                raise ValueError(
                    "aggregation property must reference the object_set object_type"
                )
            for reference in aggregation.group_by:
                if reference.object_type != self.object_type:
                    raise ValueError(
                        "aggregation group_by must reference the object_set object_type"
                    )
        if self.semantic is not None:
            if self.semantic.object_type != self.object_type:
                raise ValueError(
                    "semantic object_type must equal the object_set object_type"
                )
            if self.semantic.query_parameter not in parameter_names:
                raise ValueError(
                    f"semantic query_parameter {self.semantic.query_parameter!r}"
                    " is not declared"
                )
        return self


class ObjectViewDefinition(Definition):
    definition_type: Literal[DefinitionType.OBJECT_VIEW] = DefinitionType.OBJECT_VIEW
    source: ViewSource
    fields: tuple[PropertyReference, ...]
    relations: tuple[OntologySchemaReference, ...] = ()
    functions: tuple[DefinitionReference, ...] = ()
    actions: tuple[DefinitionReference, ...] = ()
    display_hints: FrozenJsonMap

    @field_validator("definition_type", mode="before")
    @classmethod
    def _validate_definition_type(cls, value: object, info: Any) -> object:
        if info.mode == "python" and value is not DefinitionType.OBJECT_VIEW:
            raise ValueError("definition_type must be DefinitionType.OBJECT_VIEW")
        return value

    @field_validator("display_hints", mode="before")
    @classmethod
    def _freeze_display_hints(cls, value: object) -> FrozenJsonMap:
        return _freeze_json_map(value, "display_hints")

    @model_validator(mode="after")
    def _validate_object_view(self) -> ObjectViewDefinition:
        for relation in self.relations:
            if relation.schema_type is not OntologySchemaType.RELATION_TYPE:
                raise ValueError("relations must reference relation types")
        for function in self.functions:
            if function.definition_type is not DefinitionType.FUNCTION:
                raise ValueError("function reference must target a function definition")
        for action in self.actions:
            if action.definition_type is not DefinitionType.ACTION:
                raise ValueError("action reference must target an action definition")
        if self.source.object_type is not None:
            for field in self.fields:
                if field.object_type != self.source.object_type:
                    raise ValueError("view field must reference the source object_type")
        _reject_duplicates(self.fields, "view field")
        _reject_duplicates(self.relations, "view relation")
        _reject_duplicates(self.functions, "view function")
        _reject_duplicates(self.actions, "view action")
        _validate_display_hints(self.display_hints)
        return self


class InterfaceDefinition(Definition):
    definition_type: Literal[DefinitionType.INTERFACE] = DefinitionType.INTERFACE
    object_view: DefinitionReference
    functions: tuple[DefinitionReference, ...] = ()
    actions: tuple[DefinitionReference, ...] = ()
    input_schema: FrozenJsonMap
    channel_mappings: FrozenJsonMap

    @field_validator("definition_type", mode="before")
    @classmethod
    def _validate_definition_type(cls, value: object, info: Any) -> object:
        if info.mode == "python" and value is not DefinitionType.INTERFACE:
            raise ValueError("definition_type must be DefinitionType.INTERFACE")
        return value

    @field_validator("input_schema", "channel_mappings", mode="before")
    @classmethod
    def _freeze_json_contracts(cls, value: object, info: Any) -> FrozenJsonMap:
        return _freeze_json_map(value, info.field_name)

    @model_validator(mode="after")
    def _validate_interface(self) -> InterfaceDefinition:
        if self.object_view.definition_type is not DefinitionType.OBJECT_VIEW:
            raise ValueError("object_view must reference an object_view definition")
        for function in self.functions:
            if function.definition_type is not DefinitionType.FUNCTION:
                raise ValueError("function reference must target a function definition")
        for action in self.actions:
            if action.definition_type is not DefinitionType.ACTION:
                raise ValueError("action reference must target an action definition")
        _reject_duplicates(self.functions, "interface function")
        _reject_duplicates(self.actions, "interface action")
        _validate_channel_mappings(self.channel_mappings)
        return self

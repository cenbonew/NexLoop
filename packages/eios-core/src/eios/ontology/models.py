from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MISSING = object()
_INTERNAL_EVENT_NAMES = frozenset(
    {"ObjectCreated", "ObjectUpdated", "RelationLinked", "RelationUnlinked"}
)


class OntologyValidationError(ValueError):
    code = "property_validation_failed"


class PropertyValueType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    JSON = "json"


class RelationCardinality(str, Enum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class PropertyTypeKind(str, Enum):
    """Discriminated property-type union: six scalar leaves plus composites.

    GEO_* is schema-level GeoJSON validation only; TIMESERIES_REF/MEDIA_REF
    are artifact references. Dedicated geo/timeseries query engines are an
    explicit non-goal (master plan, stage 7 honesty boundary).
    """

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    JSON = "json"
    ARRAY = "array"
    STRUCT = "struct"
    GEO_POINT = "geo_point"
    GEO_SHAPE = "geo_shape"
    TIMESERIES_REF = "timeseries_ref"
    MEDIA_REF = "media_ref"


_SCALAR_TYPE_KINDS = frozenset(
    {
        PropertyTypeKind.STRING,
        PropertyTypeKind.INTEGER,
        PropertyTypeKind.NUMBER,
        PropertyTypeKind.BOOLEAN,
        PropertyTypeKind.DATETIME,
        PropertyTypeKind.JSON,
    }
)
_REF_TYPE_KINDS = frozenset(
    {PropertyTypeKind.TIMESERIES_REF, PropertyTypeKind.MEDIA_REF}
)
_MAX_TYPE_DEPTH = 16
_MAX_VALUE_DEPTH = 64


class StructField(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    descriptor: "PropertyTypeDescriptor"
    required: bool = False

    @model_validator(mode="after")
    def validate_definition(self) -> "StructField":
        object.__setattr__(self, "name", _assert_name(self.name, "name"))
        return self


class PropertyTypeDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: PropertyTypeKind
    element: "PropertyTypeDescriptor | None" = None
    fields: tuple[StructField, ...] = ()
    enum: tuple[Any, ...] = ()
    format: str = ""
    unit: str = ""

    @model_validator(mode="after")
    def validate_definition(self) -> "PropertyTypeDescriptor":
        self._validate_shape(depth=0)
        return self

    def _validate_shape(self, *, depth: int) -> None:
        if depth > _MAX_TYPE_DEPTH:
            raise ValueError("type descriptor is too deep")
        if self.kind is PropertyTypeKind.ARRAY:
            if self.element is None:
                raise ValueError("array descriptor requires an element type")
            if self.fields or self.enum:
                raise ValueError("array descriptor cannot declare fields or enum")
            self.element._validate_shape(depth=depth + 1)
            return
        if self.element is not None:
            raise ValueError("only array descriptors declare an element type")
        if self.kind is PropertyTypeKind.STRUCT:
            if not self.fields:
                raise ValueError("struct descriptor requires fields")
            names = [field.name for field in self.fields]
            if len(names) != len(set(names)):
                raise ValueError("duplicate struct field name")
            if self.enum:
                raise ValueError("struct descriptor cannot declare an enum")
            for field in self.fields:
                field.descriptor._validate_shape(depth=depth + 1)
            return
        if self.fields:
            raise ValueError("only struct descriptors declare fields")
        if self.enum:
            if self.kind not in (
                PropertyTypeKind.STRING,
                PropertyTypeKind.INTEGER,
                PropertyTypeKind.NUMBER,
            ):
                raise ValueError("enum constraints require string or numeric kinds")
            for item in self.enum:
                if not _matches_scalar_enum_kind(self.kind, item):
                    raise ValueError("enum values must match the declared kind")
            if len(self.enum) != len(set(self.enum)):
                raise ValueError("duplicate enum values")


def _matches_scalar_enum_kind(kind: PropertyTypeKind, value: Any) -> bool:
    if kind is PropertyTypeKind.STRING:
        return isinstance(value, str)
    if kind is PropertyTypeKind.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is PropertyTypeKind.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


class PropertyRenderHint(str, Enum):
    """How a UI should format one property value."""

    PLAIN = "plain"
    EMAIL = "email"
    URL = "url"
    CURRENCY = "currency"
    PERCENT = "percent"


class PropertyVisibility(str, Enum):
    """How prominently a UI should surface one property."""

    PROMINENT = "prominent"
    NORMAL = "normal"
    HIDDEN = "hidden"


def _assert_name(value: str, field_name: str) -> str:
    clean = str(value or "").strip()
    if not _NAME_PATTERN.fullmatch(clean):
        raise ValueError(f"{field_name} must match {_NAME_PATTERN.pattern}")
    return clean


def _normalize_value(
    value_type: PropertyValueType, value: Any, *, nullable: bool, field_name: str
) -> Any:
    if value is None:
        if nullable:
            return None
        raise OntologyValidationError(f"{field_name} is not nullable")
    if value_type is PropertyValueType.STRING:
        if not isinstance(value, str):
            raise OntologyValidationError(f"{field_name} must be string")
        return value
    if value_type is PropertyValueType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise OntologyValidationError(f"{field_name} must be integer")
        return value
    if value_type is PropertyValueType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OntologyValidationError(f"{field_name} must be number")
        return value
    if value_type is PropertyValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise OntologyValidationError(f"{field_name} must be boolean")
        return value
    if value_type is PropertyValueType.DATETIME:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise OntologyValidationError(
                    f"{field_name} must be ISO-8601 datetime"
                ) from exc
        raise OntologyValidationError(f"{field_name} must be datetime")
    if value_type is PropertyValueType.JSON:
        if isinstance(value, (str, int, float, bool, list, dict)):
            return value
        raise OntologyValidationError(f"{field_name} must be JSON-compatible")
    raise OntologyValidationError(f"unsupported property type: {value_type}")


def _normalize_typed_value(
    descriptor: "PropertyTypeDescriptor",
    value: Any,
    *,
    nullable: bool,
    field_name: str,
    depth: int,
) -> Any:
    """Recursive, fail-closed validation against a type descriptor."""

    if depth > _MAX_VALUE_DEPTH:
        raise OntologyValidationError(f"{field_name} is too deeply nested")
    if value is None:
        if nullable:
            return None
        raise OntologyValidationError(f"{field_name} is not nullable")
    kind = descriptor.kind
    if kind in _SCALAR_TYPE_KINDS:
        normalized = _normalize_value(
            PropertyValueType(kind.value),
            value,
            nullable=nullable,
            field_name=field_name,
        )
        if descriptor.enum and normalized not in descriptor.enum:
            raise OntologyValidationError(
                f"{field_name} must be one of the declared enum values"
            )
        return normalized
    if kind is PropertyTypeKind.ARRAY:
        if not isinstance(value, list):
            raise OntologyValidationError(f"{field_name} must be an array")
        assert descriptor.element is not None  # shape invariant
        return [
            _normalize_typed_value(
                descriptor.element,
                item,
                nullable=False,
                field_name=f"{field_name}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    if kind is PropertyTypeKind.STRUCT:
        if not isinstance(value, dict):
            raise OntologyValidationError(f"{field_name} must be an object")
        declared = {field.name: field for field in descriptor.fields}
        unknown = sorted(set(value) - set(declared))
        if unknown:
            raise OntologyValidationError(
                f"{field_name} has an undeclared field: {unknown[0]}"
            )
        normalized: dict[str, Any] = {}
        for name, field in declared.items():
            if name not in value:
                if field.required:
                    raise OntologyValidationError(
                        f"{field_name}.{name} is required"
                    )
                continue
            normalized[name] = _normalize_typed_value(
                field.descriptor,
                value[name],
                nullable=not field.required,
                field_name=f"{field_name}.{name}",
                depth=depth + 1,
            )
        return normalized
    if kind is PropertyTypeKind.GEO_POINT:
        if (
            not isinstance(value, dict)
            or value.get("type") != "Point"
            or not isinstance(value.get("coordinates"), list)
            or len(value["coordinates"]) != 2
        ):
            raise OntologyValidationError(
                f"{field_name} must be a GeoJSON Point"
            )
        longitude, latitude = value["coordinates"]
        for item in (longitude, latitude):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise OntologyValidationError(
                    f"{field_name} coordinates must be numbers"
                )
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise OntologyValidationError(
                f"{field_name} coordinates are out of range"
            )
        return {"type": "Point", "coordinates": [longitude, latitude]}
    if kind is PropertyTypeKind.GEO_SHAPE:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("type"), str)
            or not value.get("type")
            or not isinstance(value.get("coordinates"), list)
        ):
            raise OntologyValidationError(
                f"{field_name} must be a GeoJSON geometry"
            )
        return dict(value)
    if kind in _REF_TYPE_KINDS:
        if not isinstance(value, str) or not value.strip():
            raise OntologyValidationError(
                f"{field_name} must be a non-empty artifact reference"
            )
        return value
    raise OntologyValidationError(  # pragma: no cover - enum is closed
        f"unsupported property kind: {kind}"
    )


def assert_descriptor_compatible(
    current: "PropertyTypeDescriptor",
    candidate: "PropertyTypeDescriptor",
    *,
    path: str,
) -> None:
    """Structural compatibility: no kind change, no narrowing, no field loss."""

    if current.kind is not candidate.kind:
        raise ValueError(f"property kind cannot change: {path}")
    if current.kind is PropertyTypeKind.ARRAY:
        assert current.element is not None and candidate.element is not None
        assert_descriptor_compatible(
            current.element, candidate.element, path=f"{path}[]"
        )
        return
    if current.kind is PropertyTypeKind.STRUCT:
        current_fields = {field.name: field for field in current.fields}
        candidate_fields = {field.name: field for field in candidate.fields}
        removed = sorted(set(current_fields) - set(candidate_fields))
        if removed:
            raise ValueError(f"struct field cannot be removed: {path}.{removed[0]}")
        for name in sorted(set(candidate_fields) - set(current_fields)):
            if candidate_fields[name].required:
                raise ValueError(
                    f"new struct field cannot be required: {path}.{name}"
                )
        for name in sorted(set(current_fields) & set(candidate_fields)):
            assert_descriptor_compatible(
                current_fields[name].descriptor,
                candidate_fields[name].descriptor,
                path=f"{path}.{name}",
            )
        return
    if current.enum:
        removed_values = [
            item for item in current.enum if item not in candidate.enum
        ] if candidate.enum else []
        if candidate.enum and removed_values:
            raise ValueError(f"enum values cannot be removed: {path}")


class PropertyDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    property_name: str
    value_type: PropertyValueType
    required: bool = False
    nullable: bool = False
    default_value: Any = None
    description: str = ""
    # Display metadata (additive after the digest freeze; defaults are omitted
    # from idempotency digests — see semantics._prune_additive_defaults).
    display_name: str = ""
    render_hint: PropertyRenderHint = PropertyRenderHint.PLAIN
    visibility: PropertyVisibility = PropertyVisibility.NORMAL
    # Rich type system (stage 7, additive): the discriminated-union
    # descriptor. Legacy definitions without one keep the flat scalar
    # semantics of value_type; with one, value_type must agree (the scalar
    # kind, or JSON for composite/reference kinds — the jsonb storage shape).
    type_descriptor: PropertyTypeDescriptor | None = None

    @model_validator(mode="after")
    def validate_definition(self) -> PropertyDefinition:
        object.__setattr__(
            self, "property_name", _assert_name(self.property_name, "property_name")
        )
        descriptor = self.type_descriptor
        if descriptor is not None:
            if descriptor.kind in _SCALAR_TYPE_KINDS:
                if descriptor.kind.value != self.value_type.value:
                    raise ValueError(
                        "scalar type_descriptor must agree with value_type"
                    )
            elif self.value_type is not PropertyValueType.JSON:
                raise ValueError(
                    "composite type_descriptor requires value_type json"
                )
        if self.default_value is not None:
            self.normalize_value(self.default_value, field_name="default_value")
        return self

    def normalize_value(self, value: Any, *, field_name: str) -> Any:
        """Validate one value against the effective type of this property."""

        if self.type_descriptor is None:
            return _normalize_value(
                self.value_type,
                value,
                nullable=self.nullable,
                field_name=field_name,
            )
        return _normalize_typed_value(
            self.type_descriptor,
            value,
            nullable=self.nullable,
            field_name=field_name,
            depth=0,
        )


class PropertyGroup(BaseModel):
    """Named display grouping of an Object Type's properties."""

    model_config = ConfigDict(frozen=True)

    group_name: str
    display_name: str = ""
    property_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_definition(self) -> PropertyGroup:
        object.__setattr__(
            self, "group_name", _assert_name(self.group_name, "group_name")
        )
        names = [_assert_name(name, "property_names") for name in self.property_names]
        if len(names) != len(set(names)):
            raise ValueError("duplicate property name in group")
        object.__setattr__(self, "property_names", tuple(names))
        return self


class DerivedPropertyBinding(BaseModel):
    """A Function-backed Object property (stage 11).

    Binds one derived property name on an Object Type to a published read-only
    Function. The value is never stored: the data plane computes it at read
    time by executing the bound Function (through the stage-8 result cache)
    with the object's dependency property values as input, so the cache key
    varies by value and self-invalidates when a dependency changes.
    """

    model_config = ConfigDict(frozen=True)

    property_name: str
    function_name: str
    function_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_binding(self) -> DerivedPropertyBinding:
        object.__setattr__(
            self,
            "property_name",
            _assert_name(self.property_name, "property_name"),
        )
        clean = str(self.function_name or "").strip()
        if not clean or any(
            not _NAME_PATTERN.fullmatch(part) for part in clean.split(".")
        ):
            raise ValueError("function_name must be a stable dotted name")
        object.__setattr__(self, "function_name", clean)
        return self


class ObjectTypeDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    type_name: str
    display_name: str = ""
    description: str = ""
    properties: tuple[PropertyDefinition, ...] = ()
    version: int = Field(default=1, ge=1)
    # Display metadata (additive after the digest freeze; defaults are omitted
    # from idempotency digests — see semantics._prune_additive_defaults).
    title_property: str = ""
    icon: str = ""
    color: str = ""
    plural_display_name: str = ""
    property_groups: tuple[PropertyGroup, ...] = ()
    # Natural business key (stage 7, additive): when declared, upserts derive
    # a deterministic object_id from these property values, so ingesting the
    # same business entity twice deduplicates without a new migration — the
    # existing (tenant, type, object_id) primary key enforces uniqueness.
    primary_key: tuple[str, ...] = ()
    # Function-backed derived properties (stage 11, additive): computed at
    # read time, never stored, cache-keyed by dependency values.
    derived_properties: tuple[DerivedPropertyBinding, ...] = ()
    # When enabled, object mutations must originate from a governed Action
    # execution carrying a live permit. Schema versions retain every mode
    # change as immutable configuration audit evidence.
    only_edit_via_actions: bool = False

    @model_validator(mode="after")
    def validate_definition(self) -> ObjectTypeDefinition:
        object.__setattr__(self, "type_name", _assert_name(self.type_name, "type_name"))
        names = [item.property_name for item in self.properties]
        if len(names) != len(set(names)):
            raise ValueError("duplicate property name")
        known = set(names)
        if self.title_property and self.title_property not in known:
            raise ValueError("title_property must reference a declared property")
        group_names = [group.group_name for group in self.property_groups]
        if len(group_names) != len(set(group_names)):
            raise ValueError("duplicate property group name")
        for group in self.property_groups:
            for property_name in group.property_names:
                if property_name not in known:
                    raise ValueError(
                        "property group must reference declared properties"
                    )
        if len(self.primary_key) != len(set(self.primary_key)):
            raise ValueError("duplicate primary_key property")
        for property_name in self.primary_key:
            if property_name not in known:
                raise ValueError(
                    "primary_key must reference declared properties"
                )
            if not self.property_map()[property_name].required:
                raise ValueError("primary_key properties must be required")
        derived_names = [item.property_name for item in self.derived_properties]
        if len(derived_names) != len(set(derived_names)):
            raise ValueError("duplicate derived property name")
        for derived_name in derived_names:
            if derived_name in known:
                raise ValueError(
                    "derived property name collides with a declared property"
                )
        return self

    def property_map(self) -> dict[str, PropertyDefinition]:
        return {item.property_name: item for item in self.properties}


class RelationTypeDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    relation_name: str
    source_type: str
    target_type: str
    cardinality: RelationCardinality = RelationCardinality.MANY_TO_MANY
    inverse_name: str = ""
    description: str = ""
    version: int = Field(default=1, ge=1)
    # Typed link payload schema (additive after the digest freeze; the empty
    # default is omitted from idempotency digests — see
    # semantics._prune_additive_defaults). Declared properties turn the free
    # relation metadata jsonb into a schema-constrained payload.
    properties: tuple[PropertyDefinition, ...] = ()

    @model_validator(mode="after")
    def validate_definition(self) -> RelationTypeDefinition:
        object.__setattr__(
            self, "relation_name", _assert_name(self.relation_name, "relation_name")
        )
        object.__setattr__(
            self, "source_type", _assert_name(self.source_type, "source_type")
        )
        object.__setattr__(
            self, "target_type", _assert_name(self.target_type, "target_type")
        )
        if self.inverse_name:
            object.__setattr__(
                self, "inverse_name", _assert_name(self.inverse_name, "inverse_name")
            )
        names = [item.property_name for item in self.properties]
        if len(names) != len(set(names)):
            raise ValueError("duplicate property name")
        return self

    def property_map(self) -> dict[str, PropertyDefinition]:
        return {item.property_name: item for item in self.properties}


class EventTypeDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_name: str
    object_type: str
    payload_properties: tuple[PropertyDefinition, ...] = ()
    description: str = ""
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_definition(self) -> EventTypeDefinition:
        object.__setattr__(
            self, "event_name", _assert_name(self.event_name, "event_name")
        )
        object.__setattr__(
            self, "object_type", _assert_name(self.object_type, "object_type")
        )
        if self.event_name in _INTERNAL_EVENT_NAMES:
            raise ValueError("event_name is reserved for internal events")
        names = [item.property_name for item in self.payload_properties]
        if len(names) != len(set(names)):
            raise ValueError("duplicate property name")
        return self


class OntologySchemaSnapshot(BaseModel):
    tenant_id: str
    object_types: tuple[ObjectTypeDefinition, ...] = ()
    relation_types: tuple[RelationTypeDefinition, ...] = ()
    event_types: tuple[EventTypeDefinition, ...] = ()
    revision: str


def _validate_properties(
    definitions: tuple[PropertyDefinition, ...],
    values: dict[str, Any],
    *,
    partial: bool = False,
) -> dict[str, Any]:
    definition_map = {item.property_name: item for item in definitions}
    unknown = sorted(set(values) - set(definition_map))
    if unknown:
        raise OntologyValidationError(f"unknown_property:{unknown[0]}")
    normalized: dict[str, Any] = {}
    for name, definition in definition_map.items():
        raw = values.get(name, _MISSING)
        if raw is _MISSING:
            if not partial and definition.default_value is not None:
                raw = definition.default_value
            elif not partial and definition.required:
                raise OntologyValidationError(f"missing_required_property:{name}")
            else:
                continue
        normalized[name] = definition.normalize_value(raw, field_name=name)
    return normalized


def validate_object_properties(
    definition: ObjectTypeDefinition,
    values: dict[str, Any],
    *,
    partial: bool = False,
) -> dict[str, Any]:
    return _validate_properties(definition.properties, values, partial=partial)


def validate_event_payload(
    definition: EventTypeDefinition, values: dict[str, Any]
) -> dict[str, Any]:
    return _validate_properties(definition.payload_properties, values)


def validate_relation_properties(
    definition: RelationTypeDefinition, values: dict[str, Any]
) -> dict[str, Any]:
    """Validate a link payload against the relation's declared properties.

    Relation types declaring no properties keep the legacy free-form
    metadata semantics untouched.
    """

    if not definition.properties:
        return dict(values)
    return _validate_properties(definition.properties, values)

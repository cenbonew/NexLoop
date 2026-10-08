from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
)


MAX_POLICY_DEPTH = 20
MAX_POLICY_NODES = 2_048
MAX_POLICY_COMPARISON_WEIGHT = 10_000
MAX_POLICY_CHILDREN = 256
MAX_POLICY_COLLECTION_ITEMS = 256
MAX_POLICY_STRING_BYTES = 1_024
MAX_POLICY_IDENTIFIER_BYTES = 255
MIN_POLICY_INTEGER = -(2**63)
MAX_POLICY_INTEGER = 2**63 - 1
_RFC3339_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _bounded_text(value: str, field_name: str, *, identifier: bool = False) -> str:
    limit = MAX_POLICY_IDENTIFIER_BYTES if identifier else MAX_POLICY_STRING_BYTES
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-blank text")
    return value


def _field_snapshot(value: object) -> object:
    if isinstance(value, BaseModel):
        return (
            type(value),
            tuple(
                (field_name, _field_snapshot(value.__dict__[field_name]))
                for field_name in type(value).model_fields
            ),
        )
    if type(value) is tuple:
        return (tuple, tuple(_field_snapshot(item) for item in value))
    return (type(value), value)


class _StrictFrozenModel(BaseModel):
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
            (field_name, _field_snapshot(self.__dict__[field_name]))
            for field_name in type(self).model_fields
        )

    def _construction_is_pristine(self) -> bool:
        return self.__pydantic_private__ == {
            "_validated_snapshot": self._field_snapshot()
        }


class SubjectRef(_StrictFrozenModel):
    kind: Literal["subject"]
    attribute: str

    @field_validator("attribute")
    @classmethod
    def _validate_attribute(cls, value: str) -> str:
        return _bounded_text(value, "attribute", identifier=True)


class ApplicationRef(_StrictFrozenModel):
    kind: Literal["application"]
    attribute: str

    @field_validator("attribute")
    @classmethod
    def _validate_attribute(cls, value: str) -> str:
        return _bounded_text(value, "attribute", identifier=True)


class ResourceRef(_StrictFrozenModel):
    kind: Literal["resource"]
    attribute: str

    @field_validator("attribute")
    @classmethod
    def _validate_attribute(cls, value: str) -> str:
        return _bounded_text(value, "attribute", identifier=True)


class ObjectPropertyRef(_StrictFrozenModel):
    kind: Literal["object_property"]
    object_type: str
    property_name: str

    @field_validator("object_type")
    @classmethod
    def _validate_object_type(cls, value: str) -> str:
        return _bounded_text(value, "object_type", identifier=True)

    @field_validator("property_name")
    @classmethod
    def _validate_property_name(cls, value: str) -> str:
        return _bounded_text(value, "property_name", identifier=True)


class ActionParameterRef(_StrictFrozenModel):
    kind: Literal["action_parameter"]
    parameter_name: str

    @field_validator("parameter_name")
    @classmethod
    def _validate_parameter_name(cls, value: str) -> str:
        return _bounded_text(value, "parameter_name", identifier=True)


class TrustedTimeRef(_StrictFrozenModel):
    kind: Literal["trusted_time"]
    value: Literal["now"]


PolicyReference: TypeAlias = Annotated[
    SubjectRef
    | ApplicationRef
    | ResourceRef
    | ObjectPropertyRef
    | ActionParameterRef
    | TrustedTimeRef,
    Field(discriminator="kind"),
]
PolicyScalar: TypeAlias = bool | int | float | str | datetime
PolicyValue: TypeAlias = PolicyScalar | tuple[PolicyScalar, ...]


def _scalar_identity(value: PolicyScalar) -> tuple[type[object], object]:
    if type(value) is datetime:
        return (datetime, value)
    return (type(value), value)


def _validate_scalar(value: object) -> PolicyScalar:
    if type(value) is str:
        return _bounded_text(value, "literal value")
    if type(value) is bool:
        return value
    if type(value) is int:
        if not MIN_POLICY_INTEGER <= value <= MAX_POLICY_INTEGER:
            raise ValueError("literal integer is outside the supported range")
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("literal number must be finite")
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("literal datetime must be timezone-aware")
        return value.astimezone(UTC)
    raise ValueError("literal value type is not supported")


class LiteralValue(_StrictFrozenModel):
    kind: Literal["literal"]
    value_type: Literal["scalar", "datetime"] = "scalar"
    value: PolicyValue

    @field_validator("value", mode="before")
    @classmethod
    def _decode_datetime(cls, value: object, info: ValidationInfo) -> object:
        if info.data.get("value_type") != "datetime" or type(value) is datetime:
            return value
        if (
            type(value) is not str
            or len(value.encode("utf-8")) > 64
            or _RFC3339_DATETIME.fullmatch(value) is None
        ):
            raise ValueError("datetime literal must use bounded RFC3339 text")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("datetime literal must use bounded RFC3339 text") from None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: PolicyValue, info: ValidationInfo) -> PolicyValue:
        if info.data.get("value_type") == "datetime":
            if type(value) is not datetime:
                raise ValueError("datetime literal must contain exactly one datetime")
            return _validate_scalar(value)
        if type(value) is tuple:
            if len(value) > MAX_POLICY_COLLECTION_ITEMS:
                raise ValueError("literal collection is too large")
            if any(type(item) is datetime for item in value):
                raise ValueError("datetime literal collections are not supported")
            checked = tuple(_validate_scalar(item) for item in value)
            identities = tuple(_scalar_identity(item) for item in checked)
            if len(identities) != len(set(identities)):
                raise ValueError("literal collection must not contain duplicates")
            return checked
        if type(value) is datetime:
            raise ValueError("datetime literal requires explicit value_type")
        return _validate_scalar(value)


PolicyOperand: TypeAlias = Annotated[
    SubjectRef
    | ApplicationRef
    | ResourceRef
    | ObjectPropertyRef
    | ActionParameterRef
    | TrustedTimeRef
    | LiteralValue,
    Field(discriminator="kind"),
]


class _PolicyNode(_StrictFrozenModel):
    node_id: str

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, value: str) -> str:
        return _bounded_text(value, "node_id", identifier=True)


class All(_PolicyNode):
    op: Literal["all"]
    children: tuple["PolicyExpression", ...] = Field(
        min_length=1,
        max_length=MAX_POLICY_CHILDREN,
    )


class AnyOf(_PolicyNode):
    op: Literal["any"]
    children: tuple["PolicyExpression", ...] = Field(
        min_length=1,
        max_length=MAX_POLICY_CHILDREN,
    )


class Not(_PolicyNode):
    op: Literal["not"]
    child: "PolicyExpression"


class _BinaryComparison(_PolicyNode):
    left: PolicyOperand
    right: PolicyOperand


class Eq(_BinaryComparison):
    op: Literal["eq"]


class Neq(_BinaryComparison):
    op: Literal["neq"]


class In(_BinaryComparison):
    op: Literal["in"]


class Contains(_BinaryComparison):
    op: Literal["contains"]


class Subset(_BinaryComparison):
    op: Literal["subset"]


class Lt(_BinaryComparison):
    op: Literal["lt"]


class Lte(_BinaryComparison):
    op: Literal["lte"]


class Gt(_BinaryComparison):
    op: Literal["gt"]


class Gte(_BinaryComparison):
    op: Literal["gte"]


class Exists(_PolicyNode):
    op: Literal["exists"]
    value: PolicyReference


PolicyExpression: TypeAlias = Annotated[
    All
    | AnyOf
    | Not
    | Eq
    | Neq
    | In
    | Contains
    | Subset
    | Lt
    | Lte
    | Gt
    | Gte
    | Exists,
    Field(discriminator="op"),
]


for _model in (All, AnyOf, Not):
    _model.model_rebuild(_types_namespace={"PolicyExpression": PolicyExpression})


Any = AnyOf


__all__ = [
    "ActionParameterRef",
    "All",
    "Any",
    "AnyOf",
    "ApplicationRef",
    "Contains",
    "Eq",
    "Exists",
    "Gt",
    "Gte",
    "In",
    "LiteralValue",
    "Lt",
    "Lte",
    "MAX_POLICY_CHILDREN",
    "MAX_POLICY_COLLECTION_ITEMS",
    "MAX_POLICY_COMPARISON_WEIGHT",
    "MAX_POLICY_DEPTH",
    "MAX_POLICY_IDENTIFIER_BYTES",
    "MAX_POLICY_INTEGER",
    "MAX_POLICY_NODES",
    "MAX_POLICY_STRING_BYTES",
    "MIN_POLICY_INTEGER",
    "Neq",
    "Not",
    "ObjectPropertyRef",
    "PolicyExpression",
    "PolicyOperand",
    "PolicyReference",
    "PolicyScalar",
    "PolicyValue",
    "ResourceRef",
    "SubjectRef",
    "Subset",
    "TrustedTimeRef",
]

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from .models import PolicyExpression, _StrictFrozenModel


class PolicyPlan(_StrictFrozenModel):
    policy_id: str
    revision: int = Field(ge=1)
    condition: PolicyExpression

    @field_validator("policy_id")
    @classmethod
    def _validate_policy_id(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or len(value.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("policy_id must be bounded non-blank text")
        return value


class PolicyEvaluationResult(_StrictFrozenModel):
    matched: bool
    had_error: bool
    matched_node_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_evidence(self) -> PolicyEvaluationResult:
        if (not self.matched or self.had_error) and self.matched_node_ids:
            raise ValueError("non-matching evaluations cannot contain matched nodes")
        if len(self.matched_node_ids) != len(set(self.matched_node_ids)):
            raise ValueError("matched_node_ids must not contain duplicates")
        return self


class ObjectPropertyType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_SET = "string_set"
    INTEGER_SET = "integer_set"
    NUMBER_SET = "number_set"
    BOOLEAN_SET = "boolean_set"


class ObjectPropertyDefinition(_StrictFrozenModel):
    object_type: str
    property_name: str
    value_type: ObjectPropertyType

    @field_validator("object_type", "property_name")
    @classmethod
    def _validate_name(cls, value: str, info: Any) -> str:
        if (
            not value
            or value != value.strip()
            or len(value.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"{info.field_name} must be bounded non-blank text")
        return value


class ObjectSchema(_StrictFrozenModel):
    definitions: tuple[ObjectPropertyDefinition, ...] = Field(
        min_length=1,
        max_length=2_048,
    )

    @model_validator(mode="after")
    def _validate_unique_definitions(self) -> ObjectSchema:
        keys = tuple(
            (definition.object_type, definition.property_name)
            for definition in self.definitions
        )
        if len(keys) != len(set(keys)):
            raise ValueError("object property definitions must be unique")
        return self


class SqlDialect(str, Enum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class CompiledSqlPlan(_StrictFrozenModel):
    """Row-filter plan compiled for the fail-closed REQUIRE polarity only.

    ``predicate`` folds evaluation errors (three-valued NULL) to FALSE, which
    is only safe when the predicate selects rows to keep (``WHERE predicate``).
    ``error_predicate`` is the explicit error channel: it is TRUE exactly when
    the underlying three-valued expression evaluates to NULL for a row. Both
    predicates bind the same ``parameters`` tuple. DENY_IF polarity is rejected
    at compile time and must never be expressed as ``WHERE NOT predicate``.
    """

    dialect: SqlDialect
    predicate: str
    error_predicate: str
    parameters: tuple[Any, ...] = Field(max_length=10_000)

    @field_validator("predicate", "error_predicate")
    @classmethod
    def _validate_predicate(cls, value: str) -> str:
        if not value or len(value.encode("utf-8")) > 1_000_000:
            raise ValueError("SQL predicate must be non-empty and bounded")
        return value


__all__ = [
    "CompiledSqlPlan",
    "ObjectPropertyDefinition",
    "ObjectPropertyType",
    "ObjectSchema",
    "PolicyEvaluationResult",
    "PolicyPlan",
    "SqlDialect",
]

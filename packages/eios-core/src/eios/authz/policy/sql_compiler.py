from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from eios.authz.errors import AuthorizationValidationError

from .models import (
    All,
    AnyOf,
    Contains,
    Eq,
    Exists,
    Gt,
    Gte,
    In,
    LiteralValue,
    Lt,
    Lte,
    MAX_POLICY_COLLECTION_ITEMS,
    MAX_POLICY_COMPARISON_WEIGHT,
    MAX_POLICY_INTEGER,
    MAX_POLICY_STRING_BYTES,
    MIN_POLICY_INTEGER,
    Neq,
    Not,
    ObjectPropertyRef,
    Subset,
)
from .plans import (
    CompiledSqlPlan,
    ObjectPropertyDefinition,
    ObjectPropertyType,
    ObjectSchema,
    SqlDialect,
)
from .registry import PolicyEffect
from .validator import validate_policy_expression


class PolicySqlCompileError(AuthorizationValidationError):
    code = "policy_sql_unsupported"


MAX_COMPILED_SQL_BYTES = 1_000_000
MAX_COMPILED_SQL_PARAMETERS = 10_000


@dataclass(frozen=True, slots=True)
class _Fragment:
    sql: str
    parameters: tuple[object, ...]

    def __post_init__(self) -> None:
        if (
            type(self.sql) is not str
            or not self.sql
            or len(self.sql.encode("utf-8")) > MAX_COMPILED_SQL_BYTES
            or type(self.parameters) is not tuple
            or len(self.parameters) > MAX_COMPILED_SQL_PARAMETERS
        ):
            raise PolicySqlCompileError("policy cannot be compiled safely")


@dataclass(frozen=True, slots=True)
class _CompiledNode:
    expression: _Fragment


_SCALAR_TYPES = frozenset(
    {
        ObjectPropertyType.STRING,
        ObjectPropertyType.INTEGER,
        ObjectPropertyType.NUMBER,
        ObjectPropertyType.BOOLEAN,
    }
)
_COLLECTION_MEMBER_TYPE = {
    ObjectPropertyType.STRING_SET: ObjectPropertyType.STRING,
    ObjectPropertyType.INTEGER_SET: ObjectPropertyType.INTEGER,
    ObjectPropertyType.NUMBER_SET: ObjectPropertyType.NUMBER,
    ObjectPropertyType.BOOLEAN_SET: ObjectPropertyType.BOOLEAN,
}
_UNSAFE_NUMBER_TYPES = frozenset(
    {
        ObjectPropertyType.NUMBER,
        ObjectPropertyType.NUMBER_SET,
    }
)
_POSTGRES_UNSAFE_INTEGER_TYPES = frozenset(
    {
        ObjectPropertyType.INTEGER,
        ObjectPropertyType.INTEGER_SET,
    }
)
_SQLITE_UNSAFE_STRING_TYPES = frozenset(
    {
        ObjectPropertyType.STRING,
        ObjectPropertyType.STRING_SET,
    }
)
_SQL_OPERATOR = {
    Eq: "=",
    Neq: "<>",
    Lt: "<",
    Lte: "<=",
    Gt: ">",
    Gte: ">=",
}


def _join(operator: str, fragments: tuple[_Fragment, ...]) -> _Fragment:
    return _Fragment(
        sql="(" + f") {operator} (".join(fragment.sql for fragment in fragments) + ")",
        parameters=tuple(
            parameter for fragment in fragments for parameter in fragment.parameters
        ),
    )


def _case_when_false(valid: _Fragment, matched: _Fragment) -> _Fragment:
    return _Fragment(
        sql=f"CASE WHEN {valid.sql} THEN ({matched.sql}) ELSE FALSE END",
        parameters=valid.parameters + matched.parameters,
    )


def _case_when_null(valid: _Fragment, matched: _Fragment) -> _Fragment:
    return _Fragment(
        sql=f"CASE WHEN {valid.sql} THEN ({matched.sql}) ELSE NULL END",
        parameters=valid.parameters + matched.parameters,
    )


def _negate(fragment: _Fragment) -> _Fragment:
    return _Fragment(
        sql=f"NOT ({fragment.sql})",
        parameters=fragment.parameters,
    )


def _schema_map(schema: object) -> dict[tuple[str, str], ObjectPropertyDefinition]:
    if type(schema) is not ObjectSchema:
        raise PolicySqlCompileError("policy cannot be compiled safely")
    try:
        if (
            set(schema.__dict__) != set(ObjectSchema.model_fields)
            or schema.__pydantic_extra__ is not None
            or not schema._construction_is_pristine()
            or type(schema.definitions) is not tuple
            or any(
                type(definition) is not ObjectPropertyDefinition
                or set(definition.__dict__)
                != set(ObjectPropertyDefinition.model_fields)
                or definition.__pydantic_extra__ is not None
                or not definition._construction_is_pristine()
                or type(definition.value_type) is not ObjectPropertyType
                for definition in schema.definitions
            )
        ):
            raise ValueError
        validated = ObjectSchema.model_validate(
            schema.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception:
        raise PolicySqlCompileError("policy cannot be compiled safely") from None
    return {
        (definition.object_type, definition.property_name): definition
        for definition in validated.definitions
    }


def _definition(
    reference: object,
    definitions: dict[tuple[str, str], ObjectPropertyDefinition],
) -> ObjectPropertyDefinition:
    if type(reference) is not ObjectPropertyRef:
        raise PolicySqlCompileError("policy cannot be compiled safely")
    found = definitions.get((reference.object_type, reference.property_name))
    if found is None:
        raise PolicySqlCompileError("policy cannot be compiled safely")
    return found


def _literal(operand: object) -> object:
    if type(operand) is not LiteralValue:
        raise PolicySqlCompileError("policy cannot be compiled safely")
    return operand.value


def _value_matches_type(value: object, value_type: ObjectPropertyType) -> bool:
    if value_type is ObjectPropertyType.STRING:
        return type(value) is str
    if value_type is ObjectPropertyType.INTEGER:
        return type(value) is int
    if value_type is ObjectPropertyType.NUMBER:
        return type(value) is float
    if value_type is ObjectPropertyType.BOOLEAN:
        return type(value) is bool
    return False


def _require_scalar(value: object, value_type: ObjectPropertyType) -> object:
    if value_type not in _SCALAR_TYPES or not _value_matches_type(value, value_type):
        raise PolicySqlCompileError("policy cannot be compiled safely")
    return value


def _require_collection(
    value: object,
    value_type: ObjectPropertyType,
) -> tuple[object, ...]:
    member_type = _COLLECTION_MEMBER_TYPE.get(value_type)
    if type(value) is not tuple or member_type is None:
        raise PolicySqlCompileError("policy cannot be compiled safely")
    if not all(_value_matches_type(item, member_type) for item in value):
        raise PolicySqlCompileError("policy cannot be compiled safely")
    return value


def _enforce_sql_comparison_budget(expression: object) -> None:
    """Reject plans whose persisted collection rows can exhaust the evaluator budget."""

    total = 0
    stack = [expression]
    while stack:
        node = stack.pop()
        if type(node) in (All, AnyOf):
            stack.extend(cast(All | AnyOf, node).children)
            continue
        if type(node) is Not:
            stack.append(cast(Not, node).child)
            continue
        if type(node) is Contains:
            total += MAX_POLICY_COLLECTION_ITEMS
        elif type(node) is Subset:
            right = cast(Subset, node).right
            right_value = right.value if type(right) is LiteralValue else ()
            right_size = len(right_value) if type(right_value) is tuple else 0
            total += MAX_POLICY_COLLECTION_ITEMS + right_size
        elif type(node) is In:
            right = cast(In, node).right
            right_value = right.value if type(right) is LiteralValue else ()
            total += max(1, len(right_value) if type(right_value) is tuple else 1)
        elif type(node) in _SQL_OPERATOR:
            total += 1
        if total > MAX_POLICY_COMPARISON_WEIGHT:
            raise PolicySqlCompileError("policy cannot be compiled safely")


class _DialectCompiler:
    def __init__(
        self,
        definitions: dict[tuple[str, str], ObjectPropertyDefinition],
        dialect: SqlDialect,
    ) -> None:
        self._definitions = definitions
        self._dialect = dialect
        self._placeholder = "?" if dialect is SqlDialect.SQLITE else "%s"
        self.object_types: set[str] = set()

    def _json_path(self, property_name: str) -> str:
        escaped = property_name.replace("\\", "\\\\").replace('"', '\\"')
        return f'$."{escaped}"'

    def _property_key(self, reference: ObjectPropertyRef) -> object:
        if self._dialect is SqlDialect.SQLITE:
            return self._json_path(reference.property_name)
        return reference.property_name

    def _definition(
        self,
        reference: object,
    ) -> ObjectPropertyDefinition:
        definition = _definition(reference, self._definitions)
        if (
            definition.value_type in _UNSAFE_NUMBER_TYPES
            or (
                self._dialect is SqlDialect.POSTGRES
                and definition.value_type in _POSTGRES_UNSAFE_INTEGER_TYPES
            )
            or (
                self._dialect is SqlDialect.SQLITE
                and definition.value_type in _SQLITE_UNSAFE_STRING_TYPES
            )
        ):
            # The dialect cannot preserve or validate the exact runtime value
            # contract for this property type without a trusted adapter/UDF.
            raise PolicySqlCompileError("policy cannot be compiled safely")
        return definition

    def _object_type_match(self) -> str:
        if self._dialect is SqlDialect.SQLITE:
            return "object_type COLLATE BINARY = ? COLLATE BINARY"
        return 'object_type COLLATE "C" = %s::text COLLATE "C"'

    def _postgres_string_has_no_controls(self, value_sql: str) -> str:
        return (
            f"NOT ({value_sql} ~ ('[' || chr(1) || '-' || chr(31) || chr(127) || ']'))"
        )

    def _scalar_validity(
        self,
        reference: ObjectPropertyRef,
        value_type: ObjectPropertyType,
    ) -> _Fragment:
        key = self._property_key(reference)
        placeholder = self._placeholder
        if self._dialect is SqlDialect.SQLITE:
            json_type = f"json_type(object_properties, {placeholder})"
            value = f"json_extract(object_properties, {placeholder})"
            if value_type is ObjectPropertyType.INTEGER:
                sql = (
                    f"({self._object_type_match()} AND {json_type} = 'integer' AND "
                    f"typeof({value}) = 'integer' AND {value} BETWEEN "
                    f"{MIN_POLICY_INTEGER} AND {MAX_POLICY_INTEGER})"
                )
                return _Fragment(sql, (reference.object_type, key, key, key))
            if value_type is ObjectPropertyType.BOOLEAN:
                return _Fragment(
                    f"({self._object_type_match()} AND "
                    f"{json_type} IN ('true', 'false'))",
                    (reference.object_type, key),
                )
            raise PolicySqlCompileError("policy cannot be compiled safely")

        json_type = f"jsonb_typeof(object_properties -> {placeholder})"
        value = f"object_properties ->> {placeholder}"
        if value_type is ObjectPropertyType.STRING:
            sql = (
                f"({self._object_type_match()} AND {json_type} = 'string' AND "
                f"octet_length({value}) BETWEEN 1 AND {MAX_POLICY_STRING_BYTES} "
                f"AND {self._postgres_string_has_no_controls(value)})"
            )
            return _Fragment(sql, (reference.object_type, key, key, key))
        if value_type is ObjectPropertyType.BOOLEAN:
            return _Fragment(
                f"({self._object_type_match()} AND {json_type} = 'boolean')",
                (reference.object_type, key),
            )
        raise PolicySqlCompileError("policy cannot be compiled safely")

    def _array_type_validity(
        self,
        reference: ObjectPropertyRef,
    ) -> _Fragment:
        placeholder = self._placeholder
        key = self._property_key(reference)
        if self._dialect is SqlDialect.SQLITE:
            json_type = f"json_type(object_properties, {placeholder})"
        else:
            json_type = f"jsonb_typeof(object_properties -> {placeholder})"
        return _Fragment(
            f"({self._object_type_match()} AND {json_type} = 'array')",
            (reference.object_type, key),
        )

    def _collection_item_invalid(self, value_type: ObjectPropertyType) -> str:
        member_type = _COLLECTION_MEMBER_TYPE.get(value_type)
        if member_type is None:
            raise PolicySqlCompileError("policy cannot be compiled safely")
        if self._dialect is SqlDialect.SQLITE:
            value = "policy_item.value"
            if member_type is ObjectPropertyType.INTEGER:
                return (
                    "(policy_item.type <> 'integer' OR "
                    f"typeof({value}) <> 'integer' OR {value} NOT BETWEEN "
                    f"{MIN_POLICY_INTEGER} AND {MAX_POLICY_INTEGER})"
                )
            if member_type is ObjectPropertyType.BOOLEAN:
                return "policy_item.type NOT IN ('true', 'false')"
        else:
            value = "policy_item.value #>> '{}'"
            if member_type is ObjectPropertyType.STRING:
                return (
                    "(jsonb_typeof(policy_item.value) <> 'string' OR "
                    f"octet_length({value}) NOT BETWEEN 1 AND "
                    f"{MAX_POLICY_STRING_BYTES} OR NOT "
                    f"({self._postgres_string_has_no_controls(value)}))"
                )
            if member_type is ObjectPropertyType.BOOLEAN:
                return "jsonb_typeof(policy_item.value) <> 'boolean'"
        raise PolicySqlCompileError("policy cannot be compiled safely")

    def _collection_validity(
        self,
        reference: ObjectPropertyRef,
        value_type: ObjectPropertyType,
    ) -> _Fragment:
        array_valid = self._array_type_validity(reference)
        key = self._property_key(reference)
        placeholder = self._placeholder
        invalid_item = self._collection_item_invalid(value_type)
        if self._dialect is SqlDialect.SQLITE:
            bounded = _Fragment(
                f"json_array_length(object_properties, {placeholder}) <= "
                f"{MAX_POLICY_COLLECTION_ITEMS}",
                (key,),
            )
            items_valid = _Fragment(
                "NOT EXISTS (SELECT 1 FROM "
                f"json_each(object_properties, {placeholder}) AS policy_item "
                f"WHERE {invalid_item})",
                (key,),
            )
        else:
            bounded = _Fragment(
                f"jsonb_array_length(object_properties -> {placeholder}) <= "
                f"{MAX_POLICY_COLLECTION_ITEMS}",
                (key,),
            )
            items_valid = _Fragment(
                "NOT EXISTS (SELECT 1 FROM "
                f"jsonb_array_elements(object_properties -> {placeholder}) "
                f"AS policy_item(value) WHERE {invalid_item})",
                (key,),
            )
        checks = _case_when_false(bounded, items_valid)
        return _case_when_false(array_valid, checks)

    def _value_validity(
        self,
        reference: ObjectPropertyRef,
        value_type: ObjectPropertyType,
    ) -> _Fragment:
        if value_type in _COLLECTION_MEMBER_TYPE:
            return self._collection_validity(reference, value_type)
        return self._scalar_validity(reference, value_type)

    def _exists(
        self,
        reference: ObjectPropertyRef,
        definition: ObjectPropertyDefinition,
    ) -> _CompiledNode:
        placeholder = self._placeholder
        key = self._property_key(reference)
        object_type_valid = _Fragment(
            self._object_type_match(),
            (reference.object_type,),
        )
        if self._dialect is SqlDialect.SQLITE:
            present = _Fragment(
                f"json_type(object_properties, {placeholder}) IS NOT NULL",
                (key,),
            )
        else:
            present = _Fragment(
                f"object_properties ? {placeholder}",
                (key,),
            )
        value_valid = self._value_validity(reference, definition.value_type)
        return _CompiledNode(
            expression=_Fragment(
                f"CASE WHEN {object_type_valid.sql} THEN "
                f"(CASE WHEN NOT ({present.sql}) THEN FALSE "
                f"WHEN ({value_valid.sql}) THEN TRUE ELSE NULL END) "
                "ELSE NULL END",
                object_type_valid.parameters
                + present.parameters
                + value_valid.parameters,
            )
        )

    def _scalar_access(
        self,
        reference: ObjectPropertyRef,
        value_type: ObjectPropertyType,
    ) -> tuple[str, tuple[object, ...]]:
        placeholder = self._placeholder
        key = self._property_key(reference)
        if self._dialect is SqlDialect.SQLITE:
            if value_type not in {
                ObjectPropertyType.INTEGER,
                ObjectPropertyType.BOOLEAN,
            }:
                raise PolicySqlCompileError("policy cannot be compiled safely")
            return (f"json_extract(object_properties, {placeholder})", (key,))
        access = f"object_properties ->> {placeholder}"
        if value_type is ObjectPropertyType.STRING:
            return (f'({access}) COLLATE "C"', (key,))
        if value_type is ObjectPropertyType.BOOLEAN:
            return (f"({access})::boolean", (key,))
        raise PolicySqlCompileError("policy cannot be compiled safely")

    def _value_placeholder(self, value_type: ObjectPropertyType) -> str:
        if self._dialect is SqlDialect.SQLITE:
            if value_type not in {
                ObjectPropertyType.INTEGER,
                ObjectPropertyType.BOOLEAN,
            }:
                raise PolicySqlCompileError("policy cannot be compiled safely")
            return self._placeholder
        if value_type is ObjectPropertyType.STRING:
            return f'{self._placeholder}::text COLLATE "C"'
        if value_type is ObjectPropertyType.BOOLEAN:
            return f"{self._placeholder}::boolean"
        raise PolicySqlCompileError("policy cannot be compiled safely")

    def _scalar_comparison(self, node: object) -> _CompiledNode:
        comparison = cast(Eq, node)
        definition = self._definition(comparison.left)
        reference = cast(ObjectPropertyRef, comparison.left)
        self.object_types.add(reference.object_type)
        value = _require_scalar(_literal(comparison.right), definition.value_type)
        if type(node) in (Lt, Lte, Gt, Gte) and definition.value_type not in {
            ObjectPropertyType.STRING,
            ObjectPropertyType.INTEGER,
            ObjectPropertyType.NUMBER,
        }:
            raise PolicySqlCompileError("policy cannot be compiled safely")
        access, access_parameters = self._scalar_access(
            reference, definition.value_type
        )
        valid = self._value_validity(reference, definition.value_type)
        operator = _SQL_OPERATOR[type(node)]
        match = _Fragment(
            f"({access} {operator} {self._value_placeholder(definition.value_type)})",
            access_parameters + (value,),
        )
        return _CompiledNode(expression=_case_when_null(valid, match))

    def _in(self, node: In) -> _CompiledNode:
        definition = self._definition(node.left)
        reference = cast(ObjectPropertyRef, node.left)
        self.object_types.add(reference.object_type)
        if definition.value_type not in _SCALAR_TYPES:
            raise PolicySqlCompileError("policy cannot be compiled safely")
        values = _literal(node.right)
        if type(values) is not tuple or not all(
            _value_matches_type(value, definition.value_type) for value in values
        ):
            raise PolicySqlCompileError("policy cannot be compiled safely")
        valid = self._value_validity(reference, definition.value_type)
        if not values:
            return _CompiledNode(
                expression=_case_when_null(valid, _Fragment("0 = 1", ())),
            )
        access, access_parameters = self._scalar_access(
            reference, definition.value_type
        )
        placeholders = ", ".join(
            self._value_placeholder(definition.value_type) for _ in values
        )
        raw_match = _Fragment(
            f"({access} IN ({placeholders}))",
            access_parameters + values,
        )
        return _CompiledNode(expression=_case_when_null(valid, raw_match))

    def _collection_item_access(
        self,
        value_type: ObjectPropertyType,
    ) -> str:
        member_type = _COLLECTION_MEMBER_TYPE[value_type]
        if self._dialect is SqlDialect.SQLITE:
            if member_type not in {
                ObjectPropertyType.INTEGER,
                ObjectPropertyType.BOOLEAN,
            }:
                raise PolicySqlCompileError("policy cannot be compiled safely")
            return "policy_item.value"
        if member_type is ObjectPropertyType.STRING:
            return "(policy_item.value #>> '{}') COLLATE \"C\""
        if member_type is ObjectPropertyType.BOOLEAN:
            return "(policy_item.value #>> '{}')::boolean"
        raise PolicySqlCompileError("policy cannot be compiled safely")

    def _collection_source(self) -> str:
        placeholder = self._placeholder
        if self._dialect is SqlDialect.SQLITE:
            return f"json_each(object_properties, {placeholder}) AS policy_item"
        return (
            f"jsonb_array_elements(object_properties -> {placeholder}) "
            "AS policy_item(value)"
        )

    def _contains(self, node: Contains) -> _CompiledNode:
        definition = self._definition(node.left)
        reference = cast(ObjectPropertyRef, node.left)
        self.object_types.add(reference.object_type)
        member_type = _COLLECTION_MEMBER_TYPE.get(definition.value_type)
        value = _literal(node.right)
        if member_type is None or not _value_matches_type(value, member_type):
            raise PolicySqlCompileError("policy cannot be compiled safely")
        valid = self._collection_validity(reference, definition.value_type)
        match = _Fragment(
            "EXISTS (SELECT 1 FROM "
            f"{self._collection_source()} WHERE "
            f"{self._collection_item_access(definition.value_type)} = "
            f"{self._value_placeholder(member_type)})",
            (self._property_key(reference), value),
        )
        return _CompiledNode(expression=_case_when_null(valid, match))

    def _subset(self, node: Subset) -> _CompiledNode:
        definition = self._definition(node.left)
        reference = cast(ObjectPropertyRef, node.left)
        self.object_types.add(reference.object_type)
        values = _require_collection(_literal(node.right), definition.value_type)
        valid = self._collection_validity(reference, definition.value_type)
        item_access = self._collection_item_access(definition.value_type)
        if values:
            member_type = _COLLECTION_MEMBER_TYPE[definition.value_type]
            placeholders = ", ".join(
                self._value_placeholder(member_type) for _ in values
            )
            outside = f"{item_access} NOT IN ({placeholders})"
        else:
            outside = "1 = 1"
        match = _Fragment(
            f"NOT EXISTS (SELECT 1 FROM {self._collection_source()} WHERE {outside})",
            (self._property_key(reference),) + values,
        )
        return _CompiledNode(expression=_case_when_null(valid, match))

    def compile(self, node: object) -> _CompiledNode:
        if type(node) in _SQL_OPERATOR:
            return self._scalar_comparison(node)
        if type(node) is In:
            return self._in(node)
        if type(node) is Contains:
            return self._contains(node)
        if type(node) is Subset:
            return self._subset(node)
        if type(node) is Exists:
            reference = cast(Exists, node).value
            definition = self._definition(reference)
            exact = cast(ObjectPropertyRef, reference)
            self.object_types.add(definition.object_type)
            return self._exists(exact, definition)
        if type(node) is Not:
            child = self.compile(node.child)
            return _CompiledNode(expression=_negate(child.expression))
        if type(node) in (All, AnyOf):
            children = tuple(self.compile(child) for child in node.children)
            operator = "AND" if type(node) is All else "OR"
            return _CompiledNode(
                expression=_join(
                    operator,
                    tuple(child.expression for child in children),
                )
            )
        raise PolicySqlCompileError("policy cannot be compiled safely")


def compile_policy_sql(
    expression: object,
    schema: object,
    *,
    dialect: SqlDialect,
    effect: PolicyEffect = PolicyEffect.REQUIRE,
) -> CompiledSqlPlan:
    """Compile the exact safe object-property subset without interpolating inputs.

    Only the REQUIRE polarity is compilable: ``predicate`` folds evaluation
    errors to FALSE, which fails closed when rows must match to be kept
    (``WHERE predicate``). Under DENY_IF polarity (``WHERE NOT predicate``)
    that same fold would silently skip denial on attribute errors, so DENY_IF
    (and OBLIGATION_IF) are rejected at compile time. ``error_predicate`` on
    the returned plan exposes the raw error channel for callers that must
    distinguish "did not match" from "could not be evaluated".
    """

    try:
        if type(dialect) is not SqlDialect:
            raise PolicySqlCompileError("policy cannot be compiled safely")
        if type(effect) is not PolicyEffect:
            raise PolicySqlCompileError("policy cannot be compiled safely")
        if effect is not PolicyEffect.REQUIRE:
            raise PolicySqlCompileError(
                f"{effect.value} polarity is not supported by row-filter SQL "
                "compilation: NULL evaluation errors fold to FALSE, which is "
                "fail-open outside WHERE-predicate (REQUIRE) usage"
            )
        validated = validate_policy_expression(expression)
        _enforce_sql_comparison_budget(validated)
        compiler = _DialectCompiler(_schema_map(schema), dialect)
        compiled = compiler.compile(validated)
        if len(compiler.object_types) != 1:
            raise PolicySqlCompileError("policy cannot be compiled safely")
        predicate = f"COALESCE(({compiled.expression.sql}), FALSE)"
        error_predicate = f"(({compiled.expression.sql}) IS NULL)"
        parameters = compiled.expression.parameters
        return CompiledSqlPlan(
            dialect=dialect,
            predicate=predicate,
            error_predicate=error_predicate,
            parameters=parameters,
        )
    except PolicySqlCompileError:
        raise
    except Exception:
        raise PolicySqlCompileError("policy cannot be compiled safely") from None


__all__ = ["PolicySqlCompileError", "compile_policy_sql"]

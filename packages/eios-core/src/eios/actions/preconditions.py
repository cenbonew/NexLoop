"""Deterministic, total evaluation of Action submission criteria (stage 5).

``ActionPrecondition.expression`` uses a closed boolean vocabulary over the
same comparison operators as Object Set filters, so authors learn one
language:

    {"all": [<expr>, ...]}                 — conjunction (empty: true)
    {"any": [<expr>, ...]}                 — disjunction (empty: false)
    {"not": <expr>}                        — negation
    {"property": <name>, "operator": <FilterOperator>,
     "value": <json> | "parameter": <name>}          — object-property leaf
    {"input": <name>, "operator": ..., "value"|"parameter": ...}
                                            — bound-request-input leaf

Evaluation is total and fail-closed: a missing property or input, a type
mismatch, an unbound parameter, or an unknown shape never raises at
evaluation time — the leaf evaluates to False (validation of the shape
itself happens up front via ``validate_precondition_expression``, so a
published Action can never carry an unevaluable expression).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eios.ontology.definitions import FilterOperator


_OPERATORS = frozenset(item.value for item in FilterOperator)
_LEAF_SUBJECTS = ("property", "input")


class PreconditionExpressionError(ValueError):
    """A precondition expression does not follow the closed vocabulary."""

    code = "action_precondition_expression_invalid"


def validate_precondition_expression(expression: Mapping[str, Any]) -> None:
    """Reject any expression outside the closed vocabulary (author-time)."""

    _validate(expression, depth=0)


def _validate(expression: object, *, depth: int) -> None:
    if depth > 16:
        raise PreconditionExpressionError("expression is too deep")
    if not isinstance(expression, Mapping):
        raise PreconditionExpressionError("expression must be a JSON object")
    keys = set(expression)
    if keys == {"all"} or keys == {"any"}:
        (branches,) = expression.values()
        if not isinstance(branches, (list, tuple)):
            raise PreconditionExpressionError("all/any requires a JSON array")
        for branch in branches:
            _validate(branch, depth=depth + 1)
        return
    if keys == {"not"}:
        _validate(expression["not"], depth=depth + 1)
        return
    subject_keys = keys.intersection(_LEAF_SUBJECTS)
    operand_keys = keys.intersection(("value", "parameter"))
    if (
        len(subject_keys) != 1
        or len(operand_keys) != 1
        or keys != subject_keys | {"operator"} | operand_keys
    ):
        raise PreconditionExpressionError(
            "leaf requires exactly one of property/input, an operator, and"
            " exactly one of value/parameter"
        )
    subject = expression[next(iter(subject_keys))]
    if not isinstance(subject, str) or not subject.strip():
        raise PreconditionExpressionError("leaf subject must be a non-empty name")
    if expression["operator"] not in _OPERATORS:
        raise PreconditionExpressionError("leaf operator is unknown")
    if "parameter" in expression:
        parameter = expression["parameter"]
        if not isinstance(parameter, str) or not parameter.strip():
            raise PreconditionExpressionError(
                "leaf parameter must be a non-empty name"
            )


def evaluate_precondition(
    expression: Mapping[str, Any],
    *,
    object_properties: Mapping[str, Any],
    request_input: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> bool:
    """Total, deterministic evaluation; every failure mode is False."""

    return _evaluate(
        expression,
        object_properties=object_properties,
        request_input=request_input,
        parameters=parameters,
        depth=0,
    )


def _evaluate(
    expression: object,
    *,
    object_properties: Mapping[str, Any],
    request_input: Mapping[str, Any],
    parameters: Mapping[str, Any],
    depth: int,
) -> bool:
    if depth > 16 or not isinstance(expression, Mapping):
        return False
    keys = set(expression)
    context = {
        "object_properties": object_properties,
        "request_input": request_input,
        "parameters": parameters,
    }
    if keys == {"all"}:
        branches = expression["all"]
        if not isinstance(branches, (list, tuple)):
            return False
        return all(
            _evaluate(branch, depth=depth + 1, **context) for branch in branches
        )
    if keys == {"any"}:
        branches = expression["any"]
        if not isinstance(branches, (list, tuple)):
            return False
        return any(
            _evaluate(branch, depth=depth + 1, **context) for branch in branches
        )
    if keys == {"not"}:
        return not _evaluate(expression["not"], depth=depth + 1, **context)
    return _evaluate_leaf(
        expression,
        object_properties=object_properties,
        request_input=request_input,
        parameters=parameters,
    )


def _evaluate_leaf(
    expression: Mapping[str, Any],
    *,
    object_properties: Mapping[str, Any],
    request_input: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> bool:
    if "property" in expression:
        source, subject = object_properties, expression.get("property")
    elif "input" in expression:
        source, subject = request_input, expression.get("input")
    else:
        return False
    if not isinstance(subject, str) or subject not in source:
        return False
    actual = source[subject]
    if "parameter" in expression:
        parameter = expression.get("parameter")
        if not isinstance(parameter, str) or parameter not in parameters:
            return False
        operand = parameters[parameter]
    else:
        operand = expression.get("value")
    operator = expression.get("operator")
    if operator in (FilterOperator.IN.value, FilterOperator.NOT_IN.value):
        if not isinstance(operand, (list, tuple)):
            return False
        contained = any(
            _comparable(actual, item) and actual == item for item in operand
        )
        return contained if operator == FilterOperator.IN.value else not contained
    if not _comparable(actual, operand):
        return False
    try:
        if operator == FilterOperator.EQ.value:
            return actual == operand
        if operator == FilterOperator.NE.value:
            return actual != operand
        if operator == FilterOperator.GT.value:
            return actual > operand
        if operator == FilterOperator.GTE.value:
            return actual >= operand
        if operator == FilterOperator.LT.value:
            return actual < operand
        if operator == FilterOperator.LTE.value:
            return actual <= operand
    except TypeError:
        return False
    return False


def _comparable(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return True
    return type(left) is type(right)


__all__ = [
    "PreconditionExpressionError",
    "evaluate_precondition",
    "validate_precondition_expression",
]

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from pydantic import BaseModel, TypeAdapter

from eios.authz.errors import AuthorizationValidationError

from .models import (
    ActionParameterRef,
    All,
    AnyOf,
    ApplicationRef,
    Contains,
    Eq,
    Exists,
    Gt,
    Gte,
    In,
    LiteralValue,
    Lt,
    Lte,
    MAX_POLICY_COMPARISON_WEIGHT,
    MAX_POLICY_DEPTH,
    MAX_POLICY_NODES,
    Neq,
    Not,
    ObjectPropertyRef,
    PolicyExpression,
    ResourceRef,
    SubjectRef,
    Subset,
    TrustedTimeRef,
)
from .plans import PolicyPlan


class PolicyValidationError(AuthorizationValidationError):
    code = "policy_invalid"


_EXPRESSION_ADAPTER = TypeAdapter(PolicyExpression)
_PLAN_ADAPTER = TypeAdapter(PolicyPlan)
_NODE_TYPES = (
    All,
    AnyOf,
    Not,
    Eq,
    Neq,
    In,
    Contains,
    Subset,
    Lt,
    Lte,
    Gt,
    Gte,
    Exists,
)
_OPERAND_TYPES = (
    SubjectRef,
    ApplicationRef,
    ResourceRef,
    ObjectPropertyRef,
    ActionParameterRef,
    TrustedTimeRef,
    LiteralValue,
)
_COMPARISON_TYPES = (Eq, Neq, In, Contains, Subset, Lt, Lte, Gt, Gte)
_DISCRIMINATORS = {
    All: ("op", "all"),
    AnyOf: ("op", "any"),
    Not: ("op", "not"),
    Eq: ("op", "eq"),
    Neq: ("op", "neq"),
    In: ("op", "in"),
    Contains: ("op", "contains"),
    Subset: ("op", "subset"),
    Lt: ("op", "lt"),
    Lte: ("op", "lte"),
    Gt: ("op", "gt"),
    Gte: ("op", "gte"),
    Exists: ("op", "exists"),
    SubjectRef: ("kind", "subject"),
    ApplicationRef: ("kind", "application"),
    ResourceRef: ("kind", "resource"),
    ObjectPropertyRef: ("kind", "object_property"),
    ActionParameterRef: ("kind", "action_parameter"),
    TrustedTimeRef: ("kind", "trusted_time"),
    LiteralValue: ("kind", "literal"),
}


def _model_is_pristine(value: BaseModel) -> bool:
    fields = type(value).model_fields
    if set(value.__dict__) != set(fields):
        return False
    if value.__pydantic_extra__ is not None:
        return False
    pristine_check = getattr(value, "_construction_is_pristine", None)
    if pristine_check is None or pristine_check() is not True:
        return False
    discriminator = _DISCRIMINATORS.get(type(value))
    if discriminator is not None:
        field_name, expected = discriminator
        if value.__dict__.get(field_name) != expected:
            return False
    fields_set = value.__pydantic_fields_set__
    return type(fields_set) is set and fields_set <= set(fields)


def _iter_model_values(value: object) -> Iterable[object]:
    if isinstance(value, BaseModel):
        yield from value.__dict__.values()
    elif type(value) is tuple:
        yield from value


def _reject_tainted_tree(root: object) -> None:
    stack = [root]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, BaseModel):
            identity = id(current)
            if identity in seen:
                raise PolicyValidationError("policy expression is invalid")
            seen.add(identity)
            if type(current) not in _NODE_TYPES + _OPERAND_TYPES + (PolicyPlan,):
                raise PolicyValidationError("policy expression is invalid")
            if not _model_is_pristine(current):
                raise PolicyValidationError("policy expression is invalid")
        elif type(current) is tuple:
            pass
        elif type(current) not in (str, int, float, bool) and current is not None:
            # Datetimes are scalar leaves; all other container/object types are rejected.
            from datetime import datetime

            if type(current) is not datetime:
                raise PolicyValidationError("policy expression is invalid")
        stack.extend(_iter_model_values(current))


def _children(node: object) -> tuple[object, ...]:
    if type(node) in (All, AnyOf):
        return cast(All | AnyOf, node).children
    if type(node) is Not:
        return (cast(Not, node).child,)
    return ()


def _comparison_weight(node: object) -> int:
    if type(node) not in _COMPARISON_TYPES:
        return 0
    comparison = cast(Eq, node)
    weight = 1
    for operand in (comparison.left, comparison.right):
        if type(operand) is LiteralValue and type(operand.value) is tuple:
            weight = max(weight, len(operand.value))
    if type(node) is Subset:
        weights = [
            len(operand.value)
            for operand in (comparison.left, comparison.right)
            if type(operand) is LiteralValue and type(operand.value) is tuple
        ]
        if weights:
            weight = max(1, sum(weights))
    return weight


def _validate_graph(expression: PolicyExpression) -> None:
    stack: list[tuple[object, int]] = [(expression, 0)]
    node_ids: set[str] = set()
    node_count = 0
    comparison_weight = 0
    while stack:
        node, depth = stack.pop()
        if type(node) not in _NODE_TYPES:
            raise PolicyValidationError("policy expression is invalid")
        if depth > MAX_POLICY_DEPTH:
            raise PolicyValidationError("policy expression is invalid")
        node_count += 1
        if node_count > MAX_POLICY_NODES or node.node_id in node_ids:
            raise PolicyValidationError("policy expression is invalid")
        node_ids.add(node.node_id)
        comparison_weight += _comparison_weight(node)
        if comparison_weight > MAX_POLICY_COMPARISON_WEIGHT:
            raise PolicyValidationError("policy expression is invalid")
        nested = _children(node)
        stack.extend((child, depth + 1) for child in reversed(nested))


def validate_policy_expression(value: object) -> PolicyExpression:
    """Reject forged instances and return a fully revalidated, unaliased AST."""

    if type(value) not in _NODE_TYPES:
        raise PolicyValidationError("policy expression is invalid")
    try:
        _reject_tainted_tree(value)
        payload = value.model_dump(mode="python", round_trip=True)
        validated = _EXPRESSION_ADAPTER.validate_python(payload, strict=True)
        _reject_tainted_tree(validated)
        _validate_graph(validated)
        return validated
    except PolicyValidationError:
        raise
    except Exception:
        raise PolicyValidationError("policy expression is invalid") from None


def validate_policy_plan(value: object) -> PolicyPlan:
    if type(value) is not PolicyPlan:
        raise PolicyValidationError("policy plan is invalid")
    try:
        _reject_tainted_tree(value)
        payload = value.model_dump(mode="python", round_trip=True)
        validated = _PLAN_ADAPTER.validate_python(payload, strict=True)
        condition = validate_policy_expression(validated.condition)
        return PolicyPlan(
            policy_id=validated.policy_id,
            revision=validated.revision,
            condition=condition,
        )
    except PolicyValidationError:
        raise
    except Exception:
        raise PolicyValidationError("policy plan is invalid") from None


__all__ = [
    "PolicyValidationError",
    "validate_policy_expression",
    "validate_policy_plan",
]

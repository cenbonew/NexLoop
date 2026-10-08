from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias, cast

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
    MAX_POLICY_COLLECTION_ITEMS,
    MAX_POLICY_COMPARISON_WEIGHT,
    MAX_POLICY_IDENTIFIER_BYTES,
    MAX_POLICY_INTEGER,
    MAX_POLICY_STRING_BYTES,
    Neq,
    Not,
    ObjectPropertyRef,
    PolicyOperand,
    PolicyScalar,
    ResourceRef,
    SubjectRef,
    Subset,
    TrustedTimeRef,
    MIN_POLICY_INTEGER,
)
from .plans import PolicyEvaluationResult
from .validator import validate_policy_expression


RuntimeValue: TypeAlias = PolicyScalar | tuple[PolicyScalar, ...]
MAX_CONTEXT_ATTRIBUTES = 2_048


class PolicyEvaluationContextError(ValueError):
    pass


def _bounded_name(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_POLICY_IDENTIFIER_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PolicyEvaluationContextError("policy evaluation context is invalid")
    return value


def _copy_scalar(value: object) -> PolicyScalar:
    if type(value) is str:
        if (
            not value
            or len(value.encode("utf-8")) > MAX_POLICY_STRING_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        return value
    if type(value) is bool:
        return value
    if type(value) is int:
        if not MIN_POLICY_INTEGER <= value <= MAX_POLICY_INTEGER:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        return value.astimezone(UTC)
    raise PolicyEvaluationContextError("policy evaluation context is invalid")


def _copy_value(value: object) -> RuntimeValue:
    if type(value) is tuple:
        if len(value) > MAX_POLICY_COLLECTION_ITEMS:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        return tuple(_copy_scalar(item) for item in value)
    return _copy_scalar(value)


def _copy_named_values(values: object) -> Mapping[str, RuntimeValue]:
    if not isinstance(values, Mapping) or len(values) > MAX_CONTEXT_ATTRIBUTES:
        raise PolicyEvaluationContextError("policy evaluation context is invalid")
    copied: dict[str, RuntimeValue] = {}
    for key, value in values.items():
        name = _bounded_name(key)
        if name in copied:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        copied[name] = _copy_value(value)
    return MappingProxyType(copied)


def _copy_object_values(
    values: object,
) -> Mapping[tuple[str, str], RuntimeValue]:
    if not isinstance(values, Mapping) or len(values) > MAX_CONTEXT_ATTRIBUTES:
        raise PolicyEvaluationContextError("policy evaluation context is invalid")
    copied: dict[tuple[str, str], RuntimeValue] = {}
    for key, value in values.items():
        if type(key) is not tuple or len(key) != 2:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        exact = (_bounded_name(key[0]), _bounded_name(key[1]))
        if exact in copied:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        copied[exact] = _copy_value(value)
    return MappingProxyType(copied)


class EvaluationContext:
    __slots__ = (
        "_action_parameters",
        "_application",
        "_object_properties",
        "_resource",
        "_subject",
        "_trusted_now",
    )

    def __init__(
        self,
        *,
        subject: Mapping[str, object],
        application: Mapping[str, object],
        resource: Mapping[str, object],
        object_properties: Mapping[tuple[str, str], object],
        action_parameters: Mapping[str, object],
        trusted_now: datetime,
    ) -> None:
        try:
            object.__setattr__(self, "_subject", _copy_named_values(subject))
            object.__setattr__(self, "_application", _copy_named_values(application))
            object.__setattr__(self, "_resource", _copy_named_values(resource))
            object.__setattr__(
                self,
                "_object_properties",
                _copy_object_values(object_properties),
            )
            object.__setattr__(
                self,
                "_action_parameters",
                _copy_named_values(action_parameters),
            )
            checked_now = _copy_scalar(trusted_now)
            if type(checked_now) is not datetime:
                raise PolicyEvaluationContextError(
                    "policy evaluation context is invalid"
                )
            object.__setattr__(self, "_trusted_now", checked_now)
        except PolicyEvaluationContextError:
            raise
        except Exception:
            raise PolicyEvaluationContextError(
                "policy evaluation context is invalid"
            ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("policy evaluation context is frozen")


class _State(Enum):
    TRUE = 1
    FALSE = 2
    ERROR = 3


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


class _ComparisonBudget:
    __slots__ = ("remaining",)

    def __init__(self) -> None:
        self.remaining = MAX_POLICY_COMPARISON_WEIGHT

    def consume(self, amount: int) -> bool:
        self.remaining -= max(1, amount)
        return self.remaining >= 0


def _resolve(operand: PolicyOperand, context: EvaluationContext) -> object:
    if type(operand) is LiteralValue:
        return operand.value
    if type(operand) is SubjectRef:
        return context._subject.get(operand.attribute, _MISSING)
    if type(operand) is ApplicationRef:
        return context._application.get(operand.attribute, _MISSING)
    if type(operand) is ResourceRef:
        return context._resource.get(operand.attribute, _MISSING)
    if type(operand) is ObjectPropertyRef:
        return context._object_properties.get(
            (operand.object_type, operand.property_name), _MISSING
        )
    if type(operand) is ActionParameterRef:
        return context._action_parameters.get(operand.parameter_name, _MISSING)
    if type(operand) is TrustedTimeRef:
        return context._trusted_now
    return _MISSING


def _equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is tuple:
        return len(left) == len(right) and all(
            _equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _member(value: object, collection: tuple[object, ...]) -> bool:
    return any(_equal(value, candidate) for candidate in collection)


def _compare(
    node: object,
    context: EvaluationContext,
    budget: _ComparisonBudget,
) -> _State:
    comparison = cast(Eq, node)
    left = _resolve(comparison.left, context)
    right = _resolve(comparison.right, context)
    if left is _MISSING or right is _MISSING:
        return _State.ERROR
    if type(left) is not type(right) and type(node) in (Eq, Neq):
        return _State.ERROR
    if type(node) in (Eq, Neq):
        cost = max(len(left), len(right)) if type(left) is tuple else 1
        if not budget.consume(cost):
            return _State.ERROR
    if type(node) is Eq:
        return _State.TRUE if _equal(left, right) else _State.FALSE
    if type(node) is Neq:
        return _State.FALSE if _equal(left, right) else _State.TRUE
    if type(node) is In:
        if type(right) is not tuple or type(left) is tuple:
            return _State.ERROR
        if not budget.consume(len(right)):
            return _State.ERROR
        return _State.TRUE if _member(left, right) else _State.FALSE
    if type(node) is Contains:
        if type(left) is not tuple or type(right) is tuple:
            return _State.ERROR
        if not budget.consume(len(left)):
            return _State.ERROR
        return _State.TRUE if _member(right, left) else _State.FALSE
    if type(node) is Subset:
        if type(left) is not tuple or type(right) is not tuple:
            return _State.ERROR
        if not budget.consume(len(left) + len(right)):
            return _State.ERROR
        return (
            _State.TRUE if all(_member(item, right) for item in left) else _State.FALSE
        )
    if type(left) is not type(right) or type(left) not in (int, float, str, datetime):
        return _State.ERROR
    if not budget.consume(1):
        return _State.ERROR
    try:
        if type(node) is Lt:
            matched = left < right
        elif type(node) is Lte:
            matched = left <= right
        elif type(node) is Gt:
            matched = left > right
        elif type(node) is Gte:
            matched = left >= right
        else:
            return _State.ERROR
    except Exception:
        return _State.ERROR
    return _State.TRUE if matched else _State.FALSE


def _evaluate(
    node: object,
    context: EvaluationContext,
    budget: _ComparisonBudget,
) -> tuple[_State, tuple[str, ...]]:
    if type(node) is Exists:
        resolved = _resolve(node.value, context)
        if resolved is _MISSING:
            return (_State.FALSE, ())
        return (_State.TRUE, (node.node_id,))
    if type(node) in (Eq, Neq, In, Contains, Subset, Lt, Lte, Gt, Gte):
        state = _compare(node, context, budget)
        return (state, (node.node_id,) if state is _State.TRUE else ())
    if type(node) is Not:
        child_state, _ = _evaluate(node.child, context, budget)
        if child_state is _State.ERROR:
            return (_State.ERROR, ())
        if child_state is _State.TRUE:
            return (_State.FALSE, ())
        return (_State.TRUE, (node.node_id,))
    if type(node) in (All, AnyOf):
        evaluated = tuple(_evaluate(child, context, budget) for child in node.children)
        if type(node) is All:
            if any(state is _State.FALSE for state, _ in evaluated):
                return (_State.FALSE, ())
            if any(state is _State.ERROR for state, _ in evaluated):
                return (_State.ERROR, ())
        else:
            if not any(state is _State.TRUE for state, _ in evaluated):
                if any(state is _State.ERROR for state, _ in evaluated):
                    return (_State.ERROR, ())
                return (_State.FALSE, ())
        evidence = tuple(
            node_id
            for state, node_ids in evaluated
            if state is _State.TRUE
            for node_id in node_ids
        )
        return (_State.TRUE, evidence + (node.node_id,))
    return (_State.ERROR, ())


def evaluate_policy(expression: object, context: object) -> PolicyEvaluationResult:
    """Evaluate without I/O; every malformed or exceptional input fails closed."""

    try:
        if type(context) is not EvaluationContext:
            raise PolicyEvaluationContextError("policy evaluation context is invalid")
        validated = validate_policy_expression(expression)
        state, matched_node_ids = _evaluate(
            validated,
            context,
            _ComparisonBudget(),
        )
        if state is not _State.TRUE:
            return PolicyEvaluationResult(
                matched=False,
                had_error=state is _State.ERROR,
                matched_node_ids=(),
            )
        return PolicyEvaluationResult(
            matched=True,
            had_error=False,
            matched_node_ids=matched_node_ids,
        )
    except Exception:
        return PolicyEvaluationResult(
            matched=False,
            had_error=True,
            matched_node_ids=(),
        )


__all__ = [
    "EvaluationContext",
    "MAX_CONTEXT_ATTRIBUTES",
    "PolicyEvaluationContextError",
    "evaluate_policy",
]

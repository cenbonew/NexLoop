from __future__ import annotations

from pydantic import TypeAdapter
from pydantic_core import from_json

from .models import PolicyExpression
from .plans import PolicyPlan
from .validator import (
    PolicyValidationError,
    validate_policy_expression,
    validate_policy_plan,
)


MAX_POLICY_DOCUMENT_BYTES = 1_048_576
_EXPRESSION_ADAPTER = TypeAdapter(PolicyExpression)
_PLAN_ADAPTER = TypeAdapter(PolicyPlan)


_JSON_WHITESPACE = b" \t\r\n"
_JSON_VALUE_DELIMITERS = b",]} \t\r\n"


def _skip_whitespace(raw: bytes, position: int) -> int:
    while position < len(raw) and raw[position] in _JSON_WHITESPACE:
        position += 1
    return position


def _string_end(raw: bytes, position: int) -> int:
    if position >= len(raw) or raw[position] != ord('"'):
        raise ValueError("JSON string expected")
    position += 1
    while position < len(raw):
        current = raw[position]
        if current == ord('"'):
            return position + 1
        if current == ord("\\"):
            position += 2
        else:
            position += 1
    raise ValueError("unterminated JSON string")


def _scan_json_value(raw: bytes, position: int, depth: int) -> int:
    if depth > 64:
        raise ValueError("JSON nesting is too deep")
    position = _skip_whitespace(raw, position)
    if position >= len(raw):
        raise ValueError("JSON value expected")
    current = raw[position]
    if current == ord('"'):
        return _string_end(raw, position)
    if current == ord("{"):
        position = _skip_whitespace(raw, position + 1)
        keys: set[str] = set()
        if position < len(raw) and raw[position] == ord("}"):
            return position + 1
        while True:
            key_start = position
            key_end = _string_end(raw, key_start)
            key = from_json(raw[key_start:key_end])
            if type(key) is not str or key in keys:
                raise ValueError("duplicate or invalid JSON object key")
            keys.add(key)
            position = _skip_whitespace(raw, key_end)
            if position >= len(raw) or raw[position] != ord(":"):
                raise ValueError("JSON object colon expected")
            position = _scan_json_value(raw, position + 1, depth + 1)
            position = _skip_whitespace(raw, position)
            if position < len(raw) and raw[position] == ord("}"):
                return position + 1
            if position >= len(raw) or raw[position] != ord(","):
                raise ValueError("JSON object delimiter expected")
            position = _skip_whitespace(raw, position + 1)
    if current == ord("["):
        position = _skip_whitespace(raw, position + 1)
        if position < len(raw) and raw[position] == ord("]"):
            return position + 1
        while True:
            position = _scan_json_value(raw, position, depth + 1)
            position = _skip_whitespace(raw, position)
            if position < len(raw) and raw[position] == ord("]"):
                return position + 1
            if position >= len(raw) or raw[position] != ord(","):
                raise ValueError("JSON array delimiter expected")
            position = _skip_whitespace(raw, position + 1)
    start = position
    while position < len(raw) and raw[position] not in _JSON_VALUE_DELIMITERS:
        position += 1
    if position == start:
        raise ValueError("JSON scalar expected")
    return position


def _validate_json_structure(raw: bytes) -> None:
    end = _skip_whitespace(raw, _scan_json_value(raw, 0, 0))
    if end != len(raw):
        raise ValueError("trailing JSON content")


def _document_bytes(document: str | bytes) -> bytes:
    if type(document) is str:
        try:
            raw = document.encode("utf-8")
        except UnicodeError:
            raise PolicyValidationError("policy document is invalid") from None
    elif type(document) is bytes:
        raw = bytes(document)
    else:
        raise PolicyValidationError("policy document is invalid")
    if not raw or len(raw) > MAX_POLICY_DOCUMENT_BYTES:
        raise PolicyValidationError("policy document is invalid")
    try:
        _validate_json_structure(raw)
    except Exception:
        raise PolicyValidationError("policy document is invalid") from None
    return raw


def _json_policy_collections(value: object) -> object:
    """Decode only AST collection fields before strict Python validation.

    Frozen AST models have custom constructors: their nested JSON arrays reach
    those constructors as Python lists, which strict tuple fields reject. Keep
    JSON scalar types and all unknown fields intact so existing validators still
    reject them; this is a wire-format conversion, not permissive model coercion.
    """
    if type(value) is list:
        return [_json_policy_collections(item) for item in value]
    if type(value) is not dict:
        return value
    decoded = {key: _json_policy_collections(item) for key, item in value.items()}
    if decoded.get("op") in ("all", "any") and type(decoded.get("children")) is list:
        decoded["children"] = tuple(decoded["children"])
    if (
        decoded.get("kind") == "literal"
        and decoded.get("value_type", "scalar") == "scalar"
        and type(decoded.get("value")) is list
    ):
        decoded["value"] = tuple(decoded["value"])
    return decoded


def parse_policy_expression(document: str | bytes) -> PolicyExpression:
    try:
        raw = _document_bytes(document)
        parsed = _EXPRESSION_ADAPTER.validate_python(
            _json_policy_collections(from_json(raw)), strict=True
        )
        return validate_policy_expression(parsed)
    except PolicyValidationError as error:
        if str(error) == "policy document is invalid":
            raise
        raise PolicyValidationError("policy document is invalid") from None
    except Exception:
        raise PolicyValidationError("policy document is invalid") from None


def parse_policy_plan(document: str | bytes) -> PolicyPlan:
    try:
        raw = _document_bytes(document)
        parsed = _PLAN_ADAPTER.validate_python(
            _json_policy_collections(from_json(raw)), strict=True
        )
        return validate_policy_plan(parsed)
    except PolicyValidationError as error:
        if str(error) == "policy document is invalid":
            raise
        raise PolicyValidationError("policy document is invalid") from None
    except Exception:
        raise PolicyValidationError("policy document is invalid") from None


__all__ = ["MAX_POLICY_DOCUMENT_BYTES", "parse_policy_expression", "parse_policy_plan"]

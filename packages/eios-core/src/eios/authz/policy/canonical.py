from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256 as _sha256
from json import dumps
import math
import re

from pydantic import BaseModel, field_validator, model_validator

from .models import PolicyExpression
from .parser import MAX_POLICY_DOCUMENT_BYTES, parse_policy_expression
from .registry import PolicyRegistryError, _StrictFrozenRegistryModel
from .validator import validate_policy_expression


_SHA256 = re.compile(r"[0-9a-f]{64}")


class PolicyCanonicalizationError(PolicyRegistryError):
    """Raised when a policy cannot be represented by the EIOS canonical form."""


class CanonicalPolicyAst(_StrictFrozenRegistryModel):
    """Validated canonical UTF-8 policy bytes and their content digest."""

    utf8: bytes
    sha256: str

    @field_validator("utf8")
    @classmethod
    def _validate_utf8(cls, value: bytes) -> bytes:
        if (
            type(value) is not bytes
            or not value
            or len(value) > MAX_POLICY_DOCUMENT_BYTES
        ):
            raise ValueError("canonical policy bytes must be bounded UTF-8")
        try:
            value.decode("utf-8")
        except UnicodeError:
            raise ValueError("canonical policy bytes must be bounded UTF-8") from None
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ValueError("sha256 must be a full lowercase digest")
        return value

    @model_validator(mode="after")
    def _validate_digest(self) -> CanonicalPolicyAst:
        if _sha256(self.utf8).hexdigest() != self.sha256:
            raise ValueError("canonical policy digest does not match bytes")
        return self


def _encode_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PolicyCanonicalizationError("policy expression is invalid") from None
    return dumps(value, ensure_ascii=False, allow_nan=False)


def _encode_float(value: float) -> str:
    if not math.isfinite(value):
        raise PolicyCanonicalizationError("policy expression is invalid")
    if value == 0.0:
        return "0.0"
    encoded = repr(value).lower()
    if "." not in encoded and "e" not in encoded:
        encoded += ".0"
    return encoded


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PolicyCanonicalizationError("policy expression is invalid")
    normalized = value.astimezone(UTC)
    return _encode_string(normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        fields = {name: value.__dict__[name] for name in type(value).model_fields}
        return (
            "{"
            + ",".join(
                f"{_encode_string(name)}:{_canonical_json(fields[name])}"
                for name in sorted(fields)
            )
            + "}"
        )
    if isinstance(value, Enum):
        return _canonical_json(value.value)
    if type(value) is tuple:
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if type(value) is datetime:
        return _encode_datetime(value)
    if type(value) is str:
        return _encode_string(value)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return _encode_float(value)
    if value is None:
        return "null"
    raise PolicyCanonicalizationError("policy expression is invalid")


def canonicalize_policy_expression(expression: object) -> CanonicalPolicyAst:
    """Validate an EIOS AST and encode it without losing scalar type identity."""

    try:
        validated: PolicyExpression = validate_policy_expression(expression)
        raw = _canonical_json(validated).encode("utf-8")
        return CanonicalPolicyAst(utf8=raw, sha256=_sha256(raw).hexdigest())
    except PolicyCanonicalizationError:
        raise
    except Exception:
        raise PolicyCanonicalizationError("policy expression is invalid") from None


def canonicalize_policy_document(document: str | bytes) -> CanonicalPolicyAst:
    """Strictly parse, validate, and canonicalize an untrusted policy document."""

    try:
        return canonicalize_policy_expression(parse_policy_expression(document))
    except Exception:
        raise PolicyCanonicalizationError("policy document is invalid") from None


def verify_canonical_policy_bytes(
    document: str | bytes,
    expected_sha256: str,
) -> CanonicalPolicyAst:
    """Require byte-exact canonical source after a fresh parse and validation."""

    try:
        if (
            type(expected_sha256) is not str
            or _SHA256.fullmatch(expected_sha256) is None
        ):
            raise ValueError
        if type(document) is str:
            raw = document.encode("utf-8")
        elif type(document) is bytes:
            raw = bytes(document)
        else:
            raise ValueError
        canonical = canonicalize_policy_document(raw)
        if raw != canonical.utf8 or canonical.sha256 != expected_sha256:
            raise ValueError
        return canonical
    except Exception:
        raise PolicyCanonicalizationError(
            "canonical policy bytes are invalid"
        ) from None


__all__ = [
    "CanonicalPolicyAst",
    "PolicyCanonicalizationError",
    "canonicalize_policy_document",
    "canonicalize_policy_expression",
    "verify_canonical_policy_bytes",
]

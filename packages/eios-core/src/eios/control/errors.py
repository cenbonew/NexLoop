from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, ValidationError


class SafeValidationIssue(BaseModel):
    """Typed validation detail that never carries rejected input or context."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    code: str
    location: tuple[str | int, ...]


def safe_validation_errors(error: ValidationError) -> tuple[SafeValidationIssue, ...]:
    """Return deterministic validation details safe for audit serialization."""

    return tuple(
        SafeValidationIssue(
            code=detail["type"],
            location=detail["loc"],
        )
        for detail in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    )


class AccessControlError(RuntimeError):
    """Base class for safe access-control contract failures."""

    code = "access_control_error"


class InvalidApiKey(AccessControlError, ValueError):
    code = "invalid_api_key"


class ApiKeySecretConsumed(AccessControlError):
    code = "api_key_secret_consumed"


class AuthenticationUnavailable(AccessControlError):
    code = "authentication_unavailable"


class AccessCommandConflict(AccessControlError):
    code = "access_command_conflict"


class AccessValidationError(AccessControlError, ValueError):
    code = "access_validation_error"


class AccessTargetUnavailable(AccessControlError):
    code = "access_target_unavailable"


class ApiKeyNotFound(AccessTargetUnavailable):
    code = "api_key_not_found"


class AccessLifecycleConflict(AccessControlError):
    code = "access_lifecycle_conflict"


class AccessCredentialCollision(AccessLifecycleConflict):
    code = "access_credential_collision"


class AccessConcurrentConflict(AccessLifecycleConflict):
    code = "access_concurrent_conflict"


class ApiKeyScopeExceedsCaller(AccessControlError):
    """P0-5:新 key 的 scope 超出了签发者自己的 scope(权限提升)。

    ``excess`` 只列超出项,⛔ 回显调用方全集。
    """

    code = "access_scope_exceeds_caller"

    def __init__(self, excess: Iterable[str]) -> None:
        self.excess = tuple(sorted(set(excess)))
        super().__init__("requested scopes exceed the caller's scopes")


def require_scopes_within_caller(
    requested: Iterable[str], caller_scopes: frozenset[str] | None
) -> None:
    """``caller_scopes is None`` = 运维平面(DB 角色直连的 CLI),不经 HTTP,不在本闸内。"""

    if caller_scopes is None:
        return
    excess = set(requested) - set(caller_scopes)
    if excess:
        raise ApiKeyScopeExceedsCaller(excess)


class AccessStorageUnavailable(AccessControlError):
    code = "access_storage_unavailable"

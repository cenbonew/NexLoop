from __future__ import annotations


class AuthorizationError(RuntimeError):
    """Base class for bounded, framework-independent authorization failures."""

    code = "authorization_error"


class AuthorizationValidationError(AuthorizationError, ValueError):
    code = "authorization_invalid_input"


class AuthorizationUnavailable(AuthorizationError):
    code = "authorization_unavailable"


__all__ = [
    "AuthorizationError",
    "AuthorizationUnavailable",
    "AuthorizationValidationError",
]

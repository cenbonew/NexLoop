from __future__ import annotations


class IdentityError(RuntimeError):
    """Base class for bounded, framework-independent identity failures."""

    code = "identity_error"


class IdentityValidationError(IdentityError, ValueError):
    code = "identity_invalid_input"


class IdentityNotFound(IdentityError, LookupError):
    code = "identity_not_found"


class IdentityConflict(IdentityError):
    code = "identity_conflict"


class CredentialInvalid(IdentityError):
    code = "credentials_invalid"


class SessionInvalid(CredentialInvalid):
    code = "session_invalid"


class RequestOriginInvalid(IdentityValidationError):
    code = "request_origin_invalid"


class PasswordPolicyViolation(CredentialInvalid):
    code = "password_policy"


class IdentityRateLimited(IdentityError):
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        if type(retry_after_seconds) is not int or not 1 <= retry_after_seconds <= 3600:
            raise ValueError("identity retry delay is invalid")
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class IdentityUnavailable(IdentityError):
    code = "identity_unavailable"


__all__ = [
    "CredentialInvalid",
    "IdentityConflict",
    "IdentityError",
    "IdentityNotFound",
    "IdentityRateLimited",
    "IdentityUnavailable",
    "IdentityValidationError",
    "PasswordPolicyViolation",
    "RequestOriginInvalid",
    "SessionInvalid",
]

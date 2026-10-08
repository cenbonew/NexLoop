"""Typed request-scoped enterprise Identity rate limiting."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .errors import IdentityRateLimited, IdentityUnavailable
from .models import SecretDigest32, _FrozenModel, _aware_utc
from .ports import TrustedIdentityOperator


IdentityRateLimitAction = Literal[
    "local_login",
    "oidc_start",
    "password_reset",
    "password_reset_complete",
    "recovery",
    "invitation_accept",
]
_ACTIONS = frozenset(
    {
        "local_login",
        "oidc_start",
        "password_reset",
        "password_reset_complete",
        "recovery",
        "invitation_accept",
    }
)


class IdentityRateLimitDecision(_FrozenModel):
    allowed: bool
    retry_after_seconds: int = Field(ge=0, le=3_600)
    remaining: int = Field(ge=0, le=1_000_000)
    window_ends_at: datetime
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_decision(self) -> IdentityRateLimitDecision:
        _aware_utc(self.window_ends_at, "window_ends_at")
        if (self.allowed and self.retry_after_seconds != 0) or (
            not self.allowed and (self.retry_after_seconds < 1 or self.remaining != 0)
        ):
            raise ValueError("identity rate-limit decision is invalid")
        return self


class IdentityRateLimiter(Protocol):
    def consume(
        self,
        tenant_id: str,
        action: IdentityRateLimitAction,
        client_digest: SecretDigest32,
        *,
        operator: TrustedIdentityOperator,
    ) -> IdentityRateLimitDecision: ...


class IdentityRequestRateLimiter:
    """Bind a durable limiter to one HMAC client digest and trusted operator."""

    def __init__(
        self,
        repository: IdentityRateLimiter,
        *,
        client_digest: SecretDigest32,
        operator: TrustedIdentityOperator,
    ) -> None:
        if not callable(getattr(repository, "consume", None)):
            raise TypeError("identity rate limiter is required")
        if type(client_digest) is not SecretDigest32:
            raise TypeError("identity client digest is required")
        self._repository = repository
        self._client_digest = client_digest
        self._operator = TrustedIdentityOperator.model_validate(operator)

    def require(self, tenant_id: str, action: IdentityRateLimitAction) -> None:
        if (
            type(tenant_id) is not str
            or not 0 < len(tenant_id.strip()) <= 320
            or action not in _ACTIONS
        ):
            raise IdentityUnavailable("identity service is unavailable")
        try:
            decision = IdentityRateLimitDecision.model_validate(
                self._repository.consume(
                    tenant_id,
                    action,
                    self._client_digest,
                    operator=self._operator,
                )
            )
        except Exception:
            raise IdentityUnavailable("identity service is unavailable") from None
        if not decision.allowed:
            raise IdentityRateLimited(
                "identity rate limit exceeded",
                retry_after_seconds=decision.retry_after_seconds,
            )


__all__ = [
    "IdentityRateLimitAction",
    "IdentityRateLimitDecision",
    "IdentityRateLimiter",
    "IdentityRequestRateLimiter",
]

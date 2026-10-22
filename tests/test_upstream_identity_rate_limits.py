from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eios.identity.errors import IdentityRateLimited, IdentityUnavailable
from eios.identity.models import SecretDigest32
from eios.identity.ports import TrustedIdentityOperator
import eios.identity.rate_limits as rate_module


NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


class _Repository:
    def __init__(self, decision: object) -> None:
        self.decision = decision
        self.calls: list[tuple[object, ...]] = []

    def consume(
        self,
        tenant_id: str,
        action: str,
        client_digest: SecretDigest32,
        *,
        operator: TrustedIdentityOperator,
    ) -> object:
        self.calls.append((tenant_id, action, client_digest, operator))
        return self.decision


def _operator() -> TrustedIdentityOperator:
    return TrustedIdentityOperator(
        operator_principal_id="nex_eios_access_operator",
        request_id="request-rate-limit",
        trace_id="trace-rate-limit",
    )


def _decision(**changes: object):
    values: dict[str, object] = {
        "allowed": True,
        "retry_after_seconds": 0,
        "remaining": 4,
        "window_ends_at": NOW + timedelta(minutes=15),
        "revision": 1,
    }
    values.update(changes)
    return rate_module.IdentityRateLimitDecision(**values)


def test_request_rate_limiter_consumes_one_typed_digest_with_trusted_operator() -> None:
    repository = _Repository(_decision())
    digest = SecretDigest32(b"c" * 32)
    limiter = rate_module.IdentityRequestRateLimiter(
        repository,
        client_digest=digest,
        operator=_operator(),
    )

    limiter.require("tenant-a", "oidc_start")

    assert repository.calls == [("tenant-a", "oidc_start", digest, _operator())]


def test_request_rate_limiter_maps_denial_without_leaking_repository_details() -> None:
    repository = _Repository(
        _decision(allowed=False, retry_after_seconds=30, remaining=0)
    )
    limiter = rate_module.IdentityRequestRateLimiter(
        repository,
        client_digest=SecretDigest32(b"c" * 32),
        operator=_operator(),
    )

    with pytest.raises(IdentityRateLimited, match="rate limit exceeded") as caught:
        limiter.require("tenant-a", "local_login")
    assert caught.value.retry_after_seconds == 30


@pytest.mark.parametrize(
    "decision",
    (
        {
            "allowed": False,
            "retry_after_seconds": 0,
            "remaining": 0,
            "window_ends_at": NOW + timedelta(minutes=15),
            "revision": 1,
        },
        {
            "allowed": True,
            "retry_after_seconds": 1,
            "remaining": 4,
            "window_ends_at": NOW + timedelta(minutes=15),
            "revision": 1,
        },
        {
            "allowed": False,
            "retry_after_seconds": 3601,
            "remaining": 0,
            "window_ends_at": NOW + timedelta(minutes=15),
            "revision": 1,
        },
        object(),
    ),
)
def test_request_rate_limiter_fails_closed_on_invalid_adapter_results(
    decision: object,
) -> None:
    limiter = rate_module.IdentityRequestRateLimiter(
        _Repository(decision),
        client_digest=SecretDigest32(b"c" * 32),
        operator=_operator(),
    )

    with pytest.raises(IdentityUnavailable, match="identity service is unavailable"):
        limiter.require("tenant-a", "password_reset")


@pytest.mark.parametrize("value", (None, True, 0, 3601))
def test_rate_limited_error_requires_bounded_retry_hint(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        IdentityRateLimited(
            "identity rate limit exceeded",
            retry_after_seconds=value,  # type: ignore[arg-type]
        )

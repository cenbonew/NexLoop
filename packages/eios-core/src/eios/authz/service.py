from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from secrets import token_bytes

from .cache import AuthorizationDecisionCache, RevisionVectorValidator
from .errors import AuthorizationUnavailable
from .facts import AuthorizationFactQuery, AuthorizationFactsResolver
from .intersection import IntersectionResult, evaluate_intersection


@dataclass(frozen=True)
class AuthorizationDecisionResult:
    decision_id: str
    allowed: bool
    authoritative: bool
    reason_codes: tuple[str, ...]
    matched_grants: tuple[str, ...]
    matched_policies: tuple[str, ...]
    obligations: tuple[object, ...]
    expires_at: datetime
    cache_status: str


class AuthorizationDecisionService:
    def __init__(
        self,
        *,
        resolver: AuthorizationFactsResolver | None = None,
        cache: AuthorizationDecisionCache | None = None,
        revision_validator: RevisionVectorValidator | None = None,
    ) -> None:
        self._resolver = resolver
        self._cache = cache
        self._revision_validator = revision_validator

    def decide(self, query: AuthorizationFactQuery) -> AuthorizationDecisionResult:
        if self._resolver is None:
            raise AuthorizationUnavailable(
                "production authorization resolver is unavailable"
            )
        return self.decide_resolved(self._resolver.resolve(query))

    def decide_resolved(
        self,
        context: object,
        *,
        revision_validator: RevisionVectorValidator | None = None,
    ) -> AuthorizationDecisionResult:
        result: IntersectionResult | None = None
        cache_status = "bypass"
        validator = revision_validator or self._revision_validator
        if self._cache is not None and validator is not None:
            result = self._cache.get(context, validator)
            cache_status = "hit" if result is not None else "miss"
        if result is None:
            result = evaluate_intersection(context)
            if self._cache is not None:
                self._cache.put(context, result)
        return AuthorizationDecisionResult(
            decision_id=token_bytes(16).hex(),
            allowed=result.allowed,
            authoritative=True,
            reason_codes=tuple(reason.value for reason in result.reason_codes),
            matched_grants=result.matched_grants,
            matched_policies=result.matched_policies,
            obligations=result.obligations,
            expires_at=result.expires_at,
            cache_status=cache_status,
        )


__all__ = ["AuthorizationDecisionResult", "AuthorizationDecisionService"]

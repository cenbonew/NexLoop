from datetime import timedelta

import pytest

from eios.authz.cache import AuthorizationDecisionCache, RevisionValidation
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.facts import AuthorizationFactsResolver
from eios.authz.service import AuthorizationDecisionService

from test_eios_intersection import _resolved
from test_eios_facts import NOW, RecordingProvider, direct_query


class Validator:
    def __init__(self) -> None:
        self.valid = True
        self.calls = 0

    def validate(
        self, *, tenant_id: str, revision_vector: object
    ) -> RevisionValidation:
        self.calls += 1
        return RevisionValidation(
            valid=self.valid,
            trusted_now=NOW + timedelta(seconds=self.calls),
            event_high_water=101,
        )


def test_cache_hit_revalidates_full_vector_and_never_reuses_decision_id() -> None:
    context = _resolved()
    validator = Validator()
    cache = AuthorizationDecisionCache()
    service = AuthorizationDecisionService(cache=cache, revision_validator=validator)

    first = service.decide_resolved(context)
    second = service.decide_resolved(context)

    assert first.allowed and second.allowed
    assert first.cache_status == "miss"
    assert second.cache_status == "hit"
    assert first.decision_id != second.decision_id
    assert validator.calls == 1


def test_vector_drift_evicts_and_recomputes_without_stale_allow() -> None:
    context = _resolved()
    validator = Validator()
    service = AuthorizationDecisionService(
        cache=AuthorizationDecisionCache(), revision_validator=validator
    )
    first = service.decide_resolved(context)
    validator.valid = False
    second = service.decide_resolved(context)

    assert first.allowed and second.allowed
    assert first.decision_id != second.decision_id
    assert validator.calls == 1


def test_validator_exception_evicts_and_fails_closed() -> None:
    context = _resolved()

    class Broken:
        def validate(self, **kwargs: object) -> RevisionValidation:
            raise OSError("database unavailable")

    service = AuthorizationDecisionService(
        cache=AuthorizationDecisionCache(), revision_validator=Broken()
    )
    service.decide_resolved(context)
    with pytest.raises(AuthorizationUnavailable):
        service.decide_resolved(context)


def test_service_distinguishes_authoritative_deny_from_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.grants = None  # type: ignore[assignment]
    service = AuthorizationDecisionService(
        resolver=AuthorizationFactsResolver(provider)
    )

    with pytest.raises(AuthorizationUnavailable):
        service.decide(query)

    denied = AuthorizationDecisionService().decide_resolved(
        _resolved(include_dependency_grant=False)
    )
    assert not denied.allowed
    assert denied.authoritative

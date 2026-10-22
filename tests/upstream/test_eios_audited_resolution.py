from contextlib import contextmanager

from eios.authz.facts import AuthorizationFactsResolver

from test_eios_facts import RecordingProvider, direct_query


def test_existing_uow_resolution_does_not_open_or_close_provider_transaction() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.opens = 0

    @contextmanager
    def forbidden_open(query):
        provider.opens += 1
        yield provider.unit_of_work

    provider.open_unit_of_work = forbidden_open
    resolver = AuthorizationFactsResolver(provider)
    context = resolver.resolve_in_unit_of_work(query, provider.unit_of_work)
    assert context.query.tenant_id == query.tenant_id
    assert provider.opens == 0

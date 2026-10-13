"""NX-037 provider routing on actual PostgreSQL: reconciling an unknown result tries the configured providers' profile digests
until the ledger accepts one (0044 binds each intent to the provider that sent it). A refused digest must leave no trace: the
whole authorization is one transaction that raises before anything is written, so no query admission, no audit or ledger row,
no change to the lease or fence. Proven by fingerprinting every table, then by a routed reconcile that tries the wrong provider
first."""
import pytest

from nexloop_eios.effect_dispatch import EffectDispatcher, ProviderRoutes
from nexloop_eios.effect_execution import EffectExecutionUnavailable
from nexloop_eios.effect_provider import EffectProviderConfiguration, HttpEffectProvider
from support.effect_provider import effect_provider
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401


def transport(server):
    return HttpEffectProvider(EffectProviderConfiguration(server.origin, test_loopback_http=True, timeout=1))


def fingerprint(admin):
    """Row count and content digest of every table of the application schemas."""
    tables = admin.execute("""select format('%I.%I', schemaname, tablename) from pg_tables
        where schemaname in ('runtime','control','authz','ontology') order by 1""").fetchall()
    out = {}
    for (table,) in tables:
        out[table] = admin.execute(f'select count(*), md5(coalesce(string_agg(t::text, chr(10) order by t::text), \'\')) from {table} t').fetchone()
    return out


def unknown_intent(fixture, a):
    receipt = fixture.ports[0].submit_effect_intent(parameters={'message': 'routing reconcile'})
    intent = receipt['intent_id']
    pa = transport(a)
    with fixture.current_executor() as executor:
        job = executor.claim_effect(lease_seconds=30)
        identity = dict(intent_id=intent, fence=job['fence'])
        admitted = executor.prepare_effect_dispatch(**identity, provider_profile_digest=pa.profile_digest)
        assert pa.dispatch(intent_id=intent, payload_digest=admitted['provider_payload_digest'], parameters=admitted['parameters']).state == 'accepted'
        executor.record_effect_unknown(**identity)
    return intent


def test_a_refused_provider_digest_leaves_no_trace(governed_effect_executor, admin, tmp_path):  # noqa: F811
    fixture = governed_effect_executor
    with effect_provider(tmp_path / 'a.sqlite') as a, effect_provider(tmp_path / 'b.sqlite') as b:
        intent = unknown_intent(fixture, a)
        pb = transport(b)
        with fixture.current_executor() as worker:
            job = worker.claim_effect(lease_seconds=30)
            assert job['stage'] == 'reconcile' and job.get('action') is None  # a query claim carries no Action
            before = fingerprint(admin)
            lease = admin.execute('select state,fence,lease_until,lease_credential,lease_principal from runtime.nexloop_effect_outbox where intent_id=%s', (intent,)).fetchone()
            for _ in range(3):
                with pytest.raises(EffectExecutionUnavailable):
                    worker.authorize_effect_query(intent_id=intent, fence=job['fence'], provider_profile_digest=pb.profile_digest)
            assert fingerprint(admin) == before  # no admission, audit, ledger or any other row written or changed
            assert admin.execute('select state,fence,lease_until,lease_credential,lease_principal from runtime.nexloop_effect_outbox where intent_id=%s', (intent,)).fetchone() == lease
            assert admin.execute('select count(*) from runtime.nexloop_effect_query_admissions where intent_id=%s', (intent,)).fetchone() == (0,)
            assert b.control('snapshot')['requests'] == []
            # The same lease still authorizes the right provider afterwards: the refusals consumed nothing.
            admission = worker.authorize_effect_query(intent_id=intent, fence=job['fence'], provider_profile_digest=transport(a).profile_digest)
            assert admission['query_id']
            assert admin.execute('select count(*) from runtime.nexloop_effect_query_admissions where intent_id=%s', (intent,)).fetchone() == (1,)


def test_a_routed_reconcile_tries_the_wrong_provider_first_and_queries_only_the_one_that_sent(governed_effect_executor, admin, tmp_path):  # noqa: F811
    fixture = governed_effect_executor
    with effect_provider(tmp_path / 'a.sqlite') as a, effect_provider(tmp_path / 'b.sqlite') as b:
        intent = unknown_intent(fixture, a)
        a.control('fulfill', intent_id=intent)
        routes = ProviderRoutes({'nexloop.enterprise.deliver': transport(b), 'nexloop.service.request': transport(a)})  # b is tried first
        with fixture.current_executor() as worker:
            result = EffectDispatcher(worker, routes, lease_seconds=30).run_once()
        assert result['status'] == 'fulfilled' and result['business_action_success'] is True
        assert a.control('snapshot')['requests'] == [('POST', intent, 202), ('GET', intent, 200)]
        assert b.control('snapshot')['requests'] == []
        assert admin.execute('select count(*) from runtime.nexloop_effect_query_admissions where intent_id=%s', (intent,)).fetchone() == (1,)


def test_an_unrouted_intent_is_recorded_once_as_nxe01_and_shows_in_the_metrics(governed_effect_executor, admin, tmp_path):  # noqa: F811
    """The worker's 'unrouted' answer is recorded as the effect refusal NXE01 (0470) through the executor's own port: one row per
    intent within ten minutes, the executor's own intents only, visible as refusals.last_5m in the M12 snapshot."""
    import uuid
    fixture = governed_effect_executor
    receipt = fixture.ports[0].submit_effect_intent(parameters={'message': 'no route'})
    intent = receipt['intent_id']
    routes = ProviderRoutes({'nexloop.enterprise.deliver': HttpEffectProvider(EffectProviderConfiguration('http://127.0.0.1:20011', test_loopback_http=True, timeout=1))})
    with fixture.current_executor() as worker:
        assert EffectDispatcher(worker, routes, lease_seconds=30).run_once() == {'claimed': True, 'status': 'unrouted', 'business_action_success': False}
        assert worker.record_unrouted(intent_id=intent) is False  # the dispatcher already recorded it: deduplicated
        assert worker.record_unrouted(intent_id=str(uuid.uuid4())) is None  # not this executor's intent: refused, nothing written
    rows = admin.execute("select path,code from runtime.nexloop_dispatch_refusals where intent_id=%s", (intent,)).fetchall()
    assert rows == [('effect', 'NXE01')]
    assert admin.execute("select count(*) from runtime.nexloop_dispatch_refusals where code='NXE01'").fetchone() == (1,)
    tenant = admin.execute('select tenant_id from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()[0]
    snapshot = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'refusals'->'last_5m'", (tenant,)).fetchone()[0]
    assert snapshot.get('NXE01') == 1
    assert admin.execute("select state from runtime.nexloop_effect_intents where intent_id=%s", (intent,)).fetchone() == ('accepted',)  # never admitted

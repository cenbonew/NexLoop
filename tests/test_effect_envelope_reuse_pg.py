"""ADR-029b (owner 2026-10-26, option A): a guarded effect tool request takes the Run's still-valid relationship, formal fact and
catalog envelopes from the Run's entries (ADR-029) instead of building them again; SQL re-verifies each one against current state
at every use exactly as a new one. Under test: reuse happens; a revoked Source READ, a revised offering and a refused reused envelope
refuse the next effect request, and a refusal drops the Run's entries (the next request builds fresh); kinds outside the three
(the role envelope) stay request-only; the 2 s limit, the lock order and the authorization scope are not touched here. Actual PG
authority (catalog envelope, test_context_artifacts fixture) plus unit checks of the kind filter."""
import pytest
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios import role_runs
from nexloop_eios.effect_intents import EffectIntentUnavailable
from nexloop_eios.runtime_activation import RuntimeActivationPort
from test_context_artifacts import active_worker, context_message, revoke_scope  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


@pytest.fixture(autouse=True)
def empty_cache():
    role_runs._run_envelopes.clear()
    yield
    role_runs._run_envelopes.clear()


@pytest.fixture
def builds(monkeypatch):
    count = {'catalog': 0, 'role': 0}
    catalog, role = RuntimeActivationPort._context_catalog_envelope_now, role_runs._role_envelope_for_run

    def counted_catalog(self, resolved):
        count['catalog'] += 1
        return catalog(self, resolved)

    def counted_role(*arguments):
        count['role'] += 1
        return role(*arguments)
    monkeypatch.setattr(RuntimeActivationPort, '_context_catalog_envelope_now', counted_catalog)
    monkeypatch.setattr(role_runs, '_role_envelope_for_run', counted_role)
    return count


def authorize(worker, activation, command, operation='model'):
    return worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation=operation)


def submit(worker, activation, command, message='actual guarded service'):
    return worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='submit', parameters={'message': message})


def entries():
    return {key[1] for key in role_runs._run_envelopes}


def test_an_effect_request_reuses_the_runs_catalog_envelope_but_builds_its_role_envelope(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        assert authorize(worker, activation, command)['authorized'] is True
        assert builds['catalog'] == 1 and 'catalog' in entries()
        roles = builds['role']
        for _ in range(2):
            assert submit(worker, activation, command)['receipt']['state'] == 'accepted'
        # catalog from the Run's entries; the role envelope is not one of the three kinds: built for each request.
        assert builds['catalog'] == 1 and builds['role'] == roles + 2
        # Even a Run entry of another kind is never taken by an effect request: a refused role envelope placed there is ignored.
        catalog = next(key for key in role_runs._run_envelopes if key[1] == 'catalog')
        role_runs._run_envelopes[(catalog[0], 'role') + catalog[2:5] + (catalog[0],)] = (envelope(60), role_runs._run_envelopes[catalog][1])
        assert submit(worker, activation, command)['receipt']['state'] == 'accepted' and builds['role'] == roles + 3


def test_a_revoked_source_read_refuses_the_next_effect_request(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        assert submit(worker, activation, command)['receipt']['state'] == 'accepted' and builds['catalog'] == 1
        revoke_scope(f, admin, 'artifact.read')
        worker = worker._backend.authenticate(f['f']['tokens']['assembly-runtime-worker'], world='real')
        with pytest.raises(EffectIntentUnavailable):
            submit(worker, activation, command, 'forbidden after revocation')


def test_a_revised_offering_refuses_the_next_effect_request(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        admin.execute("update ontology.objects set nexloop_revision=nexloop_revision+1 where type_name='ServiceOffering'")
        with pytest.raises(EffectIntentUnavailable):
            submit(worker, activation, command)
        assert builds['catalog'] == 1 and not role_runs._run_envelopes  # refused with the reused one; the Run's entries dropped
        with pytest.raises(AuthorizationUnavailable):
            authorize(worker, activation, command)  # fresh envelope, still the revised offering
        assert builds['catalog'] == 2


def test_a_refused_reused_envelope_is_dropped_and_the_next_effect_request_builds_fresh(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        key = next(key for key in role_runs._run_envelopes if key[1] == 'catalog')
        value, until = role_runs._run_envelopes[key]
        role_runs._run_envelopes[key] = (dict(value, signature='0' * len(value['signature'])), until)  # SQL refuses it
        with pytest.raises(EffectIntentUnavailable):
            submit(worker, activation, command)  # one request, one answer: no rebuild inside it
        assert not role_runs._run_envelopes
        assert submit(worker, activation, command)['receipt']['state'] == 'accepted'
        assert builds['catalog'] == 2


def envelope(seconds):
    from datetime import UTC, datetime, timedelta
    return {'text': '{"expires_at":"%s"}' % (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), 'signature': 's'}


def test_inside_a_request_only_the_allowed_kinds_come_from_the_runs_entries():
    calls = []

    def compute(label):
        def run():
            calls.append(label)
            return envelope(20)
        return run
    with role_runs.run_envelope_scope('run-a'):  # an authorization: every kind is kept for the Run
        for kind in ('catalog', 'relationship', 'context_formal', 'role', 'policy'):
            role_runs.scoped_envelope((kind, 1), compute(kind))
    assert len(calls) == 5
    with role_runs.request_envelope_scope(), role_runs.run_envelope_scope('run-a', kinds=role_runs.EFFECT_REUSE_KINDS):
        for kind in ('catalog', 'relationship', 'context_formal', 'role', 'policy'):
            role_runs.scoped_envelope((kind, 1), compute(kind))
            role_runs.scoped_envelope((kind, 1), compute(kind))  # the request's own cache answers the second time
    assert calls[5:] == ['role', 'policy']
    with role_runs.request_envelope_scope():  # a request without a Run scope: request-only, as before ADR-029b
        role_runs.scoped_envelope(('catalog', 1), compute('catalog'))
    assert calls[7:] == ['catalog']
    assert role_runs.EFFECT_REUSE_KINDS == {'relationship', 'context_formal', 'catalog'}

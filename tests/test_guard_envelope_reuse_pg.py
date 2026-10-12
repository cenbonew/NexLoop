"""ADR-029 (owner 2026-10-12, conditional approval): a guard authorization reuses the Run's still-valid Source-signed Context
envelopes; SQL re-verifies each one against current state at every use exactly as a new one. Conditions under test: a
revoked Source authority, a revised object and a revoked Run credential refuse the next authorization (no stale pass); a
reused envelope SQL refuses makes that authorization fail and drops the Run's entries (the next one builds fresh); an envelope with less than 5 s left is
rebuilt; entries are per Run, bounded, and a failure is never cached. A contact restriction is not checked at activation
for new or reused envelopes alike (it is enforced at effect dispatch), so reuse changes nothing there. Actual PG authority
(catalog envelope, test_context_artifacts fixture) plus unit checks of the cache."""
from datetime import UTC, datetime, timedelta

import pytest
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios import role_runs
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
    count = {'catalog': 0}
    original = RuntimeActivationPort._context_catalog_envelope_now

    def counted(self, resolved):
        count['catalog'] += 1
        return original(self, resolved)
    monkeypatch.setattr(RuntimeActivationPort, '_context_catalog_envelope_now', counted)
    return count


def authorize(worker, activation, command, operation='model'):
    return worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation=operation)


def test_reuse_within_validity_and_sql_still_verifies(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        for operation in ('model', 'tool', 'model'):
            assert authorize(worker, activation, command, operation)['authorized'] is True
        assert builds['catalog'] == 1 and len(role_runs._run_envelopes) == 1


def test_revoked_source_authority_refuses_the_next_authorization(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        revoke_scope(f, admin, 'artifact.read')
        worker = worker._backend.authenticate(f['f']['tokens']['assembly-runtime-worker'], world='real')
        with pytest.raises(AuthorizationUnavailable):
            authorize(worker, activation, command)


def test_revised_offering_refuses_the_next_authorization(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        admin.execute("update ontology.objects set nexloop_revision=nexloop_revision+1 where type_name='ServiceOffering'")
        with pytest.raises(AuthorizationUnavailable):
            authorize(worker, activation, command)
        assert builds['catalog'] == 1 and not role_runs._run_envelopes  # refused with the reused one; the Run's entries dropped


def test_revoked_run_credential_refuses_the_next_authorization(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s", (command['run_id'],))
        with pytest.raises(AuthorizationUnavailable):
            authorize(worker, activation, command)


def test_a_refused_reused_envelope_is_dropped_and_the_next_authorization_builds_fresh(context_message, admin, tmp_path, builds):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)
        (key, (value, until)), = role_runs._run_envelopes.items()
        role_runs._run_envelopes[key] = (dict(value, signature='0' * len(value['signature'])), until)  # SQL refuses it
        with pytest.raises(AuthorizationUnavailable):
            authorize(worker, activation, command)  # one authorization, one answer: no retry inside the guard
        assert not role_runs._run_envelopes  # the Run's entries were dropped
        assert authorize(worker, activation, command)['authorized'] is True
        assert builds['catalog'] == 2


def test_less_than_the_margin_left_is_rebuilt(context_message, admin, tmp_path, builds, monkeypatch):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    monkeypatch.setattr(role_runs, 'RUN_ENVELOPE_MARGIN', 3600.0)  # every envelope has less left than this
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        before = builds['catalog']
        for _ in range(3):
            authorize(worker, activation, command)
        assert builds['catalog'] - before == 3 and not role_runs._run_envelopes


def envelope(seconds):
    return {'text': '{"expires_at":"%s"}' % (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), 'signature': 's'}


def test_entries_are_per_run_bounded_and_failures_are_not_cached(monkeypatch):
    calls = []

    def compute(value):
        def run():
            calls.append(value)
            return value
        return run
    with role_runs.run_envelope_scope('run-a'):
        role_runs.scoped_envelope(('catalog', 1), compute(envelope(20)))
        role_runs.scoped_envelope(('catalog', 1), compute(envelope(20)))
    with role_runs.run_envelope_scope('run-b'):
        role_runs.scoped_envelope(('catalog', 1), compute(envelope(20)))  # another Run never sees run-a's entry
    assert len(calls) == 2
    with role_runs.run_envelope_scope('run-c'):
        with pytest.raises(RuntimeError):
            role_runs.scoped_envelope(('catalog', 1), lambda: (_ for _ in ()).throw(RuntimeError('refused')))
    assert not any(key[0] == 'run-c' for key in role_runs._run_envelopes)
    assert role_runs.envelope_valid_until({'text': '{"no":"expiry"}'}) is None  # nothing without a readable expiry
    monkeypatch.setattr(role_runs, 'RUN_ENVELOPE_LIMIT', 3)
    for index in range(6):
        with role_runs.run_envelope_scope(f'run-{index}'):
            role_runs.scoped_envelope(('catalog', 1), compute(envelope(20)))
    assert len(role_runs._run_envelopes) == 3
    role_runs.forget_run_envelopes('run-5')
    assert not any(key[0] == 'run-5' for key in role_runs._run_envelopes)


def test_inspect_and_final_recheck_refuse_a_context_envelope_in_the_claims(context_message, admin, tmp_path):
    """0176: the ADR-027/028 guards against a Context-carrying inspect or final recheck look at the signed claims too."""
    import psycopg
    from nexloop_eios.authorization import _identity
    from nexloop_eios.runtime_activation import _command
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        authorize(worker, activation, command)  # execution marker exists, envelopes are cached
        port = RuntimeActivationPort(worker._backend._pool, worker._session, worker._backend._signer)
        envelope = next(iter(role_runs._run_envelopes.values()))[0]
        command_text, digest = _command(command)
        with port.pool.connection() as db, db.transaction():
            queue = db.execute('select authz.nexloop_runtime_activation_hint(%s,%s,%s)', (worker._session.token_digest, worker._session.world, activation['activation_ref'])).fetchone()[0]
        assert 'formal_reads' in admin.execute("select pg_get_functiondef('authz.nexloop_runtime_activation_command(text,text,text,text,text)'::regprocedure)").fetchone()[0]
        for operation, extra in (('inspect', {}), ('tool', {'final_recheck': True})):
            binding = dict(activation_ref=activation['activation_ref'], command_text=command_text, command_digest=digest, input_digest=None, operation=operation)
            with port.pool.connection() as db:
                resolved = port._execute(db, port._signed(queue, 'resolve', **dict(binding, operation='inspect')))
                run = _identity(db, resolved['_run_digest'], worker._session.world)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    port._execute(db, port._signed(queue, 'authorize', **binding, **extra, run_proofs=port._run_proofs(run), context_catalog_envelope=envelope))

"""Inspect authorization scope (0360, ADR-027): an inspect is authorized by the activation core only. When the Run's
Context artifact can no longer be read (deleted, expired or altered), the Run's state can still be inspected, with no Context
in the answer, while model, tool and resume are refused as before. The core's own rechecks still refuse an inspect: a changed
source authority (any revocation through the manifest voids the Run credential, 0034) and a lost task lease. Actual PG
authority, guard port only (no Host)."""
import pytest
from eios.authz.errors import AuthorizationUnavailable
from test_context_artifacts import active_worker, context_message, revoke_scope  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


FAULTS = {'status': "update runtime.nexloop_local_artifacts set status='deleted'",
          'expiry': "update runtime.nexloop_local_artifacts set retention_until=clock_timestamp()-interval '1 second'",
          'sha256': "update runtime.nexloop_local_artifacts set sha256=repeat('0',64)"}
INSPECT_KEYS = {'authorized', 'activation_ref', 'run_id', 'task_id', 'fence', 'owner_epoch', 'command_digest', 'input_digest', 'ever_execution_authorized'}


@pytest.mark.parametrize('fault', sorted(FAULTS))
def test_inspect_survives_an_unreadable_context_without_context_while_execution_is_refused(context_message, admin, tmp_path, fault):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        before = worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='start', input=text)
        assert before['authorized'] is True and 'context_artifact' in before
        inspected = worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='inspect')
        assert inspected['authorized'] is True and set(inspected) <= INSPECT_KEYS
        admin.execute(FAULTS[fault])
        for operation in ('model', 'tool'):
            with pytest.raises(AuthorizationUnavailable):
                worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation=operation)
        for operation in ('start', 'resume'):
            with pytest.raises(AuthorizationUnavailable):
                worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation=operation, input=text)
        still = worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='inspect')
        assert still['authorized'] is True and set(still) <= INSPECT_KEYS and still['run_id'] == command['run_id']
        assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone() == (0,)


def test_inspect_is_refused_once_the_source_authority_changed(context_message, admin, tmp_path):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        assert worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='inspect')['authorized'] is True
        revoke_scope(f, admin, 'artifact.read')
        worker = worker._backend.authenticate(f['f']['tokens']['assembly-runtime-worker'], world='real')
        for operation in ('inspect', 'model'):
            with pytest.raises(AuthorizationUnavailable):
                worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation=operation)


def test_inspect_still_rechecks_the_task_lease(context_message, admin, tmp_path):
    f = context_message
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        assert worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='inspect')['authorized'] is True
        admin.execute("update runtime.jobs set lease_until=clock_timestamp()-interval '1 second' where job_id=(select task_id from authz.nexloop_runtime_activations where activation_ref=%s)",
                      (activation['activation_ref'],))
        with pytest.raises(AuthorizationUnavailable):
            worker.authorize_runtime_activation(activation_ref=activation['activation_ref'], command=command, operation='inspect')

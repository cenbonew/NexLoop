"""Embedded trusted Backend queue ports, using real restricted PostgreSQL roles."""
from contextlib import ExitStack
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from authority_fixture import seed_authority
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.backend import BackendClosed, open_backend
from nexloop_eios.bootstrap import bootstrap


@pytest.fixture
def backends(admin, pg, tmp_path):
    bootstrap(admin)
    material = secrets.token_bytes(32)
    key = tmp_path / 'synthetic-queue-key'
    key.write_bytes(material)
    key.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',
        ('synthetic-backend-queue', material))
    roles = ('nexloop_api', 'nexloop_domain_worker', 'nexloop_scheduler')
    tokens = {role: seed_authority(admin, 'synthetic-a',
        'eios:action:NexLoop.queue.operations:1', operation=Operation.EXECUTE,
        resource_type=ResourceType.ACTION, identity_suffix='-' + role)[0] for role in roles}
    # All synthetic authority configuration completes before authentication;
    # realm epoch and live checks are preserved, never patched around.
    with ExitStack() as stack:
        opened = {}
        for role in roles:
            backend = stack.enter_context(open_backend(database_url=make_conninfo(pg, user=role),
                artifact_root=tmp_path / role, signing_key_file=key,
                signing_key_id='synthetic-backend-queue'))
            opened[role] = (backend, backend.authenticate(tokens[role], world='real'))
        yield opened


def accept(api):
    return api.accept_event(queue='operations', source_id='synthetic-channel',
        event_id='synthetic-backend-event', payload={'text': 'synthetic'})


def test_backend_api_domain_worker_and_scheduler_complete_real_ledger(backends, admin):
    _, api = backends['nexloop_api']
    _, worker = backends['nexloop_domain_worker']
    _, scheduler = backends['nexloop_scheduler']
    accepted = accept(api)
    assert accepted['accepted'] and accepted['created']
    assert accept(api) == {**accepted, 'created': False}
    job = worker.claim_task(queue='operations', lease_seconds=1)
    assert job['task_id'] == accepted['task_id']
    assert worker.renew_task(queue='operations', task_id=job['task_id'], fence=job['fence'], lease_seconds=30)['fence'] == job['fence']
    assert worker.finish_task(queue='operations', task_id=job['task_id'], fence=job['fence'],
        status='succeeded', result={'synthetic': True})['status'] == 'succeeded'
    message = scheduler.claim_outbox(queue='operations')
    assert scheduler.acknowledge_outbox(queue='operations', outbox_id=message['outbox_id'], fence=message['fence'])['delivered']
    assert scheduler.acknowledge_outbox(queue='operations', outbox_id=message['outbox_id'], fence=message['fence'])['delivered']
    assert api.inspect_task(queue='operations',task_id=accepted['task_id'])['result']=={'synthetic':True}
    assert scheduler.claim_outbox(queue='operations') is None
    assert worker.claim_task(queue='operations') is None
    assert admin.execute('select status from runtime.invocations').fetchone()[0] == 'succeeded'
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['claim_task', 'claim_outbox'])
def test_backend_api_cannot_dispatch_worker_ports(backends, operation):
    _, api = backends['nexloop_api']
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        getattr(api, operation)(queue='operations')


def test_backend_queue_rechecks_live_authority(backends, admin):
    _, api = backends['nexloop_api']
    _, worker = backends['nexloop_domain_worker']
    accept(api)
    job = worker.claim_task(queue='operations')
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",
        (worker._session.token_digest,))
    with pytest.raises(AuthorizationUnavailable):
        worker.finish_task(queue='operations', task_id=job['task_id'], fence=job['fence'], status='succeeded')
    assert admin.execute('select status from runtime.jobs').fetchone()[0] == 'running'


@pytest.mark.parametrize('operation', ['inspect_task', 'accept_event', 'claim_task', 'finish_task',
    'renew_task', 'claim_outbox', 'acknowledge_outbox'])
def test_backend_closed_queue_handles_are_denied(backends, operation):
    backend, api = backends['nexloop_api']
    backend._shutdown()
    kwargs = {'queue': 'operations'}
    if operation == 'accept_event':
        kwargs.update(source_id='synthetic', event_id='synthetic', payload={})
    elif operation=='inspect_task':
        kwargs.update(task_id='synthetic')
    elif operation in ('finish_task', 'renew_task'):
        kwargs.update(task_id='synthetic', fence=1)
        if operation == 'finish_task':
            kwargs['status'] = 'succeeded'
    elif operation == 'acknowledge_outbox':
        kwargs.update(outbox_id='synthetic', fence=1)
    with pytest.raises(BackendClosed):
        getattr(api, operation)(**kwargs)

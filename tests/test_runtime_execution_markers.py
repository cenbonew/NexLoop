"""Actual restricted PG execution history, independent of queue attempt count."""
import psycopg
import pytest
from eios.authz.errors import AuthorizationUnavailable
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials, activation, authorize


def test_marker_is_monotonic_across_new_fence(authority,synthetic_credentials,admin):
    ref=activation(authority,synthetic_credentials)
    _,command,worker,_,_=authority
    assert ref['ever_execution_authorized'] is False
    assert authorize(worker,ref['activation_ref'],command,'start')['ever_execution_authorized'] is False
    assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()[0]==0
    assert authorize(worker,ref['activation_ref'],command,'model')['ever_execution_authorized'] is True
    first=admin.execute('select * from authz.nexloop_runtime_execution_markers').fetchall()
    assert authorize(worker,ref['activation_ref'],command,'tool')['ever_execution_authorized'] is True
    assert admin.execute('select * from authz.nexloop_runtime_execution_markers').fetchall()==first
    worker.finish_task(queue='operations',task_id=ref['task_id'],fence=ref['fence'],status='retry_wait',retry_seconds=0)
    job=worker.claim_task(queue='operations')
    assert job['fence']>ref['fence']
    next_ref=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],
        run_id=command['run_id'],command=command,input='synthetic activation input',owner_epoch=1)
    assert next_ref['ever_execution_authorized'] is True
    assert admin.execute('select * from authz.nexloop_runtime_execution_markers').fetchall()==first


def test_final_expiry_rolls_back_execution_marker(authority,synthetic_credentials,admin):
    ref=activation(authority,synthetic_credentials)
    _,command,worker,_,_=authority
    # This disposable technical trigger creates the precise final-check window;
    # no production database or formal business row is touched.
    admin.execute("create function public.synthetic_marker_delay() returns trigger language plpgsql as $$begin perform pg_sleep(1.2);return new;end$$")
    admin.execute('grant execute on function public.synthetic_marker_delay() to nexloop_owner')
    admin.execute('create trigger synthetic_marker_delay before insert on authz.nexloop_runtime_execution_markers for each row execute function public.synthetic_marker_delay()')
    worker.renew_task(queue='operations',task_id=ref['task_id'],fence=ref['fence'],lease_seconds=1)
    with pytest.raises(AuthorizationUnavailable):authorize(worker,ref['activation_ref'],command,'model')
    assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()[0]==0


@pytest.mark.parametrize('operation',['model','tool'])
def test_revocation_cannot_create_execution_marker(authority,synthetic_credentials,admin,operation):
    ref=activation(authority,synthetic_credentials)
    _,command,worker,_,invocation=authority
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s",([invocation.release_id],))
    with pytest.raises(AuthorizationUnavailable):authorize(worker,ref['activation_ref'],command,operation)
    assert admin.execute('select count(*) from authz.nexloop_runtime_execution_markers').fetchone()[0]==0


@pytest.mark.parametrize('statement',[
    'select * from authz.nexloop_runtime_execution_markers',
    'delete from authz.nexloop_runtime_execution_markers',
    "select authz.nexloop_runtime_activation_command_v0038('','','','','')",
])
def test_worker_cannot_bypass_protected_marker(authority,statement):
    worker=authority[2]
    with worker._backend._pool.connection() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():connection.execute(statement)

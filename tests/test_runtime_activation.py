"""Restricted PostgreSQL activation recovery; no raw Run secret reaches Host.

Only disposable test authority/schema configuration uses the privileged fixture.
The synthetic credential factory capture stays in test memory to reopen normal
Backend authentication; credentials are not stored in runtime files or fixtures.
"""
from contextlib import contextmanager
from datetime import UTC, datetime
import copy
import json
import time
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.backend import open_backend
import test_runtime_authority as runtime_fixture
from test_runtime_authority import authority


class SyntheticCredentials(dict):
    def __repr__(self):return '<synthetic credentials redacted>'
    __str__=__repr__


@pytest.fixture(autouse=True)
def synthetic_credentials(monkeypatch):
    tokens=SyntheticCredentials()
    seed=runtime_fixture.seed_authority
    agent=runtime_fixture.seed_agent
    def remember_authority(*args,**kwargs):
        result=seed(*args,**kwargs)
        tokens[kwargs.get('identity_suffix')]=result[0]
        return result
    def remember_agent(*args,**kwargs):
        result=agent(*args,**kwargs);tokens['agent']=result[0];return result
    monkeypatch.setattr(runtime_fixture,'seed_authority',remember_authority)
    monkeypatch.setattr(runtime_fixture,'seed_agent',remember_agent)
    return tokens


@contextmanager
def reopened_worker(pg,tmp_path,tokens):
    with open_backend(database_url=make_conninfo(pg,user='nexloop_domain_worker'),
        artifact_root=tmp_path/'reopened-worker',signing_key_file=tmp_path/'synthetic-authority',
        signing_key_id='synthetic-runtime') as backend:
        yield backend.authenticate(tokens['-queue-worker'],world='real')


def registration(authority,tokens,*,command=None,issued=None,task_id=None):
    guard,original,_,original_issued,_=authority
    command=command or original;issued=issued or original_issued;task_id=task_id or guard.task_id
    api=guard.backend.authenticate(tokens['-queue-api'],world='real')
    return api.register_runtime_run(queue='operations',task_id=task_id,run_token=issued.token,
        command=command,input='synthetic activation input')


def activation(authority,tokens):
    guard,command,worker,issued,_=authority
    registered=registration(authority,tokens)
    assert registered['registered'] and issued.token not in repr(registered)
    result=worker.create_runtime_activation(queue='operations',task_id=guard.task_id,fence=guard.fence,
        run_id=issued.run_id,command=command,input='synthetic activation input',owner_epoch=command['runtime_owner_epoch'])
    assert result['activation_ref'].startswith('activation_') and issued.token not in repr(result)
    return result


def authorize(worker,ref,command,operation='model'):
    arguments={'input':'synthetic activation input'} if operation in ('start','resume') else {}
    return worker.authorize_runtime_activation(activation_ref=ref,command=command,operation=operation,**arguments)


def test_reopen_restricted_backend_recovers_by_opaque_ref(authority,synthetic_credentials,pg,tmp_path):
    result=activation(authority,synthetic_credentials);_,command,original_worker,issued,_=authority
    original_worker._backend._shutdown()
    with reopened_worker(pg,tmp_path,synthetic_credentials) as worker:
        for operation in ['start','resume','inspect','cancel','model','tool']:
            allowed=authorize(worker,result['activation_ref'],command,operation)
            assert allowed['authorized'] and allowed['run_id']==issued.run_id
            assert issued.token not in repr(allowed)
    assert 'token' not in ' '.join(result.keys()).lower()


def test_worker_cannot_bind_an_arbitrary_known_run_without_api_registration(authority):
    guard,command,worker,issued,_=authority
    with pytest.raises(AuthorizationUnavailable):
        worker.create_runtime_activation(queue='operations',task_id=guard.task_id,fence=guard.fence,
            run_id=issued.run_id,command=command,input='synthetic activation input',owner_epoch=1)


@pytest.mark.parametrize('field',['run_id','tenant_id','world_id','role_ref','budget','runtime_owner_epoch'])
def test_activation_command_digest_and_realm_cannot_be_overridden(authority,synthetic_credentials,field):
    result=activation(authority,synthetic_credentials);_,command,worker,_,_=authority
    changed=copy.deepcopy(command)
    changed[field]={'maximum_model_turns':1} if field=='budget' else str(uuid.uuid4())
    with pytest.raises(AuthorizationUnavailable):authorize(worker,result['activation_ref'],changed)


@pytest.mark.parametrize('mutation',['ref','operation','input','owner_epoch','run_id','fence'])
def test_activation_invalid_reference_or_conflicting_binding_denied(authority,synthetic_credentials,mutation):
    result=activation(authority,synthetic_credentials);guard,command,worker,issued,_=authority
    if mutation in ['ref','operation']:
        with pytest.raises(AuthorizationUnavailable):
            authorize(worker,'activation_'+str(uuid.uuid4()) if mutation=='ref' else result['activation_ref'],command,
                'unguarded' if mutation=='operation' else 'model')
    else:
        kwargs=dict(queue='operations',task_id=guard.task_id,fence=guard.fence,run_id=issued.run_id,
            command=command,input='synthetic activation input',owner_epoch=command['runtime_owner_epoch'])
        kwargs[mutation]=str(uuid.uuid4()) if mutation=='run_id' else (guard.fence+1 if mutation=='fence' else (2 if mutation=='owner_epoch' else 'different input'))
        with pytest.raises(AuthorizationUnavailable):worker.create_runtime_activation(**kwargs)


def test_current_source_agent_revocation_denies_opaque_activation(authority,synthetic_credentials,admin):
    result=activation(authority,synthetic_credentials);_,command,worker,_,invocation=authority
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s",([invocation.release_id],))
    with pytest.raises(AuthorizationUnavailable):authorize(worker,result['activation_ref'],command)


def test_finished_task_denies_opaque_activation(authority,synthetic_credentials):
    result=activation(authority,synthetic_credentials);guard,command,worker,_,_=authority
    worker.finish_task(queue='operations',task_id=guard.task_id,fence=guard.fence,status='succeeded')
    with pytest.raises(AuthorizationUnavailable):authorize(worker,result['activation_ref'],command)


@pytest.mark.parametrize('authority',[1],indirect=True)
def test_expired_task_and_reclaimed_fence_deny_old_activation(authority,synthetic_credentials):
    result=activation(authority,synthetic_credentials);guard,command,worker,_,_=authority
    time.sleep(1.1)
    with pytest.raises(AuthorizationUnavailable):authorize(worker,result['activation_ref'],command)
    job=worker.claim_task(queue='operations')
    assert job['task_id']==guard.task_id and job['fence']==guard.fence+1
    with pytest.raises(AuthorizationUnavailable):authorize(worker,result['activation_ref'],command)
    next_activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],
        run_id=command['run_id'],command=command,input='synthetic activation input',owner_epoch=1)
    assert next_activation['activation_ref']!=result['activation_ref']
    assert authorize(worker,next_activation['activation_ref'],command)['authorized']


def test_api_database_role_cannot_gain_worker_activation_authority(authority,synthetic_credentials):
    result=activation(authority,synthetic_credentials);guard,command,_,issued,_=authority
    api=guard.backend.authenticate(synthetic_credentials['-queue-api'],world='real')
    with pytest.raises(AuthorizationUnavailable):authorize(api,result['activation_ref'],command)
    with pytest.raises(AuthorizationUnavailable):
        api.create_runtime_activation(queue='operations',task_id=guard.task_id,fence=guard.fence,
            run_id=issued.run_id,command=command,input='synthetic activation input',owner_epoch=1)


def test_actual_short_lived_run_expiry_denies_activation(authority,synthetic_credentials):
    guard,original,worker,_,_=authority
    issued=guard.backend.authenticate(synthetic_credentials['agent'],world='real').issue_run_credential(
        action_resources=['eios:action:Consumer.create:1'],ttl_seconds=1)
    command=copy.deepcopy(original);command.update(run_id=issued.run_id,
        request_id='synthetic-short-run-'+str(uuid.uuid4()),trigger_event_id=str(uuid.uuid4()),
        credential_ref='run_credential:'+issued.run_id,not_after=issued.expires_at.isoformat())
    api=guard.backend.authenticate(synthetic_credentials['-queue-api'],world='real')
    api.accept_event(queue='operations',source_id='synthetic',event_id='short-'+str(uuid.uuid4()),payload={'run_command':command})
    job=worker.claim_task(queue='operations')
    registration(authority,synthetic_credentials,command=command,issued=issued,task_id=job['task_id'])
    result=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],
        run_id=issued.run_id,command=command,input='synthetic activation input',owner_epoch=1)
    time.sleep(1.1)
    with pytest.raises(AuthorizationUnavailable):authorize(worker,result['activation_ref'],command)


def test_activation_records_never_persist_raw_run_secret(authority,synthetic_credentials,admin):
    activation(authority,synthetic_credentials);_,_,_,issued,_=authority
    tables=admin.execute("select table_schema,table_name from information_schema.tables where table_type='BASE TABLE' and table_schema in ('runtime','authz') order by table_schema,table_name").fetchall()
    # All owned technical ledgers and auth metadata are inspected in memory;
    # assertion reports do not contain serialized authority rows or credentials.
    from psycopg import sql
    found=False
    for schema,table in tables:
        rows=admin.execute(sql.SQL('select to_jsonb(t)::text from {}.{} t').format(sql.Identifier(schema),sql.Identifier(table))).fetchall()
        if 'activat' in table:found=found or bool(rows)
        assert all(issued.token not in row[0] for row in rows), 'raw Run credential persisted'
    assert found, 'activation ledger did not persist a real record'


def test_restricted_worker_cannot_read_raw_activation_ledger(authority,synthetic_credentials,admin):
    activation(authority,synthetic_credentials);guard,_,_,_,_=authority
    from psycopg import sql
    tables=admin.execute("select table_schema,table_name from information_schema.tables where table_type='BASE TABLE' and table_schema in ('runtime','authz') and table_name like '%activat%'").fetchall()
    assert tables
    with guard.worker._backend._pool.connection() as connection:
        for schema,table in tables:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction():connection.execute(sql.SQL('select * from {}.{}').format(sql.Identifier(schema),sql.Identifier(table)))


@pytest.mark.parametrize('operation',['start','resume'])
@pytest.mark.parametrize('input',['missing','different'])
def test_start_resume_cannot_omit_or_change_registered_prompt(authority,synthetic_credentials,operation,input):
    result=activation(authority,synthetic_credentials);_,command,worker,_,_=authority
    arguments={} if input=='missing' else {'input':'different prompt'}
    with pytest.raises(AuthorizationUnavailable):
        worker.authorize_runtime_activation(activation_ref=result['activation_ref'],command=command,
            operation=operation,**arguments)


def test_worker_cannot_register_run_even_with_known_credential(authority):
    guard,command,worker,issued,_=authority
    with pytest.raises(AuthorizationUnavailable):
        worker.register_runtime_run(queue='operations',task_id=guard.task_id,
            run_token=issued.token,command=command,input='synthetic activation input')


def test_api_cannot_bind_task_to_another_valid_run_secret(authority,synthetic_credentials):
    guard,command,_,_,_=authority
    other=guard.backend.authenticate(synthetic_credentials['agent'],world='real').issue_run_credential(
        action_resources=['eios:action:Consumer.create:1'])
    api=guard.backend.authenticate(synthetic_credentials['-queue-api'],world='real')
    with pytest.raises(AuthorizationUnavailable):
        api.register_runtime_run(queue='operations',task_id=guard.task_id,
            run_token=other.token,command=command,input='synthetic activation input')


@pytest.mark.parametrize('world',['real','test'])
def test_other_authenticated_tenant_cannot_use_activation_reference(authority,synthetic_credentials,admin,pg,tmp_path,world):
    result=activation(authority,synthetic_credentials);_,command,_,_,_=authority
    from authority_fixture import seed_authority
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    token,_=seed_authority(admin,str(uuid.uuid4()),'eios:action:NexLoop.queue.operations:1',
        world=world,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-other-worker')
    with open_backend(database_url=make_conninfo(pg,user='nexloop_domain_worker'),
        artifact_root=tmp_path/'other-worker',signing_key_file=tmp_path/'synthetic-authority',
        signing_key_id='synthetic-runtime') as backend:
        with pytest.raises(AuthorizationUnavailable):
            authorize(backend.authenticate(token,world=world),result['activation_ref'],command)


def test_null_action_proofs_cannot_authorize_activation_through_protected_sql(authority,synthetic_credentials):
    result=activation(authority,synthetic_credentials);_,command,worker,_,_=authority
    import hashlib
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.runtime_activation import RuntimeActivationPort
    backend=worker._backend;port=RuntimeActivationPort(backend._pool,worker._session,backend._signer)
    text=canonical_payload(command)
    # A valid queue proof is insufficient: the protected final SQL authority port
    # must reject a missing complete current EIOS Run Action proof vector.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        port._call('operations','authorize',activation_ref=result['activation_ref'],
            command_text=text,command_digest=hashlib.sha256(text.encode()).hexdigest(),
            input_digest=None,operation='model',run_proofs=None)

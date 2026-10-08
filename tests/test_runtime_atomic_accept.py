"""Actual PG atomic Inbox/Run enrollment and process-kill acknowledgement gates."""
from contextlib import contextmanager
import copy
import multiprocessing
import os
import signal
import time
import uuid

import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.backend import open_backend
from nexloop_eios.durable_queue import QueueConflict
from test_runtime_authority import authority
from test_runtime_activation import synthetic_credentials


TABLES=('runtime.invocations','runtime.jobs','runtime.nexloop_inbox','runtime.nexloop_outbox',
    'runtime.job_events','authz.nexloop_runtime_run_bindings')


def ledger(admin):
    from psycopg import sql
    return tuple(admin.execute(sql.SQL('select count(*) from {}.{}').format(
        *(sql.Identifier(part) for part in table.split('.')))).fetchone()[0] for table in TABLES)


@pytest.fixture
def accepted_input(authority,synthetic_credentials):
    guard,original,worker,_,_=authority
    issued=guard.backend.authenticate(synthetic_credentials['agent'],world='real').issue_run_credential(
        action_resources=['eios:action:Consumer.create:1'])
    command=copy.deepcopy(original);command.update(run_id=issued.run_id,
        request_id='synthetic-atomic-'+str(uuid.uuid4()),trigger_event_id=str(uuid.uuid4()),
        credential_ref='run_credential:'+issued.run_id,not_after=issued.expires_at.isoformat())
    api=guard.backend.authenticate(synthetic_credentials['-queue-api'],world='real')
    arguments={'queue':'operations','source_id':'synthetic-atomic','event_id':str(uuid.uuid4()),
        'run_token':issued.token,'command':command,'input':'synthetic atomic input'}
    # Repr redaction covers accidental pytest failure display; no token is logged.
    from test_runtime_activation import SyntheticCredentials
    yield api,worker,SyntheticCredentials(arguments),issued


def test_ack_has_atomic_enrollment_and_exact_replay_then_activation(accepted_input,admin):
    api,worker,args,issued=accepted_input
    accepted=api.accept_runtime_event(**args)
    assert accepted['accepted'] and accepted['registered'] and accepted['created']
    replay=api.accept_runtime_event(**args)
    assert replay['task_id']==accepted['task_id'] and replay['created'] is False
    job=worker.claim_task(queue='operations');assert job['task_id']==accepted['task_id']
    activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],
        run_id=args['command']['run_id'],command=args['command'],input=args['input'],owner_epoch=1)
    result=worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=args['command'],
        operation='start',input=args['input'])
    assert result['authorized'] and issued.token not in repr(result)
    rows=admin.execute('select to_jsonb(t)::text from authz.nexloop_runtime_run_bindings t').fetchall()
    assert all(issued.token not in row[0] for row in rows)
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_registration_failure_rolls_back_every_accepted_ledger(accepted_input,admin):
    api,_,args,_=accepted_input;before=ledger(admin)
    admin.execute("create function public.synthetic_atomic_failure() returns trigger language plpgsql as $$begin raise exception 'synthetic enrollment failure';end$$")
    admin.execute('grant execute on function public.synthetic_atomic_failure() to nexloop_owner')
    admin.execute('create trigger synthetic_atomic_failure before insert on authz.nexloop_runtime_run_bindings for each row execute function public.synthetic_atomic_failure()')
    with pytest.raises(AuthorizationUnavailable):api.accept_runtime_event(**args)
    assert ledger(admin)==before
    admin.execute('drop trigger synthetic_atomic_failure on authz.nexloop_runtime_run_bindings')
    assert api.accept_runtime_event(**args)['created']


@pytest.mark.parametrize('field',['input','command','max_attempts'])
def test_same_event_different_atomic_payload_cannot_replace_enrollment(accepted_input,admin,field):
    api,_,args,_=accepted_input;accepted=api.accept_runtime_event(**args);before=ledger(admin)
    changed=dict(args)
    if field=='command':changed['command']={**args['command'],'consumer_ref':'consumer:different'}
    elif field=='input':changed['input']='different prompt'
    else:changed['max_attempts']=4
    with pytest.raises(QueueConflict):api.accept_runtime_event(**changed)
    assert ledger(admin)==before
    assert api.accept_runtime_event(**args)['task_id']==accepted['task_id']


def test_revoked_source_refuses_atomic_ack(accepted_input,authority,admin):
    api,_,args,_=accepted_input;before=ledger(admin);invocation=authority[4]
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{status}','\"revoked\"') where fact_kind='agent_release' and entity_key=%s",([invocation.release_id],))
    with pytest.raises(AuthorizationUnavailable):api.accept_runtime_event(**args)
    assert ledger(admin)==before


def test_worker_role_cannot_admit_atomic_runtime_event(accepted_input,admin):
    _,worker,args,_=accepted_input;before=ledger(admin)
    with pytest.raises(AuthorizationUnavailable):worker.accept_runtime_event(**args)
    assert ledger(admin)==before


def _accept_process(config,token,args,pipe):
    try:
        with open_backend(**config) as backend:
            api=backend.authenticate(token,world='real');pipe.send('ready')
            result=api.accept_runtime_event(**args);pipe.send(result);pipe.recv()
    except Exception:
        pipe.send({'event':'acceptance_denied'})


@contextmanager
def accepting_process(pg,tmp_path,token,args):
    config=dict(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'child-api',
        signing_key_file=tmp_path/'synthetic-authority',signing_key_id='synthetic-runtime')
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_accept_process,args=(config,token,args,child))
    process.start();child.close()
    try:
        assert parent.poll(15) and parent.recv()=='ready';yield process,parent
    finally:
        if process.is_alive():process.kill()
        process.join(10);parent.close();assert not process.is_alive()


def test_sigkill_during_atomic_commit_has_no_ack_and_atomic_replay_recovers(accepted_input,synthetic_credentials,admin,pg,tmp_path):
    api,_,args,_=accepted_input;before=ledger(admin)
    # Deferred trigger pauses after both ledgers exist but before commit/ACK.
    admin.execute("create function public.synthetic_atomic_pause() returns trigger language plpgsql as $$begin perform pg_sleep(3);return new;end$$")
    admin.execute('grant execute on function public.synthetic_atomic_pause() to nexloop_owner')
    admin.execute('create constraint trigger synthetic_atomic_pause after insert on authz.nexloop_runtime_run_bindings deferrable initially deferred for each row execute function public.synthetic_atomic_pause()')
    with accepting_process(pg,tmp_path,synthetic_credentials['-queue-api'],args) as (process,pipe):
        deadline=time.monotonic()+10;paused=False
        while time.monotonic()<deadline:
            paused=admin.execute("select exists(select 1 from pg_stat_activity where usename='nexloop_api' and wait_event='PgSleep')").fetchone()[0]
            if paused:break
            time.sleep(.02)
        assert paused and not pipe.poll(), 'atomic accept acknowledged an uncommitted request'
        process.kill();process.join(10);assert process.exitcode==-signal.SIGKILL
    admin.execute('drop trigger synthetic_atomic_pause on authz.nexloop_runtime_run_bindings')
    after=ledger(admin)
    # COMMIT has reached PostgreSQL while its deferred trigger pauses. Killing
    # the client cannot undo a COMMIT already executing on the server. Either
    # no ledger or the complete atomic pair is legal; partial persistence is not.
    assert after in (before,tuple(count+1 for count in before))
    replay=api.accept_runtime_event(**args)
    assert replay['accepted'] and replay['registered']
    assert replay['created'] is (after==before)
    assert ledger(admin)==tuple(count+1 for count in before)


def test_sigkill_after_atomic_ack_preserves_task_and_enrollment(accepted_input,synthetic_credentials,admin,pg,tmp_path):
    api,_,args,_=accepted_input
    with accepting_process(pg,tmp_path,synthetic_credentials['-queue-api'],args) as (process,pipe):
        assert pipe.poll(15);accepted=pipe.recv();assert accepted['accepted'] and accepted['registered']
        process.kill();process.join(10);assert process.exitcode==-signal.SIGKILL
    replay=api.accept_runtime_event(**args)
    assert replay['task_id']==accepted['task_id'] and replay['created'] is False


def test_sigkill_before_commit_request_rolls_back_atomic_pair(accepted_input,synthetic_credentials,admin,pg,tmp_path):
    api,_,args,_=accepted_input;before=ledger(admin)
    # Unlike the deferred COMMIT-window test, this runs inside the registration
    # statement, before the Python transaction can send COMMIT to PostgreSQL.
    admin.execute("create function public.synthetic_precommit_pause() returns trigger language plpgsql as $$begin perform pg_sleep(3);return new;end$$")
    admin.execute('grant execute on function public.synthetic_precommit_pause() to nexloop_owner')
    admin.execute('create trigger synthetic_precommit_pause before insert on authz.nexloop_runtime_run_bindings for each row execute function public.synthetic_precommit_pause()')
    with accepting_process(pg,tmp_path,synthetic_credentials['-queue-api'],args) as (process,pipe):
        deadline=time.monotonic()+10;paused=False
        while time.monotonic()<deadline:
            paused=admin.execute("select exists(select 1 from pg_stat_activity where usename='nexloop_api' and wait_event='PgSleep')").fetchone()[0]
            if paused:break
            time.sleep(.02)
        assert paused and not pipe.poll()
        process.kill();process.join(10);assert process.exitcode==-signal.SIGKILL
    admin.execute('drop trigger synthetic_precommit_pause on authz.nexloop_runtime_run_bindings')
    assert ledger(admin)==before
    accepted=api.accept_runtime_event(**args)
    assert accepted['accepted'] and accepted['registered'] and accepted['created']


@pytest.mark.parametrize('payload_input',['different registered prompt',17])
def test_standalone_registration_cannot_rebind_persisted_queue_prompt(accepted_input,admin,payload_input):
    api,_,args,issued=accepted_input
    accepted=api.accept_event(queue='operations',source_id='synthetic-standalone',event_id=str(uuid.uuid4()),
        payload={'run_command':args['command'],'input':payload_input})
    before=admin.execute('select count(*) from authz.nexloop_runtime_run_bindings').fetchone()[0]
    with pytest.raises(AuthorizationUnavailable):
        api.register_runtime_run(queue='operations',task_id=accepted['task_id'],run_token=issued.token,
            command=args['command'],input=args['input'])
    assert admin.execute('select count(*) from authz.nexloop_runtime_run_bindings').fetchone()[0]==before


@pytest.mark.parametrize('role',['api','worker'])
def test_restricted_application_roles_cannot_execute_previous_inner_activation_function(accepted_input,role):
    api,worker,_,_=accepted_input
    services=api if role=='api' else worker
    with services._backend._pool.connection() as connection:
        with pytest.raises(__import__('psycopg').errors.InsufficientPrivilege):
            connection.execute('select authz.nexloop_runtime_activation_command_v0037(%s,%s,%s,%s,%s)',
                ('synthetic-invalid-digest','real','{}','synthetic-invalid-signature','{}'))

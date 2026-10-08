"""Real UUID Human Message→Source Run admission; candidate-only PG evidence."""
from concurrent.futures import ThreadPoolExecutor
import hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path

import psycopg
import pytest
from eios.authz.errors import AuthorizationUnavailable
from psycopg.conninfo import make_conninfo
from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge,ConversationRunReader,MessageRuntimeUnavailable,MessageRuntimeConflict
from nexloop_eios.browser_authorization import authenticate_browser_business
from message_runtime_fixture import message_plan


def bind(f):return f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=f['command'])


def queued_count(admin,f):
    return admin.execute("select count(*) from runtime.nexloop_inbox where tenant_id=%s and source_id='webchat'",(f['tenant'],)).fetchone()[0]


def test_actual_message_run_admission_and_current_owned_safe_read(message_plan,admin):
    f=message_plan;assert f['reader'].read_message_run(message_id=f['message']['id']) is None
    bound=bind(f);result=f['bridge'].deliver_one(run_token=f['run'].token)
    assert bound['run_id']==result['run_id']==f['run'].run_id and result['request_id']==f['command']['request_id']
    assert queued_count(admin,f)==1 and f['reader'].read_message_run(message_id=f['message']['id'])==result
    assert f['bridge'].deliver_one(run_token=f['run'].token) is None
    job=f['worker'].claim_task(queue='operations',lease_seconds=30)
    assert job['task_id']==result['task_id']
    activation=f['worker'].create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=f['run'].run_id,
        command=f['command'],input=f['message']['body'],owner_epoch=1)
    assert activation['run_id']==f['run'].run_id
    row=admin.execute('select event_envelope from runtime.nexloop_message_routes where message_id=%s',(f['message']['id'],)).fetchone()[0]
    assert row['event_id']==f['command']['trigger_event_id'] and row['source']=='webchat' and row['subject_ref']=='message:'+f['message']['id']
    assert row['payload']=={'message_ref':'message:'+f['message']['id'],'conversation_ref':'conversation:'+f['conversation']['id']}
    persisted=admin.execute('select row_to_json(r)::text from runtime.nexloop_message_routes r').fetchone()[0]
    persisted+=admin.execute('select row_to_json(j)::text from runtime.jobs j').fetchone()[0]
    assert f['run'].token not in persisted and f['issued'].session_token.get_secret_value() not in persisted


def test_exact_bind_replay_and_changed_command_conflict(message_plan,admin):
    f=message_plan;first=bind(f);assert bind(f)==first
    changed={**f['command'],'request_id':'different-request-'+f['command']['run_id']}
    with pytest.raises(MessageRuntimeConflict):f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=changed)
    assert queued_count(admin,f)==0
    assert admin.execute('select count(*) from runtime.nexloop_message_routes').fetchone()==(1,)


@pytest.mark.parametrize('field,value',[('consumer_ref','consumer:'+'0'*64),('goal_version_ref','goal:'+'0'*64+':revision:1:step:1:control:1'),('trigger_event_id','00000000-0000-0000-0000-000000000000')])
def test_server_run_refs_must_match_actual_source_consumer_and_plan(message_plan,admin,field,value):
    f=message_plan;command={**f['command'],field:value}
    with pytest.raises(MessageRuntimeUnavailable):f['bridge'].bind_message(message_id=f['message']['id'],run_token=f['run'].token,command=command)
    assert queued_count(admin,f)==0 and admin.execute('select count(*) from runtime.nexloop_message_routes').fetchone()==(0,)


def test_source_revoked_before_delivery_sends_zero_queue_events(message_plan,admin):
    f=message_plan;bind(f)
    admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(f['run'].run_id,))
    bridge=ConversationRuntimeBridge(f['api'].authenticate(f['owner_token'],world='real'))
    with pytest.raises(MessageRuntimeUnavailable):bridge.deliver_one(run_token=f['run'].token)
    assert queued_count(admin,f)==0
    assert admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('pending',)


def test_two_real_bridge_instances_claim_one_message(message_plan,admin):
    f=message_plan;bind(f)
    second=ConversationRuntimeBridge(f['api'].authenticate(f['owner_token'],world='real'))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda bridge:bridge.deliver_one(run_token=f['run'].token),[f['bridge'],second]))
    assert sum(result is not None for result in results)==1 and queued_count(admin,f)==1


def test_direct_role_tables_and_private_source_helper_denied(message_plan):
    with psycopg.connect(make_conninfo(message_plan['pg'],user='nexloop_api')) as db:
        for statement in ('select * from runtime.nexloop_message_routes',
            "select authz.nexloop_assert_message_source('x','real','x','x','{}','[]')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute(statement)


_CHILD='''
import json,sys
from pathlib import Path
from nexloop_eios.backend import open_backend
from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge
c=json.loads(Path(sys.argv[1]).read_text())
try:
 with open_backend(database_url=c['dsn'],artifact_root=c['artifact_root'],signing_key_file=c['signing_key_file'],signing_key_id='runtime-effect') as backend:
  bridge=ConversationRuntimeBridge(backend.authenticate(c['service_token'],world='real'))
  bridge.deliver_one(lease_seconds=3,run_token=c['run_token'])
 print('completed',flush=True)
except Exception:
 print('unavailable',flush=True)
 sys.exit(1)
'''


def test_sigkill_after_actual_queue_commit_replays_ack_without_new_execution(message_plan,admin,tmp_path):
    f=message_plan;bind(f)
    # Disposable PG fault trigger delays only the second, technical ACK tx.
    admin.execute("create function public.synthetic_message_ack_pause() returns trigger language plpgsql as $$begin perform pg_sleep(20);return new;end$$")
    admin.execute('grant execute on function public.synthetic_message_ack_pause() to nexloop_owner')
    admin.execute("create trigger synthetic_message_ack_pause after update on runtime.nexloop_message_outbox for each row when(new.status='delivered') execute function public.synthetic_message_ack_pause()")
    cfg=tmp_path/'bridge-private.json';cfg.write_text(json.dumps({'dsn':make_conninfo(f['pg'],user='nexloop_api'),
        'artifact_root':str(tmp_path/'child-artifacts'),'signing_key_file':str(f['signing_key']),
        'service_token':f['owner_token'],'run_token':f['run'].token}));cfg.chmod(0o600)
    child=subprocess.Popen([sys.executable,'-c',_CHILD,str(cfg)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+15
        while queued_count(admin,f)==0 and time.monotonic()<deadline:
            if child.poll() is not None:
                out,err=child.communicate(timeout=5)
                for value in (f['run'].token,f['owner_token'],make_conninfo(f['pg'],user='nexloop_api')):assert value not in out+err
                raise AssertionError('bridge exited before durable queue acceptance: '+out+err)
            time.sleep(.02)
        assert queued_count(admin,f)==1
        child.send_signal(signal.SIGKILL);out,err=child.communicate(timeout=5)
        assert child.returncode==-signal.SIGKILL
        for value in (f['run'].token,f['owner_token'],make_conninfo(f['pg'],user='nexloop_api')):assert value not in out+err
        # Killing the client closes its socket; terminate only this designated
        # disposable ACK backend, which may still be inside pg_sleep.
        for (pid,) in admin.execute("select pid from pg_stat_activity where usename='nexloop_api' and query like 'select authz.nexloop_message_runtime_command%' and state='active'").fetchall():
            admin.execute('select pg_cancel_backend(%s)',(pid,))
        admin.execute('drop trigger synthetic_message_ack_pause on runtime.nexloop_message_outbox')
        assert admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('pending',)
        old=admin.execute('select fence from runtime.nexloop_message_routes').fetchone()[0]
        # Previously accepted Run need not be re-authorized for technical ACK.
        # Revocation cannot authorize a fresh event or runtime activation.
        admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(f['run'].run_id,))
        deadline=time.monotonic()+5
        while admin.execute('select lease_until>clock_timestamp() from runtime.nexloop_message_routes').fetchone()[0] and time.monotonic()<deadline:time.sleep(.05)
        bridge=ConversationRuntimeBridge(f['api'].authenticate(f['owner_token'],world='real'))
        result=bridge.deliver_one(lease_seconds=3)
        assert result['run_id']==f['run'].run_id and queued_count(admin,f)==1
        assert admin.execute('select fence from runtime.nexloop_message_routes').fetchone()[0]>old
        with pytest.raises(psycopg.errors.InsufficientPrivilege):bridge._call('ack',message_id=f['message']['id'],fence=old,allow_missing=False)
        assert admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('delivered',)
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=5)


def test_live_human_read_revoke_hides_committed_run_link(message_plan,admin):
    f=message_plan;bind(f);assert f['bridge'].deliver_one(run_token=f['run'].token)
    payload=admin.execute('select payload from control.nexloop_browser_subjects where subject_id=%s',(f['issued'].session.subject_id,)).fetchone()[0]
    from psycopg.types.json import Jsonb
    admin.execute('update control.nexloop_browser_subjects set payload=%s where subject_id=%s',(Jsonb({**payload,'status':'disabled','revision':2}),f['issued'].session.subject_id))
    with pytest.raises(MessageRuntimeUnavailable):f['reader'].read_message_run(message_id=f['message']['id'])


def test_route_service_revoke_cannot_ack_existing_queue_commit(message_plan,admin):
    f=message_plan;bind(f);item=f['bridge']._call('claim',lease_seconds=30)
    f['owner'].accept_runtime_event(queue='operations',source_id='webchat',event_id=item['event_id'],run_token=f['run'].token,
        command=f['command'],input=f['message']['body'])
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(hashlib.sha256(f['owner_token'].encode()).hexdigest(),))
    with pytest.raises(AuthorizationUnavailable):f['bridge']._call('ack',message_id=f['message']['id'],fence=item['fence'],allow_missing=False)
    assert queued_count(admin,f)==1 and admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('pending',)


def test_closed_backend_bridge_cannot_claim_or_enqueue(message_plan,admin):
    f=message_plan;bind(f);f['api']._shutdown()
    with pytest.raises(MessageRuntimeUnavailable):f['bridge'].deliver_one(run_token=f['run'].token)
    assert queued_count(admin,f)==0 and admin.execute('select fence from runtime.nexloop_message_routes').fetchone()==(0,)

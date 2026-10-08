"""Actual clean-PG / independent CLI candidate tests, pending 0049 registration.

Technical fixture configuration only; Goal, Step and Assignment writes below
are performed exclusively by production CLI through real governed Actions.
"""
from datetime import UTC,datetime,timedelta
import json,os,subprocess,sys
from pathlib import Path
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.ontology.definitions import ActionDefinition
from eios.ontology.version_resolution import CapabilityContractSnapshot
from nexloop_eios.message_assignment_contract import message_assignment_action
from nexloop_eios.message_relay import message_assignment_schema
from nexloop_eios.run_credential_vault import RunCredentialVault
from runtime_effect_fixture import seed_multi_uuid,PrivatePlan,QUEUE
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from message_runtime_fixture import message_plan
from test_conversation_effect_receipts import receipt_plan


@pytest.fixture
def relay_plan(receipt_plan,admin,tmp_path):
    f=receipt_plan
    definition_json,capability_json=admin.execute('select definition,capability from control.nexloop_action_definitions where tenant_id=%s and resource_id=%s',
        (f['tenant'],'eios:action:Consumer.create:1')).fetchone()
    capability=CapabilityContractSnapshot.model_validate_json(json.dumps(capability_json))
    schema=message_assignment_schema()
    definition,capability=message_assignment_action(tenant=f['tenant'],created_by='synthetic-configurator',created_at=datetime.now(UTC),capability=capability)
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)',(f['tenant'],schema.type_name,Jsonb(schema.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
        (f['tenant'],'real','eios:action:MessageAssignment.create:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    targets=[('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in ('Goal.create','PlanStep.create','MessageAssignment.create','nexloop.plan.bind_effect_context')]
    planner_token=seed_multi_uuid(admin,f['tenant'],targets,suffix='-relay-planner')
    # All service objects below are newly authenticated after canonical config.
    recipe={'consumer_id':f['consumer'],'control_id':f['control'],'control_revision':1,'consumer_revision':1,
        'valid_until':(datetime.now(UTC)+timedelta(seconds=150)).isoformat(),'role_ref':'role:source',
        'context_manifest_ref':'artifact:message-relay','runtime_profile':'deterministic-test',
        'budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},
        'runtime_owner_epoch':1,'queue':'operations'}
    private=tmp_path/'relay-private';private.mkdir(mode=0o700)
    vault=private/'vault';vault.mkdir(mode=0o700)
    material={'database-url-file':make_conninfo(f['pg'],user='nexloop_api'),'route-credential-file':f['owner_token'],
        'source-credential-file':f['source_token'],'planner-credential-file':planner_token,
        'executor-credential-file':f['executor_token'],'recipe-file':json.dumps(recipe)}
    argv=[]
    for name,value in material.items():
        path=private/name;path.write_text(value);path.chmod(0o600);argv+=['--'+name,str(path)]
    argv+=['--signing-key-file',str(f['signing_key']),'--signing-key-id','runtime-effect',
        '--artifact-root',str(private/'relay-artifacts'),'--vault-root',str(vault),'--once']
    yield PrivatePlan(f=f,argv=argv,vault=vault,recipe=recipe,private=private,planner_token=planner_token)


def invoke(f):
    return subprocess.run([sys.executable,'-m','nexloop_eios.message_relay_cli',*f['argv']],capture_output=True,text=True,timeout=30)


def test_independent_cli_governed_assignment_and_persist_before_ack(relay_plan,admin):
    f=relay_plan;result=invoke(f)
    assert result.returncode==0 and result.stderr==''
    assert result.stdout=='Message relay ready\n{"status":"queued"}\n'
    item=admin.execute('select run_id,request_id,expires_at from authz.nexloop_message_run_issuances where message_id=%s',(f['f']['message']['id'],)).fetchone()
    assert item is not None
    with RunCredentialVault(f['vault']) as vault:
        record=vault.read(vault.message_key(f['f']['tenant'],'real',f['f']['message']['id']))
    assert str(item[0])==record.run_id and item[1]==record.request_id and item[2].astimezone(UTC)==datetime.fromisoformat(record.expires_at)
    assignments=admin.execute("select object_id,properties from ontology.objects where tenant_id=%s and type_name='MessageAssignment'",(f['f']['tenant'],)).fetchall()
    assert len(assignments)==1 and assignments[0][1]['message_id']==f['f']['message']['id']
    assert admin.execute('select count(*) from runtime.jobs where tenant_id=%s',(f['f']['tenant'],)).fetchone()==(1,)
    assert admin.execute('select status from runtime.nexloop_message_outbox where message_id=%s',(f['f']['message']['id'],)).fetchone()==('delivered',)
    assert record.token not in result.stdout+result.stderr
    persisted=admin.execute('select row_to_json(r)::text from authz.nexloop_message_run_issuances r').fetchone()[0]
    persisted+=admin.execute('select row_to_json(j)::text from runtime.jobs j').fetchone()[0]
    assert record.token not in persisted
    again=invoke(f);assert again.returncode==0 and '"status":"idle"' in again.stdout
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='MessageAssignment'",(f['f']['tenant'],)).fetchone()==(1,)


def wait_until(check,timeout=10):
    import time
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if check():return
        time.sleep(.01)
    raise AssertionError('synthetic fault window not reached')


def start(f):
    return subprocess.Popen([sys.executable,'-m','nexloop_eios.message_relay_cli',*f['argv']],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)


def expire_owned_leases(admin,f):
    # Technical fault-state manipulation only inside owned disposable PG.
    admin.execute("update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp()-interval '1 second'")
    admin.execute("update runtime.nexloop_message_routes set lease_until=clock_timestamp()-interval '1 second'")


@pytest.mark.parametrize('window',['prepared_before_pg_commit','pg_commit_before_vault_issued'])
def test_real_process_kill_issuance_window_same_run(relay_plan,admin,window):
    import fcntl,psycopg,signal
    f=relay_plan;barrier=938190
    locker=psycopg.connect(make_conninfo(f['f']['pg']),autocommit=True)
    locker.execute('select pg_advisory_lock(%s)',(barrier,))
    admin.execute('''create function public.synthetic_issue_barrier() returns trigger language plpgsql as $$
    begin perform pg_advisory_lock(938190); perform pg_advisory_unlock(938190);return new;end $$''')
    admin.execute('create trigger synthetic_issue_barrier before insert on authz.nexloop_run_credentials for each row execute function public.synthetic_issue_barrier()')
    process=start(f);lockfd=None
    try:
        wait_until(lambda:bool(list(f['vault'].glob('*.json'))))
        with RunCredentialVault(f['vault']) as vault:
            key=vault.message_key(f['f']['tenant'],'real',f['f']['message']['id']);prepared=vault.read(key)
        if window=='pg_commit_before_vault_issued':
            lockfd=os.open(f['vault']/(key+'.lock'),os.O_RDWR);fcntl.flock(lockfd,fcntl.LOCK_EX)
            locker.execute('select pg_advisory_unlock(%s)',(barrier,))
            wait_until(lambda:admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()[0]==1)
        process.send_signal(signal.SIGKILL);stdout,stderr=process.communicate(timeout=5)
        assert process.returncode==-signal.SIGKILL
        assert prepared.token not in stdout+stderr
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)
        if lockfd is not None:os.close(lockfd)
        locker.close()
        admin.execute('drop trigger synthetic_issue_barrier on authz.nexloop_run_credentials')
        admin.execute('drop function public.synthetic_issue_barrier()')
    expire_owned_leases(admin,f)
    result=invoke(f);assert result.returncode==0 and '"status":"queued"' in result.stdout
    actual=admin.execute('select run_id,request_id from authz.nexloop_message_run_issuances').fetchall()
    assert actual==[( __import__('uuid').UUID(prepared.run_id),prepared.request_id)]
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(1,)
    with RunCredentialVault(f['vault']) as vault:assert vault.read(key).token==prepared.token


@pytest.mark.parametrize('revocation',['run_status','source_grant'])
def test_real_queue_commit_ack_window_recovery_without_source_execution(relay_plan,admin,revocation):
    import psycopg,signal
    f=relay_plan;barrier=938191
    locker=psycopg.connect(make_conninfo(f['f']['pg']),autocommit=True);locker.execute('select pg_advisory_lock(%s)',(barrier,))
    admin.execute('''create function public.synthetic_ack_barrier() returns trigger language plpgsql as $$
    begin if new.task_id is not null then perform pg_advisory_lock(938191);perform pg_advisory_unlock(938191);end if;return new;end $$''')
    admin.execute('create trigger synthetic_ack_barrier before update on runtime.nexloop_message_routes for each row execute function public.synthetic_ack_barrier()')
    process=start(f)
    try:
        wait_until(lambda:admin.execute('select count(*) from runtime.jobs').fetchone()[0]==1)
        issued=admin.execute('select run_id,request_id from authz.nexloop_message_run_issuances').fetchone()
        process.send_signal(signal.SIGKILL);stdout,stderr=process.communicate(timeout=5)
        assert process.returncode==-signal.SIGKILL
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)
        locker.close();admin.execute('drop trigger synthetic_ack_barrier on runtime.nexloop_message_routes');admin.execute('drop function public.synthetic_ack_barrier()')
    expire_owned_leases(admin,f)
    # Revocation affects real Run execution. Technical ACK must not reissue it.
    if revocation=='run_status':
        admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(issued[0],))
    else:
        principal=f['f']['api'].authenticate(f['f']['source_token'],world='real')._session.authentication.subject_principal_id
        replace_fact(admin,f['f']['tenant'],'grants',[principal,'eios:action:nexloop.service.request:1'],F.GrantFacts,grants=[])
    for path in f['vault'].glob('*.json'):path.unlink()  # Owned private test artifact only.
    result=invoke(f)
    assert result.returncode==0 and '"status":"queued"' in result.stdout
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(1,)
    assert admin.execute('select run_id,request_id from authz.nexloop_message_run_issuances').fetchone()==issued
    assert admin.execute('select status from runtime.nexloop_message_outbox').fetchone()==('delivered',)
    assert not list(f['vault'].glob('*.json'))


# Existing trusted launchers and actual provider/PG ports, not substitute authority.
import secrets,sqlite3
from nexloop_eios.runtime_dispatch import RuntimeDispatcher
from nexloop_eios.host_control import HostControlConfiguration
from nexloop_eios.conversation_messages import ConversationDenied
from test_message_runtime_effect_e2e import (files,fresh_worker,guard_server,actual_host,effect_configuration,
    tool_evidence,ExecutorConfiguration,settle,await_available)
from support.effect_provider import effect_provider
from authority_fixture import replace_fact
from eios.authz import facts as F
from test_conversation_effect_receipts import FUNCTION

def test_cli_same_message_actual_pi_effect_and_human_receipt(relay_plan,admin,tmp_path):
    candidate=relay_plan;f=candidate['f']
    admitted=invoke(candidate)
    assert admitted.returncode==0 and '"status":"queued"' in admitted.stdout
    command=admin.execute('select normalized_input from runtime.jobs').fetchone()[0]['run_command']
    f['command']=command
    with RunCredentialVault(candidate['vault']) as vault:
        f['run']=vault.read(vault.message_key(f['tenant'],'real',f['message']['id']))
    queued=admin.execute('select task_id from runtime.nexloop_message_routes').fetchone()[0]
    queued={'task_id':queued}
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    assert f['receipt_query']()['run']['run_id']==command['run_id']
    with fresh_worker(f,tmp_path) as worker:
        projected=f['receipt_query']()
        assert projected['run']['run_id']==command['run_id'] and projected['run']['task_id']==queued['task_id'] and projected['receipt'] is None
        with guard_server(worker,tmp_path,guard_key) as port:
            with actual_host(runtime,key,effect_configuration(tmp_path,port,guard_key)) as (_,client,headers):
                host_control=HostControlConfiguration(str(client.base_url).rstrip('/'),key,tmp_path/'host-cert.pem')
                dispatched=RuntimeDispatcher(worker,host_control,queue='operations',total_timeout=15).run_once()
                assert dispatched['claimed'] is True and dispatched['task_id']==queued['task_id'] and dispatched['status']=='succeeded'
                result=worker.inspect_task(queue='operations',task_id=queued['task_id'])['result']
                assert result['scope']=='runtime_only' and result['runtime_outcome']=='succeeded' and result['business_action_success'] is False
                assert result['run_id']==command['run_id']
                persistence=result['runtime_receipt']
                assert persistence['request_id']==command['request_id'] and persistence['persistence']=={'journal_mode':'wal','synchronous':2}
                with sqlite3.connect(runtime/command['run_id']/'runtime.sqlite') as database:
                    assert database.execute('select count(*) from submissions').fetchone()==(1,)
                    assert database.execute('select id from submissions').fetchone()[0]==persistence['submission_id']
                    assert database.execute('select count(*) from conversations where id=?',(persistence['conversation_id'],)).fetchone()==(1,)
                calls,receipts=tool_evidence(runtime/command['run_id']/'runtime.sqlite')
                requests=[call for call in calls if call['name']=='nexloop.service.request']
                assert [call['id'] for call in requests]==['service-request-first','service-request-rebuilt']
                assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
                intent=receipts[0]['intent_id'];receipt_id=receipts[0]['receipt_id']
                before=f['receipt_query']()['receipt']
                assert before['intent_id']==intent and before['receipt_id']==receipt_id and before['state']=='accepted'
                assert before['provider_state'] is None and before['governed_claim_finalized'] is False and before['business_action_success'] is False
                with effect_provider(tmp_path/'durable-message-provider.sqlite') as provider:
                    executor=ExecutorConfiguration(f,tmp_path)
                    accepted=settle(executor,provider)
                    assert accepted['status']=='dispatching' and accepted['provider_state']=='accepted' and accepted['business_action_success'] is False
                    observed=f['receipt_query']()['receipt']
                    assert observed['state']=='dispatching' and observed['provider_state']=='accepted'
                    assert observed['business_action_success'] is False and observed['governed_claim_finalized'] is False
                    # Fulfillment is a real independent provider ledger change;
                    # only actual subsequent HTTP GET supplies PG observation.
                    provider.control('fulfill',intent_id=intent);await_available(admin,intent)
                    terminal=settle(executor,provider)
                    assert terminal['status']=='fulfilled' and terminal['provider_state']=='fulfilled' and terminal['business_action_success'] is True
                    final=f['receipt_query']()['receipt']
                    assert final['intent_id']==intent and final['receipt_id']==receipt_id and final['state']=='fulfilled'
                    assert final['provider_state']=='fulfilled' and final['governed_claim_finalized'] is True and final['business_action_success'] is True
                    snapshot=provider.control('snapshot')
                    assert snapshot['effects']==1 and snapshot['requests']==[('POST',intent,202),('GET',intent,200)]
                    assert snapshot['persistence']=={'journal_mode':'wal','synchronous':2}
                    principal=f['issued'].session.principal_id
                    replace_fact(admin,f['tenant'],'grants',[principal,'eios:function:'+FUNCTION+':1'],F.GrantFacts,grants=[])
                    with pytest.raises(ConversationDenied):f['receipt_query']()
                    assert provider.control('snapshot')['requests']==snapshot['requests']
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions').fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(1,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,1)
    assert admin.execute("select claim->>'state',claim->'terminal_outcome'->>'status' from runtime.nexloop_action_claims where action_name='nexloop.service.request'").fetchone()==('terminal','succeeded')
    assert admin.execute('select task_id,run_id::text from runtime.nexloop_message_routes').fetchone()==(queued['task_id'],command['run_id'])
    for path in runtime.rglob('*'):
        if path.is_file():
            contents=path.read_bytes()
            for secret in (f['run'].token,f['executor_token'],f['issued'].session_token.get_secret_value(),guard_key.read_text()):assert secret.encode() not in contents


def prepare_actual_issue(f):
    import hashlib
    from nexloop_eios.message_relay import MessageRelay
    from nexloop_eios.postgres_artifacts import canonical_payload
    api=f['f']['api']
    route=api.authenticate(f['f']['owner_token'],world='real')
    source=api.authenticate(f['f']['source_token'],world='real')
    planner=api.authenticate(f['planner_token'],world='real')
    vault=RunCredentialVault(f['vault'])
    relay=MessageRelay(route=route,source=source,planner=planner,executor_token=f['f']['executor_token'],vault=vault,recipe=f['recipe'])
    item=relay.port.call('claim',consumer_id=f['recipe']['consumer_id'],lease_seconds=30)
    message=item['message_id'];principal=source._session.authentication.subject_principal_id
    executor=api.authenticate(f['f']['executor_token'],world='real')._session.authentication.subject_principal_id
    goal=relay._create('Goal',message,{'consumer_id':f['recipe']['consumer_id'],'state':'active','valid_until':f['recipe']['valid_until']})
    step=relay._create('PlanStep',message,{'consumer_id':f['recipe']['consumer_id'],'goal_id':goal,'control_id':f['recipe']['control_id'],
        'submitter_principals':[principal],'action_name':'nexloop.service.request','state':'ready'})
    properties={'message_id':message,'consumer_id':f['recipe']['consumer_id'],'goal_id':goal,'goal_revision':1,'step_id':step,'step_revision':1,
        'control_id':f['recipe']['control_id'],'control_revision':1,'consumer_revision':1,'source_principal':principal,'executor_principal':executor,
        'recipe_digest':hashlib.sha256(canonical_payload(f['recipe']).encode()).hexdigest(),'allowed_actions':['eios:action:nexloop.service.request:1'],
        'state':'active','valid_until':f['recipe']['valid_until']}
    assignment=relay._create('MessageAssignment',message,properties)
    record=vault.prepare(message_key=vault.message_key(item['tenant_id'],'real',message),assignment_digest=hashlib.sha256(canonical_payload(properties).encode()).hexdigest())
    digest,proofs,definition,capability=relay.port.source_proofs(source)
    parameters={'message_id':message,'fence':item['fence'],'assignment_id':assignment,'assignment_revision':1,'assignment_digest':record.assignment_digest,
        'assignment_text':canonical_payload(properties),'run_id':record.run_id,'request_id':record.request_id,'issuance_nonce':record.issuance_nonce,
        'token_digest':record.token_digest,'source_digest':digest,'source_proofs':proofs,'source_definition':definition,'source_capability':capability,'ttl_seconds':300}
    vault.close()
    return relay.port,parameters,record


@pytest.mark.parametrize('field,value',[('assignment_revision',None),('run_id',None),('issuance_nonce',None),
    ('source_proofs',None),('ttl_seconds',None),('token_digest',None),('request_id','wrong-message-request')])
def test_signed_sql_incomplete_issue_rejects_zero_credentials(relay_plan,admin,field,value):
    from nexloop_eios.message_relay import MessageRelayUnavailable
    port,parameters,record=prepare_actual_issue(relay_plan)
    before=admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]
    with pytest.raises(MessageRelayUnavailable):port.call('issue',**{**parameters,field:value})
    assert admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]==before
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()==(0,)


def test_exact_issue_replay_and_conflict_keeps_first_expiry(relay_plan,admin):
    from nexloop_eios.message_relay import MessageRelayUnavailable
    port,parameters,record=prepare_actual_issue(relay_plan)
    original=port.call('issue',**parameters)
    assert port.call('issue',**{**parameters,'ttl_seconds':1})==original
    with pytest.raises(MessageRelayUnavailable):port.call('issue',**{**parameters,'token_digest':'a'*64})
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()==(1,)
    assert admin.execute('select expires_at from authz.nexloop_message_run_issuances').fetchone()[0].astimezone(UTC).isoformat()==original['expires_at']


def test_cli_source_revocation_zero_new_issuance_or_queue(relay_plan,admin):
    import hashlib
    digest=hashlib.sha256(relay_plan['f']['source_token'].encode()).hexdigest()
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(digest,))
    result=invoke(relay_plan)
    assert result.returncode==1 and result.stderr=='Message relay unavailable\n'
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)


def test_actual_one_second_issued_run_expiry_does_not_extend(relay_plan,admin):
    import time
    port,parameters,record=prepare_actual_issue(relay_plan)
    first=port.call('issue',**{**parameters,'ttl_seconds':1})
    time.sleep(1.1)
    expired=port.call('issue',**parameters)
    assert expired['state']=='requires_governed_replan' and expired['run_id']==first['run_id'] and expired['expires_at']==first['expires_at']
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)


def test_other_actual_source_cannot_borrow_assignment(relay_plan,admin):
    from nexloop_eios.message_relay import MessageRelayPort,MessageRelayUnavailable
    f=relay_plan['f']
    token=seed_multi_uuid(admin,f['tenant'],[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE)],suffix='-different-real-relay-source')
    port,parameters,record=prepare_actual_issue(relay_plan)
    different=f['api'].authenticate(token,world='real')
    digest,proofs,definition,capability=port.source_proofs(different)
    with pytest.raises(MessageRelayUnavailable):port.call('issue',**{**parameters,'source_digest':digest,'source_proofs':proofs,'source_definition':definition,'source_capability':capability})
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()==(0,)


def test_cross_consumer_actual_object_not_selected_from_messages(relay_plan,admin):
    f=relay_plan['f'];owner=f['api'].authenticate(f['owner_token'],world='real')
    other=owner.create_object(action_name='Consumer.create',action_version=1,intent_id='separate-actual-consumer-for-relay',type_name='Consumer',properties={})['object_id']
    config={**relay_plan['recipe'],'consumer_id':other}
    path=relay_plan['private']/'recipe-file';path.write_text(json.dumps(config));path.chmod(0o600)
    result=invoke(relay_plan)
    assert result.returncode==0 and '"status":"idle"' in result.stdout
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)


def test_real_bind_before_accept_sigkill_replays_same_run(relay_plan,admin):
    import psycopg,signal
    f=relay_plan;barrier=938192
    locker=psycopg.connect(make_conninfo(f['f']['pg']),autocommit=True);locker.execute('select pg_advisory_lock(%s)',(barrier,))
    admin.execute('''create function public.synthetic_accept_barrier() returns trigger language plpgsql as $$
    begin perform pg_advisory_lock(938192);perform pg_advisory_unlock(938192);return new;end $$''')
    admin.execute('create trigger synthetic_accept_barrier before insert on runtime.nexloop_inbox for each row execute function public.synthetic_accept_barrier()')
    process=start(f)
    try:
        wait_until(lambda:admin.execute('select count(*) from runtime.nexloop_message_routes').fetchone()[0]==1)
        route=admin.execute('select run_id,command from runtime.nexloop_message_routes').fetchone()
        wait_until(lambda:bool(list(f['vault'].glob('*.json'))))
        process.send_signal(signal.SIGKILL);stdout,stderr=process.communicate(timeout=5)
        assert process.returncode==-signal.SIGKILL
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)
        locker.close();admin.execute('drop trigger synthetic_accept_barrier on runtime.nexloop_inbox');admin.execute('drop function public.synthetic_accept_barrier()')
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)
    expire_owned_leases(admin,f)
    result=invoke(f);assert result.returncode==0 and '"status":"queued"' in result.stdout
    assert admin.execute('select run_id,command from runtime.nexloop_message_routes').fetchone()==route
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(1,)
    assert admin.execute('select count(*) from authz.nexloop_message_run_issuances').fetchone()==(1,)


def test_real_47_and_49_concurrent_route_claims_have_one_live_owner(relay_plan,admin):
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from nexloop_eios.conversation_runtime_bridge import ConversationRuntimeBridge,canonical_event_id
    from nexloop_eios.message_relay import MessageRelayUnavailable
    from nexloop_eios.postgres_artifacts import canonical_payload
    f=relay_plan;port,parameters,record=prepare_actual_issue(f)
    issued=port.call('issue',**parameters)
    assignment=json.loads(parameters['assignment_text'])
    api=f['f']['api'];planner=api.authenticate(f['planner_token'],world='real')
    planner.bind_effect_context(step_id=assignment['step_id'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
        run_id=record.run_id,run_token=record.token,executor_token=f['f']['executor_token'])
    command={**f['f']['command'],'run_id':record.run_id,'request_id':record.request_id,'credential_ref':'run:'+record.run_id,
        'not_after':issued['expires_at'],'goal_version_ref':'goal:'+assignment['goal_id']+':revision:1:step:1:control:1'}
    bridge=ConversationRuntimeBridge(api.authenticate(f['f']['owner_token'],world='real'))
    bridge.bind_message(message_id=f['f']['message']['id'],run_token=record.token,command=command)
    expire_owned_leases(admin,f)
    def old_claim():return bridge._call('claim',lease_seconds=30)
    def new_claim():
        try:return port.call('claim',consumer_id=f['recipe']['consumer_id'],lease_seconds=30)
        except MessageRelayUnavailable:return None  # Other actual live owner denies, never success.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(old_claim),executor.submit(new_claim)]
        results=[future.result(timeout=10) for future in futures]
    assert sum(result is not None for result in results)==1
    lease=admin.execute('select fence,lease_until>clock_timestamp() from runtime.nexloop_message_routes').fetchone()
    assert lease[0]==1 and lease[1] is True
    assert admin.execute('select count(*) from runtime.jobs').fetchone()==(0,)

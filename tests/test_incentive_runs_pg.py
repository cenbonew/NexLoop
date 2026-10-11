"""AT-043 (0160): the Agent issues a catalog incentive inside its Run, through the runtime worker facade.

A real governed Run (issued, bound, accepted, claimed and activated, as the NX-024 run-outcome tests) submits through
nexloop.incentive.run_submit:1: SQL re-checks the live activation, the lease and the exact command, requires the Run's
resources to name nexloop.incentive.issue:1, derives the Consumer and the origin. D7: a plan reevaluation Run does not
issue to a restricted customer; a Run answering the customer's inbound message may. Synthetic data; admin seeds/probes."""
from datetime import UTC,datetime,timedelta
import hashlib,secrets,uuid

import pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import runtime_effect_fixture as fixture
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_reevaluation_pg import planning,services,spec  # noqa: F401
from test_plan_outcome_pg import INPUT
from nexloop_eios.incentives import IncentiveIssuer,LocalIncentiveLedger,RunIncentivePort

ISSUE='eios:action:nexloop.incentive.issue:1'


def run_with(plan,worker,label,resources):
    source=plan['api'].authenticate(plan['source_tokens'][0],world='real')
    run=source.issue_run_credential(action_resources=resources)
    planner=plan['api'].authenticate(plan['planner_token'],world='real')
    owner=plan['api'].authenticate(plan['owner_token'],world='real')
    planner.bind_effect_context(step_id=plan['step'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
    command={'schema_version':'1.0','run_id':run.run_id,'tenant_id':plan['tenant'],'world_id':'real','mode':'real','request_id':'at043-'+str(uuid.uuid4()),'trigger_event_id':str(uuid.uuid4()),
        'role_ref':'role:plan-reevaluator','consumer_ref':'consumer:'+plan['consumer'],'goal_version_ref':'goal:'+plan['goal']+':revision:1:step:1:control:1',
        'context_manifest_ref':'artifact:synthetic-effect','runtime_profile':'deterministic-test','credential_ref':'run_credential:'+run.run_id,
        'budget':{'maximum_model_turns':8,'maximum_tool_calls':12,'active_timeout_seconds':300,'maximum_cost':'1.0','currency':'USD'},
        'not_after':run.expires_at.isoformat(),'runtime_owner_epoch':1}
    accepted=owner.accept_runtime_event(queue='operations',source_id='at043',event_id='at043-'+label,run_token=run.token,command=command,input=INPUT)
    job=worker.claim_task(queue='operations',lease_seconds=60)
    assert job['task_id']==accepted['task_id']
    ref=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=run.run_id,command=command,input=INPUT,owner_epoch=1)['activation_ref']
    assert worker.authorize_runtime_activation(activation_ref=ref,command=command,operation='start',input=INPUT)['authorized'] is True
    return run,command,ref


def seed_catalog(admin,tenant,consumer):
    offering,binding=secrets.token_hex(32),secrets.token_hex(32)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','IncentiveOffering',%s,1,%s,'nexloop-action','synthetic',now(),now())''',(tenant,offering,Jsonb({'incentive_code':'renewal-credit','kind':'fixed_amount',
            'max_amount_minor':'500','per_consumer_period_limit_minor':'2000','currency':'CNY','eligibility':'bound_consumer','title':'续费优惠','active':True,
            'valid_until':(datetime.now(UTC)+timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')})))
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','ConsumerIncentiveOffering',%s,1,%s,'nexloop-action','synthetic',now(),now())''',(tenant,binding,Jsonb({'consumer_id':consumer,
            'offering_id':offering,'offering_revision':1,'source_principal':'synthetic-owner-principal','active':True})))
        admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
        now=datetime.now(UTC)
        admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
            values(%s,'real','incentive','CNY','10000',%s,%s,1) on conflict do nothing""",(tenant,now-timedelta(days=1),now+timedelta(days=30)))
    return {'incentive_offering_id':offering,'offering_revision':1,'binding_id':binding,'amount_minor':'200','currency':'CNY','notification_text':''}


@pytest.fixture(autouse=True)
def source_may_name_the_incentive(monkeypatch):
    monkeypatch.setattr(fixture,'SOURCE_EXTRA_TARGETS',((ISSUE,ResourceType.ACTION,Operation.EXECUTE),))


@pytest.fixture
def agent(planning):
    f=planning;plan=f['plan'];admin=f['admin']
    from commercial_fixture import publish_registry_actions
    publish_registry_actions(admin,('nexloop.incentive.issue',),tenant=plan['tenant'])  # a Run may name only published Actions
    token=fixture.seed_multi_uuid(admin,plan['tenant'],[('eios:action:'+fixture.QUEUE+':1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:action:nexloop.incentive.run_submit:1',ResourceType.ACTION,Operation.EXECUTE)],suffix='-at043-runtime-worker')
    issuer_token=fixture.seed_multi_uuid(admin,plan['tenant'],[('eios:action:nexloop.incentive.execute:1',ResourceType.ACTION,Operation.EXECUTE)],suffix='-at043-issuer')
    offer=seed_catalog(admin,plan['tenant'],plan['consumer'])
    s=spec(plan);f['port']().establish(s)
    # Sessions last: every seed above moves the authority directory.
    yield dict(f,worker=plan['backend_worker'].authenticate(token,world='real'),issuer=lambda:plan['backend_worker'].authenticate(issuer_token,world='real'),spec=s,offer=offer)


def restrict(admin,tenant,consumer):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
        admin.execute("""insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,certainty,matched_text,restricted_at)
            values(%s,'real',%s,true,1,'synthetic','',0,'synthetic','refuse','',clock_timestamp())""",(tenant,consumer))


def issued(f,ledger):
    return IncentiveIssuer(*services(f['issuer']()),ledger).run_once()


def test_a_plan_run_issues_inside_its_run_and_never_to_a_restricted_customer(agent,tmp_path):
    f=agent;plan=f['plan'];admin=f['admin'];w=f['worker'];ledger=LocalIncentiveLedger(tmp_path/'ledger')
    run,command,ref=run_with(plan,w,'a',['eios:action:'+fixture.EFFECT+':1',ISSUE])
    f['port']().link_run(plan_id=f['spec']['plan_id'],version=1,run_id=run.run_id,triggers=[{'kind':'reassess_due','cause':'system'}])
    out=w.issue_run_incentive(activation_ref=ref,command=command,parameters=f['offer'])
    assert out['state']=='accepted' and out['origin']=='plan' and out['replayed'] is False and out['run_id']==run.run_id
    # The business key is derived from the Run and the offering: the same call replays.
    assert w.issue_run_incentive(activation_ref=ref,command=command,parameters=f['offer'])['replayed'] is True
    row=admin.execute('select consumer_id,initiator,origin,run_id::text,reply_to_message_id from runtime.nexloop_incentive_issues').fetchone()
    assert row==(plan['consumer'],'agent','plan',run.run_id,None)
    # D7: the customer is restricted before dispatch; a plan Run's issue is refused, the provider gets nothing.
    restrict(admin,plan['tenant'],plan['consumer'])
    issued(f,ledger)
    assert admin.execute('select state,refusal_code from runtime.nexloop_incentive_issues').fetchone()==('refused','contact_restricted')
    assert ledger.requests==[]


def reply_run(f,w,label,body):
    """A Run issued for one inbound message of the customer (as the message Run issuance records it)."""
    plan=f['plan'];admin=f['admin']
    run,command,ref=run_with(plan,w,label,['eios:action:'+fixture.EFFECT+':1',ISSUE])
    conversation=hashlib.sha256(b'at043-conversation').hexdigest();message=hashlib.sha256(('at043-inbound-'+label).encode()).hexdigest()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'synthetic-human','at043')
            on conflict do nothing""",(plan['tenant'],conversation,plan['consumer']))
        sequence=admin.execute('select count(*)+1 from runtime.nexloop_conversation_messages where conversation_id=%s',(conversation,)).fetchone()[0]
        admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
            values(%s,'real',%s,%s,%s,%s,%s,%s)""",(plan['tenant'],conversation,sequence,message,'at043-in-'+label,'d'*64,Jsonb({'body':body,'sequence':sequence})))
        admin.execute("""insert into authz.nexloop_message_run_issuances(tenant_id,world,message_id,run_id,request_id,issuance_nonce,assignment_id,assignment_revision,
            assignment_digest,issuer_principal,run_digest,issued_at,expires_at) select %s,'real',%s,run_id,'at043',%s,'a',1,%s,'synthetic',token_digest,now(),now()+interval '60 seconds'
            from authz.nexloop_run_credentials where run_id=%s""",(plan['tenant'],message,'e'*64,'f'*64,run.run_id))
    return run,command,ref,message


def test_a_run_answering_the_customer_may_issue_to_a_restricted_customer(agent,tmp_path):
    f=agent;plan=f['plan'];admin=f['admin'];w=f['worker'];ledger=LocalIncentiveLedger(tmp_path/'ledger')
    run,command,ref,message=reply_run(f,w,'b','续费有优惠吗')
    # Straight to the SQL port: the seeded issuance is not a full message route, so the guard's activation pre-check (covered
    # above) would refuse it; SQL re-checks the activation, the lease and the exact command itself.
    port=lambda:RunIncentivePort(*services(w))
    out=port().submit(activation_ref=ref,command=command,parameters=f['offer'])
    assert out['origin']=='reply'
    run2,command2,ref2,_=reply_run(f,w,'c','优惠到账了吗')
    port().submit(activation_ref=ref2,command=command2,parameters={**f['offer'],'notification_text':'优惠已到账'})
    # The customer becomes restricted before dispatch (D7 is decided at dispatch).
    restrict(admin,plan['tenant'],plan['consumer'])
    issued(f,ledger)
    assert admin.execute('select state,reply_to_message_id from runtime.nexloop_incentive_issues where run_id=%s',(run.run_id,)).fetchone()==('fulfilled',message)
    # With a notification it would reach the customer: refused whole (D6), even for a reply Run.
    assert admin.execute('select state,refusal_code from runtime.nexloop_incentive_issues where run_id=%s',(run2.run_id,)).fetchone()==('refused','attached_notification')
    assert len(ledger.requests)==1


def test_outside_the_run_or_the_catalog_nothing_is_accepted(agent):
    f=agent;plan=f['plan'];admin=f['admin'];w=f['worker']
    # The Run's resources do not name the incentive Action.
    run,command,ref=run_with(plan,w,'d',['eios:action:'+fixture.EFFECT+':1'])
    with pytest.raises(Exception):w.issue_run_incentive(activation_ref=ref,command=command,parameters=f['offer'])
    run,command,ref=run_with(plan,w,'e',['eios:action:'+fixture.EFFECT+':1',ISSUE])
    # Above the catalog maximum, and with a tampered command.
    with pytest.raises(Exception):w.issue_run_incentive(activation_ref=ref,command=command,parameters={**f['offer'],'amount_minor':'501'})
    with pytest.raises(Exception):w.issue_run_incentive(activation_ref=ref,command={**command,'consumer_ref':'consumer:'+'0'*64},parameters=f['offer'])
    assert admin.execute('select count(*) from runtime.nexloop_incentive_issues').fetchone()==(0,)

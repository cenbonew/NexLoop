"""ADR-023 §2.7 over the actual NX-047 chain: every inbound message gets a reply, by a fallback reply Run if needed.

Human HTTPS Message → relay → Pi Run that does NOT reply → the pending reply comes due → the reply worker starts the
one fallback reply Run (message path, 0110): governed planning objects, fallback issuance, fallback Context v6 with
this message, accepted into the runtime queue → the runtime worker and the actual Host run it (deterministic
provider, one bound reply) → effect worker delivers → the pending reply is settled. Plus: at most one fallback per
message, server-side single-tool restriction, mutual exclusion with settlement, escalation when the fallback fails.
Synthetic data; admin probes and moves due times forward (explicit time injection).
"""
from contextlib import contextmanager
import json
import secrets
import time

import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.backend import open_backend
from nexloop_eios.contact_restrictions import ContactReadPort,ReplyGuaranteeWorker,load_reply_policy
from nexloop_eios.reply_fallback import FallbackReplyLauncher
from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401
from test_context_v6_pg import STRATEGY,seed_strategy
from test_outbound_messages_pg import chain,delivery_material,delivery_service,effect_worker,outbound
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
POLICY=load_reply_policy(ROOT/'deploy/configuration/reply-guarantee.v1.json')
TEST_POLICY={**POLICY,'fallback':{**POLICY['fallback'],'runtime_profile':'deterministic-test'}}  # configurable: test provider
pytestmark=pytest.mark.reply_fallback
GUARANTOR=[(a,ResourceType.ACTION,Operation.EXECUTE) for a in ('eios:action:NexLoop.feed.reply-due:1','eios:action:nexloop.reply.guarantee:1','eios:action:nexloop.contact.read:1')]


def silent(cfg):
    """The original message Run completes without replying (no deterministic tool calls, plain completion)."""
    cfg.pop('deterministic_message_from_input');cfg.pop('context_input_protocol')


def make_due(admin):
    admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp()-interval '1 second' where feed='reply-due'")


def pending(admin,tenant):
    return [r[0] for r in admin.execute("select item_key from runtime.nexloop_work_feed where tenant_id=%s and feed='reply-due'",(tenant,)).fetchall()]


def provision(f,admin):
    """Reply worker authority, seeded before the conversation starts: seeding later changes the Source directory, which
    (correctly) invalidates Runs already issued under it."""
    from nexloop_eios.assembly import open_core
    o=f['original']
    with open_core(make_conninfo(o['pg'],user='nexloop_domain_worker')) as pool:
        _,token=seed_multi_authority(admin,pool,GUARANTOR,identity_suffix='-reply-guarantor',tenant=o['tenant'])
    seed_strategy(admin,o['tenant'],STRATEGY)
    return token


@contextmanager
def guarantor(c,admin,tmp_path,token):
    """The reply worker (domain worker) with its launcher (message relay identities on the API backend)."""
    o=c['o'];p=c['p']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'reply-worker-artifacts',
            signing_key_file=p['backend_signing'],signing_key_id='explicit-configuration') as domain:
        api=c['backend'];tokens=c['tokens']
        def session(label):return lambda:api.authenticate(tokens[label],world='real')
        launcher=FallbackReplyLauncher(route=session('assembly-route'),source=session('assembly-source'),planner=session('assembly-planner'),
            executor_token=tokens['assembly-executor'],recipe=c['recipe'],policy=TEST_POLICY)
        def worker(with_launcher=True):
            current=domain.authenticate(token,world='real')
            return ReplyGuaranteeWorker(domain._pool,current._session,domain._signer,policy=TEST_POLICY,launcher=launcher if with_launcher else None)
        def read():
            current=domain.authenticate(token,world='real');return ContactReadPort(domain._pool,current._session,domain._signer)
        yield dict(worker=worker,launcher=launcher,read=read,domain=domain)


def run_fallback_on_host(c,tmp_path,g,mode='reply_once'):
    """The runtime worker claims the fallback Run's task and the actual Host runs it (deterministic provider)."""
    import httpx  # noqa: F401
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,host
    from test_runtime_effect_tools import effect_configuration
    last=g['launcher'].last;command=last['command'];text=last['input']
    worker=g['domain'].authenticate(c['tokens']['assembly-runtime-worker'],world='real')
    job=worker.claim_task(queue='operations',lease_seconds=60)
    activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=command['run_id'],command=command,input=text,owner_epoch=1)['activation_ref']
    root=tmp_path/'fallback-host';root.mkdir(mode=0o700,exist_ok=True)
    runtime,key=files(root)
    guard_key=tmp_path/'fallback-guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    o=c['o'];p=c['p']
    spawn=dict(database_url=make_conninfo(o['pg'],user='nexloop_domain_worker'),signing_key_file=p['backend_signing'],signing_key_id='explicit-configuration',
        artifact_root=tmp_path/'reply-worker-artifacts',token=c['tokens']['assembly-runtime-worker'],world='real')  # the deployed four guard processes (ADR-024)
    with guard_server(worker,tmp_path/'fallback-host',guard_key,spawn=spawn,workers=4) as port:  # deployment shape (ADR-022 §4, ADR-024)
        config=effect_configuration(tmp_path/'fallback-host',port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message')
        body.update(context_input_protocol='nexloop.context-pack.v6',deterministic_reply_once=True);config.write_text(json.dumps(body))
        with host(runtime,key,config) as (_,client,headers):
            assert client.post('/internal/v1/runs/start',headers=headers,json={'activation_ref':activation,'command':command,'input':text}).status_code==202
            deadline=time.monotonic()+60
            while True:
                result=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':activation,'command':command}).json()
                if result['submission_status']=='done':return result
                assert time.monotonic()<deadline;time.sleep(.05)


def test_unanswered_message_gets_one_llm_fallback_reply_and_settles(assembled_message,admin,tmp_path):
    f=assembled_message;token=provision(f,admin)
    with chain(f,admin,tmp_path,body='你们的退款什么时候到账？',host_update=silent) as c:
        c['recipe']=f['recipe'];tenant=c['tenant'];consumer=c['consumer']
        assert outbound(admin,tenant)==[] and pending(admin,tenant)==['reply:'+consumer['id']]
        with guarantor(c,admin,tmp_path,token) as g:
            assert not any(g['worker']().run_once().values())  # not due before start_after_seconds
            make_due(admin)
            assert g['worker']().run_once()['fallback_started']==1
            run_id=g['launcher'].last['run_id']
            assert admin.execute('select message_id from authz.nexloop_message_fallback_issuances where run_id=%s',(run_id,)).fetchone()==(consumer['id'],)
            pack=json.loads(g['launcher'].last['input'])
            assert pack['schema_version']=='nexloop.context-pack.v6' and pack['user_statement']['message_id']==consumer['id']
            assert run_fallback_on_host(c,tmp_path,g)['runtime_outcome']=='succeeded'
            ((intent,state,_,_,_,trigger,_),)=outbound(admin,tenant)
            assert state=='persisted' and trigger==consumer['id']
            material=delivery_material(tmp_path)
            with delivery_service(c,tmp_path,material):
                assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
            assert outbound(admin,tenant)[0][1]=='delivered' and pending(admin,tenant)==[]
            assert g['read']().escalations()==[]


def activated_fallback(c,g):
    """The runtime worker claims and activates the fallback Run (start authorized), without a Host."""
    last=g['launcher'].last;command=last['command'];text=last['input']
    worker=g['domain'].authenticate(c['tokens']['assembly-runtime-worker'],world='real')
    job=worker.claim_task(queue='operations',lease_seconds=60)
    ref=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=command['run_id'],command=command,input=text,owner_epoch=1)['activation_ref']
    assert worker.authorize_runtime_activation(activation_ref=ref,command=command,operation='start',input=text)['authorized'] is True
    return worker,ref,command


def test_one_fallback_per_message_and_its_only_tool_is_the_bound_reply(assembled_message,admin,tmp_path):
    import uuid
    from nexloop_eios.effect_intents import EffectIntentConflict,EffectIntentUnavailable
    f=assembled_message;token=provision(f,admin)
    with chain(f,admin,tmp_path,body='请问发票开了吗？',host_update=silent) as c:
        c['recipe']=f['recipe'];tenant=c['tenant'];consumer=c['consumer']
        with guarantor(c,admin,tmp_path,token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started']==1
            runs=admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]
            # Re-triggered (or a restarted worker): never a second fallback Run for the message.
            state={'consumer_id':g['launcher'].recipe['consumer_id']}
            with pytest.raises(Exception):g['launcher'].start(message_id=consumer['id'],state=state)
            assert admin.execute('select count(*) from authz.nexloop_message_fallback_issuances').fetchone()==(1,)
            assert admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]==runs
            worker,ref,command=activated_fallback(c,g)
            # Server-side tool restriction (SQL, not Host configuration): any other effect tool is refused.
            with pytest.raises(EffectIntentUnavailable):
                worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='find',intent_id=str(uuid.uuid4()))
            from test_plan_outcome_pg import outcome
            with pytest.raises(Exception):worker.record_plan_outcome(activation_ref=ref,command=command,outcome=outcome())
            # The one reply is bound to this message by the server; a second, different reply is refused.
            first=worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'您好，发票已在处理中。'})
            ((intent,_,_,_,_,trigger,_),)=outbound(admin,tenant)
            assert intent==first['receipt']['intent_id'] and trigger==consumer['id']
            with pytest.raises(EffectIntentConflict):
                worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'另外再推荐一个产品。'})
            assert len(outbound(admin,tenant))==1


def test_settled_message_never_gets_a_fallback(assembled_message,admin,tmp_path):
    f=assembled_message;token=provision(f,admin)
    with chain(f,admin,tmp_path,body='谢谢，收到了。') as c:
        c['recipe']=f['recipe'];tenant=c['tenant'];consumer=c['consumer']
        material=delivery_material(tmp_path)
        with delivery_service(c,tmp_path,material):
            assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
        assert pending(admin,tenant)==[]  # the Agent's bound reply settled it
        with guarantor(c,admin,tmp_path,token) as g:
            assert not any(g['worker']().run_once().values())
            with pytest.raises(Exception):g['launcher'].start(message_id=consumer['id'],state={'consumer_id':g['launcher'].recipe['consumer_id']})
            assert admin.execute('select count(*) from authz.nexloop_message_fallback_issuances').fetchone()==(0,)


def test_fallback_reply_is_refused_once_the_original_reply_was_accepted(assembled_message,admin,tmp_path):
    """Issued while the Agent's reply was still in flight; the Agent's reply is then accepted and settles the message: the
    fallback's reply is refused at dispatch (one reply per inbound message), zero further deliveries."""
    f=assembled_message;token=provision(f,admin)
    with chain(f,admin,tmp_path,body='物流到哪了？') as c:
        c['recipe']=f['recipe'];tenant=c['tenant']
        ((agent_intent,state,*_),)=outbound(admin,tenant);assert state=='persisted'
        with guarantor(c,admin,tmp_path,token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started']==1  # unsettled at its time: the fallback is issued
            material=delivery_material(tmp_path)
            with delivery_service(c,tmp_path,material):
                assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])  # the Agent's reply
                assert outbound(admin,tenant)[0][1]=='delivered' and pending(admin,tenant)==[]
                worker,ref,command=activated_fallback(c,g)
                worker.runtime_effect_tool(activation_ref=ref,command=command,tool_operation='submit',parameters={'message':'您好，包裹已发出。'})
                fallback_intent=next(r[0] for r in outbound(admin,tenant) if r[0]!=agent_intent)
                assert 'fulfilled' not in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
            states=dict((r[0],r[1]) for r in outbound(admin,tenant))
            assert states[agent_intent]=='delivered' and states[fallback_intent] in ('persisted','failed')
            assert len([p for p in material['root'].iterdir() if p.name.endswith('.export.json')])==1


def test_fallback_that_cannot_start_or_stays_unanswered_is_escalated(assembled_message,admin,tmp_path):
    f=assembled_message;token=provision(f,admin)
    with chain(f,admin,tmp_path,body='在吗？',host_update=silent) as c:
        c['recipe']=f['recipe'];tenant=c['tenant'];consumer=c['consumer']
        with guarantor(c,admin,tmp_path,token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started']==1
            # The fallback Run never replies (model unavailable / timed out): when its time is up, the owner takes over.
            make_due(admin)
            assert g['worker']().run_once()['escalated']==1
            (row,)=g['read']().escalations()
            assert row['message_id']==consumer['id'] and row['reason']=='fallback_unanswered' and row['detail']['fallback_run_id']==g['launcher'].last['run_id']
            assert pending(admin,tenant)==[]


def test_restricted_consumer_unanswered_message_still_gets_its_bound_fallback_reply(assembled_message,admin,tmp_path):
    f=assembled_message;token=provision(f,admin)
    with chain(f,admin,tmp_path,body='以后别再给我发消息了，不过退款到账了吗？',host_update=silent) as c:
        c['recipe']=f['recipe'];tenant=c['tenant'];consumer=c['consumer']
        assert admin.execute('select active from control.nexloop_contact_restrictions where tenant_id=%s',(tenant,)).fetchone()==(True,)
        with guarantor(c,admin,tmp_path,token) as g:
            make_due(admin)
            assert g['worker']().run_once()['fallback_started']==1
            pack=json.loads(g['launcher'].last['input'])
            assert pack['goal']['control_snapshot'] is not None or pack['insufficient']  # current control state is in the Context
            assert run_fallback_on_host(c,tmp_path,g)['runtime_outcome']=='succeeded'
            material=delivery_material(tmp_path)
            with delivery_service(c,tmp_path,material):
                assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
            assert outbound(admin,tenant)[0][1]=='delivered' and pending(admin,tenant)==[]
            assert admin.execute('select active from control.nexloop_contact_restrictions where tenant_id=%s',(tenant,)).fetchone()==(True,)

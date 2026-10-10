"""NX-027 J01 end to end: a verified payment wakes the Consumer's plan → reevaluation Run on the actual Host and Pi.

PRD J01 (renewal reminder): the customer pays; the signed payment event is verified, the recorder applies the
CommercialRecord through the governed Action and, in the same transaction, marks the Consumer's active plan
(trigger 'commercial_event', references only). The plan worker launches the bounded Role reevaluation Run (production
issuance, Context v6 carrying the plan), the actual Host runs Pi with the deterministic plan protocol and records a
normal no_action: no intent, no outbox, no outbound Message, the plan stays active. Synthetic data only; run serially.
"""
import json

from psycopg.conninfo import make_conninfo
from nexloop_eios.assembly import open_core
from commercial_fixture import commercial_setup
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_closure_plan_run_pg import actual_host,launched,role_settings,run_on_host,tool_names  # noqa: F401
from test_plan_outcome_pg import outcome_worker_and_spawn
from test_plan_reevaluation_pg import feed
from test_plan_role_launcher_pg import role_planning,within_ceiling  # noqa: F401


def test_j01_verified_payment_wakes_the_plan_and_the_reevaluation_concludes_no_contact(role_planning,admin,pg,tmp_path):
    f=role_planning;plan=f['plan'];tenant=plan['tenant']
    # Provision every service identity first (the recorders, then the runtime worker): seeding authority later would move
    # the directory the Run is issued under and that the worker authenticated against.
    with open_core(make_conninfo(pg,user='nexloop_api')) as api,\
         commercial_setup(admin,pg,tmp_path,api_pool=api,signer=plan['backend_worker']._signer,tenant=tenant) as env:
        worker,spawn=outcome_worker_and_spawn(plan,admin,'-nx027-runtime')
        env.connector();env.link('cust-j01',plan['consumer'])
        s=f['establish'](steps=[within_ceiling()])
        admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")  # its own establish wake: taken
        # The customer pays the renewal: a verified signed event, applied through the governed recorder.
        assert env.post(env.event('subscription.renewed','renewal-2026',customer='cust-j01',amount=19900))['status']==202
        assert env.tick()[0]['created']==1
        record=env.record('renewal','renewal-2026')
        assert record['properties']['consumer_ref']==plan['consumer'] and record['properties']['status']=='succeeded'
        ((key,payload,status,later),)=feed(admin)
        assert key=='plan:'+s['plan_id'] and status=='pending' and later is False
        assert [(t['kind'],t['cause'],t['ref']) for t in payload['triggers']]==[('commercial_event','external','commercial:'+record['object_id'])]
        # The plan worker launches the bounded reevaluation Run carrying that trigger.
        assert f['worker']().run_once()['launched']==1
        run_id,command,text=launched(f,s['plan_id'])
        (triggers,)=admin.execute('select triggers from runtime.nexloop_plan_runs where run_id=%s',(run_id,)).fetchone()
        assert [t['kind'] for t in triggers]==['commercial_event']
        assert [i['ref'] for i in json.loads(text)['open_work'] if i['subsection']=='plan']==[f"nexloop:plan:{s['plan_id']}@1"]
        with actual_host(f,tmp_path,worker,'no_action',spawn) as (runtime,client,headers):
            result=run_on_host(client,headers,worker,command,text)
    assert result['runtime_outcome']=='succeeded' and tool_names(runtime,run_id)==['nexloop.plan.outcome']
    assert admin.execute('select kind,version,intent_ref from runtime.nexloop_plan_outcomes where run_id=%s',(run_id,)).fetchone()==('no_action',1,None)
    # Paid: no reminder is sent — no intent, no outbox, no outbound Message; the plan stays active.
    assert admin.execute('select count(*) from runtime.nexloop_effect_submissions where run_id=%s',(run_id,)).fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_outbound_messages').fetchone()==(0,)
    assert admin.execute('select version,status from runtime.nexloop_plan_events where plan_id=%s order by event_id desc limit 1',(s['plan_id'],)).fetchone()==(1,'active')
    # The Run's model calls carry the Run budget's currency (D4) and are model cost entries of this Run.
    currency=command['budget']['currency']
    calls=admin.execute('select currency,cost is not null from runtime.nexloop_model_requests where run_id=%s',(run_id,)).fetchall()
    assert calls and all(c==currency for c,_ in calls)
    priced=sum(1 for _,has in calls if has)
    assert admin.execute("select count(*) from runtime.nexloop_cost_entries where run_id=%s and cost_kind='model' and currency=%s",(run_id,currency)).fetchone()==(priced,)

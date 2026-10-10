"""NX-027 slice 3 actual PostgreSQL: cost entries and budget settlement (AT-043, D4, D5, D7), migration 0132.

Model costs come from the model results the Host records through the guard (v6 Run); the reservation of the Run is
settled when its task reaches a terminal status. Discount costs come from incentive reservations; cost entries project
to 'cost_entry' metric sources. Synthetic data only. Admin seeds NX-022 inputs (budget limits and the Run's dispatch
reservation, tested there) and probes; it never authorizes anything.
"""
from contextlib import contextmanager
from datetime import UTC,datetime,timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.backend import open_backend
from nexloop_eios.commercial import CostReadPort
from test_context_v6_pg import v6,owner,request_snapshot
from test_context_artifacts import context_message,source_declarations
from local_message_assembly_fixture import assembled_message,business_plan,configured
from commercial_fixture import TENANT,commercial_env,published_action

OK={'call_sequence':1,'result_status':'succeeded','usage':{'input':120,'output':30,'cache_read':0,'cache_write':0,'total':150},'cost':'0.00012000','response_digest':'b'*64}


@contextmanager
def claimed_run(f,tmp_path):
    with open_backend(database_url=make_conninfo(f['original']['pg'],user='nexloop_domain_worker'),artifact_root=tmp_path/'worker-artifacts',
     signing_key_file=f['original']['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        worker=backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
        job=worker.claim_task(queue='operations',lease_seconds=60);assert job is not None
        command=job['payload']['run_command'];text=job['payload']['input']
        activation=worker.create_runtime_activation(queue='operations',task_id=job['task_id'],fence=job['fence'],run_id=command['run_id'],command=command,input=text,owner_epoch=1)
        guard=lambda **kw:worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model',**kw)
        yield worker,job,command,text,guard


def reserve_run(admin,tenant,command,amount):
    """NX-022 dispatch reservation of the Run's model budget (consumption id run:<run_id>), seeded as its input."""
    now=datetime.now(UTC)
    with admin.transaction():
        owner(admin,tenant)
        admin.execute("insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision) values(%s,'real','model',%s,'10',%s,%s,1)",
            (tenant,command['budget']['currency'],now-timedelta(days=1),now+timedelta(days=30)))
        admin.execute("insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id) values(%s,'real','model',%s,%s,%s,%s,'synthetic-dispatcher')",
            (tenant,'run:'+command['run_id'],amount,command['budget']['currency'],'run:'+command['run_id']))


def rows(admin,tenant,sql,params=()):
    with admin.transaction():
        owner(admin,tenant)
        return admin.execute(sql,params).fetchall()


def test_model_costs_carry_the_run_currency_and_the_terminal_task_releases_the_rest(v6,admin,tmp_path):
    f=v6;tenant=f['original']['tenant']
    assert f['v6_relay']().run_once()=='queued'
    with claimed_run(f,tmp_path) as (worker,job,command,text,guard):
        currency=command['budget']['currency'];run=command['run_id']
        reserve_run(admin,tenant,command,'0.50')
        guard(request_snapshot=request_snapshot(1,text));guard(model_result=OK)
        guard(request_snapshot=request_snapshot(2,text));guard(model_result=dict(OK,call_sequence=2,cost='0.00030000'))
        # D4: the recorded result carries the Run budget's currency, set with the result and never afterwards.
        assert rows(admin,tenant,'select call_sequence,currency from runtime.nexloop_model_requests order by 1')==[(1,currency),(2,currency)]
        with admin.transaction():
            owner(admin,tenant)
            with pytest.raises(psycopg.errors.InvalidParameterValue),admin.transaction():
                admin.execute("update runtime.nexloop_model_requests set currency='XXX' where call_sequence=1")
        entries=rows(admin,tenant,"select entry_id,cost_kind,amount::text,currency,amount_unit,basis,run_id::text,data_mode from runtime.nexloop_cost_entries order by entry_id")
        assert entries==[('model:'+run+':1','model','0.00012000',currency,'major','provider_reported',run,'real'),
                         ('model:'+run+':2','model','0.00030000',currency,'major','provider_reported',run,'real')]
        # Running: nothing is settled yet.
        assert rows(admin,tenant,'select count(*) from runtime.nexloop_budget_settlements')==[(0,)]
        worker.finish_task(queue='operations',task_id=job['task_id'],fence=job['fence'],status='succeeded',result={'status':'succeeded'})
    reservation=rows(admin,tenant,"select recorded_at from control.nexloop_budget_consumption where consumption_id=%s",('run:'+run,))[0][0]
    assert rows(admin,tenant,"select consumption_id,amount::text,unit,recorded_at from control.nexloop_budget_consumption order by consumption_id")==[
        ('run:'+run,'0.50',currency,reservation),('run:'+run+':settle','-0.49958000',currency,reservation)]
    assert rows(admin,tenant,'select consumption_id,reserved::text,actual::text,released::text,outcome from runtime.nexloop_budget_settlements')==[
        ('run:'+run,'0.50','0.00042000','0.49958000','released')]
    # Settled once: a second settle call changes nothing; the budget table stays append-only.
    with admin.transaction():
        assert admin.execute("select runtime.nexloop_budget_settle_run(%s,'real',%s)",(tenant,run)).fetchone()==(None,)
        owner(admin,tenant)
        with pytest.raises(psycopg.Error),admin.transaction():
            admin.execute("update control.nexloop_budget_consumption set amount=1 where consumption_id=%s",('run:'+run+':settle',))
    # Only a settlement row may be negative.
    with admin.transaction():
        owner(admin,tenant)
        with pytest.raises(psycopg.errors.CheckViolation),admin.transaction():
            admin.execute("insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id) values(%s,'real','model','forged',-1,%s,'x','x')",
                (tenant,currency))


def test_an_unknown_model_result_keeps_the_whole_reservation(v6,admin,tmp_path):
    f=v6;tenant=f['original']['tenant']
    assert f['v6_relay']().run_once()=='queued'
    with claimed_run(f,tmp_path) as (worker,job,command,text,guard):
        run=command['run_id'];reserve_run(admin,tenant,command,'0.50')
        guard(request_snapshot=request_snapshot(1,text));guard(model_result=OK)
        guard(request_snapshot=request_snapshot(2,text))
        guard(model_result={'call_sequence':2,'result_status':'unknown','usage':None,'cost':None,'response_digest':None})
        worker.finish_task(queue='operations',task_id=job['task_id'],fence=job['fence'],status='failed',result={'status':'failed'})
    # D5: a call whose result is unknown counts as reserved; no release row, the outcome is recorded.
    assert rows(admin,tenant,"select consumption_id from control.nexloop_budget_consumption order by 1")==[('run:'+run,)]
    assert rows(admin,tenant,'select reserved::text,actual,released::text,outcome from runtime.nexloop_budget_settlements')==[('0.50',None,'0','result_pending_or_unknown')]
    # The unknown call has no cost entry; the known one has.
    assert rows(admin,tenant,'select entry_id from runtime.nexloop_cost_entries')==[('model:'+run+':1',)]


def seed_incentive(admin,limit='100000'):
    now=datetime.now(UTC)
    with admin.transaction():
        owner(admin,TENANT)
        admin.execute("insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision) values(%s,'real','incentive','CNY',%s,%s,%s,1)",
            (TENANT,limit,now-timedelta(days=1),now+timedelta(days=30)))


def reserve_incentive(admin,consumption_id,amount,unit='CNY'):
    with admin.transaction():
        owner(admin,TENANT)
        admin.execute("insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id) values(%s,'real','incentive',%s,%s,%s,%s,'synthetic-dispatcher')",
            (TENANT,consumption_id,amount,unit,'offer:'+consumption_id))


def test_discount_costs_project_to_cost_metrics_and_are_read_per_currency(commercial_env,admin):
    env=commercial_env
    now=datetime.now(UTC);window=(now-timedelta(days=1),now+timedelta(days=1))
    env.metric('discount-cost',source_kind='cost_entry',kinds=(),statuses=(),cost_kinds=('discount',))
    env.metric('discount-cost-usd',currency='USD',source_kind='cost_entry',kinds=(),statuses=(),cost_kinds=('discount',))
    env.metric('discount-count',aggregation='count',value='count',currency=None,source_kind='cost_entry',kinds=(),statuses=(),cost_kinds=('discount',))
    env.kr('g-discount','discount-cost',window);env.kr('g-discount-usd','discount-cost-usd',window);env.kr('g-discount-count','discount-count',window)
    seed_incentive(admin)
    reserve_incentive(admin,'coupon-1','300');reserve_incentive(admin,'coupon-2','500')
    entries=rows(admin,TENANT,"select entry_id,cost_kind,amount::text,currency,amount_unit,basis,data_mode from runtime.nexloop_cost_entries order by entry_id")
    assert entries==[('discount:coupon-1','discount','300.00000000','CNY','minor','budget_reservation','real'),
                     ('discount:coupon-2','discount','500.00000000','CNY','minor','budget_reservation','real')]
    assert Decimal(env.compute('g-discount')['value'])==800
    assert Decimal(env.compute('g-discount-count')['value'])==2
    usd=env.compute('g-discount-usd')
    assert Decimal(usd['value'])==0 and usd['excluded_other_currency']==2
    # Cost entries are append-only even for their owner.
    with admin.transaction():
        owner(admin,TENANT)
        with pytest.raises(psycopg.Error),admin.transaction():
            admin.execute("update runtime.nexloop_cost_entries set amount=1")
        with pytest.raises(psycopg.Error),admin.transaction():
            admin.execute("delete from runtime.nexloop_cost_entries")
    port=CostReadPort(env['worker'],env.session(),env['signer'])
    assert port.summary()==[{'cost_kind':'discount','data_mode':'real','currency':'CNY','amount_unit':'minor','amount':'800.00000000','entries':2,'units':'2'}]
    assert [e['entry_id'] for e in port.entries(cost_kind='discount')]==['discount:coupon-1','discount:coupon-2']
    assert port.entries(cost_kind='model')==[] and port.settlements()==[]
    # Another world sees nothing of these.
    assert CostReadPort(env['worker'],env.session('test'),env['signer']).summary()==[]


def test_channel_rate_comes_only_from_a_versioned_configured_rate(commercial_env,admin):
    """D7: units always; an amount only when the current settings version carries a well-formed rate for the Action."""
    rate=lambda action,version:admin.execute('select runtime.nexloop_cost_channel_rate(%s,%s)',(action,version)).fetchone()[0]
    assert rate('nexloop.service.request',1) is None                                 # v1 ships no rates: unpriced
    current=admin.execute('select definition from control.nexloop_commercial_settings order by version desc limit 1').fetchone()[0]
    v2=dict(current,version=2,channel_unit_rates=[{'action':'nexloop.service.request','version':1,'currency':'CNY','amount_per_unit':'0.05'},
        {'action':'nexloop.bad.rate','version':1,'currency':'CNY','amount_per_unit':'1e3'}])
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("insert into control.nexloop_commercial_settings(version,definition,definition_digest,published_by) values(2,%s,%s,'synthetic-owner')",(Jsonb(v2),'0'*64))
    assert rate('nexloop.service.request',1)=={'action':'nexloop.service.request','version':1,'currency':'CNY','amount_per_unit':'0.05'}
    assert rate('nexloop.service.request',2) is None and rate('nexloop.bad.rate',1) is None


def test_service_and_labour_costs_entered_by_the_owner_are_corrected_by_supersession(commercial_env,admin):
    """Governed-entry handler (NX-028 registry signature); the registry row follows once 0150 is merged."""
    import json
    env=commercial_env;now=datetime.now(UTC).replace(microsecond=0)
    env.metric('service-cost',source_kind='cost_entry',kinds=(),statuses=(),cost_kinds=('service','labour'))
    env.kr('g-service','service-cost',(now-timedelta(days=1),now+timedelta(days=1)))
    record=lambda intent,body:admin.execute("select control.nexloop_cost_record_handler(%s,'real','human-principal','consumer',%s,%s::jsonb)",
        (TENANT,intent,json.dumps(body))).fetchone()[0]
    first={'request_id':'r1','cost_kind':'service','amount':'50000','currency':'CNY','amount_unit':'minor','occurred_at':now.isoformat()}
    assert record('intent-1',first)=={'entry_id':'manual:intent-1','replayed':False,'corrects_entry_id':None}
    assert record('intent-1',first)['replayed'] is True                                        # same intent, same body
    with pytest.raises(psycopg.errors.UniqueViolation):record('intent-1',dict(first,amount='1'))
    assert Decimal(env.compute('g-service')['value'])==50000
    fix=dict(first,request_id='r2',amount='45000',corrects_entry_id='manual:intent-1')
    assert record('intent-2',fix)['corrects_entry_id']=='manual:intent-1'
    kr=env.compute('g-service');assert Decimal(kr['value'])==45000 and kr['corrections_applied']==1
    port=CostReadPort(env['worker'],env.session(),env['signer'])
    assert port.summary()==[{'cost_kind':'service','data_mode':'real','currency':'CNY','amount_unit':'minor','amount':'45000.00000000','entries':1,'units':'1'}]
    assert [(e['entry_id'],e['superseded'],e['corrects_entry_id']) for e in port.entries(cost_kind='service')]==[
        ('manual:intent-1',True,None),('manual:intent-2',False,'manual:intent-1')]
    assert admin.execute("select basis,recorded_by from runtime.nexloop_cost_entries where entry_id='manual:intent-2'").fetchone()==('operator_entered','human-principal')
    for bad in (dict(first,cost_kind='model'),dict(first,amount='1.5'),dict(first,amount='-1'),dict(first,currency='cny'),dict(first,extra=1),
                dict(first,occurred_at=(now+timedelta(days=1)).isoformat()),dict(first,corrects_entry_id='manual:intent-1'),   # already corrected
                dict(first,cost_kind='labour',corrects_entry_id='manual:intent-2'),dict(first,units='0')):
        with pytest.raises(psycopg.errors.InvalidParameterValue):record('intent-bad',bad)
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_configurator'):
        assert admin.execute("select has_function_privilege(%s,'control.nexloop_cost_record_handler(text,text,text,text,text,jsonb)','execute')",(role,)).fetchone()==(False,)

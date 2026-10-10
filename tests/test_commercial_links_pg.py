"""NX-027 slice 4 actual PostgreSQL: recorded CommercialRecord states reach plans (NX-024) and commitments (NX-026), 0133.

On the NX-026 commitment fixture (real Consumer, governed Commitment objects through the keeper) plus the commercial
configuration: verified signed events → recorder → governed CommercialRecord → (same transaction) plan marking of the
linked Consumer's active plans and 'commercial_event' evidence for bound commitments → the keeper fulfils the commitment.
Synthetic data only. Admin seeds the NX-024 plan rows (as test_commitments_pg does) and calls the owner-internal binding
interface whose human Action belongs to NX-028.
"""
from datetime import UTC,datetime,timedelta
import json

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.assembly import open_core
from commercial_fixture import TENANT,commercial_setup
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_commitments_pg import active_plan,fresh,plan_triggers

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


@pytest.fixture
def linked(commitments,admin,pg,tmp_path):
    c=commitments
    with open_core(make_conninfo(pg,user='nexloop_api')) as api,commercial_setup(admin,pg,tmp_path,api_pool=api,signer=c['signer']) as env:
        env.connector();env.link('cust-1',c['consumer'])
        yield c,env


def bind(admin,commitment,external_id,*,kind='payment',statuses=None,connector='synthetic-shop',world='real'):
    return admin.execute('select runtime.nexloop_commercial_bind_commitment(%s,%s,%s,%s,%s,%s,%s,%s)',
        (TENANT,world,commitment,connector,kind,external_id,statuses,'synthetic-owner')).fetchone()[0]


def evidence(admin,commitment):
    return admin.execute("select kind,ref,detail->>'status' from runtime.nexloop_commitment_evidence where commitment_id=%s and kind='commercial_event' order by evidence_number",
        (commitment,)).fetchall()


def commercial_triggers(c,plan):return [t for t in plan_triggers(c,plan) if t[0]=='commercial_event']


def test_verified_payment_is_commitment_evidence_and_wakes_the_consumers_plans(linked,admin):
    c,env=linked
    plan=active_plan(c);commitment,_=fresh(c)
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")  # registration marks are NX-026's own
    assert bind(admin,commitment,'pay-1')['evidence']==0
    assert env.post(env.event('payment.pending','pay-1',occurred_at=datetime.now(UTC)-timedelta(seconds=2)))['status']==202
    env.tick()
    record=env.record('payment','pay-1')
    ref='commercial:'+record['object_id']
    # The pending state wakes the Consumer's plan but is no evidence: the binding asks for 'succeeded'.
    assert commercial_triggers(c,plan)==[('commercial_event',ref)] and evidence(admin,commitment)==[]
    assert env.post(env.event('payment.succeeded','pay-1',occurred_at=datetime.now(UTC)))['status']==202
    env.tick()
    assert evidence(admin,commitment)==[('commercial_event',ref,'succeeded')]
    assert commercial_triggers(c,plan)==[('commercial_event',ref),('commercial_event',ref)]
    # The keeper moves the commitment on that evidence alone.
    c.tick()
    p=c.commitment(commitment)['properties']
    assert p['status']=='fulfilled' and p['late'] is False and [e['ref'] for e in p['fulfillment_evidence']]==[ref]
    # Once per object revision: re-running the recorder wakes nobody again and writes nothing twice.
    wakes=admin.execute('select revision,plans,evidence from runtime.nexloop_commercial_wakes where record_key=%s order by revision',(env.record_key('payment','pay-1'),)).fetchall()
    assert [(w[1],w[2]) for w in wakes]==[(1,0),(1,1)]
    admin.execute("select runtime.nexloop_commercial_touch(%s,'real',%s)",(TENANT,env.record_key('payment','pay-1')))
    env.tick()
    assert len(commercial_triggers(c,plan))==2 and len(evidence(admin,commitment))==1
    # The plan trigger carries references only (no amount, no customer reference).
    trigger=admin.execute("select payload from runtime.nexloop_work_feed where feed='plan-reevaluate'").fetchone()[0]['triggers'][0]
    assert set(trigger)=={'kind','cause','ref','record_kind','status','at'} and trigger['cause']=='external'


def test_no_binding_other_consumer_or_other_world_writes_no_evidence(linked,admin):
    c,env=linked
    plan=active_plan(c);commitment,_=fresh(c)
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    other=env.consumer();env.link('cust-2',other)
    bind(admin,commitment,'pay-2')
    # Bound reference, but the record's Consumer is someone else: an owner exception, no evidence.
    env.post(env.event('payment.succeeded','pay-2',customer='cust-2',occurred_at=datetime.now(UTC)));env.tick()
    assert evidence(admin,commitment)==[]
    assert [(s,r) for s,r,_ in env.exceptions()]==[('record:'+env.record_key('payment','pay-2'),'commitment_consumer_mismatch')]
    assert commercial_triggers(c,plan)==[]  # the other Consumer has no plan here
    # The right Consumer but no binding: nothing is matched by guessing (the plan is still woken).
    env.post(env.event('payment.succeeded','pay-3',occurred_at=datetime.now(UTC)));env.tick()
    assert evidence(admin,commitment)==[] and len(commercial_triggers(c,plan))==1
    # A test-world connector's record never reaches a real commitment, even with the same external id.
    env.connector('synthetic-test-shop',world='test',data_mode='test')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):bind(admin,commitment,'pay-9',connector='synthetic-test-shop')
    env.post(env.event('payment.succeeded','pay-2',occurred_at=datetime.now(UTC)),connector_id='synthetic-test-shop');env.tick('test')
    assert evidence(admin,commitment)==[]
    # An unlinked customer's record wakes nobody.
    env.post(env.event('payment.succeeded','pay-4',customer='cust-unlinked',occurred_at=datetime.now(UTC)));env.tick()
    assert len(commercial_triggers(c,plan))==1
    # Binding after the record already reached the bound status: evidence at once.
    assert bind(admin,commitment,'pay-3')['evidence']==1
    assert evidence(admin,commitment)==[('commercial_event','commercial:'+env.record('payment','pay-3')['object_id'],'succeeded')]
    # Binding shape.
    for kind,statuses in (('payment',['paid']),('refund',['refunded']),('payment',[]),('voucher',None)):
        with pytest.raises(psycopg.errors.InvalidParameterValue):bind(admin,commitment,'pay-5',kind=kind,statuses=statuses)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):bind(admin,'0'*64,'pay-5')
    # Bindings and wakes are append-only.
    with admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        for table in ('runtime.nexloop_commercial_commitment_bindings','runtime.nexloop_commercial_wakes'):
            with pytest.raises(psycopg.Error),admin.transaction():admin.execute(f'delete from {table}')


def test_governed_entry_handler_binds_with_the_principal_and_checks_the_body(linked,admin):
    """NX-028 registry handler (signature of design §15.2a); the registry row follows once 0150 is merged."""
    c,env=linked;commitment,_=fresh(c)
    handler=lambda body:admin.execute("select control.nexloop_commercial_bind_commitment_handler(%s,'real','human-principal','consumer',%s,%s::jsonb)",
        (TENANT,'intent-bind-1',json.dumps(body))).fetchone()[0]
    out=handler({'commitment_id':commitment,'connector_id':'synthetic-shop','record_kind':'order','external_id':'ord-1','statuses':['paid']})
    assert out['bound'] is True and out['evidence']==0 and out['intent_id']=='intent-bind-1'
    assert admin.execute('select bound_by,statuses from runtime.nexloop_commercial_commitment_bindings').fetchall()==[('human-principal',['paid'])]
    for bad in ({'commitment_id':commitment,'connector_id':'synthetic-shop','record_kind':'order','external_id':'ord-1','extra':1},
                {'commitment_id':'x','connector_id':'synthetic-shop','record_kind':'order','external_id':'ord-1'},
                {'commitment_id':commitment,'connector_id':'synthetic-shop','record_kind':'order','external_id':'ord-1','statuses':[]},
                {'commitment_id':commitment,'connector_id':'synthetic-shop','record_kind':'order','external_id':'ord-1','statuses':[1]}):
        with pytest.raises(psycopg.errors.InvalidParameterValue):handler(bad)
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_configurator'):
        assert admin.execute("select has_function_privilege(%s,'control.nexloop_commercial_bind_commitment_handler(text,text,text,text,text,jsonb)','execute')",(role,)).fetchone()==(False,)

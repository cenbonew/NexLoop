"""NX-027 slice 1 (M08; AT-011, AT-012): signed intake, verified records, corrections, late events, refunds, links, D1, D6."""
import json,threading
from datetime import UTC,datetime,timedelta
import psycopg
import pytest
from commercial_fixture import commercial_env,published_action,TENANT  # noqa: F401


def test_signed_event_creates_one_verified_record(commercial_env):
    c=commercial_env;c.connector()
    body=c.event('payment.succeeded','pay-1',amount=12900)
    assert c.post(body)['status']==202
    assert c.tick()[0]['created']==1
    record=c.record('payment','pay-1')
    p=record['properties']
    assert p['status']=='succeeded' and p['amount_minor']==12900 and p['currency']=='CNY' and p['verification_state']=='verified'
    assert p['data_mode']=='real' and p['link_state']=='unlinked' and p['consumer_ref'] is None and record['revision']==1


def receipts(c,world='real'):
    return dict(c['admin'].execute('select outcome,count(*) from runtime.nexloop_commercial_receipts where tenant_id=%s and world=%s group by outcome',(TENANT,world)).fetchall())


def events(c,world='real'):
    return c['admin'].execute('select count(*) from runtime.nexloop_commercial_events where tenant_id=%s and world=%s',(TENANT,world)).fetchone()[0]


def test_at012_same_signed_event_ten_times_is_one_business_change(commercial_env):
    c=commercial_env;c.connector()
    body=c.event('payment.succeeded','pay-10',amount=5000)
    outcomes=[c.post(body)['outcome'] for _ in range(10)]
    assert outcomes==['accepted']+['duplicate']*9
    assert receipts(c)=={'verified':1,'duplicate':9} and events(c)==1
    c.tick();c.tick()
    record=c.record('payment','pay-10')
    assert record['revision']==1 and record['properties']['event_count']==1 and record['properties']['amount_minor']==5000
    assert c['admin'].execute("select count(*) from ontology.objects where tenant_id=%s and type_name='CommercialRecord'",(TENANT,)).fetchone()[0]==1


def test_concurrent_first_arrivals_store_exactly_one_event(commercial_env):
    c=commercial_env;c.connector();body=c.event('order.paid','ord-c',amount=800)
    results=[];threads=[threading.Thread(target=lambda:results.append(c.post(body)['outcome'])) for _ in range(8)]
    for t in threads:t.start()
    for t in threads:t.join(20)
    assert sorted(results)==['accepted']+['duplicate']*7 and events(c)==1


def test_same_event_id_with_another_payload_is_a_conflict(commercial_env):
    c=commercial_env;c.connector()
    body=c.event('payment.succeeded','pay-x',amount=100)
    assert c.post(body)['outcome']=='accepted';c.tick()
    changed=dict(body,amount_minor=999)
    result=c.post(changed)
    assert result['status']==409 and result['outcome']=='conflict'
    assert c.record('payment','pay-x')['properties']['amount_minor']==100 and events(c)==1
    assert [(s.split(':')[0],r) for s,r,_ in c.exceptions()]==[('event','event_payload_conflict')]


def test_signature_window_connector_and_schema_failures_change_nothing(commercial_env):
    import time
    c=commercial_env;c.connector();body=c.event('payment.succeeded','pay-bad')
    assert c.post(body,signature='v1='+'0'*64)['outcome']=='rejected_signature'
    assert c.post(body,signature='nonsense')['outcome']=='rejected_signature'
    assert c.post(body,timestamp=str(int(time.time())-3600))['outcome']=='replay_window'
    assert c.post(body,timestamp='not-a-time')['outcome']=='replay_window'
    assert c.post(body,connector_id='no-such-shop',signature='v1='+'0'*64)['status']==404
    for change,reason in [({'amount_minor':12.5},'amount_minor'),({'amount_minor':-1},'amount_minor'),({'currency':'XXX'},'currency'),
            ({'currency':'EUR'},'currency'),({'type':'invoice.paid'},'type'),({'tenant_id':'synthetic-b'},'unknown_field'),
            ({'customer_ref':'pseudonym:'+'a'*32},'customer_ref'),({'occurred_at':'yesterday'},'occurred_at'),
            ({'related_kind':'payment','related_external_id':'p'},'refund_target')]:
        result=c.post(dict(body,**change))
        assert (result['status'],result['outcome'],result['reason'])==(400,'rejected_schema',reason),change
    future=dict(body,occurred_at=(datetime.now(UTC)+timedelta(hours=2)).isoformat())
    assert c.post(future)['reason']=='occurred_at_future'
    assert c.post(None,raw='[1,2]')['reason']=='not_an_object'
    assert events(c)==0 and c.tick()[0]=={'created':0,'edited':0,'unchanged':0,'late':0,'retry':0,'dead_lettered':0,'changed':0,'lease_lost':0}
    # A disabled connector accepts nothing; nothing of a rejected request is stored but its digest and outcome.
    from nexloop_eios import commercial
    commercial.disable_connector(c['configurator_dsn'],'synthetic-shop')
    assert c.post(body)['outcome']=='disabled' and events(c)==0
    stored=c['admin'].execute('select count(*) from runtime.nexloop_commercial_receipts where outcome in (%s,%s) and payload_digest is null',('replay_window','disabled')).fetchone()[0]
    assert stored==receipts(c)['replay_window']+receipts(c)['disabled']


def test_key_rotation_accepts_the_previous_key_only_within_its_window(commercial_env):
    c=commercial_env;c.connector(key_id='k1');old=c['keys']['synthetic-shop']
    c.connector(key_id='k2',rotation_seconds=60)
    from nexloop_eios import commercial
    import time
    body=c.event('order.created','ord-rot');stamp=str(int(time.time()));text=json.dumps(body)
    assert c.post(body,timestamp=stamp,signature=commercial.sign(old,stamp,text),raw=text)['outcome']=='accepted'
    c['admin'].execute("update control.nexloop_commercial_connectors set previous_valid_until=clock_timestamp()-interval '1 second' where connector_id='synthetic-shop'")
    body2=c.event('order.created','ord-rot-2');text2=json.dumps(body2)
    assert c.post(body2,timestamp=stamp,signature=commercial.sign(old,stamp,text2),raw=text2)['outcome']=='rejected_signature'
    assert c.post(body2)['outcome']=='accepted'


def test_late_older_event_never_moves_the_state_back_and_corrections_apply(commercial_env):
    c=commercial_env;c.connector();t0=datetime.now(UTC)-timedelta(minutes=10)
    assert c.post(c.event('payment.succeeded','pay-l',amount=3000,occurred_at=t0+timedelta(minutes=2),sequence=2))['outcome']=='accepted'
    c.tick()
    assert c.post(c.event('payment.pending','pay-l',amount=3000,occurred_at=t0,sequence=1))['outcome']=='accepted'  # arrives late
    out=c.tick()
    p=c.record('payment','pay-l')['properties']
    assert p['status']=='succeeded' and p['late_event_count']==1 and p['event_count']==2 and out[0]['late']==1
    dispositions=c['admin'].execute('select status,disposition from runtime.nexloop_commercial_events where tenant_id=%s order by received_sequence',(TENANT,)).fetchall()
    assert dispositions==[('succeeded','applied'),('pending','late')]
    # Provider amount correction: canonically latest amount, new revision; earlier events stay.
    assert c.post(c.event('payment.succeeded','pay-l',amount=2800,occurred_at=t0+timedelta(minutes=3),sequence=3))['outcome']=='accepted'
    c.tick();record=c.record('payment','pay-l')
    assert record['properties']['amount_minor']==2800 and record['revision']==3 and events(c)==3


def test_refunds_partial_full_over_and_without_payment(commercial_env):
    c=commercial_env;c.connector();t=datetime.now(UTC)-timedelta(minutes=5)
    c.post(c.event('payment.succeeded','pay-r',amount=10000,occurred_at=t));c.tick()
    c.post(c.event('refund.succeeded','ref-1',amount=3000,occurred_at=t+timedelta(minutes=1),related=('payment','pay-r')));c.tick()
    p=c.record('payment','pay-r')['properties'];refund=c.record('refund','ref-1')
    assert (p['status'],p['refunded_minor'])==('refunded_partial',3000) and refund['properties']['related_record_id']==c.record('payment','pay-r')['object_id']
    c.post(c.event('refund.succeeded','ref-2',amount=7000,occurred_at=t+timedelta(minutes=2),related=('payment','pay-r')));c.tick()
    assert (c.record('payment','pay-r')['properties']['status'],c.record('payment','pay-r')['properties']['refunded_minor'])==('refunded',10000)
    c.post(c.event('refund.succeeded','ref-3',amount=1,occurred_at=t+timedelta(minutes=3),related=('payment','pay-r')));c.tick()
    p=c.record('payment','pay-r')['properties']
    assert p['refunded_minor']==10001 and ('refund_exceeds_amount' in [r for _,r,_ in c.exceptions()])  # never truncated
    # A refund that arrives before its payment counts once the payment arrives; a refund of a failed payment is a conflict.
    c.post(c.event('refund.succeeded','ref-early',amount=500,occurred_at=t,related=('order','ord-late')));c.tick()
    c.post(c.event('order.paid','ord-late',amount=2000,occurred_at=t-timedelta(minutes=1)));c.tick()
    assert (c.record('order','ord-late')['properties']['status'],c.record('order','ord-late')['properties']['refunded_minor'])==('refunded_partial',500)
    c.post(c.event('payment.failed','pay-f',amount=900,occurred_at=t));c.post(c.event('refund.succeeded','ref-f',amount=900,occurred_at=t,related=('payment','pay-f')))
    c.tick()
    assert c.record('payment','pay-f')['properties']['status']=='failed' and 'refund_without_payment' in [r for _,r,_ in c.exceptions()]
    c.post(c.event('refund.succeeded','ref-usd',amount=1,currency='USD',occurred_at=t,related=('order','ord-late')));c.tick()
    assert c.record('order','ord-late')['properties']['refunded_minor']==500 and 'refund_currency_mismatch' in [r for _,r,_ in c.exceptions()]


def test_currency_change_of_one_record_is_a_conflict_not_a_sum(commercial_env):
    c=commercial_env;c.connector();t=datetime.now(UTC)-timedelta(minutes=5)
    c.post(c.event('order.created','ord-cur',amount=100,currency='CNY',occurred_at=t));c.post(c.event('order.paid','ord-cur',amount=15,currency='USD',occurred_at=t+timedelta(seconds=5)))
    c.tick();p=c.record('order','ord-cur')['properties']
    assert (p['currency'],p['amount_minor'],p['status'])==('CNY',100,'pending') and 'currency_changed' in [r for _,r,_ in c.exceptions()]


def test_at011_no_record_without_a_verified_event_and_the_guard_refuses_others(commercial_env):
    c=commercial_env;a=c['admin']
    # A customer's "I paid" is at most a statement Claim: there is no path from it to a CommercialRecord.
    props={'kind':'payment','external_id':'claimed','connector_id':'x','data_mode':'real','currency':'CNY','occurred_at':'2026-10-10T00:00:00.000000Z',
        'related_record_id':None,'verification_state':'verified','status':'succeeded','status_at':'2026-10-10T00:00:00.000000Z','amount_minor':1,
        'refunded_minor':0,'consumer_ref':None,'link_state':'unlinked','event_count':1,'late_event_count':0,'provider_sequence':None,'last_event_ref':'commercial-event:1'}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        a.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','CommercialRecord',%s,1,%s,'nexloop-action','commercial-forged',clock_timestamp(),clock_timestamp())''',(TENANT,'f'*64,json.dumps(props)))
    from nexloop_eios.object_actions import GovernedObjectCreator
    with pytest.raises(Exception):
        GovernedObjectCreator(c['worker'],c.session(),c['signer']).create(action_name='CommercialRecord.create',action_version=1,intent_id='commercial-forged',
            type_name='CommercialRecord',properties=props)
    assert a.execute("select count(*) from ontology.objects where type_name='CommercialRecord'").fetchone()[0]==0
    # An existing record cannot be edited to anything but its derived state, nor deleted.
    c.connector();c.post(c.event('payment.succeeded','pay-g',amount=10));c.tick();record=c.record('payment','pay-g')
    from nexloop_eios.object_edits import GovernedObjectEditor
    with pytest.raises(Exception):
        GovernedObjectEditor(c['worker'],c.session(),c['signer']).edit(action_name='CommercialRecord.edit',action_version=1,intent_id='commercial-forged-edit',
            type_name='CommercialRecord',object_id=record['object_id'],expected_revision=1,properties={'amount_minor':99999})
    with pytest.raises(psycopg.errors.CheckViolation):
        a.execute("update ontology.objects set properties=properties||'{\"status\":\"refunded\"}' where object_id=%s",(record['object_id'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):a.execute('delete from ontology.objects where object_id=%s',(record['object_id'],))
    assert c.record('payment','pay-g')['properties']['amount_minor']==10


def test_customer_link_and_unlink_rederive_the_consumer(commercial_env):
    c=commercial_env;c.connector();consumer=c.consumer()
    c.post(c.event('subscription.renewed','sub-1',amount=9900,customer='cust-9'));c.tick()
    assert c.record('renewal','sub-1')['properties']['link_state']=='unlinked'
    assert c.link('cust-9',consumer)=={'linked':True,'records':1};c.tick()
    p=c.record('renewal','sub-1')['properties']
    assert (p['consumer_ref'],p['link_state'])==(consumer,'linked')
    with pytest.raises(psycopg.errors.UniqueViolation):c.link('cust-9',c.consumer())
    with pytest.raises(psycopg.errors.InvalidParameterValue):c.link('cust-9','a'*64)  # not an existing Consumer
    c.unlink('cust-9');c.tick()
    assert c.record('renewal','sub-1')['properties']['link_state']=='unlinked'


def test_d1_test_connector_only_writes_the_test_world_and_real_needs_confirmation(commercial_env):
    c=commercial_env
    with pytest.raises(psycopg.errors.InvalidParameterValue):c.connector('shop-test-real',world='real',data_mode='test')
    with pytest.raises(psycopg.errors.InvalidParameterValue):c.connector('shop-real-test',world='test',data_mode='real')
    with pytest.raises(psycopg.errors.InvalidParameterValue):c.connector('shop-unconfirmed',world='real',data_mode='real',confirmation='')
    c.connector('shop-test',world='test',data_mode='test',confirmation=None)
    assert c.post(c.event('payment.succeeded','pay-t',amount=777),connector_id='shop-test')['outcome']=='accepted'
    c.tick('test')
    assert c.record('payment','pay-t',connector_id='shop-test',world='test')['properties']['data_mode']=='test'
    assert c['admin'].execute("select count(*) from ontology.objects where world='real' and type_name='CommercialRecord'").fetchone()[0]==0
    # The binding of a connector never changes.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):c.connector('shop-test',world='other',data_mode='test',confirmation=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        c['admin'].execute("insert into runtime.nexloop_commercial_events(tenant_id,world,data_mode,connector_id,source_event_id,event_type,record_kind,record_key,status,occurred_at,amount_minor,currency,customer_ref,payload_digest,signature_key_id) values(%s,'real','test','x','e','payment.succeeded','payment',%s,'succeeded',clock_timestamp(),1,'CNY','c',%s,'k')",(TENANT,'a'*64,'b'*64))


def test_d6_pseudonymization_keeps_money_and_chains_and_breaks_the_link(commercial_env):
    c=commercial_env;a=c['admin'];c.connector();consumer=c.consumer();c.link('cust-del',consumer)
    t=datetime.now(UTC)-timedelta(minutes=5)
    c.post(c.event('payment.succeeded','pay-d',amount=4000,customer='cust-del',occurred_at=t))
    c.post(c.event('refund.succeeded','ref-d',amount=1000,customer='cust-del',occurred_at=t+timedelta(minutes=1),related=('payment','pay-d')))
    c.tick()
    before=c.record('payment','pay-d')['properties']
    result=a.execute('select runtime.nexloop_commercial_pseudonymize_consumer(%s,%s,%s)',(TENANT,'real',consumer)).fetchone()[0]
    assert result=={'events':2,'records':2};c.tick()
    after=c.record('payment','pay-d')['properties']
    assert (after['consumer_ref'],after['link_state'])==(None,'pseudonymized')
    assert {k:v for k,v in after.items() if k not in ('consumer_ref','link_state')}=={k:v for k,v in before.items() if k not in ('consumer_ref','link_state')}
    refs={r[0] for r in a.execute('select distinct customer_ref from runtime.nexloop_commercial_events').fetchall()}
    assert len(refs)==1 and next(iter(refs)).startswith('pseudonym:')  # one pseudonym for the Consumer, random
    assert a.execute('select count(*) from control.nexloop_commercial_customer_links').fetchone()[0]==0
    assert not any('cust-del' in json.dumps(row,default=str) for row in a.execute('select * from runtime.nexloop_commercial_events').fetchall())
    # The same provider customer later: a new, unlinked record, never re-linked to the deleted Consumer.
    c.post(c.event('payment.succeeded','pay-d2',amount=10,customer='cust-del'));c.tick()
    assert c.record('payment','pay-d2')['properties']['link_state']=='unlinked'


def test_application_roles_cannot_configure_or_read_connector_keys(commercial_env):
    c=commercial_env;c.connector()
    def refused(pool,sql):
        with pool.connection() as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute(sql)
    refused(c['api_pool'],"select control.nexloop_commercial_configure_connector('synthetic-a','real','x','real','{payment.succeeded}','{CNY}','k','\\x00','c',0)")
    refused(c['worker'],'select key_material from control.nexloop_commercial_connectors')
    refused(c['worker'],"select authz.nexloop_commercial_ingest('synthetic-shop','1','v1=00','{}')")
    refused(c['api_pool'],'select * from runtime.nexloop_commercial_events')


def test_read_port_and_settings(commercial_env):
    c=commercial_env;c.connector();consumer=c.consumer();c.link('cust-r',consumer)
    c.post(c.event('order.paid','ord-read',amount=50,customer='cust-r'));c.tick()
    reader=c.reader()
    listed=reader.records(consumer)
    assert [r['properties']['external_id'] for r in listed]==['ord-read']
    one=reader.record(listed[0]['record_id'])
    assert [e['event_type'] for e in one['events']]==['order.paid'] and one['events'][0]['disposition']=='applied' and 'customer_ref' not in one['events'][0]
    assert reader.receipts()=={'verified':1}
    from nexloop_eios import commercial
    from commercial_fixture import SETTINGS
    text,digest=commercial.canonical_settings(SETTINGS)
    row=c['admin'].execute('select definition::text,definition_digest from runtime.nexloop_commercial_settings where version=1').fetchone()
    assert json.loads(row[0])==json.loads(text) and row[1]==digest
    bad=json.loads(SETTINGS.read_text());bad['financial_retention_days']=0
    path=c['tmp_path']/'bad.json';path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):commercial.load_settings(path)


def test_http_webhook_route_statuses(commercial_env):
    """The API route passes the raw body to the signed intake; public outcomes only, no payload or reason echoed."""
    import time
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from nexloop_eios import commercial
    from nexloop_eios.commercial_http import router
    c=commercial_env;c.connector()
    app=FastAPI();app.include_router(router(rate=0.001,burst=3));app.state.backend=SimpleNamespace(_pool=c['api_pool'])
    client=TestClient(app)
    body=json.dumps(c.event('order.paid','ord-http',amount=10));stamp=str(int(time.time()))
    headers={'content-type':'application/json','x-nexloop-timestamp':stamp,'x-nexloop-signature':commercial.sign(c['keys']['synthetic-shop'],stamp,body)}
    first=client.post('/api/v1/webhooks/commercial/synthetic-shop',content=body,headers=headers)
    again=client.post('/api/v1/webhooks/commercial/synthetic-shop',content=body,headers=headers)
    assert (first.status_code,first.json()['code'])==(202,'accepted') and (again.status_code,again.json()['code'])==(200,'duplicate')
    bad=client.post('/api/v1/webhooks/commercial/synthetic-shop',content=body,headers=dict(headers,**{'x-nexloop-signature':'v1='+'0'*64}))
    assert bad.status_code==401 and 'reason' not in bad.json()['details']
    assert client.post('/api/v1/webhooks/commercial/synthetic-shop',content=body,headers=dict(headers,**{'content-type':'text/plain'})).status_code==400
    assert client.post('/api/v1/webhooks/commercial/synthetic-shop',content=body,headers=headers).status_code==429  # per-connector bound (burst 3)
    big='x'*300000
    assert client.post('/api/v1/webhooks/commercial/other-shop',content=big,headers=dict(headers)).status_code==413

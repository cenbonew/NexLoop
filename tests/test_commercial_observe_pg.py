"""NX-027 human reads (D09 GET /commercial-observations, /costs, /metrics), CommercialRecord.observe:1, migration 0134.

An actual human browser-business session holding EXECUTE on the observe Action (seeded as trusted configuration, as
the goal fixture does) reads records, costs and key results; services are refused before any grant. The HTTP router is
exercised with the same human session (browser cookie plumbing is covered by the browser and context-audit tests).
Synthetic data only.
"""
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from eios.identity.errors import CredentialInvalid
from nexloop_eios.commercial import CommercialObserver
from nexloop_eios import commercial_observe_http as H
from nexloop_eios.commercial_observe_http import router
from nexloop_eios.context_engine.authority import ContextDenied,action_claims
from commercial_fixture import TENANT,commercial_env,published_action  # noqa: F401
from goal_fixture import authenticate_human,seed_human_owner
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_costs_pg import reserve_incentive,seed_incentive

ORIGIN='https://observe.invalid'


@pytest.fixture
def observed(commercial_env,admin,identity,uow):
    env=commercial_env
    browser=seed_human_owner(admin,env['api_pool'],['CommercialRecord.observe'],identity,uow)
    env.connector();consumer=env.consumer();env.link('cust-1',consumer)
    now=datetime.now(UTC)
    env.metric('revenue');env.kr('g-revenue','revenue',(now-timedelta(days=1),now+timedelta(days=1)))
    assert env.post(env.event('payment.succeeded','pay-1',amount=12000))['status']==202
    env.tick()
    seed_incentive(admin);reserve_incentive(admin,'coupon-1','300')
    human=authenticate_human(env['api_pool'],browser)
    return env,human,consumer


def test_human_reads_records_costs_and_key_results_and_services_are_refused(observed,admin):
    env,human,consumer=observed
    o=CommercialObserver(env['api_pool'],human,env['signer'])
    records=o.records()
    assert records['world']=='real' and len(records['records'])==1
    (r,)=records['records'];assert r['properties']['data_mode']=='real' and r['properties']['amount_minor']==12000
    assert [x['record_id'] for x in o.records(consumer)['records']]==[r['record_id']] and o.records('0'*64)['records']==[]
    detail=o.record(r['record_id']);assert detail['data_mode']=='real' and [e['status'] for e in detail['events']]==['succeeded']
    costs=o.costs()
    assert costs['world']=='real' and costs['costs']==[{'cost_kind':'discount','data_mode':'real','currency':'CNY','amount_unit':'minor',
        'amount':'300.00000000','entries':1,'units':'1'}] and costs['settlements']==[]
    assert [e['data_mode'] for e in o.cost_entries(cost_kind='discount')['entries']]==['real']
    kr=o.metric('g-revenue',1,'kr');assert kr['world']=='real' and Decimal(kr['value'])==12000
    with pytest.raises(psycopg.errors.InvalidParameterValue):o.metric('g-revenue',1,'BAD KEY')
    # A service holding no grant is refused by authority; with the human's own claims, by identity first.
    service=CommercialObserver(env['worker'],env.session(),env['signer'])
    with pytest.raises(ContextDenied):service.records()
    claims=action_claims(env['api_pool'],human,'CommercialRecord.observe')
    claims['definition']=admin.execute("select definition from control.nexloop_action_definitions where world='real' and resource_id='eios:action:CommercialRecord.observe:1' and active").fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        CommercialObserver(env['api_pool'],env.session(),env['signer'])._call({'verb':'records'},claims=claims)
    # Not callable outside the API role.
    assert admin.execute("select has_function_privilege('nexloop_domain_worker','authz.nexloop_commercial_observe(text,text,text,text,text)','execute')").fetchone()==(False,)


def test_same_origin_routes(observed):
    env,human,consumer=observed
    config=SimpleNamespace(origin=ORIGIN,application_id='synthetic-application')
    state={'session':SimpleNamespace(restricted=False)}
    def inspect(config,request):
        if state['session'] is None:raise CredentialInvalid('no session')
        return state['session']
    ports=lambda request,session:SimpleNamespace(observer=lambda:CommercialObserver(env['api_pool'],human,env['signer']))
    app=FastAPI();app.include_router(router(config,ports_for_browser=ports,inspect=inspect))
    with TestClient(app,base_url=ORIGIN) as client:
        h={'Origin':ORIGIN}
        got=client.get('/api/v1/commercial-observations',headers=h)
        assert got.status_code==200 and got.headers['cache-control']=='no-store' and got.json()['world']=='real'
        record_id=got.json()['records'][0]['record_id']
        assert client.get('/api/v1/commercial-observations/'+record_id,headers=h).json()['record_id']==record_id
        assert client.get('/api/v1/costs',headers=h).json()['costs'][0]['cost_kind']=='discount'
        assert client.get('/api/v1/costs/entries?cost_kind=discount',headers=h).json()['entries'][0]['entry_id']=='discount:coupon-1'
        assert Decimal(client.get('/api/v1/metrics/g-revenue/1/kr',headers=h).json()['value'])==12000
        # Every answer has exactly its published OpenAPI component shape.
        for path,model in (('/api/v1/commercial-observations',H.CommercialObservations),('/api/v1/commercial-observations/'+record_id,H.CommercialObservation),
                           ('/api/v1/costs',H.CostSummary),('/api/v1/costs/entries',H.CostEntries),('/api/v1/metrics/g-revenue/1/kr',H.KeyResultObservation)):
            model.model_validate(client.get(path,headers=h).json())
        H.ObserveError.model_validate(client.get('/api/v1/costs/entries?cost_kind=rent',headers=h).json())
        components=app.openapi()['components']['schemas']
        assert {'CommercialObservations','CommercialObservation','CostSummary','CostEntries','KeyResultObservation','ObserveError'}<=set(components)
        for path in ('/api/v1/commercial-observations?consumer_id=x','/api/v1/commercial-observations/nope','/api/v1/costs/entries?cost_kind=rent',
                     '/api/v1/costs/entries?run_id=x','/api/v1/metrics/G/1/kr','/api/v1/metrics/g-revenue/0/kr'):
            assert client.get(path,headers=h).status_code==422,path
        assert client.get('/api/v1/costs',headers={'Origin':'https://elsewhere.invalid'}).status_code==403
        assert client.post('/api/v1/costs',headers=h).status_code==405
        state['session']=SimpleNamespace(restricted=True);assert client.get('/api/v1/costs',headers=h).status_code==403
        state['session']=None;assert client.get('/api/v1/costs',headers=h).status_code==401

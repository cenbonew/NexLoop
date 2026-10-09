"""Micro benchmark (measurement only): one typical governed call, warmed, repeated.

Run through scripts/perf/run_profile.sh so the timing hooks attribute the time:
  scripts/perf/run_profile.sh micro scripts/perf/test_micro_authz.py
Uses the ordinary test fixtures (disposable PostgreSQL, published Consumer.create,
service credential); no product code is changed.
"""
import json,os,statistics,time,uuid
from pathlib import Path
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authorization_service
from nexloop_eios.object_actions import GovernedObjectCreator
from conftest import pg,admin  # noqa: F401  (fixtures)
from test_action_definitions import published_action  # noqa: F401

N=int(os.environ.get('NEXLOOP_MICRO_N','40'))


def _timed(fn,n):
    fn()  # warm caches/pool
    samples=[]
    for _ in range(n):
        start=time.perf_counter();fn();samples.append(1000*(time.perf_counter()-start))
    return {'n':n,'p50':statistics.median(samples),'p95':sorted(samples)[int(.95*(n-1))],'max':max(samples),'mean':statistics.mean(samples)}


def test_micro_governed_create_and_decision(published_action):
    reader,_,_=published_action;session=reader.session;pool=reader.pool
    creator=GovernedObjectCreator(pool,session,reader.signer)
    def create():creator.create(action_name='Consumer.create',action_version=1,intent_id='micro-'+uuid.uuid4().hex,type_name='Consumer',properties={})
    def decide():
        assert authorization_service(pool,session).decide(session.query(resource_id='eios:action:Consumer.create:1',
            resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)).allowed
    def definition():reader.get('Consumer.create',1)
    def roundtrip():
        with pool.connection() as c:c.execute('select 1').fetchone()
    def fact_load():
        with pool.connection() as c:
            c.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',(session.token_digest,session.world,'revision',['catalog'])).fetchone()
    result={'governed_create':_timed(create,N),'authorization_decision':_timed(decide,N*2),'action_definition_read':_timed(definition,N*2),
        'pool_select_1':_timed(roundtrip,N*10),'single_fact_load_sql':_timed(fact_load,N*10)}
    out=Path(os.environ['NEXLOOP_PERF_OUT']);out.mkdir(parents=True,exist_ok=True)
    (out/'micro.json').write_text(json.dumps(result,indent=1))
    print(json.dumps(result,indent=1))

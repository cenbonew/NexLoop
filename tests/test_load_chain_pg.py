"""NX-031 slice 2 on clean catalog PostgreSQL: manifest consumers set up and enrolled on the actual multi-consumer relay chain,
small scale. The production MessageRelay (run profile mode) claims every customer's message with that customer's own profile."""
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path

from psycopg.conninfo import make_conninfo
from nexloop_eios.backend import open_backend
from nexloop_eios.browser_identity import open_browser_identity
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from nexloop_eios.message_relay import MessageRelay
from nexloop_eios.run_credential_vault import RunCredentialVault
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from load_chain import LoadChain

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('load_synthetic',ROOT/'scripts/load/synthetic.py');S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)


@contextmanager
def chain_tenant(f,tmp_path):
    o=f['original'];p=o['paths']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_api'),artifact_root=tmp_path/'load-api',signing_key_file=p['backend_signing'],
                      signing_key_id='explicit-configuration') as api, open_browser_identity(make_conninfo(o['pg'],user='nexloop_identity')) as pool:
        yield api,PostgresBrowserSessionUnitOfWork(pool,tenant_id=o['tenant'],application_id=o['application'])


def test_manifest_consumers_enroll_and_the_relay_routes_each_with_its_own_profile(assembled_message,admin,tmp_path):
    f=assembled_message
    *records,trailer=[json.loads(x) for x in S.lines(31,3)]
    with chain_tenant(f,tmp_path) as (api,uow):
        chain=LoadChain(f,admin,api,uow)
        loaded=chain.load(records)
        assert loaded['consumers']==loaded['enrolled']==3 and loaded['messages']==trailer['totals']['messages']
        vault_dir=tmp_path/'vault';vault_dir.mkdir(mode=0o700)
        with RunCredentialVault(vault_dir) as vault:
            relay=MessageRelay(route=api.authenticate(chain.route_token,world='real'),source=api.authenticate(chain.source_token,world='real'),
                               planner=api.authenticate(chain.planner_token,world='real'),executor_token=chain.executor_token,vault=vault,run_profile=chain.run_profile)
            statuses=[relay.run_once() for _ in range(3)]
        assert statuses==['queued']*3,statuses
        people={p['consumer']:p for _,p in loaded['people']}
        for consumer,message in admin.execute('select consumer_id,message_id from runtime.nexloop_relay_claims').fetchall():
            props=admin.execute("select properties from ontology.objects where type_name='MessageAssignment' and properties->>'message_id'=%s",(message,)).fetchone()[0]
            assert props['consumer_id']==consumer and props['control_id']==people[consumer]['control']


def test_the_load_relay_binds_message_runs_with_context_v6_when_given_the_strategy(assembled_message,admin,tmp_path):
    """AT-060 B1: the load cells start the relay in run profile mode with the deployed built-in strategy (scripts/load/matrix.py
    MESSAGE_CONTEXT_STRATEGY); with the load chain's own Source (enrollment-derived READ, 0142) each message Run binds a v6 pack."""
    from test_context_v6_pg import STRATEGY,seed_strategy
    f=assembled_message
    *records,trailer=[json.loads(x) for x in S.lines(31,2)]
    with chain_tenant(f,tmp_path) as (api,uow):
        chain=LoadChain(f,admin,api,uow)
        assert chain.load(records)['enrolled']==2
        seed_strategy(admin,f['original']['tenant'],STRATEGY)
        vault_dir=tmp_path/'vault';vault_dir.mkdir(mode=0o700)
        with RunCredentialVault(vault_dir) as vault:
            relay=MessageRelay(route=api.authenticate(chain.route_token,world='real'),source=api.authenticate(chain.source_token,world='real'),
                               planner=api.authenticate(chain.planner_token,world='real'),executor_token=chain.executor_token,vault=vault,run_profile=chain.run_profile,
                               context_strategy='recent_plus_required')
            statuses=[relay.run_once() for _ in range(2)]
        assert statuses==['queued']*2,statuses
    versions=[r[0] for r in admin.execute("select pack_text::jsonb->>'schema_version' from runtime.nexloop_context_artifact_bindings where tenant_id=%s",(f['original']['tenant'],)).fetchall()]
    assert versions==['nexloop.context-pack.v6']*2,versions


def test_the_relay_records_each_step_of_a_queued_message(assembled_message,admin,tmp_path,monkeypatch):
    """Relay step timings (dispatcher 2026-10-13): one queued message records every step it went through, numbers only."""
    from nexloop_eios import observability
    from nexloop_eios.observability import RELAY_STEPS,StepTimings
    timings=StepTimings(RELAY_STEPS)
    monkeypatch.setattr(observability,'RELAY',timings)
    f=assembled_message
    *records,trailer=[json.loads(x) for x in S.lines(31,1)]
    with chain_tenant(f,tmp_path) as (api,uow):
        chain=LoadChain(f,admin,api,uow)
        chain.load(records)
        vault_dir=tmp_path/'vault';vault_dir.mkdir(mode=0o700)
        with RunCredentialVault(vault_dir) as vault:
            relay=MessageRelay(route=api.authenticate(chain.route_token,world='real'),source=api.authenticate(chain.source_token,world='real'),
                               planner=api.authenticate(chain.planner_token,world='real'),executor_token=chain.executor_token,vault=vault,run_profile=chain.run_profile,
                               max_backlog=100)
            assert relay.run_once()=='queued'
            first,_=timings.drain()
            while relay.run_once()=='queued':pass
            timings.drain()
            assert relay.run_once()=='idle' and timings.drain()[0]=={}  # an idle tick records nothing
    assert set(first)=={'backlog','claim','lookup','goal_create','step_create','assignment_create','vault_prepare','source_proofs','issue',
                        'effect_bind','context_prepare','message_bind','own_route','route_authorize','runtime_accept','ack','release','total'}
    assert all(s['calls']==1 for s in first.values())
    steps=sum(s['max_ms'] for name,s in first.items() if name!='total')
    assert abs(first['total']['max_ms']-steps)<=1.0+0.001*steps  # the steps add up to the total (rounding only)


def _relay(api,chain,vault):
    return MessageRelay(route=api.authenticate(chain.route_token,world='real'),source=api.authenticate(chain.source_token,world='real'),
                        planner=api.authenticate(chain.planner_token,world='real'),executor_token=chain.executor_token,vault=vault,run_profile=chain.run_profile)


@contextmanager
def second_backend(f,tmp_path):
    o=f['original'];p=o['paths']
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_api'),artifact_root=tmp_path/'load-api-2',signing_key_file=p['backend_signing'],
                      signing_key_id='explicit-configuration') as api:
        yield api


def test_two_relays_route_every_message_exactly_once(assembled_message,admin,tmp_path):
    """Relay processes 2-3 (dispatcher 2026-10-13): two relays with their own vaults and backends, running at the same time,
    never route one message twice. claim_next (0141) gives a consumer to one relay at a time (advisory lock and live lease),
    each message is claimed under its own lock and fence, and a message has at most one route (primary key)."""
    import threading,time
    f=assembled_message
    *records,trailer=[json.loads(x) for x in S.lines(31,4)]
    with chain_tenant(f,tmp_path) as (api,uow), second_backend(f,tmp_path) as api2:
        chain=LoadChain(f,admin,api,uow)
        loaded=chain.load(records)
        total=loaded['messages']
        assert loaded['enrolled']==4 and total>4
        tenant=f['original']['tenant']
        (tmp_path/'vault-1').mkdir(mode=0o700);(tmp_path/'vault-2').mkdir(mode=0o700)
        with RunCredentialVault(tmp_path/'vault-1') as v1, RunCredentialVault(tmp_path/'vault-2') as v2:
            a,b=_relay(api,chain,v1),_relay(api2,chain,v2)
            # A consumer whose message another relay holds is skipped: with one consumer pending, the second relay is idle.
            first=a.port.call('claim_next',lease_seconds=30)
            busy=first['consumer_id']
            others=[c for c, in admin.execute("select distinct cv.consumer_id from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv "
                    "on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id where b.tenant_id=%s and b.status='pending'",(tenant,)).fetchall()
                    if c!=busy]
            seen=b.port.call('claim_next',lease_seconds=30)
            assert seen is None or seen['consumer_id']!=busy and seen['message_id']!=first['message_id']
            assert seen is not None or not others
            for item,relay in ((first,a),(seen,b)):
                if item is not None:relay.port.call('yield',message_id=item['message_id'],fence=item['fence'])  # give the claim back
            # The two claims given back wait out their short backoff (0150 yield: 10 s); both relays keep going until no message
            # is pending (bounded).
            outcomes={'a':[],'b':[]};done=threading.Event()
            def drain(name,relay):
                while not done.is_set():
                    status=relay.run_once()
                    outcomes[name].append(status)
                    if status=='idle':time.sleep(0.2)
            threads=[threading.Thread(target=drain,args=('a',a)),threading.Thread(target=drain,args=('b',b))]
            for t in threads:t.start()
            deadline=time.monotonic()+90
            while time.monotonic()<deadline and admin.execute("select count(*) from runtime.nexloop_message_outbox where tenant_id=%s and status='pending'",(tenant,)).fetchone()[0]:
                time.sleep(0.5)
            done.set()
            for t in threads:t.join(60)
            assert not any(t.is_alive() for t in threads)
    queued=outcomes['a'].count('queued')+outcomes['b'].count('queued')
    assert queued==total and outcomes['a'].count('queued')>0 and outcomes['b'].count('queued')>0,outcomes  # both relays worked
    assert admin.execute('select count(*),count(distinct message_id),count(distinct run_id),count(distinct task_id) from runtime.nexloop_message_routes where tenant_id=%s',
                         (tenant,)).fetchone()==(total,)*4
    assert admin.execute("select count(*) from runtime.nexloop_message_outbox where tenant_id=%s and status='pending'",(tenant,)).fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.jobs j join runtime.nexloop_message_routes m on m.task_id=j.job_id where m.tenant_id=%s",(tenant,)).fetchone()==(total,)


def test_stage_compose_runs_two_relays_with_their_own_vaults_within_the_budget():
    """Connection budget option B (dispatcher 2026-10-13): the stage Compose runs message-relay and message-relay-2, each with its
    own vault and otherwise the same configuration; the budget declares 2 relay processes x 2 and api-browser-identity 3 per
    process (the API's --identity-pool-max), total within 60, and the deploy gate's container/budget comparison passes."""
    import yaml
    from nexloop_eios.deploy_checks import compose_budget
    from nexloop_eios import connection_budget
    stage=ROOT/'deploy/stage'
    compose=yaml.safe_load((stage/'compose.app.example.yaml').read_text())
    budget=json.loads((stage/'connection-budget.v1.json').read_text())
    first,second=compose['services']['message-relay'],compose['services']['message-relay-2']
    vault=lambda spec:[v for v in spec['volumes'] if v.endswith(':/var/lib/nexloop/relay-vault')]
    assert vault(first)==['${NEXLOOP_ROOT:?root}/data/relay-vault:/var/lib/nexloop/relay-vault']
    assert vault(second)==['${NEXLOOP_ROOT:?root}/data/relay-vault-2:/var/lib/nexloop/relay-vault']  # never shared
    assert {k:v for k,v in first.items() if k!='volumes'}=={k:v for k,v in second.items() if k!='volumes'}
    assert [v for v in first['volumes'] if v not in vault(first)]==[v for v in second['volumes'] if v not in vault(second)]
    declared={s['name']:s for s in budget['services']}
    assert declared['message-relay']['processes']==2 and declared['message-relay']['pool_max']==2
    assert declared['api-browser-identity']['pool_max']==3
    apis=[spec for name,spec in compose['services'].items() if spec.get('labels',{}).get('nexloop.budget-service','').startswith('api')]
    assert apis and all(spec['command'][spec['command'].index('--identity-pool-max')+1]=='3' for spec in apis)
    assert compose_budget(budget,compose)['checks']['compose_matches_budget']
    evaluated=connection_budget.evaluate(budget)
    assert evaluated['passed'] and evaluated['details']['application_pool_total']==59

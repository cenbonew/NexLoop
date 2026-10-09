"""NX-049 (0107): fewer repeated checks inside one activation statement, same decisions.

* Action-authority assertions use the O5b level-1 memo (0096): a repeated identical
  assertion in one statement runs one full check while the visible snapshot is unchanged,
  no new advisory lock was taken and there was no own write to an authority input; every
  clock condition is re-evaluated on each hit; denials are never memoized.
* The read memo also admits accepted-message-v1 (derived Message READ) claims: rule
  deactivation, Message deletion and claim expiry inside the statement are all seen.
* 0039: with the Run's execution lock contended, the original pre-check / wait / recheck
  sequence still runs and the activation succeeds after the wait.
Synthetic disposable PG only; the loop helper is test-owned and lives in public.
"""
import hashlib,json,threading,time
from datetime import UTC,datetime,timedelta

import psycopg,pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine.authority import action_claims
from authority_fixture import seed_authority
from test_action_definitions import published_action  # noqa: F401

ACTION_INNER='authz.nexloop_assert_action_authority_before_action_memo_v0106(text,text,jsonb)'
READ_INNER='authz.nexloop_assert_read_authority_before_read_memo_v0095(text,text,jsonb)'
LOOP="""
create or replace function public.nx049_loop(p_fn text,p_inner text,p_digest text,p_world text,p_claims jsonb,p_n int,p_sleep float8,p_between text)
returns jsonb language plpgsql as $$
declare i int;out jsonb:='[]';ok boolean;err text;t0 timestamptz;m0 bigint;
begin
 m0:=coalesce(pg_stat_get_xact_function_calls(p_inner::regprocedure),0);
 for i in 1..p_n loop
  if i>1 and p_between is not null then execute p_between;end if;
  t0:=clock_timestamp();
  begin execute format('select %s($1,$2,$3)',p_fn) using p_digest,p_world,p_claims;ok:=true;err:=null;
  exception when others then ok:=false;err:=sqlstate||' '||sqlerrm;end;
  out:=out||jsonb_build_object('t0',t0,'ok',ok,'err',err,'misses',coalesce(pg_stat_get_xact_function_calls(p_inner::regprocedure),0)-m0);
  if p_sleep>0 then perform pg_sleep(p_sleep);end if;
 end loop;
 return jsonb_build_object('results',out,'misses',coalesce(pg_stat_get_xact_function_calls(p_inner::regprocedure),0)-m0);
end $$;
"""


def looper(admin,fn,inner,digest):
    admin.execute(LOOP);admin.execute("set track_functions='pl'")
    def loop(value,n=5,sleep=0.0,between=None):
        with admin.transaction():
            return admin.execute('select public.nx049_loop(%s,%s,%s,%s,%s,%s,%s,%s)',(fn,inner,digest,'real',Jsonb(value),n,sleep,between)).fetchone()[0]
    return loop


# ---------------------------------------------------------------- action authority

@pytest.fixture
def action(published_action,admin):
    reader,_,_=published_action
    target='eios:action:Consumer.create:1'
    token,_=seed_authority(admin,'synthetic-a',target,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-nx049-action')
    session=authenticate_service(reader.pool,token,world='real')
    admin.execute("select set_config('eios.tenant_id','synthetic-a',false)")
    def claims(expires_in=None):
        value=action_claims(reader.pool,session,'Consumer.create')
        if expires_in is not None:value['expires_at']=(datetime.now(UTC)+timedelta(seconds=expires_in)).isoformat()
        return value
    return dict(admin=admin,session=session,target=target,claims=claims,
        loop=looper(admin,'authz.nexloop_assert_action_authority',ACTION_INNER,session.token_digest))


def test_action_assertion_repeated_in_one_statement_runs_one_full_check(action):
    result=action['loop'](action['claims']())
    assert [r['ok'] for r in result['results']]==[True]*5 and result['misses']==1
    # Kill switch: identical results, every call a full check; the binding is the same.
    admin=action['admin'];value=action['claims']()
    with admin.transaction():
        on=admin.execute('select authz.nexloop_assert_action_authority(%s,%s,%s)',(action['session'].token_digest,'real',Jsonb(value))).fetchone()[0]
    admin.execute('update authz.nexloop_read_memo_settings set enabled=false')
    try:
        off=action['loop'](action['claims']())
        assert [r['ok'] for r in off['results']]==[True]*5 and off['misses']==5
        with admin.transaction():
            plain=admin.execute('select authz.nexloop_assert_action_authority(%s,%s,%s)',(action['session'].token_digest,'real',Jsonb(value))).fetchone()[0]
    finally:admin.execute('update authz.nexloop_read_memo_settings set enabled=true')
    assert on==plain


def test_action_own_revocation_and_new_advisory_lock_are_barriers(action):
    value=action['claims']();admin=action['admin']
    assert action['loop'](value,n=3,between='select pg_advisory_xact_lock((random()*1e12)::bigint)')['misses']==3
    assert action['loop'](value,n=3,between='select 1')['misses']==1
    key=[action['session'].authentication.subject_principal_id,action['target']]
    grant=admin.execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s",(key,)).fetchone()[0]
    revoked=dict(grant);revoked['grants']=[]
    revoke="update authz.nexloop_authority_facts set payload='%s'::jsonb where fact_kind='grants' and entity_key=array['%s','%s']"%(json.dumps(revoked),*key)
    result=action['loop'](value,n=3,between=revoke)
    assert [r['ok'] for r in result['results']]==[True,False,False],result


def test_action_claims_expiry_is_rechecked_on_every_hit_and_denials_are_not_memoized(action):
    value=action['claims'](expires_in=0.6)
    result=action['loop'](value,n=8,sleep=0.15)
    deadline=datetime.fromisoformat(value['expires_at'])
    oks=[(datetime.fromisoformat(r['t0']),r['ok']) for r in result['results']]
    assert any(ok for _,ok in oks) and all(not ok for t0,ok in oks if t0>=deadline),oks
    assert sum(ok for _,ok in oks)>result['misses']
    forged=dict(action['claims']());forged['principal_id']='synthetic-a-someone-else'
    denied=action['loop'](forged,n=3)
    assert [r['ok'] for r in denied['results']]==[False]*3


# ---------------------------------------------------------------- accepted-message READ

from test_context_artifacts import context_message, assembled_message, business_plan, configured  # noqa: F401,E402
from test_message_read_derivation import envelope, parts  # noqa: E402


@pytest.fixture
def derived(context_message,admin):
    f=context_message;pool,session,_=parts(f)
    admin.execute("select set_config('eios.tenant_id',%s,false)",(f['original']['tenant'],))
    claims=json.loads(envelope(f)['text'])
    assert claims['derivation']=='accepted-message-v1'
    return dict(f=f,admin=admin,session=session,claims=claims,
        loop=looper(admin,'authz.nexloop_assert_read_authority',READ_INNER,session.token_digest))


def test_derived_message_read_is_memoized_and_rule_deactivation_is_seen(derived):
    result=derived['loop'](derived['claims'])
    assert [r['ok'] for r in result['results']]==[True]*5 and result['misses']==1
    principal=derived['session'].authentication.subject_principal_id
    deactivate=("update authz.nexloop_authority_facts set payload=payload||'{\"active\":false}'::jsonb "
        "where fact_kind='message_read_rule' and entity_key=array['%s']"%principal)
    result=derived['loop'](derived['claims'],n=3,between=deactivate)
    assert [r['ok'] for r in result['results']]==[True,False,False],result


def test_derived_message_deletion_inside_the_statement_is_seen(derived):
    delete="delete from ontology.objects where type_name='Message' and object_id='%s'"%derived['f']['message']['id']
    result=derived['loop'](derived['claims'],n=3,between=delete)
    assert [r['ok'] for r in result['results']]==[True,False,False],result


# ---------------------------------------------------------------- 0039 contended lock

from test_runtime_atomic_accept import accepted_input  # noqa: F401,E402
from test_runtime_authority import authority  # noqa: F401,E402
from test_runtime_activation import synthetic_credentials  # noqa: F401,E402


def test_contended_execution_lock_keeps_the_original_wait_and_recheck(accepted_input,admin,pg):
    api,worker,args,issued=accepted_input;api.accept_runtime_event(**args)
    task=worker.claim_task(queue='operations',lease_seconds=60)
    with psycopg.connect(make_conninfo(pg,user='nexloop_api')) as other,other.transaction():
        other.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('nexloop-runtime-execution:'+issued.run_id,))
        result={}
        def create():
            result['value']=worker.create_runtime_activation(queue='operations',task_id=task['task_id'],fence=task['fence'],
                run_id=issued.run_id,command=args['command'],input=args['input'],owner_epoch=1)
        waiter=threading.Thread(target=create);waiter.start()
        deadline=time.monotonic()+10
        while admin.execute("select count(*) from pg_locks where locktype='advisory' and not granted").fetchone()[0]==0 and time.monotonic()<deadline:time.sleep(0.02)
        assert waiter.is_alive()  # waiting for the execution lock (try-lock did not take the fast path)
        time.sleep(0.2)
    waiter.join(30)
    assert not waiter.is_alive() and result['value']['ever_execution_authorized'] is False and result['value']['activation_ref'].startswith('activation_')

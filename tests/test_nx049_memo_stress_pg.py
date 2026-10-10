"""NX-049 (0107) stress and mutation evidence for the widened O5b memo admission.

Two admissions are new: action-authority assertions and accepted-message-v1 (derived
Message READ) assertions. Each is stressed as the O5b revocation test was (>=200
revocations per kind, concurrent asserters reusing resolved claims across statements),
with three statement modes so that every invalidation path is exercised:
  * plain:     only concurrent commits by other sessions (the snapshot is in the key);
  * own write: the revocation is written inside the asserting statement and rolled back
               afterwards (statement-level invalidation triggers);
  * expiry:    claims that expire in the middle of the statement (clock re-check on hits).
Violation (per assertion, stricter than per statement):
  * allowed although a revocation committed after the claims were resolved and before the
    assertion started (epoch-bound kinds: grants, rules);
  * allowed while the whole assertion lies inside a deny window (Message deleted / moved
    out of its Conversation, committed and not yet being restored);
  * allowed at or after the claims deadline; allowed after an own revocation.
Mutation controls run the same test, shortened, with one safeguard removed (the clock
re-check on hits, or the invalidation triggers) and must report violations. (Removing the
snapshot from the memo key is not a meaningful control for these kinds: every full check
share-locks the rule/grant fact, the Message row and its conversation row until the end of
the transaction, so no other session can commit a revocation, deletion or move while an
asserting transaction is open; epoch-bound kinds are additionally caught by the directory
hash re-check on every hit.) Synthetic disposable PG only.
"""
import json,re,threading,time
from datetime import UTC,datetime,timedelta

import psycopg,pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine.authority import action_claims
from nexloop_eios.message_read import derived_message_read_envelope,message_read_basis
from authority_fixture import seed_authority
from test_action_definitions import published_action  # noqa: F401
from test_context_artifacts import context_message, assembled_message, business_plan, configured  # noqa: F401

LOOP="""
create or replace function public.nx049_stress_loop(p_fn text,p_digest text,p_claims jsonb,p_n int,p_sleep float8,p_between text)
returns jsonb language plpgsql as $$
declare i int;out jsonb:='[]';ok boolean;t0 timestamptz;
begin
 for i in 1..p_n loop
  if i>1 and p_between is not null then execute p_between;end if;
  t0:=clock_timestamp();
  begin execute format('select %s($1,$2,$3)',p_fn) using p_digest,'real',p_claims;ok:=true;
  exception when others then ok:=false;end;
  out:=out||jsonb_build_object('i',i,'t0',t0,'t1',clock_timestamp(),'ok',ok);
  if p_sleep>0 then perform pg_sleep(p_sleep);end if;
 end loop;
 return out;
end $$;
"""
EXPIRY=re.compile(r"\s*and \(p_claims->>'expires_at'\)::timestamptz>pg_catalog\.clock_timestamp\(\)")
IDENTITY=re.compile(r"begin\s+v_identity:=authz\.nexloop_service_identity_snapshot\(p_digest,p_world\);.*?end;",re.S)


def mutate(admin,mutation,wrapper,tables):
    """Remove one safeguard; returns an undo callable."""
    if mutation is None:return lambda:None
    if mutation=='no_expiry_recheck':
        original=admin.execute('select pg_get_functiondef(%s::regprocedure)',(wrapper,)).fetchone()[0]
        mutated,count=EXPIRY.subn('',original)
        assert count==2,count
        admin.execute(mutated)
        return lambda:admin.execute(original)
    if mutation in ('no_trigger','no_trigger_no_identity_recheck'):
        # Every memo invalidation trigger (0096), not only the directly written table's.
        tables=[r[0] for r in admin.execute("select tgrelid::regclass::text from pg_trigger where tgname='nexloop_read_memo_invalidate'").fetchall()]
        assert len(tables)>=20,tables
        for table in tables:admin.execute(f'alter table {table} disable trigger nexloop_read_memo_invalidate')
        undo=[lambda:[admin.execute(f'alter table {table} enable trigger nexloop_read_memo_invalidate') for table in tables]]
        if mutation=='no_trigger_no_identity_recheck':
            original=admin.execute('select pg_get_functiondef(%s::regprocedure)',(wrapper,)).fetchone()[0]
            mutated,count=IDENTITY.subn('v_ok:=true;',original)
            assert count==1,count
            admin.execute(mutated);undo.append(lambda:admin.execute(original))
        return lambda:[u() for u in undo]
    if mutation=='no_snapshot_in_key':
        key='authz.nexloop_read_memo_key(text,text,jsonb)'
        original=admin.execute('select pg_get_functiondef(%s::regprocedure)',(key,)).fetchone()[0]
        mutated=original.replace('pg_catalog.pg_current_snapshot()::text,','')
        assert mutated!=original
        admin.execute(mutated)
        return lambda:admin.execute(original)
    raise ValueError(mutation)


def run(dsn,*,fn,tenant,resolve,revokers,own_revoke,cycles,asserters=4,deny_windows=False,modes=(0,1,2),duration=None):
    """Concurrent revokers + asserters; returns (summary, violations)."""
    events={'epoch':[],'windows':[]};lock=threading.Lock();stop=threading.Event();errors=[];records=[];aborted=[0]
    def now(c):return c.execute('select clock_timestamp()').fetchone()[0]
    def revoker(revoke,restore,epoch):
        try:
            with psycopg.connect(dsn,autocommit=True) as c:
                c.execute("select set_config('eios.tenant_id',%s,false)",(tenant,))
                def write(sql):
                    for _ in range(20):
                        try:c.execute(sql);return
                        except (psycopg.errors.DeadlockDetected,psycopg.errors.LockNotAvailable):time.sleep(0.01)
                    raise RuntimeError('revoker could not write')
                for _ in range(cycles):
                    write(revoke);committed=now(c)
                    time.sleep(0.02)
                    before_restore=now(c);write(restore);restored=now(c)
                    with lock:
                        if epoch:events['epoch']+= [committed,restored]
                        else:events['windows'].append((committed,before_restore))
                    time.sleep(0.03)
        except Exception as error:errors.append(('revoker',repr(error)))
    def asserter(index):
        try:
            with psycopg.connect(dsn,autocommit=True) as c:
                c.execute("select set_config('eios.tenant_id',%s,false)",(tenant,))
                mode=modes[index%len(modes)];k=0
                while not stop.is_set():
                    k+=1
                    try:digest,value=resolve()
                    except Exception:time.sleep(0.005);continue
                    resolved_at=now(c)
                    if mode==2:value=dict(value,expires_at=(datetime.now(UTC)+timedelta(milliseconds=30)).isoformat())
                    for _ in range(3):  # the same claims across later revocations
                        try:
                            if mode==1:
                                c.execute('begin')
                                # The transaction already has its xid: the own revocation below changes
                                # no snapshot, so only the invalidation trigger can stop a memo hit.
                                c.execute('select pg_current_xact_id()')
                                try:out=c.execute('select public.nx049_stress_loop(%s,%s,%s,4,0.004,%s)',(fn,digest,Jsonb(value),own_revoke)).fetchone()[0]
                                finally:c.execute('rollback')  # the own revocation never commits
                            else:
                                with c.transaction():
                                    out=c.execute('select public.nx049_stress_loop(%s,%s,%s,6,0.008,null)',(fn,digest,Jsonb(value))).fetchone()[0]
                        except (psycopg.errors.DeadlockDetected,psycopg.errors.LockNotAvailable,psycopg.errors.QueryCanceled):
                            # The test's own writers and share-lockers can deadlock each other: the
                            # aborted statement serves no allow at all; it is counted, not judged.
                            with lock:aborted[0]+=1
                            continue
                        records.append((mode,resolved_at,datetime.fromisoformat(value['expires_at']),out))
                        time.sleep(0.01)
        except Exception as error:errors.append(('asserter',repr(error)))
    threads=[threading.Thread(target=revoker,args=r) for r in revokers]
    workers=[threading.Thread(target=asserter,args=(i,)) for i in range(asserters)]
    for t in workers+threads:t.start()
    for t in threads:t.join(600)
    if duration:time.sleep(duration)
    stop.set()
    for t in workers:t.join(120)
    assert not errors,errors[:3]
    violations=[];allowed=0;assertions=0;stale=0
    for mode,resolved_at,deadline,out in records:
        for row in out:
            assertions+=1;t0=datetime.fromisoformat(row['t0']);t1=datetime.fromisoformat(row['t1'])
            if not row['ok']:continue
            allowed+=1
            epoch_stale=any(resolved_at<r<t0 for r in events['epoch'])
            stale+=epoch_stale
            if (epoch_stale or t0>=deadline or (mode==1 and row['i']>1)
                    or (deny_windows and any(a<t0 and t1<b for a,b in events['windows']))):
                violations.append((mode,str(resolved_at),row))
    summary=dict(revocations=len(events['epoch'])//2+len(events['windows']),statements=len(records),assertions=assertions,allowed=allowed,
        own_write_statements=sum(1 for m,*_ in records if m==1),expiry_statements=sum(1 for m,*_ in records if m==2),
        aborted_statements=aborted[0],epoch_stale_allowed=stale,violations=len(violations))
    return summary,violations


# ---------------------------------------------------------------- action authority

@pytest.fixture
def action(published_action,admin,pg):
    reader,_,_=published_action
    target='eios:action:Consumer.create:1'
    token,_=seed_authority(admin,'synthetic-a',target,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-nx049-stress')
    session=authenticate_service(reader.pool,token,world='real')
    key=[session.authentication.subject_principal_id,target]
    grant=admin.execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s",(key,)).fetchone()[0]
    revoked=dict(grant);revoked['grants']=[]
    where="fact_kind='grants' and entity_key=array['%s','%s']"%tuple(key)
    admin.execute(LOOP)
    def resolve():
        s=authenticate_service(reader.pool,token,world='real')
        return s.token_digest,action_claims(reader.pool,s,'Consumer.create')
    return dict(admin=admin,dsn=pg,resolve=resolve,
        revoke="update authz.nexloop_authority_facts set payload='%s'::jsonb where %s"%(json.dumps(revoked),where),
        restore="update authz.nexloop_authority_facts set payload='%s'::jsonb where %s"%(json.dumps(grant),where))


def quiet(mutation):
    """Mutation runs isolate the removed safeguard: for the trigger, a single asserter with
    own-write statements only and no concurrent writers (nothing else can change its snapshot);
    for the clock re-check, expiring claims only, so frequent concurrent revocations cannot
    hide the expired hits."""
    if mutation in ('no_trigger','no_trigger_no_identity_recheck'):return dict(revokers_on=False,asserters=1,modes=(1,),duration=8)
    # The clock re-check is isolated likewise: expiring claims only, no concurrent writers.
    if mutation=='no_expiry_recheck':return dict(revokers_on=False,asserters=2,modes=(2,),duration=8)
    return dict(revokers_on=True,asserters=6,modes=(0,1,2),duration=None)


MUTATIONS=[None,'no_expiry_recheck','no_trigger']


@pytest.mark.parametrize('mutation',MUTATIONS+['no_trigger_no_identity_recheck'])
def test_action_memo_never_serves_a_revoked_or_expired_allow(action,mutation):
    """Every action input write also advances the authority epoch (or changes the live
    identity), so a hit after an own revocation is refused by the directory-hash/identity
    re-check even without the invalidation trigger: 'no_trigger' alone must still give zero
    violations (defence in depth), and only removing both layers lets a stale allow through."""
    a=action
    undo=mutate(a['admin'],mutation,'authz.nexloop_assert_action_authority(text,text,jsonb)',['authz.nexloop_authority_facts'])
    try:
        q=quiet(mutation)
        summary,violations=run(a['dsn'],fn='authz.nexloop_assert_action_authority',tenant='synthetic-a',resolve=a['resolve'],
            revokers=[(a['revoke'],a['restore'],True)] if q['revokers_on'] else [],own_revoke=a['revoke'],cycles=200 if mutation is None else 40,
            asserters=q['asserters'],modes=q['modes'],duration=q['duration'])
    finally:undo()
    print('NX049_ACTION_STRESS',mutation,summary)
    if mutation=='no_trigger':
        assert summary['own_write_statements']>=100 and violations==[],violations[:5]
    elif mutation is None:
        assert summary['revocations']>=200 and summary['allowed']>0 and summary['own_write_statements']>0 and summary['expiry_statements']>0
        assert violations==[],violations[:5]
    else:
        assert violations,('mutation not detected',mutation,summary)


# ---------------------------------------------------------------- accepted-message READ

@pytest.fixture
def message(context_message,admin,pg):
    f=context_message;tenant=f['original']['tenant'];mid=f['message']['id'];conversation=f['message']['conversation_id']
    admin.execute("select set_config('eios.tenant_id',%s,false)",(tenant,))
    principal=f['source']._session.authentication.subject_principal_id
    other='f'*64
    admin.execute("""insert into runtime.nexloop_conversations select (jsonb_populate_record(null::runtime.nexloop_conversations,
        to_jsonb(c)||jsonb_build_object('conversation_id',%s::text,'idempotency_key','nx049-stress-other'))).* from runtime.nexloop_conversations c
        where tenant_id=%s and conversation_id=%s""",(other,tenant,conversation))
    message_row=admin.execute("select to_jsonb(o) from ontology.objects o where type_name='Message' and object_id=%s",(mid,)).fetchone()[0]
    admin.execute(LOOP)
    def resolve():
        services=f['backend'].authenticate(f['source_token'],world='real')
        pool,session,signer=services._backend._pool,services._session,services._backend._signer
        basis=message_read_basis(pool,session,mid)
        if basis.get('mode')!='derived':raise RuntimeError('not derived now')
        return session.token_digest,json.loads(derived_message_read_envelope(pool,session,signer,mid,basis)['text'])
    rule="fact_kind='message_read_rule' and entity_key=array['%s']"%principal
    restore_message="insert into ontology.objects select (jsonb_populate_record(null::ontology.objects,'%s'::jsonb)).*"%json.dumps(message_row).replace("'","''")
    return dict(admin=admin,dsn=pg,tenant=tenant,resolve=resolve,
        rule_off="update authz.nexloop_authority_facts set payload=payload||'{\"active\":false}'::jsonb where "+rule,
        rule_on="update authz.nexloop_authority_facts set payload=payload||'{\"active\":true}'::jsonb where "+rule,
        delete="delete from ontology.objects where type_name='Message' and object_id='%s'"%mid,restore_message=restore_message,
        move="update runtime.nexloop_conversation_messages set conversation_id='%s' where message_id='%s'"%(other,mid),
        move_back="update runtime.nexloop_conversation_messages set conversation_id='%s' where message_id='%s'"%(conversation,mid))


@pytest.mark.parametrize('mutation',MUTATIONS)
def test_derived_message_memo_never_serves_a_revoked_deleted_or_moved_allow(message,mutation):
    m=message
    undo=mutate(m['admin'],mutation,'authz.nexloop_assert_read_authority(text,text,jsonb)',
        ['authz.nexloop_authority_facts','ontology.objects','runtime.nexloop_conversation_messages'])
    # Own writes inside a statement: deletion and leaving the conversation advance no epoch,
    # so the invalidation triggers are their only guard within the transaction (rolled back).
    # The rule kind is epoch-bound and is exercised by the concurrent revoker.
    own=m['delete']+";"+m['move']
    try:
        q=quiet(mutation)
        summary,violations=run(m['dsn'],fn='authz.nexloop_assert_read_authority',tenant=m['tenant'],resolve=m['resolve'],
            revokers=[(m['rule_off'],m['rule_on'],True),(m['delete'],m['restore_message'],False),(m['move'],m['move_back'],False)] if q['revokers_on'] else [],
            own_revoke=own,cycles=200 if mutation is None else 40,asserters=q['asserters'],deny_windows=True,modes=q['modes'],duration=q['duration'])
    finally:undo()
    print('NX049_MESSAGE_STRESS',mutation,summary)
    if mutation is None:
        assert summary['revocations']>=600 and summary['allowed']>0 and summary['own_write_statements']>0 and summary['expiry_statements']>0
        assert violations==[],violations[:5]
    else:
        assert violations,('mutation not detected',mutation,summary)

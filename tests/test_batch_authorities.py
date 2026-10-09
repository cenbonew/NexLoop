"""O2b: batched per-object authority (one transaction, one fact round trip, each
target still judged by the EIOS resolver on its own).

Mirrors the O2a equivalence/negative set on the batch path and adds batch-specific
checks: SQL batch == single snapshots, per-target failure isolation, rejected keys
falling back to the single read, and the object READ projection end to end.
"""
from datetime import UTC,datetime,timedelta
import json,threading,time
import psycopg
import pytest
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
import nexloop_eios.authorization as A
from nexloop_eios.authorization import authenticate_service,authority_request_scope,resolve_authorities,resolve_authority
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_reads import AuthorizedObjectReader
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action  # noqa: F401
from test_identity_fact_memo import ALLOWED,EXPIRED,OUTSIDE_APP,MISSING,TARGETS,varied  # noqa: F401


def query(session,target,kind=ResourceType.ACTION,op=Operation.EXECUTE):
    return session.query(resource_id=target,resource_type=kind,operation=op)


def single(pool,session,target):
    entries=[]
    try:
        d=AuthorizationDecisionService().decide_resolved(resolve_authority(pool,session,query(session,target),entries))
        return (d.allowed,d.authoritative,d.expires_at,sorted((e['kind'],tuple(e['key']),e['record_hash']) for e in entries))
    except (AuthorizationUnavailable,F.AuthorizationFactDenied) as error:return ('error',type(error).__name__)


def batched(pool,session,targets):
    out=[]
    for context,entries,error in resolve_authorities(pool,session,[query(session,t) for t in targets]):
        if error is not None:out.append(('error',type(error).__name__));continue
        try:d=AuthorizationDecisionService().decide_resolved(context)
        except (AuthorizationUnavailable,F.AuthorizationFactDenied) as e:out.append(('error',type(e).__name__));continue
        out.append((d.allowed,d.authoritative,d.expires_at,sorted((e['kind'],tuple(e['key']),e['record_hash']) for e in entries)))
    return out


@pytest.fixture
def trips(monkeypatch):
    calls={'batch':0,'single':0}
    original_prefetch=A.PostgresAuthorityUnitOfWork.prefetch
    def prefetch(self,keys):
        calls['batch']+=1;return original_prefetch(self,keys)
    original_fetch=A.PostgresAuthorityUnitOfWork._fetch
    def fetch(self,kind,key):
        if self._prefetched.get((kind,tuple(key)),False) is False:calls['single']+=1
        return original_fetch(self,kind,key)
    monkeypatch.setattr(A.PostgresAuthorityUnitOfWork,'prefetch',prefetch)
    monkeypatch.setattr(A.PostgresAuthorityUnitOfWork,'_fetch',fetch)
    return calls


def test_batch_equals_single_outcome_hashes_expiry_and_isolates_failures(varied,trips):
    reader,session,_=varied
    plain=[single(reader.pool,session,t) for t in TARGETS]
    trips.update(batch=0,single=0)
    together=batched(reader.pool,session,TARGETS)
    assert together==plain  # allow/deny/error per target, authoritative, expiry, proof hashes
    assert [x[0] for x in together[:2]]==[True,True]  # denied/missing targets do not affect the others
    assert trips['batch']==1  # one round trip for the predictable facts


def test_sql_batch_equals_single_snapshots_and_rejects_like_single(varied,admin):
    reader,session,_=varied;auth=session.authentication
    keys=[{'kind':'subject','key':[auth.subject_id]},{'kind':'grants','key':[auth.subject_principal_id,ALLOWED[0]]},
          {'kind':'grants','key':[auth.subject_principal_id,'eios:action:Nope:1']},{'kind':'agent','key':['eios:agent:not-mine']}]
    with reader.pool.connection() as c:
        rows=c.execute('select authz.nexloop_load_authority_facts(%s,%s,%s::jsonb)',(session.token_digest,'real',json.dumps(keys))).fetchone()[0]
        assert [r['status'] for r in rows]==['ok','ok','missing','error']
        for key,row in zip(keys[:2],rows[:2]):
            single_row=c.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',(session.token_digest,'real',key['kind'],key['key'])).fetchone()[0]
            assert row['value']==single_row
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',(session.token_digest,'real','agent',['eios:agent:not-mine']))
        c.rollback()
        with pytest.raises(psycopg.Error):  # malformed batch is rejected outright
            c.execute('select authz.nexloop_load_authority_facts(%s,%s,%s::jsonb)',(session.token_digest,'real','{"kind":"subject"}'))


@pytest.mark.parametrize('change',['membership_suspended','grant_revoked'])
def test_revocation_denies_in_next_request_and_inside_request(varied,admin,change):
    reader,session,token=varied;auth=session.authentication
    with authority_request_scope():assert [x[0] for x in batched(reader.pool,session,ALLOWED)]==[True,True]
    if change=='membership_suspended':
        replace_fact(admin,'synthetic-a','membership',[auth.subject_id,auth.subject_principal_id],F.MembershipFacts,status='suspended')
    else:
        replace_fact(admin,'synthetic-a','grants',[auth.subject_principal_id,ALLOWED[1]],F.GrantFacts,grants=[])
    with authority_request_scope():  # old session: refused before any batch fact is used
        assert all(x[0]=='error' for x in batched(reader.pool,session,list(ALLOWED)+[EXPIRED]))
    fresh=authenticate_service(reader.pool,token,world='real')
    with authority_request_scope():
        result=batched(reader.pool,fresh,ALLOWED)
    if change=='membership_suspended':assert all(x[0] is not True for x in result)
    else:assert result[0][0] is True and result[1][0] is not True  # only the revoked target fails


def test_identity_expiry_judged_per_target_in_batch(published_action,admin):
    reader,_,_=published_action
    session,token=seed_multi_authority(admin,reader.pool,[(t,ResourceType.ACTION,Operation.EXECUTE) for t in ALLOWED],identity_suffix='-batch-expiry')
    auth=session.authentication
    replace_fact(admin,'synthetic-a','membership',[auth.subject_id,auth.subject_principal_id],F.MembershipFacts,
        valid_until=(datetime.now(UTC)+timedelta(seconds=2)).isoformat())
    session=authenticate_service(reader.pool,token,world='real')
    first=batched(reader.pool,session,ALLOWED)
    assert all(x[0] is True and x[2]<=datetime.now(UTC)+timedelta(seconds=2) for x in first)  # proofs never outlive membership
    time.sleep(2.2)
    assert all(x[0] is not True for x in batched(reader.pool,session,ALLOWED))


@pytest.mark.parametrize('variant',['principal','world','tenant'])
def test_batches_never_shared_across_principal_world_tenant(published_action,admin,variant):
    reader,_,_=published_action
    _,token_a=seed_multi_authority(admin,reader.pool,[(t,ResourceType.ACTION,Operation.EXECUTE) for t in ALLOWED],identity_suffix='-batch-a')
    options={'principal':dict(identity_suffix='-batch-b'),'world':dict(identity_suffix='-batch-sim',world='simulation'),
        'tenant':dict(identity_suffix='-batch-t',tenant='synthetic-b')}[variant]
    if variant=='tenant':admin.execute("insert into control.nexloop_tenants(tenant_id,status) values('synthetic-b','active') on conflict do nothing")
    _,token_b=seed_multi_authority(admin,reader.pool,[(ALLOWED[0],ResourceType.ACTION,Operation.EXECUTE)],**options)
    a=authenticate_service(reader.pool,token_a,world='real');b=authenticate_service(reader.pool,token_b,world=options.get('world','real'))
    with authority_request_scope():
        ra=batched(reader.pool,a,ALLOWED);rb=batched(reader.pool,b,ALLOWED)
    assert [x[0] for x in ra]==[True,True]
    assert rb[0][0] is True and rb[1][0] is not True  # b has no grant on the second target; not served from a
    b_principal=b.authentication.subject_principal_id
    assert all(key[0]!='grants' or key[1][0]==b_principal for key in rb[0][3])


def test_concurrent_batches_are_isolated(varied):
    reader,session,_=varied;errors=[];results={}
    barrier=threading.Barrier(2)
    def run(name):
        try:
            with authority_request_scope():
                barrier.wait(5);results[name]=batched(reader.pool,session,TARGETS)
        except Exception as error:errors.append(error)
    threads=[threading.Thread(target=run,args=(n,)) for n in ('a','b')]
    [t.start() for t in threads];[t.join(30) for t in threads]
    assert not errors and [x[0] for x in results['a']]==[x[0] for x in results['b']]


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_object_projection_claims_match_single_path(published_action,admin):
    reader,_,_=published_action
    object_id=GovernedObjectCreator(reader.pool,reader.session,reader.signer).create(action_name='Consumer.create',action_version=1,
        intent_id='batch-projection',type_name='Consumer',properties={'preference':'service'})['object_id']
    targets=[('eios:object:Consumer/'+object_id,ResourceType.OBJECT,Operation.READ),('eios:property:Consumer/'+object_id+'/preference',ResourceType.PROPERTY,Operation.READ)]
    session,_=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-batch-projection')
    projection=AuthorizedObjectReader(reader.pool,session,reader.signer)
    pairs=[(ResourceType.OBJECT,'Consumer/'+object_id),(ResourceType.PROPERTY,'Consumer/'+object_id+'/preference')]
    batch=projection.authorities(pairs);one=[projection._authority(k,n) for k,n in pairs]
    strip=lambda c:{k:v for k,v in c.items() if k!='expires_at'}
    assert [strip(c) for c in batch]==[strip(c) for c in one]  # same signed-proof content, per target
    assert projection.get('Consumer',object_id,fields=('preference',))['properties']=={'preference':'service'}
    # A property without READ fails exactly like the single path (whole projection refused).
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    with pytest.raises((ActionAuthorizationDenied,AuthorizationUnavailable,F.AuthorizationFactDenied)):
        projection.authorities(pairs+[(ResourceType.PROPERTY,'Consumer/'+object_id+'/missing_field')])

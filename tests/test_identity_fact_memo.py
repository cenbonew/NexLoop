"""O2a: identity-class authority facts are read once per request scope.

Equivalence with per-decision loading (outcome, proof record hashes, expiry),
revocation/expiry/session change still deny, and no sharing across requests,
principals, worlds or concurrent requests.
"""
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,threading,time
import pytest
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
import nexloop_eios.authorization as A
from nexloop_eios.authorization import IDENTITY_FACT_KINDS,authenticate_service,authority_request_scope,resolve_authority
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action  # noqa: F401

ALLOWED=('eios:action:Consumer.create:1','eios:action:Memo.allowed:1')
EXPIRED='eios:action:Memo.expired:1'
OUTSIDE_APP='eios:action:Memo.outside-app:1'
MISSING='eios:action:Memo.missing:1'
TARGETS=ALLOWED+(EXPIRED,OUTSIDE_APP,MISSING)


@pytest.fixture
def counted(monkeypatch):
    loads=[]
    original=A.PostgresAuthorityUnitOfWork._fetch
    def fetch(self,kind,key):
        loads.append((self.session.authentication.subject_principal_id,kind,tuple(key)));return original(self,kind,key)
    monkeypatch.setattr(A.PostgresAuthorityUnitOfWork,'_fetch',fetch)
    return loads


@pytest.fixture
def varied(published_action,admin):
    """One service principal with allowed, expired-grant and outside-application targets."""
    reader,_,_=published_action
    session,token=seed_multi_authority(admin,reader.pool,[(t,ResourceType.ACTION,Operation.EXECUTE) for t in ALLOWED+(EXPIRED,OUTSIDE_APP)],identity_suffix='-memo-varied')
    principal=session.authentication.subject_principal_id
    row=admin.execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s",([principal,EXPIRED],)).fetchone()[0]
    grant=row['grants'][0];grant['valid_until']=(datetime.now(UTC)-timedelta(minutes=1)).isoformat()
    replace_fact(admin,'synthetic-a','grants',[principal,EXPIRED],F.GrantFacts,grants=[grant])
    app=admin.execute("select entity_key,payload from authz.nexloop_authority_facts where fact_kind='application' and payload::text like %s",('%'+OUTSIDE_APP+'%',)).fetchone()
    replace_fact(admin,'synthetic-a','application',app[0],F.ApplicationFacts,
        resources=[r for r in app[1]['resources'] if r['resource_id']!=OUTSIDE_APP])
    return reader,authenticate_service(reader.pool,token,world='real'),token


def outcome(pool,session,target):
    entries=[]
    try:
        decision=AuthorizationDecisionService().decide_resolved(resolve_authority(pool,session,
            session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE),entries))
        return (decision.allowed,decision.authoritative,decision.expires_at,sorted((e['kind'],tuple(e['key']),e['record_hash']) for e in entries))
    except (AuthorizationUnavailable,F.AuthorizationFactDenied) as error:
        return ('error',type(error).__name__)


def test_equivalence_outcome_proof_hashes_and_expiry(varied,counted):
    reader,session,_=varied
    plain={t:outcome(reader.pool,session,t) for t in TARGETS}
    plain_loads=len(counted)
    with authority_request_scope():
        scoped={t:outcome(reader.pool,session,t) for t in TARGETS}
    assert scoped==plain  # identical allow/deny, authoritative flag, expiry and record hashes
    assert [plain[t][0] for t in ALLOWED]==[True,True]
    assert plain[EXPIRED][0] is not True and plain[OUTSIDE_APP][0] is not True and plain[MISSING][0]=='error'
    scoped_loads=counted[plain_loads:]
    identity=[l for l in scoped_loads if l[1] in IDENTITY_FACT_KINDS]
    assert len(identity)==len(set(identity))  # each identity fact read once in the request
    assert len(scoped_loads)<plain_loads


def test_record_hash_from_memo_equals_fresh_read(varied,admin):
    reader,session,_=varied
    with authority_request_scope():
        first=[];resolve_authority(reader.pool,session,session.query(resource_id=ALLOWED[0],resource_type=ResourceType.ACTION,operation=Operation.EXECUTE),first)
        second=[];resolve_authority(reader.pool,session,session.query(resource_id=ALLOWED[1],resource_type=ResourceType.ACTION,operation=Operation.EXECUTE),second)
    for entry in second:
        if entry['kind'] in IDENTITY_FACT_KINDS:
            snap=admin.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',(session.token_digest,'real',entry['kind'],entry['key'])).fetchone()[0]
            assert snap['record_hash']==entry['record_hash']


@pytest.mark.parametrize('change',['membership_suspended','subject_disabled'])
def test_identity_revocation_denies_in_next_request(varied,admin,change):
    reader,session,token=varied;auth=session.authentication
    with authority_request_scope():assert outcome(reader.pool,session,ALLOWED[0])[0] is True
    if change=='membership_suspended':
        replace_fact(admin,'synthetic-a','membership',[auth.subject_id,auth.subject_principal_id],F.MembershipFacts,status='suspended')
    else:
        replace_fact(admin,'synthetic-a','subject',[auth.subject_id],F.SubjectFacts,status='disabled')
    with authority_request_scope():  # stale session refused before any memo use
        assert outcome(reader.pool,session,ALLOWED[0])[0]=='error'
    fresh=authenticate_service(reader.pool,token,world='real')
    with authority_request_scope():
        assert outcome(reader.pool,fresh,ALLOWED[0])[0] is not True


def test_revocation_inside_request_is_not_served_from_memo(varied,admin):
    reader,session,_=varied;auth=session.authentication
    with authority_request_scope():
        assert outcome(reader.pool,session,ALLOWED[0])[0] is True
        replace_fact(admin,'synthetic-a','membership',[auth.subject_id,auth.subject_principal_id],F.MembershipFacts,status='suspended')
        # Another target in the same request: the unit of work re-checks the live directory first.
        assert outcome(reader.pool,session,ALLOWED[1])[0]=='error'


def test_identity_expiry_is_evaluated_per_decision_with_memoized_fact(published_action,admin):
    reader,_,_=published_action
    session,token=seed_multi_authority(admin,reader.pool,[(t,ResourceType.ACTION,Operation.EXECUTE) for t in ALLOWED],identity_suffix='-memo-expiry')
    auth=session.authentication
    replace_fact(admin,'synthetic-a','membership',[auth.subject_id,auth.subject_principal_id],F.MembershipFacts,
        valid_until=(datetime.now(UTC)+timedelta(seconds=2)).isoformat())
    session=authenticate_service(reader.pool,token,world='real')
    with authority_request_scope():
        first=outcome(reader.pool,session,ALLOWED[0])
        assert first[0] is True and first[2]<=datetime.now(UTC)+timedelta(seconds=2)  # proof never outlives membership
        time.sleep(2.2)
        assert outcome(reader.pool,session,ALLOWED[1])[0] is not True  # memoized membership is past valid_until


@pytest.mark.parametrize('variant',['principal','world','tenant'])
def test_memo_never_shared_across_principal_world_tenant(published_action,admin,counted,variant):
    reader,_,_=published_action
    _,token_a=seed_multi_authority(admin,reader.pool,[(ALLOWED[0],ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-memo-a')
    options={'principal':dict(identity_suffix='-memo-b'),'world':dict(identity_suffix='-memo-sim',world='simulation'),
        'tenant':dict(identity_suffix='-memo-t',tenant='synthetic-b')}[variant]
    if variant=='tenant':admin.execute("insert into control.nexloop_tenants(tenant_id,status) values('synthetic-b','active') on conflict do nothing")
    _,token_b=seed_multi_authority(admin,reader.pool,[(ALLOWED[0],ResourceType.ACTION,Operation.EXECUTE)],**options)
    a=authenticate_service(reader.pool,token_a,world='real');b=authenticate_service(reader.pool,token_b,world=options.get('world','real'))
    with authority_request_scope():
        outcome(reader.pool,a,ALLOWED[0]);start=len(counted)
        outcome(reader.pool,b,ALLOWED[0])
    b_loads=[l for l in counted[start:] if l[1] in IDENTITY_FACT_KINDS]
    assert {l[1] for l in b_loads}>={'subject','membership','actor','authentication','application','subject_authority'}
    assert all(l[0]==b.authentication.subject_principal_id for l in b_loads)


def test_memo_not_shared_across_requests_or_concurrent_requests(varied,counted):
    reader,session,_=varied
    with authority_request_scope():outcome(reader.pool,session,ALLOWED[0])
    first=[l for l in counted if l[1] in IDENTITY_FACT_KINDS]
    with authority_request_scope():outcome(reader.pool,session,ALLOWED[1])
    second=[l for l in counted if l[1] in IDENTITY_FACT_KINDS][len(first):]
    assert {l[1] for l in second}=={l[1] for l in first}  # the next request reads identity facts again
    barrier=threading.Barrier(2);memos={};errors=[]
    def run(name,target):
        try:
            with authority_request_scope():
                barrier.wait(5);assert outcome(reader.pool,session,target)[0] is True
                memos[name]=id(A._REQUEST_MEMO.get())
        except Exception as error:errors.append(error)
    threads=[threading.Thread(target=run,args=(n,t)) for n,t in (('a',ALLOWED[0]),('b',ALLOWED[1]))]
    [t.start() for t in threads];[t.join(30) for t in threads]
    assert not errors and memos['a']!=memos['b']

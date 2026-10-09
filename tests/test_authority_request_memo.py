"""O1/O4: request-scoped authorization memo and the shared-request/exclusive-close lock.

The memo lives only inside one Backend request (contextvars scope); it must never
serve a decision to another request, session, world, principal or thread, and it
must not let a request run across shutdown. Revocation is observed by the next
request; inside a request the SQL commit tail still re-verifies the record hashes.
"""
import threading,time,uuid
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import nexloop_eios.authorization as A
from nexloop_eios.authorization import authority_request_scope,authenticate_service,resolve_authority
from nexloop_eios.backend import BackendClosed,open_backend
from nexloop_eios.object_actions import GovernedObjectCreator
from authority_fixture import replace_fact,seed_authority
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action

TARGET='eios:action:Consumer.create:1'


@pytest.fixture
def counted(monkeypatch):
    calls=[]
    original=A.PostgresAuthorityProvider.open_unit_of_work
    def open_unit_of_work(self,query):
        calls.append(query.target.resource_id);return original(self,query)
    monkeypatch.setattr(A.PostgresAuthorityProvider,'open_unit_of_work',open_unit_of_work)
    return calls


def query(session,target=TARGET,operation=Operation.EXECUTE):
    return session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=operation)


def allowed(pool,session,target=TARGET):
    from eios.authz.service import AuthorizationDecisionService
    try:return AuthorizationDecisionService().decide_resolved(resolve_authority(pool,session,query(session,target))).allowed
    except (AuthorizationUnavailable,F.AuthorizationFactDenied):return False


def test_memo_only_inside_scope_and_entries_replayed(published_action,counted):
    reader,_,_=published_action;pool,session=reader.pool,reader.session
    first,again=[],[]
    resolve_authority(pool,session,query(session),first);resolve_authority(pool,session,query(session),again)
    assert len(counted)==2  # no scope: every call resolves
    with authority_request_scope():
        a,b=[],[]
        ctx=resolve_authority(pool,session,query(session),a)
        assert resolve_authority(pool,session,query(session),b) is ctx
        assert len(counted)==3 and a==b and a is not b and a[0] is not b[0]
        b[0]['record_hash']='mutated'  # caller-side mutation never reaches the memo
        c=[];resolve_authority(pool,session,query(session),c);assert c==a
        with authority_request_scope():  # nested scope reuses the request memo
            resolve_authority(pool,session,query(session),[])
        assert len(counted)==3
    with authority_request_scope():  # a new request starts empty
        resolve_authority(pool,session,query(session),[])
    assert len(counted)==4 and A._REQUEST_MEMO.get() is None


def test_revocation_is_observed_by_the_next_request(published_action,admin,counted):
    reader,_,_=published_action;pool,session=reader.pool,reader.session
    with authority_request_scope():assert allowed(pool,session)
    replace_fact(admin,'synthetic-a','grants',[session.authentication.subject_principal_id,TARGET],F.GrantFacts,grants=[])
    with authority_request_scope():
        assert not allowed(pool,session)  # stale session: directory changed
    fresh=_reauthenticate(admin,pool,session)
    with authority_request_scope():
        assert not allowed(pool,fresh)


def test_grant_change_between_requests_changes_the_decision(published_action,admin):
    reader,_,_=published_action;pool,session=reader.pool,reader.session
    with authority_request_scope():assert allowed(pool,session)
    row=admin.execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s",([session.authentication.subject_principal_id,TARGET],)).fetchone()[0]
    grant=row['grants'][0];grant.update(operations=['read'],revision=2)
    replace_fact(admin,'synthetic-a','grants',[session.authentication.subject_principal_id,TARGET],F.GrantFacts,grants=[grant],revision=2)
    fresh=_reauthenticate(admin,pool,session)
    with authority_request_scope():assert not allowed(pool,fresh)


def test_in_request_memo_cannot_bypass_sql_commit_tail(published_action,admin):
    reader,_,_=published_action;pool,session=reader.pool,reader.session
    creator=GovernedObjectCreator(pool,session,reader.signer)
    before=admin.execute("select count(*) from ontology.objects").fetchone()[0]
    with authority_request_scope():
        resolve_authority(pool,session,query(session),[])  # warm the request memo
        replace_fact(admin,'synthetic-a','grants',[session.authentication.subject_principal_id,TARGET],F.GrantFacts,grants=[])
        with pytest.raises(Exception):
            creator.create(action_name='Consumer.create',action_version=1,intent_id='memo-revoked-'+uuid.uuid4().hex,type_name='Consumer',properties={})
    assert admin.execute("select count(*) from ontology.objects").fetchone()[0]==before


@pytest.mark.parametrize('variant',['principal','world','target','operation'])
def test_memo_key_separates_sessions_world_target_operation(published_action,admin,counted,variant):
    reader,_,_=published_action;pool,session=reader.pool,reader.session
    if variant in ('principal','world'):
        world='simulation' if variant=='world' else 'real'
        _,other_token=seed_multi_authority(admin,pool,[(TARGET,ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-memo-'+variant,world=world)
        session=_reauthenticate(admin,pool,session)
        other=authenticate_service(pool,other_token,world=world)
    with authority_request_scope():
        resolve_authority(pool,session,query(session),[])
        n=len(counted)
        if variant in ('principal','world'):ctx=resolve_authority(pool,other,query(other),[])
        elif variant=='target':
            with pytest.raises(Exception):resolve_authority(pool,session,query(session,'eios:action:Other.create:1'),[])
        else:
            with pytest.raises(Exception):resolve_authority(pool,session,query(session,operation=Operation.READ),[])
        assert len(counted)==n+1  # resolved, never served from the first entry


def test_concurrent_requests_have_isolated_memos(published_action):
    reader,_,_=published_action;pool,session=reader.pool,reader.session
    seen={};barrier=threading.Barrier(2);errors=[]
    def run(name):
        try:
            with authority_request_scope():
                memo=A._REQUEST_MEMO.get();barrier.wait(5)
                for _ in range(10):assert allowed(pool,session)
                seen[name]=(id(memo),sum(1 for key in memo if key[0]!='identity-fact'))  # decision entries only
                barrier.wait(5)
        except Exception as error:errors.append(error)
    threads=[threading.Thread(target=run,args=(n,)) for n in ('run-a','run-b')]
    [t.start() for t in threads];[t.join(30) for t in threads]
    assert not errors and seen['run-a'][0]!=seen['run-b'][0] and seen['run-a'][1]==seen['run-b'][1]==1


def test_shutdown_waits_for_inflight_request_and_later_requests_fail(published_action,admin,pg,tmp_path):
    reader,_,_=published_action
    key=tmp_path/'memo-key';key.write_bytes(reader.signer.material);key.chmod(0o600)
    token,_=seed_authority(admin,'synthetic-a',TARGET,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-memo-host')
    entered=threading.Event();release=threading.Event();order=[]
    with open_backend(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'blobs',signing_key_file=key,signing_key_id=reader.signer.key_id) as backend:
        services=backend.authenticate(token,world='real')
        def inflight():
            with backend._lock,authority_request_scope():
                entered.set();release.wait(10)
                order.append('request-done')
        t=threading.Thread(target=inflight);t.start();entered.wait(5)
        closer=threading.Thread(target=lambda:(backend._shutdown(),order.append('closed')));closer.start()
        time.sleep(0.3);assert order==[]  # close is exclusive: it waits for the in-flight request
        release.set();t.join(10);closer.join(10)
        assert order==['request-done','closed']
        with pytest.raises(BackendClosed):
            services.create_object(action_name='Consumer.create',action_version=1,intent_id='after-close',type_name='Consumer',properties={})
        assert A._REQUEST_MEMO.get() is None


def _reauthenticate(admin,pool,session):
    import hashlib,secrets
    token=secrets.token_urlsafe(48)
    admin.execute('update authz.nexloop_service_credentials set token_digest=%s where token_digest=%s',(hashlib.sha256(token.encode()).hexdigest(),session.token_digest))
    return authenticate_service(pool,token,world='real')

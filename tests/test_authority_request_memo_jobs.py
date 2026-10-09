"""O1 extension: background units of work (claim scheduler/worker/extractor, matcher,
glue, recall) are one request scope each. Memo never crosses units, principals,
worlds or resources; revocation is observed by the next unit.
"""
import hashlib,secrets
import pytest
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import nexloop_eios.authorization as A
from nexloop_eios.authorization import authenticate_service,authority_request_scope,authority_request_scoped
from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler,ClaimFeed
from nexloop_eios.claim_store import ClaimExtractionDenied
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from test_claim_extraction_jobs_pg import services,EXTRACT_TARGET,QUEUE_TARGET  # noqa: F401
from test_claim_store_pg import source_targets  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_recall_pg import recall_db,recall_for,reader_targets,refs,A as TENANT_A  # noqa: F401


@pytest.fixture
def counted(monkeypatch):
    calls=[]
    original=A.PostgresAuthorityProvider.open_unit_of_work
    def open_unit_of_work(self,query):
        calls.append((self.session.authentication.subject_principal_id,query.target.resource_id,query.target.operation.value))
        return original(self,query)
    monkeypatch.setattr(A.PostgresAuthorityProvider,'open_unit_of_work',open_unit_of_work)
    return calls


def rotate(admin,pool,session,world='real'):
    token=secrets.token_urlsafe(48)
    admin.execute('update authz.nexloop_service_credentials set token_digest=%s where token_digest=%s',(hashlib.sha256(token.encode()).hexdigest(),session.token_digest))
    return authenticate_service(pool,token,world=world)


def test_decorated_unit_discards_memo_even_on_error():
    seen=[]
    @authority_request_scoped
    def unit(fail):
        seen.append(A._REQUEST_MEMO.get())
        if fail:raise RuntimeError('boom')
    unit(False)
    with pytest.raises(RuntimeError):unit(True)
    assert seen[0] is not None and seen[1] is not None and seen[0] is not seen[1]
    assert A._REQUEST_MEMO.get() is None


def test_recall_unit_memoizes_within_and_resolves_again_next_unit(recall_db,counted):
    f=recall_db;recall=recall_for(f,f['reader'])
    assert refs(recall.recall('张伟',definitions=False).instances)==['eios:object:Consumer/'+f['ids']['zhang']]
    first=list(counted)
    assert len(first)==len(set(first))  # within one unit each decision resolved once
    recall.recall('张伟',definitions=False)
    assert len(counted)==2*len(first)  # the next unit resolves again (no cross-unit memo)


def test_recall_revocation_is_observed_by_the_next_unit(recall_db):
    f=recall_db;target='eios:object:Consumer/'+f['ids']['zhang']
    assert refs(recall_for(f,f['reader']).recall('张伟',definitions=False).instances)==[target]
    replace_fact(f['admin'],TENANT_A,'grants',[f['reader'].authentication.subject_principal_id,target],F.GrantFacts,grants=[])
    fresh=rotate(f['admin'],f['worker'],f['reader'])
    assert recall_for(f,fresh).recall('张伟',definitions=False).instances==()


@pytest.mark.parametrize('variant',['principal','world'])
def test_recall_memo_never_crosses_principal_or_world_inside_one_scope(recall_db,variant):
    f=recall_db
    if variant=='principal':
        _,token=seed_multi_authority(f['admin'],f['worker'],reader_targets([f['ids']['zhang']],object_props=('member_no',)),identity_suffix='-memo-partial',tenant=TENANT_A)
        world='real'
    else:
        _,token=seed_multi_authority(f['admin'],f['worker'],reader_targets([f['ids']['sim']]),identity_suffix='-memo-sim',world='simulation',tenant=TENANT_A)
        world='simulation'
    reader=rotate(f['admin'],f['worker'],f['reader']);other=authenticate_service(f['worker'],token,world=world)
    with authority_request_scope():  # one outer scope shared by both units on purpose
        assert refs(recall_for(f,reader).recall('张伟',definitions=False).instances)==['eios:object:Consumer/'+f['ids']['zhang']]
        hits=refs(recall_for(f,other).recall('张伟',definitions=False).instances)
    if variant=='principal':assert hits==[]  # missing display_name READ is not served from the full reader's memo
    else:assert hits==['eios:object:Consumer/'+f['ids']['sim']]  # simulation reads its own world only


def test_scheduler_revocation_is_observed_by_the_next_unit(services,admin,counted):
    s=services
    assert ClaimExtractionScheduler(s['api'],s['scheduler'],s['signer']).run_once()==[]
    unit=list(counted);assert len(unit)==len(set(unit))
    principal=s['scheduler'].authentication.subject_principal_id
    replace_fact(admin,'synthetic-a','grants',[principal,EXTRACT_TARGET[0]],F.GrantFacts,grants=[])
    fresh=rotate(admin,s['api'],s['scheduler'])
    with pytest.raises(ClaimExtractionDenied):ClaimExtractionScheduler(s['api'],fresh,s['signer']).run_once()


def test_scheduler_memo_never_crosses_principal_or_resource(services,admin):
    s=services
    _,token=seed_multi_authority(admin,s['api'],[QUEUE_TARGET],identity_suffix='-memo-no-extract')
    scheduler=rotate(admin,s['api'],s['scheduler']);queue_only=authenticate_service(s['api'],token,world='real')
    with authority_request_scope():
        assert ClaimFeed(s['api'],scheduler,s['signer']).backlog() is not None
        # Same resource, different principal without the extract grant: denied, not memo-served.
        with pytest.raises(ClaimExtractionDenied):ClaimFeed(s['api'],queue_only,s['signer']).backlog()

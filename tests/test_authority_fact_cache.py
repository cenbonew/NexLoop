"""O3: content-addressed parse cache for EIOS authority facts must never serve stale authority.

Facts are still read from PostgreSQL on every decision; the cache only skips a
repeated JSON->model parse of the exact same text. Every authority change
(revocation, grant change, directory/tenant revision, other tenant/world/principal)
must reach a fresh parse or a fresh denial.
"""
from concurrent.futures import ThreadPoolExecutor
import json,threading
import psycopg
import pytest
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import FACT_PARSE_CACHE,FactParseCache,WITNESS,authenticate_service,authorization_service
from authority_fixture import authority_records,replace_fact
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action

TARGET='eios:action:Consumer.create:1'


def decide(pool,session,target=TARGET):
    try:
        return authorization_service(pool,session).decide(session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)).allowed
    except (AuthorizationUnavailable,F.AuthorizationFactDenied):
        return False


def keys_for(cache):
    with cache._lock:return {(m.__name__,json.loads(text).get('tenant_id'),text) for m,text in cache._entries}


def principal_texts(cache,principal):
    return {text for _,_,text in keys_for(cache) if principal in text}


@pytest.fixture
def warm(published_action):
    reader,_,_=published_action
    FACT_PARSE_CACHE.clear()
    assert decide(reader.pool,reader.session)
    first=FACT_PARSE_CACHE.misses
    assert decide(reader.pool,reader.session)
    assert FACT_PARSE_CACHE.hits>=first and FACT_PARSE_CACHE.misses==first  # same facts: parse reused
    return reader


def test_hit_is_identical_to_fresh_parse():
    _,_,rows=authority_records('synthetic-a',TARGET,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    cache=FactParseCache()
    for kind,key,fact in rows:
        text=json.dumps({**fact.model_dump(mode='json'),'repository_witness':WITNESS})
        first=cache.parse(type(fact),text);again=cache.parse(type(fact),text)
        assert again is first and first==type(fact).model_validate_json(text)
    assert cache.hits==len(rows) and cache.misses==len(rows)
    with pytest.raises(Exception):cache.parse(F.GrantFacts,'{"tenant_id":"synthetic-a"}')
    assert len(cache)==len(rows)  # parse failures are not cached


def test_revocation_is_never_served_from_cache(warm,admin):
    reader=warm;principal=reader.session.authentication.subject_principal_id
    replace_fact(admin,'synthetic-a','grants',[principal,TARGET],F.GrantFacts,grants=[])
    # Directory changed: the old session is refused regardless of cached facts.
    assert not decide(reader.pool,reader.session)
    token=_token_for(admin,reader.session)
    fresh=authenticate_service(reader.pool,token,world='real')
    misses=FACT_PARSE_CACHE.misses
    assert not decide(reader.pool,fresh)
    assert FACT_PARSE_CACHE.misses>misses  # the revoked grant fact was parsed fresh


def test_grant_change_produces_new_key_and_new_result(warm,admin):
    reader=warm;principal=reader.session.authentication.subject_principal_id
    before=principal_texts(FACT_PARSE_CACHE,principal)
    row=admin.execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s",([principal,TARGET],)).fetchone()[0]
    grant=row['grants'][0];grant['operations']=['read'];grant['revision']=2
    replace_fact(admin,'synthetic-a','grants',[principal,TARGET],F.GrantFacts,grants=[grant],revision=2)
    fresh=authenticate_service(reader.pool,_token_for(admin,reader.session),world='real')
    assert not decide(reader.pool,fresh)  # EXECUTE no longer granted
    after=principal_texts(FACT_PARSE_CACHE,principal)
    assert any('"operations": ["read"]' in t for t in after-before)  # changed grant parsed fresh
    assert not any('"operations": ["read"]' in t for t in before)


def test_directory_hash_change_denies_old_session_even_with_warm_cache(warm,admin):
    reader=warm
    admin.execute("update control.nexloop_tenants set authority_revision=authority_revision+1 where tenant_id='synthetic-a'")
    assert len(FACT_PARSE_CACHE)>0
    with pytest.raises(AuthorizationUnavailable):
        authorization_service(reader.pool,reader.session).decide(reader.session.query(resource_id=TARGET,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE))
    fresh=authenticate_service(reader.pool,_token_for(admin,reader.session),world='real')
    assert fresh.directory_hash!=reader.session.directory_hash and decide(reader.pool,fresh)


@pytest.mark.parametrize('variant',['principal','tenant','world'])
def test_other_principal_tenant_world_never_hit_foreign_entries(warm,admin,variant):
    reader=warm
    if variant=='tenant':admin.execute("insert into control.nexloop_tenants(tenant_id,status) values('synthetic-b','active') on conflict do nothing")
    options={'principal':dict(identity_suffix='-cache-other'),'tenant':dict(identity_suffix='-cache-b',tenant='synthetic-b'),
        'world':dict(identity_suffix='-cache-sim',world='simulation')}[variant]
    other,_=seed_multi_authority(admin,reader.pool,[(TARGET,ResourceType.ACTION,Operation.EXECUTE)],**options)
    owner=reader.session.authentication.subject_principal_id
    owner_texts=principal_texts(FACT_PARSE_CACHE,owner)
    misses=FACT_PARSE_CACHE.misses
    assert decide(reader.pool,other)  # decided on its own facts
    other_principal=other.authentication.subject_principal_id
    # Every principal-bound fact of the other identity was parsed from its own text.
    other_texts=principal_texts(FACT_PARSE_CACHE,other_principal)
    assert other_texts and not (other_texts & owner_texts)
    assert FACT_PARSE_CACHE.misses-misses>=len(other_texts)
    if variant=='tenant':
        assert {t for m,tenant,t in keys_for(FACT_PARSE_CACHE) if other_principal in t} <= {t for m,tenant,t in keys_for(FACT_PARSE_CACHE) if tenant=='synthetic-b'}
    if variant=='world':
        # The world-bound policy (world == 'simulation') is a distinct parsed fact, not the real-world one.
        assert any(m=='PolicyFacts' and '"simulation"' in t for m,_,t in keys_for(FACT_PARSE_CACHE))
        assert any(m=='PolicyFacts' and '"real"' in t for m,_,t in keys_for(FACT_PARSE_CACHE))


def test_concurrent_decisions_and_revocation_have_no_shared_state_race(warm,admin):
    reader=warm;errors=[];results=[]
    def worker(_):
        try:results.append(decide(reader.pool,reader.session))
        except Exception as error:errors.append(error)
    with ThreadPoolExecutor(8) as pool:list(pool.map(worker,range(64)))
    assert not errors and all(results) and len(results)==64
    # Pure cache stress: distinct texts across threads never cross-return.
    cache=FactParseCache(maximum=16);_,_,rows=authority_records('synthetic-a',TARGET,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    subject=next(f for k,_,f in rows if k=='subject');bad=[]
    def stress(i):
        body={**subject.model_dump(mode='json'),'repository_witness':WITNESS,'revision':1+i%40}
        body.pop('snapshot_digest',None)
        fact=cache.parse(type(subject),json.dumps(body))
        if fact.revision!=1+i%40:bad.append(i)
    threads=[threading.Thread(target=lambda n=n:[stress(n*50+j) for j in range(50)]) for n in range(8)]
    [t.start() for t in threads];[t.join() for t in threads]
    assert not bad and len(cache)<=16


def _token_for(admin,session):
    # Test-only: recover the fixture credential by re-issuing a token for the same binding.
    import hashlib,secrets
    token=secrets.token_urlsafe(48)
    admin.execute('update authz.nexloop_service_credentials set token_digest=%s where token_digest=%s',(hashlib.sha256(token.encode()).hexdigest(),session.token_digest))
    return token

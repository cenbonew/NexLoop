"""NX-049 8a/8b: verified-instance registry and memoized resource-id validation in the vendored EIOS core.

The shortcut may only skip revalidation of an instance that the registry itself fully
validated and that has not changed since; every other instance (unregistered parse,
model_construct, model_copy, in-place tampering) takes the upstream full revalidation.
Decisions, digests and expiries must be identical with the shortcut on and off.
"""
import dataclasses,gc,hashlib,json
import pytest
from eios.authz import facts as F
from eios.authz import _fact_resolver as R
from eios.authz import applications as A
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import FACT_PARSE_CACHE,WITNESS,PostgresAuthorityProvider,PostgresAuthorityUnitOfWork
from authority_fixture import authority_records
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action

TARGET='eios:action:Consumer.create:1'


def texts():
    _,_,rows=authority_records('synthetic-a',TARGET,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION)
    return [(type(fact),json.dumps({**fact.model_dump(mode='json'),'repository_witness':WITNESS})) for _,_,fact in rows]


def check(value):return R._exact_model(value,type(value),'unavailable')


def test_registered_instance_is_returned_unchanged_and_equals_full_validation(monkeypatch):
    for model,text in texts():
        instance=R.verified_model_from_json(model,text)
        assert check(instance) is instance
        monkeypatch.setattr(R,'_TRUST_VERIFIED',False)
        full=check(instance)
        monkeypatch.setattr(R,'_TRUST_VERIFIED',True)
        assert full is not instance and full==instance
        assert full.model_dump_json()==instance.model_dump_json() and full.snapshot_digest==instance.snapshot_digest


def test_unregistered_construct_and_copy_are_fully_validated():
    for model,text in texts():
        plain=model.model_validate_json(text)
        assert check(plain) is not plain  # parsed outside the registry: full revalidation (new instance)
        registered=R.verified_model_from_json(model,text)
        copied=registered.model_copy()
        assert check(copied) is not copied
        constructed=model.model_construct(**dict(registered.__dict__))
        assert check(constructed) is not constructed


def test_forged_construct_and_stale_copy_are_rejected():
    subject=next(R.verified_model_from_json(m,t) for m,t in texts() if m is F.SubjectFacts)
    forged=F.SubjectFacts.model_construct(**{**subject.__dict__,'status':'disabled'})
    with pytest.raises(AuthorizationUnavailable):check(forged)  # snapshot digest no longer matches
    stale=subject.model_copy(update={'revision':subject.revision+1})
    with pytest.raises(AuthorizationUnavailable):check(stale)


def test_in_place_tampering_of_a_registered_instance_is_rejected():
    subject=next(R.verified_model_from_json(m,t) for m,t in texts() if m is F.SubjectFacts)
    assert check(subject) is subject
    object.__setattr__(subject,'status','disabled')
    with pytest.raises(AuthorizationUnavailable):check(subject)
    # Nested: a grant inside a registered GrantFacts.
    grants=next(R.verified_model_from_json(m,t) for m,t in texts() if m is F.GrantFacts)
    assert check(grants) is grants
    grant=grants.grants[0]
    object.__setattr__(grant,'revision',grant.revision+7)
    with pytest.raises(AuthorizationUnavailable):check(grants)
    # Replacing the field dict wholesale with equal-content copies still misses (identity changed).
    actor=next(R.verified_model_from_json(m,t) for m,t in texts() if m is F.ActorFacts)
    object.__setattr__(actor,'__dict__',dict(actor.__dict__))
    assert check(actor) is not actor and check(actor)==actor


def test_tampered_container_inside_registered_instance_is_detected():
    application=next(R.verified_model_from_json(m,t) for m,t in texts() if m is F.ApplicationFacts)
    assert check(application) is application
    containers=[v for v in R._reachable(application) if type(v) is dict and v is not application.__dict__]
    assert containers  # model field/private dicts are part of the recorded graph
    target=containers[0];target['__nexloop_tamper__']=1
    try:assert check(application) is not application
    except AuthorizationUnavailable:pass
    finally:target.pop('__nexloop_tamper__',None)


def test_registry_entries_die_with_their_instance():
    model,text=texts()[0]
    instance=R.verified_model_from_json(model,text);key=id(instance)
    assert key in R._VERIFIED
    del instance;gc.collect()
    assert key not in R._VERIFIED


def test_switch_forces_full_validation(monkeypatch):
    model,text=texts()[0]
    monkeypatch.setattr(R,'_TRUST_VERIFIED',False)
    instance=R.verified_model_from_json(model,text)
    assert id(instance) not in R._VERIFIED and check(instance) is not instance


def test_resource_id_memo_matches_uncached_and_never_caches_errors():
    for value in ('eios:action:Consumer.create:1','eios:action:Consumer.create','eios:object:Consumer'):
        kind=value.split(':')[1]
        assert A._typed_resource_id(value,kind,'resource_id') is value
        assert A._typed_resource_id_uncached(value,kind,'resource_id')==value
        assert A._restriction_digest('synthetic-a',kind,value)==A._restriction_digest_uncached('synthetic-a',kind,value)
    assert A._restriction_digest('synthetic-a','action',None)==A._restriction_digest_uncached('synthetic-a','action',None)
    for _ in range(2):
        with pytest.raises(ValueError):A._typed_resource_id('eios:action:',  'action','resource_id')
        with pytest.raises(ValueError):A._typed_resource_id('eios:object:Consumer','action','resource_id')
    class Sub(str):pass
    # str subclasses bypass the cache and keep the original checks.
    with pytest.raises(ValueError):A._typed_resource_id(Sub('eios:action:Consumer.create:1 '),'action','resource_id')
    assert A._typed_resource_id(Sub('eios:action:Consumer.create:1'),'action','resource_id')=='eios:action:Consumer.create:1'


def _decisions(pool,session,targets):
    """Decision fields (decision_id is random per call) plus the fact entries a proof binds."""
    out=[]
    for target,kind,op in targets:
        entries=[]
        service=AuthorizationDecisionService(resolver=F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session,entries)))
        try:
            decision=dataclasses.asdict(service.decide(session.query(resource_id=target,resource_type=kind,operation=op)))
            decision.pop('decision_id')
            dumped=json.loads(json.dumps({'decision':decision,'entries':entries},sort_keys=True,default=str))
            out.append(('decision',hashlib.sha256(json.dumps(dumped,sort_keys=True).encode()).hexdigest(),dumped))
        except (AuthorizationUnavailable,F.AuthorizationFactDenied) as error:
            out.append(('error',type(error).__name__,str(error)))
    return out


def test_pg_decisions_identical_with_and_without_shortcut(published_action,admin,monkeypatch):
    reader,_,_=published_action
    other,_=seed_multi_authority(admin,reader.pool,[(TARGET,ResourceType.ACTION,Operation.EXECUTE),
        ('eios:action:Consumer.update:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-registry')
    targets=[(TARGET,ResourceType.ACTION,Operation.EXECUTE),('eios:action:Consumer.update:1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:action:Consumer.delete:1',ResourceType.ACTION,Operation.EXECUTE),(TARGET,ResourceType.ACTION,Operation.READ)]
    now=admin.execute('select clock_timestamp()').fetchone()[0]
    monkeypatch.setattr(PostgresAuthorityUnitOfWork,'trusted_now',lambda self:now)
    hits=[];original=R._is_verified
    monkeypatch.setattr(R,'_is_verified',lambda value:hits.append(original(value)) or hits[-1])
    results={}
    for trust in (False,True):
        monkeypatch.setattr(R,'_TRUST_VERIFIED',trust);FACT_PARSE_CACHE.clear();hits.clear()
        # Twice: the second pass is served from the parse cache (registered instances when trusted).
        results[trust]=[_decisions(reader.pool,session,targets) for session in (reader.session,other) for _ in range(2)]
        assert any(hits) is trust
    assert results[True]==results[False]
    flat=[r for run in results[True] for r in run]
    assert any(r[0]=='decision' and r[2]['decision']['allowed'] for r in flat) and any(r[0]=='error' or not r[2]['decision']['allowed'] for r in flat)


# ---- 8c: reuse of read-only views of verified facts -------------------------------------------

from pydantic import BaseModel,ConfigDict


class _Holder(BaseModel):
    model_config=ConfigDict(frozen=True,strict=True,extra='forbid')
    subject:F.SubjectFacts
    actor:F.ActorFacts
    other_subject:F.SubjectFacts|None=None


def _records():
    cells=dict(zip(R._project_authority_view.__code__.co_freevars,(c.cell_contents for c in R._project_authority_view.__closure__)))
    return cells['record_values']


def materialize(value,records=None):
    """Plain structure of a projected view (test-only access to the projector's records)."""
    records=_records() if records is None else records
    if isinstance(value,R.ReadOnlyAuthorityView):
        return {k:materialize(v,records) for k,v in sorted(records[value].items())}
    if isinstance(value,(tuple,list)):return [materialize(v,records) for v in value]
    if isinstance(value,frozenset):return sorted((json.dumps(materialize(v,records),sort_keys=True,default=str) for v in value))
    if hasattr(value,'items'):return {str(k):materialize(v,records) for k,v in sorted(value.items(),key=lambda kv:str(kv[0]))}
    return value if value is None or isinstance(value,(bool,int,float,str)) else repr(value)


def registered(model):return next(R.verified_model_from_json(m,t) for m,t in texts() if m is model)


def test_view_reuse_only_for_verified_equal_copies(monkeypatch):
    subject,actor=registered(F.SubjectFacts),registered(F.ActorFacts)
    holder=_Holder(subject=subject,actor=actor)
    assert holder.subject is not subject and holder.subject==subject  # payload-style revalidated copy
    reuse=R._reusable_views(holder,{'subject':subject,'actor':actor})
    assert set(reuse)=={id(holder.subject),id(holder.actor)}
    assert reuse[id(holder.subject)] is R._cached_view(subject)
    # The reused view has exactly the content a fresh projection of the copy has.
    assert materialize(R._project_authority_view(holder,reuse))==materialize(R._project_authority_view(holder))
    monkeypatch.setattr(R,'_TRUST_VERIFIED',False)
    assert R._reusable_views(holder,{'subject':subject,'actor':actor}) is None


def test_view_reuse_refused_for_unregistered_unequal_or_tampered_originals():
    subject,actor=registered(F.SubjectFacts),registered(F.ActorFacts)
    holder=_Holder(subject=subject,actor=actor)
    plain=F.SubjectFacts.model_validate_json(next(t for m,t in texts() if m is F.SubjectFacts))
    assert R._reusable_views(holder,{'subject':plain}) is None  # not registered
    other=registered(F.SubjectFacts)
    object.__setattr__(other,'status','disabled')  # tampered after registration
    assert R._reusable_views(_Holder(subject=registered(F.SubjectFacts),actor=actor),{'subject':other}) is None
    _,_,rows=authority_records('synthetic-a',TARGET,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-unequal')
    foreign=next(f for _,_,f in rows if type(f) is F.ActorFacts)
    assert foreign!=actor
    holder2=_Holder(subject=subject,actor=foreign)
    assert R._reusable_views(holder2,{'actor':actor}) is None  # payload copy not equal to the original
    # The same original for two fields is reused at most once, so view identities stay distinct.
    both=_Holder(subject=subject,actor=actor,other_subject=subject)
    reuse=R._reusable_views(both,{'subject':subject,'other_subject':subject})
    assert list(reuse)==[id(both.subject)]
    view=R._project_authority_view(both,reuse)
    records=_records()
    assert records[view]['subject'] is not records[view]['other_subject']


def test_cached_view_dies_with_its_instance():
    subject=registered(F.SubjectFacts);key=id(subject)
    R._cached_view(subject);assert key in R._VIEWS
    del subject;gc.collect()
    assert key not in R._VIEWS


def _contexts(pool,session,queries):
    """Materialized resolved views; queries are built once so request/trace ids match across modes."""
    out=[]
    for query in queries:
        resolver=F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session))
        try:context=resolver.resolve(query)
        except (AuthorizationUnavailable,F.AuthorizationFactDenied) as error:
            out.append(('error',type(error).__name__));continue
        context.verify_integrity()
        out.append(('context',{name:materialize(getattr(context,name)) for name in sorted(R._RESOLVED_PUBLIC_FIELDS) if name!='trusted_now'}))
    return out


def test_pg_resolved_views_identical_and_reused(published_action,admin,monkeypatch):
    reader,_,_=published_action
    targets=[(TARGET,ResourceType.ACTION,Operation.EXECUTE),(TARGET,ResourceType.ACTION,Operation.READ),
        ('eios:action:Consumer.delete:1',ResourceType.ACTION,Operation.EXECUTE)]
    now=admin.execute('select clock_timestamp()').fetchone()[0]
    monkeypatch.setattr(PostgresAuthorityUnitOfWork,'trusted_now',lambda self:now)
    queries=[reader.session.query(resource_id=t,resource_type=k,operation=o) for t,k,o in targets]
    results={}
    for trust in (False,True):
        monkeypatch.setattr(R,'_TRUST_VERIFIED',trust);FACT_PARSE_CACHE.clear()
        results[trust]=[_contexts(reader.pool,reader.session,queries) for _ in range(2)]
    assert results[True]==results[False] and any(r[0]=='context' for r in results[True][0])
    # With the shortcut, a second resolve hands out the same cached subject view.
    resolver=F.AuthorizationFactsResolver(PostgresAuthorityProvider(reader.pool,reader.session))
    query=reader.session.query(resource_id=TARGET,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
    first,second=resolver.resolve(query),resolver.resolve(query)
    assert first.subject is second.subject
    monkeypatch.setattr(R,'_TRUST_VERIFIED',False)
    assert resolver.resolve(query).subject is not first.subject

"""Live PostgreSQL fact adapter for the frozen EIOS resolver/intersection.

Service credential authentication stays distinct from browser sessions. No caller
can supply an authoritative tenant/principal/application or manufacture a sealed
resolved context. Agent invocation comes only from live credential configuration; delegation fails closed.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import contextvars
import functools
import json
import secrets
import threading
import time

from eios.authz import facts as F
from eios.authz._fact_resolver import verified_model_from_json
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from eios.identity.models import FrozenJsonMap
from nexloop_eios.assembly import verify_application_role

WITNESS = 'nexloop-postgres-authority-v1'


class FactParseCache:
    """Content-addressed cache of parsed (immutable) EIOS authority facts.

    It only removes repeated JSON->model parsing. Facts are still read from
    PostgreSQL on every decision; the key is the fact model class plus the exact
    JSON text returned for that read (tenant, principal, world-bound fields,
    grants, revisions and repository witness are all inside that text). Any
    authority change therefore produces a different key, and a hit returns
    exactly what a fresh parse of the same text would return. Fact models are
    frozen, strict and have no clock-dependent validation. Session/directory
    binding checks are untouched. Parse failures are never cached.
    """

    def __init__(self, maximum=2048):
        self._maximum = maximum
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def parse(self, model, text):
        key = (model, text)
        with self._lock:
            fact = self._entries.get(key)
            if fact is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return fact
            self.misses += 1
        fact = verified_model_from_json(model, text)
        with self._lock:
            self._entries[key] = fact
            self._entries.move_to_end(key)
            while len(self._entries) > self._maximum:
                self._entries.popitem(last=False)
        return fact

    def clear(self):
        with self._lock:
            self._entries.clear()
            self.hits = self.misses = 0

    def __len__(self):
        with self._lock:
            return len(self._entries)


FACT_PARSE_CACHE = FactParseCache()


@dataclass(frozen=True)
class RunContext:
    run_id:str
    audience:str
    allowed_resources:tuple[str,...]


@dataclass(frozen=True)
class ServiceSession:
    authentication: F.CredentialAuthenticationBinding
    world: str
    expires_at: datetime
    token_digest: str = field(repr=False)
    directory_hash: str = field(repr=False)

    agent_invocation: F.AgentInvocationBinding | None = None
    run_context: RunContext | None = None

    def query(self, *, resource_id: str, resource_type: ResourceType,
              operation: Operation, request_id=None):
        return F.AuthorizationFactQuery(
            tenant_id=self.authentication.tenant_id, authentication=self.authentication,
            agent_invocation=self.agent_invocation,
            target=F.AuthorizationTarget(tenant_id=self.authentication.tenant_id,
                resource_id=resource_id,resource_type=resource_type,operation=operation),
            request_attributes=FrozenJsonMap({'world':self.world,**({} if self.run_context is None else {'run_id':self.run_context.run_id,'audience':self.run_context.audience})}),
            request_id=request_id or secrets.token_hex(16),trace_id=secrets.token_hex(16),
        )


def _identity(connection, digest, world):
    row=connection.execute('select authz.nexloop_service_identity_snapshot(%s,%s)',(digest,world)).fetchone()
    return _identity_session(row[0] if row else None,digest,world)


def _identity_session(snapshot, digest, world):
    if not snapshot:
        raise AuthorizationUnavailable('service identity unavailable')
    row=(snapshot,)
    binding=F.CredentialAuthenticationBinding.model_validate_json(json.dumps(row[0]['binding']))
    raw=row[0].get('agent_invocation')
    invocation=None if raw is None else F.AgentInvocationBinding.model_validate_json(json.dumps(raw))
    if (binding.subject_kind.value=='agent')!=(invocation is not None):raise AuthorizationUnavailable('agent invocation binding unavailable')
    if invocation is not None and (invocation.tenant_id!=binding.tenant_id or invocation.actor_principal_id!=binding.subject_principal_id or invocation.agent_id!=binding.subject_id):raise AuthorizationUnavailable('agent invocation identity mismatch')
    raw_run=row[0].get('run_context');run=None
    if raw_run is not None:
        import uuid
        if set(raw_run)!={'run_id','audience','allowed_resources'} or raw_run['audience']!='nexloop-agent-host':raise AuthorizationUnavailable('invalid Run context')
        if str(uuid.UUID(raw_run['run_id']))!=raw_run['run_id']:raise AuthorizationUnavailable('invalid Run ID')
        resources=raw_run['allowed_resources']
        if type(resources) is not list or not 1<=len(resources)<=32 or len(set(resources))!=len(resources):raise AuthorizationUnavailable('invalid Run resources')
        run=RunContext(raw_run['run_id'],raw_run['audience'],tuple(resources))
    return ServiceSession(binding,world,datetime.fromisoformat(row[0]['expires_at']),digest,row[0]['directory_hash'],invocation,run)


def authenticate_service(pool, token: str, *, world: str, run_id=None, audience=None):
    if not isinstance(token,str) or not 32<=len(token)<=512:
        raise AuthorizationUnavailable('service authentication denied')
    digest=hashlib.sha256(token.encode()).hexdigest()
    try:
        with pool.connection() as connection,connection.transaction():
            verify_application_role(connection)
            session=_identity(connection,digest,world)
            if session.run_context is None:
                if run_id is not None or audience is not None:raise AuthorizationUnavailable('root credential is not Run authority')
            elif session.run_context.run_id!=run_id or session.run_context.audience!=audience:
                raise AuthorizationUnavailable('Run target binding denied')
            return session
    except Exception:
        # Do not leak token/digest/SQL parameters via chained diagnostics.
        raise AuthorizationUnavailable('service authentication denied') from None


class PostgresAuthorityUnitOfWork:
    repeatable_read=True
    read_only=True

    def __init__(self, connection, session, query, entries, now=None):
        self.connection,self.session,self.query=connection,session,query
        self._loaded=set()
        self.entries=entries
        self._prefetched={}
        self._now=connection.execute('select clock_timestamp()').fetchone()[0] if now is None else now

    def trusted_now(self):return self._now

    def verify_repository_witness(self, *, fact_kind, snapshot_digest, repository_witness):
        return (fact_kind,snapshot_digest,repository_witness) in self._loaded

    def prefetch(self, keys):
        """O2b: one round trip for many snapshots via the same single-fact function.

        Only 'ok'/'missing' answers are kept; anything the single-call function would
        reject is re-read singly in _fetch (and raises exactly as before). Identity
        facts already memoized for this request (O2a) are not fetched again.
        """
        keys=self.pending(keys)
        if not keys:return
        self.accept(keys,self.connection.execute('select authz.nexloop_load_authority_facts(%s,%s,%s::jsonb)',
            (self.session.token_digest,self.session.world,batch_keys(keys))).fetchone()[0])

    def pending(self, keys):
        return [(kind,tuple(key)) for kind,key in dict.fromkeys((k,tuple(v)) for k,v in keys)
            if (kind,tuple(key)) not in self._prefetched and not self._memoized_identity(kind,key)]

    def accept(self, keys, rows):
        if type(rows) is not list or len(rows)!=len(keys):return  # a hint only: anything else is read singly
        for (kind,key),row in zip(keys,rows):
            if row.get('status')=='ok':self._prefetched[(kind,key)]=row['value']
            elif row.get('status')=='missing':self._prefetched[(kind,key)]=None

    def _identity_memo_key(self, kind, key, model):
        return ('identity-fact',type(self.session).__name__,self.session.token_digest,self.session.directory_hash,
            self.session.world,kind,tuple(key),model)

    def _memoized_identity(self, kind, key):
        memo=_REQUEST_MEMO.get();model=_IDENTITY_MODELS.get(kind)
        if memo is None or model is None:return False
        hit=memo.get(self._identity_memo_key(kind,key,model))
        return hit is not None and time.monotonic()-hit[2]<REQUEST_MEMO_MAX_AGE_SECONDS

    def _fetch(self, kind, key):
        hit=self._prefetched.get((kind,tuple(key)),False)
        if hit is not False:return hit
        row=self.connection.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,kind,list(key))).fetchone()
        return None if not row or row[0] is None else row[0]

    def _load(self, kind, key, model):
        # O2a: identity-class facts are constant for one authenticated session;
        # inside one request scope they are read once (still bound to the session's
        # token digest, directory hash and world). Grants/scope/controls/policies/
        # resource graph are read on every decision.
        memo=_REQUEST_MEMO.get();memo_key=None
        if memo is not None and kind in IDENTITY_FACT_KINDS:
            memo_key=self._identity_memo_key(kind,key,model)
            hit=memo.get(memo_key)
            if hit is not None and time.monotonic()-hit[2]<REQUEST_MEMO_MAX_AGE_SECONDS:
                record_hash,fact,_=hit
                self.entries.append({'kind':kind,'key':list(key),'record_hash':record_hash})
                self._loaded.add((model.__name__,fact.snapshot_digest,fact.repository_witness))
                return fact
        value=self._fetch(kind,key)
        if value is None:return None
        self.entries.append({'kind':kind,'key':list(key),'record_hash':value['record_hash']})
        payload=dict(value['payload']);payload['repository_witness']=WITNESS
        # Stored snapshot checksum remains checked by the original EIOS model
        # (on a cache miss; a hit is the result of parsing this same text).
        fact=FACT_PARSE_CACHE.parse(model,json.dumps(payload))
        self._loaded.add((model.__name__,fact.snapshot_digest,fact.repository_witness))
        if memo_key is not None:memo[memo_key]=(value['record_hash'],fact,time.monotonic())
        return fact

    def load_subject(self,tenant_id,subject_id):return self._load('subject',[subject_id],F.SubjectFacts)
    def load_membership(self,tenant_id,subject_id,principal_id):return self._load('membership',[subject_id,principal_id],F.MembershipFacts)
    def load_actor(self,tenant_id,actor_principal_id):return self._load('actor',[actor_principal_id],F.ActorFacts)
    def load_credential_authentication(self,binding):return self._load('authentication',[binding.credential_id],F.CredentialAuthenticationFacts)
    def load_caller_application(self,tenant_id,application_id,version):return self._load('application',[application_id,version],F.ApplicationFacts)
    def load_subject_authority(self,tenant_id,subject_kind,principal_id):return self._load('subject_authority',[principal_id],F.SubjectAuthorityFacts)
    def load_resource_graph(self,target):return self._load('resource_graph',[target.resource_id],F.ResourceGraphFacts)
    def load_grants(self,tenant_id,subject_kind,principal_id,graph):return self._load('grants',[principal_id,graph.root.resource.resource_id],F.GrantFacts)
    def _target(self,kind,query,model):return self._load(kind,[query.authentication.subject_principal_id,query.target.resource_id,query.target.operation.value],model)
    def load_scope_authority(self,query):return self._target('scope',query,F.ScopeAuthorityFacts)
    def load_controls(self,query,graph):return self._target('controls',query,F.ControlFacts)
    def load_policies(self,query,graph):return self._target('policies',query,F.PolicyFacts)
    def load_revision_source(self,query):return self._load('revision',['catalog'],F.RevisionSourceFacts)
    def load_browser_authentication(self,binding):raise AuthorizationUnavailable('browser fact adapter not configured')
    def load_delegated_authentication(self,binding):raise AuthorizationUnavailable('delegation fact adapter not configured')
    def load_agent(self,tenant_id,agent_id):return self._load('agent',[agent_id],F.AgentFacts)
    def load_agent_release(self,tenant_id,release_id):return self._load('agent_release',[release_id],F.AgentReleaseFacts)
    def load_agent_application(self,tenant_id,application_id,version):return self._load('agent_application',[application_id,version],F.ApplicationFacts)


@contextmanager
def _repeatable_read_only(connection):
    """One REPEATABLE READ READ ONLY transaction; NX-049 4a: the isolation travels in the
    BEGIN statement itself (no separate SET TRANSACTION round trip). The connection's own
    defaults are restored afterwards, so a reused request connection is unchanged."""
    import psycopg
    previous=(connection.isolation_level,connection.read_only)
    connection.isolation_level,connection.read_only=psycopg.IsolationLevel.REPEATABLE_READ,True
    try:
        with connection.transaction():
            yield
    finally:
        if not connection.closed:connection.isolation_level,connection.read_only=previous


def batch_keys(keys):
    return json.dumps([{'kind':k,'key':list(v)} for k,v in keys])


def _assert_query_binding(session, query):
    if (query.authentication != session.authentication or query.tenant_id != session.authentication.tenant_id
            or query.request_attributes.get('world') != session.world or query.agent_invocation != session.agent_invocation
            or (session.run_context is not None and (query.request_attributes.get('run_id')!=session.run_context.run_id or query.request_attributes.get('audience')!=session.run_context.audience))):
        raise AuthorizationUnavailable('server identity binding mismatch')


class PostgresAuthorityProvider:
    # NX-049 means 5: a single-query unit prefetches its predicted facts in one round
    # trip (the O2b batch path); resolve_authorities prefetches for all its queries itself.
    prefetch_on_open=True

    def __init__(self,pool,session: ServiceSession,entries=None):
        self.pool,self.session=pool,session
        self._entry_sink=entries

    @contextmanager
    def open_unit_of_work(self,query):
        from nexloop_eios.browser_authorization import BrowserBusinessSession,browser_authority_unit_of_work
        if isinstance(self.session,BrowserBusinessSession):
            with browser_authority_unit_of_work(self,query) as unit:
                yield unit
            return
        _assert_query_binding(self.session,query)
        try:
            with self.pool.connection() as connection,_repeatable_read_only(connection):
                verify_application_role(connection)
                digest,world=self.session.token_digest,self.session.world
                unit=PostgresAuthorityUnitOfWork(connection,self.session,query,[] if self._entry_sink is None else self._entry_sink,now=False)  # trusted time comes from the opening statement below
                keys=unit.pending(_predicted_fact_keys(self.session,query)) if self.prefetch_on_open else []
                # NX-049: live identity, the unit's trusted time and (means 5) the predicted fact
                # snapshots in one statement of the same REPEATABLE READ transaction.
                if keys:
                    row=connection.execute('select authz.nexloop_service_identity_snapshot(%s,%s),clock_timestamp(),'
                        'authz.nexloop_load_authority_facts(%s,%s,%s::jsonb)',(digest,world,digest,world,batch_keys(keys))).fetchone()
                else:
                    row=connection.execute('select authz.nexloop_service_identity_snapshot(%s,%s),clock_timestamp()',(digest,world)).fetchone()
                live=_identity_session(row[0] if row else None,digest,world)
                if (live.authentication != self.session.authentication
                        or live.directory_hash != self.session.directory_hash
                        or live.agent_invocation != self.session.agent_invocation):
                    raise AuthorizationUnavailable('service credential binding is stale')
                unit._now=row[1]
                if keys:unit.accept(keys,row[2])
                yield unit
        except F.AuthorizationFactDenied:
            raise
        except Exception:
            raise AuthorizationUnavailable('PostgreSQL authority facts unavailable') from None


# Facts whose key depends only on the authenticated session, never on the target.
IDENTITY_FACT_KINDS = frozenset({'subject','membership','actor','authentication','application','subject_authority','revision'})
_IDENTITY_MODELS = {'subject':F.SubjectFacts,'membership':F.MembershipFacts,'actor':F.ActorFacts,'authentication':F.CredentialAuthenticationFacts,
    'application':F.ApplicationFacts,'subject_authority':F.SubjectAuthorityFacts,'revision':F.RevisionSourceFacts}
_REQUEST_MEMO = contextvars.ContextVar('nexloop_authority_request_memo', default=None)
REQUEST_MEMO_MAX_AGE_SECONDS = 5.0


@contextmanager
def authority_request_scope():
    """O1: memoize identical authorization resolutions within ONE backend request.

    Opened by Backend request entry points (not nestable across requests: an
    inner scope reuses the outer one, the outermost discards it on exit). Each
    thread/request has its own contextvars context, so nothing is shared across
    requests or threads; a revocation is observed by the next request. Within a
    request, the signed proofs still carry the resolved record hashes that the
    SQL commit tail re-verifies.
    """
    if _REQUEST_MEMO.get() is not None:
        yield
        return
    token = _REQUEST_MEMO.set({})
    try:
        yield
    finally:
        _REQUEST_MEMO.reset(token)


def authority_request_scoped(method):
    """Decorator: one background unit of work (job/claim/window) is one request scope."""
    @functools.wraps(method)
    def scoped(*args, **kwargs):
        with authority_request_scope():
            return method(*args, **kwargs)
    return scoped


def _memo_key(session, query):
    # Principal, credential, tenant, scopes, application, agent invocation, world,
    # Run binding and target are all inside the query; directory hash covers the
    # tenant authority revision and credential row. Per-call request/trace ids excluded.
    return (type(session).__name__, session.token_digest, session.directory_hash, session.world,
        query.model_dump_json(exclude={'request_id', 'trace_id'}))


def resolve_authority(pool, session, query, entries=None):
    """Drop-in for AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session,entries)).resolve(query)."""
    memo = _REQUEST_MEMO.get()
    if memo is None:
        return F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool, session, entries)).resolve(query)
    key = _memo_key(session, query)
    hit = memo.get(key)
    if hit is not None and time.monotonic() - hit[2] < REQUEST_MEMO_MAX_AGE_SECONDS:
        if entries is not None:
            entries.extend({**entry, 'key': list(entry['key'])} for entry in hit[1])
        return hit[0]
    loaded = []
    context = F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool, session, loaded)).resolve(query)
    memo[key] = (context, tuple({**entry, 'key': list(entry['key'])} for entry in loaded), time.monotonic())
    if entries is not None:
        entries.extend(loaded)
    return context


def _predicted_fact_keys(session, query):
    """Keys the EIOS resolver normally loads for this query (a prefetch hint only:
    any fact outside this list is still read singly, so correctness never depends on it)."""
    auth=session.authentication;principal=auth.subject_principal_id;target=query.target
    keys=[('subject',[auth.subject_id]),('membership',[auth.subject_id,principal]),('actor',[principal]),
        ('authentication',[auth.credential_id]),('application',[auth.caller_application_id,auth.caller_application_version]),
        ('subject_authority',[principal]),('revision',['catalog']),('resource_graph',[target.resource_id]),
        ('grants',[principal,target.resource_id])]
    keys+=[(kind,[principal,target.resource_id,target.operation.value]) for kind in ('scope','controls','policies')]
    invocation=session.agent_invocation
    if invocation is not None:
        keys+=[('agent',[invocation.agent_id]),('agent_release',[invocation.release_id]),
            ('agent_application',[invocation.agent_application_id,invocation.agent_application_version])]
    return keys


def resolve_authorities(pool, session, queries):
    """O2b: resolve many queries of ONE session with one transaction and one fact round trip.

    Returns [(context, entries, error)] in order. Each query is still resolved on its
    own by the EIOS resolver (resolve_in_unit_of_work) with its own entries, inside
    its own savepoint, so outcomes, proof record hashes, expiry and per-query failure
    are those of resolve_authority. All misses share one live identity check and one
    REPEATABLE READ snapshot. Request memo (O1) entries are honoured and filled.
    """
    from nexloop_eios.browser_authorization import BrowserBusinessSession
    results=[None]*len(queries);memo=_REQUEST_MEMO.get();pending=[]
    for index,query in enumerate(queries):
        if memo is not None:
            hit=memo.get(_memo_key(session,query))
            if hit is not None and time.monotonic()-hit[2]<REQUEST_MEMO_MAX_AGE_SECONDS:
                results[index]=(hit[0],[{**entry,'key':list(entry['key'])} for entry in hit[1]],None);continue
        pending.append(index)
    if not pending:return results
    if isinstance(session,BrowserBusinessSession) or len(pending)==1:
        for index in pending:
            entries=[]
            try:results[index]=(resolve_authority(pool,session,queries[index],entries),entries,None)
            except Exception as error:results[index]=(None,[],error)
        return results
    provider=PostgresAuthorityProvider(pool,session,None);provider.prefetch_on_open=False;resolver=F.AuthorizationFactsResolver(provider)
    try:
        for index in pending:_assert_query_binding(session,queries[index])
        with provider.open_unit_of_work(queries[pending[0]]) as unit:
            unit.prefetch([key for index in pending for key in _predicted_fact_keys(session,queries[index])])
            for index in pending:
                unit.entries=[]
                try:
                    with unit.connection.transaction():
                        context=resolver.resolve_in_unit_of_work(queries[index],unit)
                except Exception as error:
                    results[index]=(None,[],error);continue
                results[index]=(context,unit.entries,None)
                if memo is not None:
                    memo[_memo_key(session,queries[index])]=(context,tuple({**entry,'key':list(entry['key'])} for entry in unit.entries),time.monotonic())
    except Exception as error:
        for index in pending:
            if results[index] is None:results[index]=(None,[],error)
    return results


def authorization_service(pool,session):
    return AuthorizationDecisionService(resolver=F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session)))

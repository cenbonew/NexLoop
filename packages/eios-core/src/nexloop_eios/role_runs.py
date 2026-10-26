"""Trusted Source Role selection. A binding never grants EIOS permissions.

Bind genuine Source-issued Runs before admission. All lifecycle operations must
repeat this signed current READ verifier; this primitive alone is not dispatch.
"""
import contextlib
from datetime import UTC,datetime
import threading
import collections
import json
import contextvars
import hashlib
import hmac
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.service_offerings import _read_envelope
from nexloop_eios.assembly import verify_application_role

ROLE_FIELDS=('active','ceiling_ref','name','responsibility','valid_from','valid_until')
LINK_FIELDS=('active','consumer_id','role_id','scope','valid_from','valid_until')
STEP_FIELDS=('consumer_id','state','submitter_principals')


def role_binding_envelope(source,*,run_id,consumer_id,link_id,role_id,step_id):
    if source._session.run_context is not None or source._session.world!='real':
        raise PermissionError('role binding unavailable')
    reads={'link':_read_envelope(source,'ConsumerRoleLink',link_id,LINK_FIELDS),
           'role':_read_envelope(source,'RoleDefinition',role_id,ROLE_FIELDS),
           'step':_read_envelope(source,'PlanStep',step_id,STEP_FIELDS)}
    payload=canonical_payload(dict(run_id=run_id,consumer_id=consumer_id,link_id=link_id,
                                  role_id=role_id,step_id=step_id,reads=reads))
    signer=source._backend._signer
    text=canonical_payload(dict(protocol='nexloop-role-run-v1',key_id=signer.key_id,
        parameters_digest=hashlib.sha256(payload.encode()).hexdigest()))
    signature=hmac.new(signer.material,('nexloop-role-run-v1:'+text).encode(),'sha256').hexdigest()
    return text,signature,payload


def bind_role_run(source,**parameters):
    """Technical binding only; no Schema, formal object, task ACK or effect write."""
    envelope=role_binding_envelope(source,**parameters)
    with source._backend._lock:
        source._backend._assert_open()
        with source._backend._pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_role_run_bind(%s,%s,%s,%s,%s)',
                (source._session.token_digest,source._session.world,*envelope)).fetchone()[0]


# One guarded tool request computes the same Source-signed Role and formal READ
# envelopes for its guard and its intent. Reuse is bounded to that request; SQL
# still re-verifies every envelope against current authority at each use.
_request_envelopes=contextvars.ContextVar('nexloop_role_request_envelopes',default=None)


@contextlib.contextmanager
def request_envelope_scope():
    token=_request_envelopes.set({})
    try:yield
    finally:_request_envelopes.reset(token)


# ADR-029 (owner 2026-10-12): within one Run, a guard authorization may reuse the Source-signed Context envelopes it built
# for an earlier authorization of the same Run while every READ proof inside them is still valid for at least
# RUN_ENVELOPE_MARGIN seconds. SQL re-verifies every reused envelope against current state at each use exactly as a new
# one; a refusal drops that Run's entries and the authorization is rebuilt once from fresh envelopes. In memory only,
# keyed by the Run's digest (never shared across Runs, tenants or worlds), bounded, never caching a failure.
RUN_ENVELOPE_MARGIN=5.0
RUN_ENVELOPE_LIMIT=512
# ADR-029b: the Context envelope kinds a guarded effect tool request may take from its Run's entries.
EFFECT_REUSE_KINDS=frozenset({'relationship','context_formal','catalog'})
_run_envelopes=collections.OrderedDict()
_run_envelopes_lock=threading.Lock()
_run_scope=contextvars.ContextVar('nexloop_run_envelope_scope',default=None)


def _expiries(value,found):
    if isinstance(value,dict):
        for key,item in value.items():
            if key=='expires_at' and isinstance(item,str):
                try:found.append(datetime.fromisoformat(item.replace('Z','+00:00')))
                except ValueError:found.append(None)
            else:_expiries(item,found)
    elif isinstance(value,(list,tuple)):
        for item in value:_expiries(item,found)
    elif isinstance(value,str) and value[:1] in '{[':
        try:_expiries(json.loads(value),found)
        except ValueError:pass
    return found


def envelope_valid_until(value):
    """The earliest expires_at of any proof inside an envelope; None when it carries none or one is unreadable."""
    found=_expiries(value,[])
    if not found or any(item is None or item.tzinfo is None for item in found):return None
    return min(found)


@contextlib.contextmanager
def run_envelope_scope(run_digest,*,reuse=True,kinds=None):
    """Authorizations of this Run may reuse its still-valid envelopes; yields a list that records whether one was reused.
    kinds (ADR-029b): only these envelope kinds use the Run's entries; inside a guarded tool request the others stay request-only."""
    used=[]
    token=_run_scope.set((run_digest,reuse,used,None if kinds is None else frozenset(kinds)))
    try:yield used
    finally:_run_scope.reset(token)


def forget_run_envelopes(run_digest):
    with _run_envelopes_lock:
        for key in [key for key in _run_envelopes if key[0]==run_digest]:del _run_envelopes[key]


def scoped_envelope(key,compute):
    """Reuse within the current guarded tool request; within a Run scope (ADR-029, effect tool requests ADR-029b) across that
    Run's authorizations while still valid; otherwise compute fresh."""
    cache=_request_envelopes.get()
    if cache is not None:
        # ADR-029b (owner 2026-10-26): a guarded effect tool request that opened a Run scope fills a miss of an allowed kind from
        # that Run's entries; every other kind is built for this request only, as before.
        if key not in cache:cache[key]=_run_envelope(key,compute)
        return cache[key]
    return _run_envelope(key,compute)


def _run_envelope(key,compute):
    scope=_run_scope.get()
    if scope is None:return compute()
    run_digest,reuse,used,kinds=scope
    if kinds is not None and key[0] not in kinds:return compute()
    full=(run_digest,)+tuple(key)
    now=datetime.now(UTC)
    if reuse:
        with _run_envelopes_lock:
            entry=_run_envelopes.get(full)
            if entry is not None and (entry[1]-now).total_seconds()>=RUN_ENVELOPE_MARGIN:
                _run_envelopes.move_to_end(full);used.append(key[0])
                return entry[0]
    value=compute()
    until=envelope_valid_until(value)
    if until is not None and (until-now).total_seconds()>=RUN_ENVELOPE_MARGIN:
        with _run_envelopes_lock:
            _run_envelopes[full]=(value,until);_run_envelopes.move_to_end(full)
            while len(_run_envelopes)>RUN_ENVELOPE_LIMIT:_run_envelopes.popitem(last=False)
    return value


def role_envelope_for_run(pool,signer,world,run_digest):
    return scoped_envelope(('role',id(pool),signer.key_id,world,run_digest),lambda:_role_envelope_for_run(pool,signer,world,run_digest))


def _role_envelope_for_run(pool,signer,world,run_digest):
    """Private kernel helper: actual authenticated Run digest, never guessed actor.

    Runtime digest comes from actual Run auth or an owned activation/lease's
    signed resolver. Technical hint grants no READ permission or dispatch.
    """
    from types import SimpleNamespace
    from nexloop_eios.authorization import _identity
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        hint=db.execute('select authz.nexloop_role_run_hint(%s,%s)',(run_digest,world)).fetchone()[0]
        if hint is None:return None
        source=_identity(db,hint.pop('_source_digest'),world)
    holder=SimpleNamespace(_session=source,_backend=SimpleNamespace(_pool=pool,_signer=signer))
    return dict(zip(('text','signature','payload'),role_binding_envelope(holder,**hint)))


def formal_reads_for_role(pool,signer,session,envelope):
    return scoped_envelope(('formal',id(pool),signer.key_id,session.world,envelope['signature']),lambda:_formal_reads_for_role(pool,signer,session,envelope))


def _formal_reads_for_role(pool,signer,session,envelope):
    """Fresh Source READ from actual tenant-owned Role Context ledger refs."""
    import json
    from types import SimpleNamespace
    from nexloop_eios.authorization import _identity
    run_id=json.loads(envelope['payload'])['run_id']
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        hint=db.execute('select authz.nexloop_role_formal_hint(%s,%s,%s)',
                        (session.token_digest,session.world,run_id)).fetchone()[0]
        source=_identity(db,hint['_source_digest'],session.world)
    holder=SimpleNamespace(_session=source,_backend=SimpleNamespace(_pool=pool,_signer=signer))
    return {fact['type']:_read_envelope(holder,fact['type'],fact['id'],
        ('allow_effect','budget_units','executor_principal','valid_until') if fact['type']=='EffectControl' else ())
        for fact in hint['formal_facts']}

"""Trusted Source Role selection. A binding never grants EIOS permissions.

Bind genuine Source-issued Runs before admission. All lifecycle operations must
repeat this signed current READ verifier; this primitive alone is not dispatch.
"""
import contextlib
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


def _scoped(key,compute):
    cache=_request_envelopes.get()
    if cache is None:return compute()
    if key not in cache:cache[key]=compute()
    return cache[key]


def role_envelope_for_run(pool,signer,world,run_digest):
    return _scoped(('role',world,run_digest),lambda:_role_envelope_for_run(pool,signer,world,run_digest))


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
    return _scoped(('formal',session.world,envelope['signature']),lambda:_formal_reads_for_role(pool,signer,session,envelope))


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

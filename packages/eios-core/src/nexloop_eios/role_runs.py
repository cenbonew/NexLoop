"""Trusted Source Role selection. A binding never grants EIOS permissions.

Bind genuine Source-issued Runs before admission. All lifecycle operations must
repeat this signed current READ verifier; this primitive alone is not dispatch.
"""
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


def role_envelope_for_run(pool,signer,world,run_digest):
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

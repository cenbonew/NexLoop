"""Signed current-authority proofs for Context reads (public EIOS decision APIs only)."""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac

from eios.authz import facts as F
from eios.authz.errors import AuthorizationError
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.postgres_artifacts import canonical_payload

READ_PROTOCOL='nexloop-context-read-v1'


class ContextDenied(PermissionError):
    pass


def _decide(pool,session,kind,name,operation):
    target=resource_id(kind,*name) if isinstance(name,tuple) else resource_id(kind,name);entries=[]
    query=session.query(resource_id=target,resource_type=kind,operation=operation)
    try:decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session,entries)).resolve(query))
    except AuthorizationError:raise ContextDenied(target) from None  # missing/stale authority is a denial
    if not decision.allowed or not decision.authoritative or decision.obligations:raise ContextDenied(target)
    return target,decision,entries


def _base(session,target,decision,entries,operation):
    auth=session.authentication
    return {'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,'credential_id':auth.credential_id,
        'directory_hash':session.directory_hash,'world':session.world,'resource_id':target,'operation':operation,
        'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
        'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}


def read_proof(pool,session,kind,name):
    """Current READ proof for one resource, verified again by SQL at use."""
    target,decision,entries=_decide(pool,session,kind,name,Operation.READ)
    return {**_base(session,target,decision,entries,'read'),'target_resource':target}


def action_claims(pool,session,stable_name,version=1):
    target,decision,entries=_decide(pool,session,ResourceType.ACTION,(stable_name,version),Operation.EXECUTE)
    return {**_base(session,target,decision,entries,'execute'),'action_resource':target}


def decision_ref(proof):
    """Stable reference to the exact proof a source was read under (manifest access_decision_ref)."""
    return 'decision:'+hashlib.sha256(canonical_payload(proof).encode()).hexdigest()


def signed_read(pool,session,signer,verb,payload,*,proofs=(),claims=None,function='authz.nexloop_context_read'):
    from nexloop_eios.assembly import verify_application_role
    body=canonical_payload({'verb':verb,**payload})
    auth=session.authentication
    text=canonical_payload({**(claims or {}),'protocol':READ_PROTOCOL,'key_id':signer.key_id,'tenant_id':auth.tenant_id,'world':session.world,
        'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'read_proofs':list(proofs)})
    signature=hmac.new(signer.material,(READ_PROTOCOL+':'+text).encode(),'sha256').hexdigest()
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        if function not in ('authz.nexloop_context_read','authz.nexloop_context_consumer_conversations'):raise ValueError('context read function')
        return db.execute('select '+function+'(%s,%s,%s,%s,%s)',(session.token_digest,session.world,text,signature,body)).fetchone()[0]

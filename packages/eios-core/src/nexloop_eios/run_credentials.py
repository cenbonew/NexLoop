"""Trusted server Run issuance; opaque credentials never confer new authority."""
from dataclasses import dataclass,field
from datetime import UTC,datetime,timedelta
import hmac
import uuid
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.assembly import verify_application_role

AUDIENCE='nexloop-agent-host'


@dataclass(frozen=True)
class RunCredential:
    run_id:str
    audience:str
    expires_at:datetime
    token:str=field(repr=False)


def _prepare_run_credential(pool,session,signer,*,action_resources,ttl_seconds=300):
    if session.run_context is not None:raise ActionAuthorizationDenied('nested Run issuance denied')
    if type(action_resources) not in (list,tuple) or not 1<=len(action_resources)<=32 or len(set(action_resources))!=len(action_resources):raise ValueError('bounded unique Action resources required')
    if type(ttl_seconds) is not int or not 1<=ttl_seconds<=300:raise ValueError('Run credential TTL must be1–300 seconds')
    from nexloop_eios.action_definitions import PostgresActionDefinitionReader
    reader=PostgresActionDefinitionReader(pool,session,signer)
    proofs=[]
    for target in sorted(action_resources):
        name,version=target.removeprefix('eios:action:').rsplit(':',1)
        try:reader.get(name,int(version))  # Exact active published contract, not a graph label alone.
        except Exception:raise ActionAuthorizationDenied('published Run Action unavailable') from None
        entries=[]
        query=session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ActionAuthorizationDenied('Run Action authority denied')
        proofs.append({'tenant_id':session.authentication.tenant_id,'principal_id':session.authentication.subject_principal_id,
            'credential_id':session.authentication.credential_id,'directory_hash':session.directory_hash,'world':session.world,
            'resource_id':target,'action_resource':target,'operation':'execute',
            'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'facts':sorted(entries,key=lambda item:(item['kind'],item['key']))})
    run_id=str(uuid.uuid4())
    claims={'protocol':'nexloop-run-credential-v1','key_id':signer.key_id,'run_id':run_id,'audience':AUDIENCE,
        'ttl_seconds':ttl_seconds,'proofs':proofs}
    text=canonical_payload(claims)
    signature=hmac.new(signer.material,('nexloop-run-credential-v1:'+text).encode(),'sha256').hexdigest()
    return run_id,text,signature


def issue_run_credential(pool,session,signer,*,action_resources,ttl_seconds=300):
    run_id,text,signature=_prepare_run_credential(pool,session,signer,action_resources=action_resources,ttl_seconds=ttl_seconds)
    with pool.connection() as connection,connection.transaction():
        if verify_application_role(connection)!='nexloop_api':raise ActionAuthorizationDenied('trusted API issuer required')
        result=connection.execute('select authz.nexloop_issue_run_credential(%s,%s,%s,%s)',(session.token_digest,session.world,text,signature)).fetchone()[0]
    return RunCredential(result['run_id'],AUDIENCE,datetime.fromisoformat(result['expires_at']),result['token'])

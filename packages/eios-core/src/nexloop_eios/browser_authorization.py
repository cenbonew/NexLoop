"""Genuine current HUMAN BrowserSession → frozen EIOS full authorization facts."""
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import datetime
import json

from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.identity.models import BrowserSession
from nexloop_eios.authorization import ServiceSession,PostgresAuthorityUnitOfWork,WITNESS
from nexloop_eios.assembly import verify_application_role


@dataclass(frozen=True)
class BrowserBusinessSession:
    authentication:F.BrowserAuthenticationBinding
    world:str
    expires_at:datetime
    token_digest:str=field(repr=False)
    directory_hash:str=field(repr=False)
    browser_application_id:str
    agent_invocation:None=None
    run_context:None=None
    identity_kind:str='browser'
    query=ServiceSession.query


def _browser_identity(connection,digest,world):
    value=connection.execute('select authz.nexloop_browser_identity_snapshot(%s,%s)',(digest,world)).fetchone()[0]
    if type(value) is not dict or value.get('identity_kind')!='browser':raise AuthorizationUnavailable('browser business authentication unavailable')
    binding=F.BrowserAuthenticationBinding.model_validate_json(json.dumps(value['binding']))
    return BrowserBusinessSession(binding,world,datetime.fromisoformat(value['expires_at']),digest,value['directory_hash'],value['browser_application_id'])


def authenticate_browser_business(pool,session:BrowserSession,*,world='real'):
    """Only the already inspected canonical session is a trusted HTTP input.

    The application role rechecks its actual digest, realm, principal, revision,
    account epoch and deadline. No service credential is created or borrowed.
    """
    try:
        if type(session) is not BrowserSession or world!='real':raise ValueError()
        digest=session.session_token_digest.get_secret_value().hex()
        with pool.connection() as connection,connection.transaction():
            if verify_application_role(connection)!='nexloop_api':raise ValueError()
            live=_browser_identity(connection,digest,world);binding=live.authentication
            pairs=(('tenant_id',session.tenant_id),('subject_id',session.subject_id),('subject_principal_id',session.principal_id),
                ('credential_id',session.credential_id),('credential_revision',session.credential_revision),
                ('credential_epoch',session.credential_session_epoch),('session_id',session.session_id),
                ('session_revision',session.revision),('membership_revision',session.membership_revision),
                ('subject_revision',session.subject_revision))
            if any(getattr(binding,name)!=expected for name,expected in pairs) or live.browser_application_id!=session.application_id or session.restricted or session.revoked_at is not None:raise ValueError()
            return live
    except Exception:raise AuthorizationUnavailable('browser business authentication unavailable') from None


class BrowserAuthorityUnitOfWork(PostgresAuthorityUnitOfWork):
    def _load(self,kind,key,model):
        value=self.connection.execute('select authz.nexloop_browser_authority_fact_snapshot(%s,%s,%s,%s)',
            (self.session.token_digest,self.session.world,kind,list(key))).fetchone()[0]
        if value is None:return None
        self.entries.append({'kind':kind,'key':list(key),'record_hash':value['record_hash']})
        body=dict(value['payload']);body['repository_witness']=WITNESS
        fact=model.model_validate_json(json.dumps(body))
        self._loaded.add((model.__name__,fact.snapshot_digest,fact.repository_witness));return fact
    def load_browser_authentication(self,binding):return self._load('browser_authentication',[binding.session_id],F.BrowserAuthenticationFacts)
    def load_credential_authentication(self,binding):raise AuthorizationUnavailable('browser is not API-key authority')


@contextmanager
def browser_authority_unit_of_work(provider,query):
    session=provider.session
    if query.authentication!=session.authentication or query.tenant_id!=session.authentication.tenant_id or query.request_attributes.get('world')!=session.world or query.agent_invocation is not None:
        raise AuthorizationUnavailable('server browser identity binding mismatch')
    try:
        with provider.pool.connection() as connection,connection.transaction():
            connection.execute('set transaction isolation level repeatable read read only')
            if verify_application_role(connection)!='nexloop_api':raise ValueError()
            live=_browser_identity(connection,session.token_digest,session.world)
            if live.authentication!=session.authentication or live.directory_hash!=session.directory_hash:raise ValueError()
            yield BrowserAuthorityUnitOfWork(connection,session,query,[] if provider._entry_sink is None else provider._entry_sink)
    except F.AuthorizationFactDenied:raise
    except Exception:raise AuthorizationUnavailable('PostgreSQL browser authority facts unavailable') from None

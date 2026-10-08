import pytest
from psycopg.types.json import Jsonb
from eios.identity.sessions import BrowserSessionService
from eios.identity.models import SecretDigest32
from eios.identity.errors import CredentialInvalid
from nexloop_eios.browser_identity import open_browser_identity
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from test_browser_session_creation import uow,create
from test_browser_identity_reads import identity
from test_browser_evidence import authenticate
from test_browser_account_failures import OP


def test_session_inspect_actual_token_reopen_and_no_write(uow,identity,admin):
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    service=BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP)
    assert service.inspect(issued.session_token.get_secret_value())==issued.session
    assert uow.get_session(issued.session.session_id)==issued.session
    assert uow.find_by_token_digest(SecretDigest32(b'x'*32)) is None
    with open_browser_identity(identity[0]) as pool:
        reopened=PostgresBrowserSessionUnitOfWork(pool,tenant_id=uow.tenant_id,application_id=uow.application_id)
        assert reopened.get_session(issued.session.session_id)==issued.session
    assert admin.execute("select (payload->>'revision')::int from control.nexloop_browser_sessions").fetchone()[0]==1


@pytest.mark.parametrize('table,changes',[
 ('subjects',{'status':'disabled'}),('accounts',{'session_epoch':2}),('memberships',{'status':'revoked'}),
 ('memberships',{'revision':2}),('accounts',{'revision':2})])
def test_canonical_identity_revocation_denies_session(uow,identity,admin,table,changes):
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    original=admin.execute(f'select payload from control.nexloop_browser_{table}').fetchone()[0]
    admin.execute(f'update control.nexloop_browser_{table} set payload=%s',(Jsonb({**original,**changes}),))
    assert uow.get_session(issued.session.session_id) is None
    service=BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP)
    with pytest.raises(CredentialInvalid):service.inspect(issued.session_token.get_secret_value())


def test_application_revision_and_wrong_realm_denied(uow,identity,admin):
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    other=PostgresBrowserSessionUnitOfWork(uow.pool,tenant_id='synthetic-b',application_id=uow.application_id)
    assert other.get_session(issued.session.session_id) is None
    admin.execute('update control.nexloop_browser_applications set revision=2')
    assert uow.get_session(issued.session.session_id) is None

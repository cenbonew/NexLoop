from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from eios.identity.sessions import BrowserSessionService
from eios.identity.ports import RevokeBrowserSessionCommand
from eios.identity.errors import CredentialInvalid
from test_browser_session_creation import uow,create
from test_browser_identity_reads import identity
from test_browser_evidence import authenticate
from test_browser_account_failures import OP


def test_actual_logout_csrf_then_token_denied(uow,identity,admin):
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    service=BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP)
    with pytest.raises(CredentialInvalid):service.revoke(issued.session_token.get_secret_value(),'synthetic-wrong-csrf')
    assert uow.get_session(issued.session.session_id)==issued.session
    revoked=service.revoke(issued.session_token.get_secret_value(),issued.csrf_token.get_secret_value())
    assert revoked.revision==2 and revoked.revoked_at is not None
    assert uow.get_session(issued.session.session_id) is None
    with pytest.raises(CredentialInvalid):service.inspect(issued.session_token.get_secret_value())
    assert admin.execute("select (payload->>'revision')::int from control.nexloop_browser_sessions").fetchone()[0]==2


def test_future_stale_and_concurrent_revoke_once(uow,identity,admin):
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    cmd=RevokeBrowserSessionCommand(session_id=issued.session.session_id,revoked_at=uow.current_time(),expected_revision=1)
    for changes in ({'expected_revision':2},{'revoked_at':uow.current_time()+timedelta(days=1)}):
        with pytest.raises(CredentialInvalid):uow.revoke_session(cmd.model_copy(update=changes),operator=OP)
    def attempt(_):
        try:return uow.revoke_session(cmd,operator=OP)
        except CredentialInvalid:return None
    with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(attempt,range(4)))
    assert sum(v is not None for v in results)==1
    assert admin.execute("select (payload->>'revision')::int from control.nexloop_browser_sessions").fetchone()[0]==2

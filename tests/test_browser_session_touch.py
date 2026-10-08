from datetime import timedelta
import pytest
from eios.identity.sessions import BrowserSessionService
from eios.identity.ports import TouchBrowserSessionCommand
from eios.identity.errors import CredentialInvalid
from test_browser_session_creation import uow,create
from test_browser_identity_reads import identity
from test_browser_evidence import authenticate
from test_browser_account_failures import OP


def command(uow,session,**changes):
    return TouchBrowserSessionCommand(session_id=session.session_id,subject_id=session.subject_id,tenant_id=session.tenant_id,
        principal_id=session.principal_id,application_id=session.application_id,application_revision=session.application_revision,
        credential_tenant_id=session.credential_tenant_id,membership_revision=session.membership_revision,
        expected_revision=session.revision,seen_at=uow.current_time(),**changes)


def test_frozen_authenticate_and_csrf_refresh_are_persisted(uow,identity):
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    service=BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP)
    touched=service.authenticate(issued.session_token.get_secret_value())
    assert touched.last_seen_at>issued.session.last_seen_at and touched.revision==issued.session.revision
    assert touched.idle_expires_at-touched.last_seen_at==timedelta(days=7)
    refreshed=service.refresh_csrf(issued.session_token.get_secret_value())
    assert refreshed.session.session_token_digest==issued.session.session_token_digest
    assert refreshed.session.csrf_token_digest!=issued.session.csrf_token_digest
    assert uow.get_session(issued.session.session_id)==refreshed.session
    service.verify_csrf(refreshed.session,refreshed.csrf_token.get_secret_value())
    with pytest.raises(CredentialInvalid):service.verify_csrf(refreshed.session,issued.csrf_token.get_secret_value())


def test_stale_future_binding_and_revocation_denied_without_touch(uow,identity,admin):
    issued=create(uow,identity,authenticate(uow,identity).evidence);cmd=command(uow,issued.session)
    for changes in ({'expected_revision':2},{'subject_id':'other'},{'seen_at':uow.current_time()+timedelta(days=1)},{'seen_at':issued.session.last_seen_at-timedelta(seconds=1)}):
        with pytest.raises(CredentialInvalid):uow.touch_session(cmd.model_copy(update=changes),operator=OP)
    admin.execute('update control.nexloop_browser_applications set active=false')
    with pytest.raises(CredentialInvalid):uow.touch_session(cmd,operator=OP)
    assert admin.execute("select payload->>'last_seen_at' from control.nexloop_browser_sessions").fetchone()[0]==issued.session.model_dump(mode='json')['last_seen_at']


def test_expired_session_cannot_be_revived(uow,identity,admin):
    from psycopg.types.json import Jsonb
    issued=create(uow,identity,authenticate(uow,identity).evidence)
    # Explicit already-expired auth fixture, no eight-day wait claim.
    now=uow.current_time();expired=issued.session.model_copy(update={'created_at':now-timedelta(days=8),'last_seen_at':now-timedelta(days=8),'idle_expires_at':now-timedelta(days=1),'absolute_expires_at':now+timedelta(days=22)})
    body=expired.model_dump(mode='json');admin.execute('update control.nexloop_browser_sessions set payload=%s',(Jsonb(body),))
    with pytest.raises(CredentialInvalid):uow.touch_session(command(uow,expired),operator=OP)
    assert admin.execute('select payload from control.nexloop_browser_sessions').fetchone()[0]==body

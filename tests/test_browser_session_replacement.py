import pytest
from eios.identity.sessions import BrowserSessionService
from eios.identity.errors import CredentialInvalid,IdentityUnavailable
from test_browser_session_creation import uow,create
from test_browser_identity_reads import identity
from test_browser_evidence import authenticate
from test_browser_account_failures import OP


def replace(uow,identity,evidence,token):
    return BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP).create(uow.tenant_id,identity[1].subject_id,identity[2].principal_id,evidence,replaced_session_token=token)


def test_actual_login_replacement_invalidates_old_token(uow,identity,admin):
    first=create(uow,identity,authenticate(uow,identity).evidence)
    new=replace(uow,identity,authenticate(uow,identity).evidence,first.session_token.get_secret_value())
    assert uow.get_session(first.session.session_id) is None
    assert uow.get_session(new.session.session_id)==new.session
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==2


def test_replacement_commit_fault_rolls_back_revoke_insert_and_consume(uow,identity,admin):
    first=create(uow,identity,authenticate(uow,identity).evidence)
    evidence=authenticate(uow,identity).evidence
    admin.execute("create function control.synthetic_replace_commit_failure() returns trigger language plpgsql as $$ begin raise exception 'synthetic commit failure';end $$")
    admin.execute('create constraint trigger synthetic_replace_commit_failure after insert on control.nexloop_browser_sessions deferrable initially deferred for each row execute function control.synthetic_replace_commit_failure()')
    with pytest.raises(IdentityUnavailable):replace(uow,identity,evidence,first.session_token.get_secret_value())
    assert uow.get_session(first.session.session_id)==first.session
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==1
    assert admin.execute('select count(*) from control.nexloop_browser_evidence where consumed_at is null').fetchone()[0]==1
    admin.execute('drop trigger synthetic_replace_commit_failure on control.nexloop_browser_sessions')
    assert replace(uow,identity,evidence,first.session_token.get_secret_value()).session.revision==1

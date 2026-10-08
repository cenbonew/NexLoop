from concurrent.futures import ThreadPoolExecutor
import pytest
from eios.identity.sessions import BrowserSessionService
from eios.identity.errors import CredentialInvalid
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from nexloop_eios.browser_identity import open_browser_identity
from test_browser_identity_reads import identity
from test_browser_evidence import authenticate
from test_browser_account_failures import OP

@pytest.fixture
def uow(identity):
    with open_browser_identity(identity[0]) as pool:
        yield PostgresBrowserSessionUnitOfWork(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app')


def create(uow,identity,evidence):
    return BrowserSessionService(uow,uow,application_id=uow.application_id,operator=OP).create(uow.tenant_id,identity[1].subject_id,identity[2].principal_id,evidence)


def test_real_password_session_and_replay_denial(uow,identity,admin):
    evidence=authenticate(uow,identity).evidence
    issued=create(uow,identity,evidence)
    assert issued.session.subject_id==identity[1].subject_id
    with pytest.raises(CredentialInvalid):create(uow,identity,evidence)
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==1
    assert admin.execute('select consumed_at is not null from control.nexloop_browser_evidence').fetchone()[0]
    stored=admin.execute('select payload from control.nexloop_browser_sessions').fetchone()[0]
    assert issued.session_token.get_secret_value() not in str(stored)
    assert issued.csrf_token.get_secret_value() not in str(stored)


def test_concurrent_evidence_consumption_commits_one(uow,identity,admin):
    evidence=authenticate(uow,identity).evidence
    def attempt(_):
        try:return create(uow,identity,evidence)
        except CredentialInvalid:return None
    with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(attempt,range(4)))
    assert sum(v is not None for v in results)==1
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==1


def test_actual_commit_failure_rolls_back_session_and_evidence(uow,identity,admin):
    from eios.identity.errors import IdentityUnavailable
    evidence=authenticate(uow,identity).evidence
    # Owned disposable auth schema only: inject an actual deferred commit fault.
    admin.execute("create function control.synthetic_session_commit_failure() returns trigger language plpgsql as $$ begin raise exception 'synthetic commit failure';end $$")
    admin.execute('create constraint trigger synthetic_commit_failure after insert on control.nexloop_browser_sessions deferrable initially deferred for each row execute function control.synthetic_session_commit_failure()')
    with pytest.raises(IdentityUnavailable):create(uow,identity,evidence)
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==0
    assert admin.execute('select consumed_at is null from control.nexloop_browser_evidence').fetchone()[0]
    admin.execute('drop trigger synthetic_commit_failure on control.nexloop_browser_sessions')
    assert create(uow,identity,evidence).session.revision==1

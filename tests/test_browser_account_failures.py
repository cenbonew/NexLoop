from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
import psycopg
from psycopg.types.json import Jsonb
from eios.identity.ports import TrustedIdentityOperator,RecordLocalAccountFailureCommand
from eios.identity.errors import IdentityUnavailable
from nexloop_eios.browser_identity import open_browser_identity,PostgresBrowserLocalAccountRepository
from test_browser_identity_reads import identity

OP=TrustedIdentityOperator(operator_principal_id='nexloop_identity',request_id='synthetic-password-request',trace_id='synthetic-password-trace')

@pytest.fixture
def accounts(identity):
    dsn,subject,membership,account,password=identity
    with open_browser_identity(dsn) as pool:
        yield PostgresBrowserLocalAccountRepository(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app'),dsn,account


def fail(repo,account):
    return repo.record_authentication_failure(RecordLocalAccountFailureCommand(tenant_id=account.tenant_id,local_account_id=account.local_account_id,failed_at=repo.current_time()),operator=OP)


def test_failure_lock_is_durable_and_live_lock_never_shortened(accounts,admin):
    repo,dsn,account=accounts
    for expected in range(1,5):
        changed=fail(repo,account);assert changed.failed_attempts==expected and changed.lockout_level==0
    locked=fail(repo,account)
    assert locked.failed_attempts==0 and locked.lockout_level==1 and locked.locked_until-locked.updated_at==timedelta(minutes=15)
    assert fail(repo,account)==locked
    with open_browser_identity(dsn) as pool:
        reopened=PostgresBrowserLocalAccountRepository(pool,tenant_id='synthetic-a',application_id='synthetic-browser-app')
        assert reopened.get_local_account(account.tenant_id,account.local_account_id)==locked
        assert fail(reopened,account)==locked
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==5


def test_concurrent_failures_lock_once_and_preserve_latest_revision(accounts,admin):
    repo,dsn,account=accounts
    with ThreadPoolExecutor(max_workers=8) as executor:results=list(executor.map(lambda _:fail(repo,account),range(12)))
    locked=repo.get_local_account(account.tenant_id,account.local_account_id)
    assert locked.revision==account.revision+5 and locked.lockout_level==1 and locked.failed_attempts==0
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==5
    assert all(r.revision<=locked.revision for r in results)


def test_future_time_wrong_tenant_operator_and_app_revoke_fail_without_writes(accounts,admin):
    repo,dsn,account=accounts
    cmd=RecordLocalAccountFailureCommand(tenant_id=account.tenant_id,local_account_id=account.local_account_id,failed_at=repo.current_time()+timedelta(days=1))
    with pytest.raises(IdentityUnavailable):repo.record_authentication_failure(cmd,operator=OP)
    cmd=cmd.model_copy(update={'failed_at':repo.current_time(),'tenant_id':'synthetic-b'})
    with pytest.raises(IdentityUnavailable):repo.record_authentication_failure(cmd,operator=OP)
    with pytest.raises(IdentityUnavailable):repo.record_authentication_failure(cmd.model_copy(update={'tenant_id':account.tenant_id}),operator=OP.model_copy(update={'operator_principal_id':'fake'}))
    admin.execute('update control.nexloop_browser_applications set active=false')
    with pytest.raises(IdentityUnavailable):fail(repo,account)
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==0
    assert admin.execute('select (payload->>\'revision\')::int from control.nexloop_browser_accounts').fetchone()[0]==1


def test_role_cannot_directly_edit_credential_or_audit(accounts):
    repo,dsn,account=accounts
    with repo.pool.connection() as c:
        for statement in ['update control.nexloop_browser_accounts set password_hash=null','select * from control.nexloop_browser_login_events']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():c.execute(statement)


def test_expired_lockout_policy_escalates_30_60_and_caps(accounts,admin):
    repo,dsn,account=accounts
    for prior_level,minutes in [(1,30),(2,60),(3,60)]:
        now=repo.current_time()
        # Explicit canonical auth fixture with an already-expired lock; no clock
        # modification and no claim of waiting 15/30/60 minutes in this test.
        expired=account.model_copy(update={'created_at':now-timedelta(hours=2),'updated_at':now-timedelta(minutes=2),
            'locked_until':now-timedelta(minutes=1),'lockout_level':prior_level,'failed_attempts':0})
        payload=expired.model_dump(mode='json');payload.pop('password_hash');payload.pop('password_history')
        admin.execute('update control.nexloop_browser_accounts set payload=%s',(Jsonb(payload),))
        for _ in range(5):locked=fail(repo,account)
        assert locked.lockout_level==min(prior_level+1,3) and locked.locked_until-locked.updated_at==timedelta(minutes=minutes)

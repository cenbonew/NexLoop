from concurrent.futures import ThreadPoolExecutor
from argon2 import PasswordHasher
import pytest
from eios.identity.models import EncodedPasswordHash
from eios.identity.ports import SaveLocalAccountCommand
from eios.identity.errors import IdentityUnavailable
from nexloop_eios.browser_identity import open_browser_identity,PostgresBrowserLocalAccountRepository
from test_browser_account_failures import accounts,OP,fail
from test_browser_identity_reads import identity


def desired(repo,account,**changes):
    return account.model_copy(update={'updated_at':repo.current_time(),'failed_attempts':0,'lockout_level':0,'locked_until':None,**changes})


def save(repo,account,expected=None):
    return repo.save_local_account(SaveLocalAccountCommand(local_account=account,expected_revision=account.revision if expected is None else expected),operator=OP)


def test_actual_reset_rehash_and_reopen(accounts,identity,admin):
    repo,dsn,account=accounts
    failed=fail(repo,account)
    replacement=EncodedPasswordHash(PasswordHasher().hash(identity[-1]))
    saved=save(repo,desired(repo,failed,password_hash=replacement))
    assert saved.revision==failed.revision+1 and saved.failed_attempts==0
    assert PasswordHasher().verify(saved.password_hash.get_secret_value(),identity[-1])
    with open_browser_identity(dsn) as pool:
        reopened=PostgresBrowserLocalAccountRepository(pool,tenant_id=repo.tenant_id,application_id=repo.application_id)
        assert reopened.get_local_account(account.tenant_id,account.local_account_id)==saved
    events=admin.execute("select details from control.nexloop_browser_login_events where event_type='local_password_state'").fetchall()
    assert len(events)==1 and events[0][0]['rehash'] is True
    assert replacement.get_secret_value() not in str(events)


def test_stale_revision_and_concurrent_save_commit_once(accounts,admin):
    repo,dsn,account=accounts;candidate=desired(repo,account)
    def attempt(_):
        try:return save(repo,candidate)
        except IdentityUnavailable:return None
    with ThreadPoolExecutor(max_workers=6) as executor:results=list(executor.map(attempt,range(6)))
    assert sum(v is not None for v in results)==1
    with pytest.raises(IdentityUnavailable):save(repo,candidate)
    assert admin.execute("select count(*) from control.nexloop_browser_login_events where event_type='local_password_state'").fetchone()[0]==1


def test_no_admin_changes_no_lock_bypass(accounts,admin):
    repo,dsn,account=accounts
    for changes in ({'username':'other'},{'session_epoch':2},{'must_change_password':True},{'status':'disabled'},{'subject_id':'other'},{'verified_email':'other@example.invalid'}):
        with pytest.raises(IdentityUnavailable):save(repo,desired(repo,account,**changes))
    for _ in range(5):locked=fail(repo,account)
    with pytest.raises(IdentityUnavailable):save(repo,desired(repo,locked))
    assert repo.get_local_account(account.tenant_id,account.local_account_id)==locked
    assert admin.execute("select count(*) from control.nexloop_browser_login_events where event_type='local_password_state'").fetchone()[0]==0


def test_revoked_app_and_wrong_operator_fail_closed(accounts,admin):
    repo,dsn,account=accounts;command=SaveLocalAccountCommand(local_account=desired(repo,account),expected_revision=account.revision)
    with pytest.raises(IdentityUnavailable):repo.save_local_account(command,operator=OP.model_copy(update={'operator_principal_id':'other'}))
    admin.execute('update control.nexloop_browser_applications set active=false')
    with pytest.raises(IdentityUnavailable):repo.save_local_account(command,operator=OP)


def test_raw_sql_null_timestamp_denied_without_python_validation(accounts,admin):
    import psycopg
    from psycopg.types.json import Jsonb
    repo,dsn,account=accounts
    body=account.model_dump(mode='json',exclude={'password_hash','password_history'})
    body['updated_at']=None
    with repo.pool.connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():
        c.execute('select control.nexloop_save_browser_account_password(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (repo.tenant_id,repo.application_id,'nexloop_identity',account.revision,Jsonb(body),account.password_hash.get_secret_value(),[],OP.request_id,OP.trace_id))
    assert repo.get_local_account(account.tenant_id,account.local_account_id)==account
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==0

import pytest
from eios.identity.local_accounts import LocalAccountService,_valid_recorded_failure
from eios.identity.evidence import AuthenticationEvidenceAuthority
from eios.identity.errors import CredentialInvalid,IdentityUnavailable
from eios.identity.ports import RecordLocalAccountFailureCommand
from test_browser_account_failures import accounts,OP
from test_browser_identity_reads import identity


def service(repo,verifier=None):
    # This utility verifies an already-resolved credential; it neither issues
    # evidence nor creates a browser Session. No Memory store is constructed.
    return LocalAccountService(repo,AuthenticationEvidenceAuthority(store=repo),subject_repository=repo,
        tenant_id=repo.tenant_id,application_id=repo.application_id,operator=OP,
        failure_result_verifier=repo.verify_failure_result if verifier is None else verifier)


def test_actual_password_verification_and_one_failure_per_attempt(accounts,identity,admin):
    repo,dsn,account=accounts;password=identity[-1]
    verifier=service(repo)
    verifier.verify_current_password(account,password)
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==0
    for expected in range(1,5):
        current=repo.get_local_account(account.tenant_id,account.local_account_id)
        with pytest.raises(CredentialInvalid):verifier.verify_current_password(current,'synthetic-wrong-password')
        assert repo.get_local_account(account.tenant_id,account.local_account_id).failed_attempts==expected
    with pytest.raises(CredentialInvalid):verifier.verify_current_password(repo.get_local_account(account.tenant_id,account.local_account_id),'synthetic-wrong-password')
    locked=repo.get_local_account(account.tenant_id,account.local_account_id)
    assert locked.lockout_level==1
    with pytest.raises(CredentialInvalid):verifier.verify_current_password(locked,password)
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==5


def test_database_time_gap_reconciled_by_durable_receipt(accounts):
    repo,dsn,before=accounts
    failed_at=repo.current_time()
    after=repo.record_authentication_failure(RecordLocalAccountFailureCommand(tenant_id=before.tenant_id,local_account_id=before.local_account_id,failed_at=failed_at),operator=OP)
    assert after.updated_at>failed_at
    assert not _valid_recorded_failure(before,after,failed_at)
    assert repo.verify_failure_result(before,after,failed_at,OP)
    assert not repo.verify_failure_result(before,after.model_copy(update={'failed_attempts':4}),failed_at,OP)
    assert not repo.verify_failure_result(before,after,failed_at,OP.model_copy(update={'request_id':'other-request'}))


def test_unverified_committed_result_never_retried(accounts,admin):
    repo,dsn,account=accounts;calls=[]
    def refuse(*args):calls.append(args);return False
    verifier=service(repo,refuse)
    with pytest.raises(IdentityUnavailable):verifier.verify_current_password(account,'synthetic-wrong-password')
    assert len(calls)==1
    assert repo.get_local_account(account.tenant_id,account.local_account_id).failed_attempts==1
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==1

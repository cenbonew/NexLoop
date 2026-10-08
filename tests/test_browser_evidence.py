from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import pytest
import psycopg
from psycopg.types.json import Jsonb
from eios.identity.evidence import AuthenticationEvidenceAuthority,AuthenticationEvidenceFacts,AuthenticationEvidenceRejected,authentication_evidence_proof_digest
from eios.identity.errors import CredentialInvalid,IdentityUnavailable
from eios.identity.local_accounts import LocalAccountService
from nexloop_eios.browser_evidence import PostgresBrowserEvidenceStore
from nexloop_eios.browser_identity import open_browser_identity
from test_browser_identity_reads import identity
from test_browser_account_failures import OP

@pytest.fixture
def store(identity):
    dsn,subject,membership,account,password=identity
    with open_browser_identity(dsn) as pool:
        yield PostgresBrowserEvidenceStore(pool,tenant_id=account.tenant_id,application_id='synthetic-browser-app')


def authenticate(store,identity):
    account=identity[3]
    service=LocalAccountService(store,AuthenticationEvidenceAuthority(store=store),subject_repository=store,
        tenant_id=store.tenant_id,application_id=store.application_id,operator=OP,failure_result_verifier=store.verify_failure_result)
    return service.authenticate(account.tenant_id,account.username,identity[-1])


def inspect(store,evidence):
    evidence_id,proof=evidence._export_for_trusted_transport()
    return store.inspect(evidence_id,authentication_evidence_proof_digest(proof),tenant_id=store.tenant_id,application_id=store.application_id,purpose='session.create')


def test_actual_password_authentication_issues_durable_evidence(store,identity,admin):
    auth=authenticate(store,identity);record=inspect(store,auth.evidence)
    assert record.credential_id==identity[3].local_account_id and record.authentication_methods==('password',)
    assert (record.expires_at-record.issued_at).total_seconds()==300
    with open_browser_identity(identity[0]) as pool:
        reopened=PostgresBrowserEvidenceStore(pool,tenant_id=store.tenant_id,application_id=store.application_id)
        assert inspect(reopened,auth.evidence)==record
    evidence_id,proof=auth.evidence._export_for_trusted_transport()
    assert store.inspect(evidence_id,b'x'*32,tenant_id=store.tenant_id,application_id=store.application_id,purpose='session.create') is None
    data=admin.execute('select facts,proof_digest from control.nexloop_browser_evidence').fetchone()
    assert proof not in str(data) and identity[-1] not in str(data) and identity[3].password_hash.get_secret_value() not in str(data)


def test_fail_then_success_resets_and_issues_latest_revision(store,identity,admin):
    service=LocalAccountService(store,AuthenticationEvidenceAuthority(store=store),subject_repository=store,
        tenant_id=store.tenant_id,application_id=store.application_id,operator=OP,failure_result_verifier=store.verify_failure_result)
    with pytest.raises(CredentialInvalid):service.authenticate(store.tenant_id,identity[3].username,'synthetic-wrong')
    assert admin.execute('select count(*) from control.nexloop_browser_evidence').fetchone()[0]==0
    auth=service.authenticate(store.tenant_id,identity[3].username,identity[-1]);record=inspect(store,auth.evidence)
    assert record.credential_revision==3 and store.get_local_account(store.tenant_id,identity[3].local_account_id).failed_attempts==0


def test_current_subject_account_application_revoke_and_expiry(store,identity,admin):
    auth=authenticate(store,identity)
    account=identity[3];changed=account.model_copy(update={'session_epoch':2,'revision':2})
    payload=changed.model_dump(mode='json',exclude={'password_hash','password_history'})
    admin.execute('update control.nexloop_browser_accounts set payload=%s',(Jsonb(payload),))
    assert inspect(store,auth.evidence) is None
    auth=authenticate(store,identity)
    admin.execute("update control.nexloop_browser_evidence set issued_at=transaction_timestamp()-interval '6 minutes',expires_at=transaction_timestamp()-interval '1 minute'")
    assert inspect(store,auth.evidence) is None
    auth=authenticate(store,identity)
    admin.execute('update control.nexloop_browser_applications set active=false')
    assert inspect(store,auth.evidence) is None
    with pytest.raises(IdentityUnavailable):authenticate(store,identity)


def test_duplicate_ids_digest_and_stale_facts_are_denied(store,identity,admin):
    record=inspect(store,authenticate(store,identity).evidence)
    facts=AuthenticationEvidenceFacts(**{k:getattr(record,k) for k in AuthenticationEvidenceFacts.__dataclass_fields__})
    with pytest.raises(IdentityUnavailable):store.issue(record.evidence_id,b'y'*32,facts)
    with pytest.raises(IdentityUnavailable):store.issue('synthetic-other',record.proof_digest,facts)
    with pytest.raises(AuthenticationEvidenceRejected):store.issue('synthetic-stale',b'z'*32,replace(facts,credential_revision=99))
    with ThreadPoolExecutor(max_workers=4) as executor:
        def issue(_):
            try:return store.issue('synthetic-concurrent',b'q'*32,facts)
            except IdentityUnavailable:return None
        results=list(executor.map(issue,range(4)))
    assert sum(v is not None for v in results)==1
    assert admin.execute('select count(*) from control.nexloop_browser_evidence').fetchone()[0]==2
    with store.pool.connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():
        c.execute('select * from control.nexloop_browser_evidence')

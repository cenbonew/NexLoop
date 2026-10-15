"""G-PW-RESET: nexloop_eios.identity_password_reset (operator reset of a local browser account's password) through the governed
SQL function of migration 0198 (temporary number; read from the catalog, never asserted).

Clean catalog PostgreSQL with a configured tenant and one Human created by initial_identity (test_trusted_configuration_pg). After a
reset: the old password no longer signs in, the new one does, a session created before the reset no longer reads (session_epoch),
failed attempts are cleared, the previous hash is in the history and one evidence row names the operator label and request id;
the same request replays without a change; a non-private password file, a non-identity role and a missing --confirm-reset are
refused; no output ever contains the password."""
import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from test_trusted_configuration_pg import configured, identity_manifest, create_human  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def login(f, username, password):
    """A real local-account login and browser session (the workbench/WebChat login path); returns the session token digest or None."""
    from eios.identity.errors import CredentialInvalid
    from eios.identity.evidence import AuthenticationEvidenceAuthority
    from eios.identity.local_accounts import LocalAccountService
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    from nexloop_eios.browser_identity import open_browser_identity
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    tenant, application = f['tenant'], 'explicit-local-login'
    with open_browser_identity(make_conninfo(f['pg'], user='nexloop_identity'), pool_max=1) as pool:
        store = PostgresBrowserSessionUnitOfWork(pool, tenant_id=tenant, application_id=application)
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        passwords = LocalAccountService(store, AuthenticationEvidenceAuthority(store=store), subject_repository=store, tenant_id=tenant,
                                        application_id=application, operator=op, failure_result_verifier=store.verify_failure_result)
        try:
            auth = passwords.authenticate(tenant, username, password)
        except CredentialInvalid:
            return None
        member = [m for m in store.list_memberships(auth.subject_id) if m.tenant_id == tenant][0]
        issued = BrowserSessionService(store, store, application_id=application, operator=op).create(tenant, auth.subject_id, member.principal_id, auth.evidence)
        return issued.session.session_token_digest  # SecretDigest32, as the session store reads it


def session_reads(f, digest):
    from nexloop_eios.browser_identity import open_browser_identity
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    with open_browser_identity(make_conninfo(f['pg'], user='nexloop_identity'), pool_max=1) as pool:
        try:
            return PostgresBrowserSessionUnitOfWork(pool, tenant_id=f['tenant'], application_id='explicit-local-login').find_by_token_digest(digest) is not None
        except Exception:
            return False


def account(admin, f):
    return admin.execute("select payload, password_history from control.nexloop_browser_accounts where tenant_id=%s", (f['tenant'],)).fetchone()


def tool(f, *extra, dsn_user='nexloop_identity', confirm=True):
    path = f['tmp'] / ('dsn-' + dsn_user)
    if not path.exists():
        path.write_text(make_conninfo(f['pg'], user=dsn_user))
        path.chmod(0o600)
    argv = [sys.executable, '-m', 'nexloop_eios.identity_password_reset', '--identity-database-url-file', str(path), '--tenant', f['tenant'],
            '--application-id', 'explicit-local-login', '--username', 'explicit-user', '--operator-label', 'stage-owner', *extra,
            *(['--confirm-reset'] if confirm else [])]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, env={**os.environ, 'PYTHONPATH': str(ROOT / 'packages/eios-core/src')})
    return done.returncode, done.stdout, done.stderr


@pytest.fixture
def human(configured, tmp_path):  # noqa: F811
    f = configured
    create_human(f, identity_manifest(f))
    f = dict(f)
    f['tmp'] = tmp_path
    return f


def test_reset_replaces_the_password_ends_sessions_clears_failures_and_replays(human, admin):
    f = human
    old = f['paths']['password'].read_text()
    digest = login(f, 'explicit-user', old)
    assert digest and session_reads(f, digest)
    assert login(f, 'explicit-user', 'wrong-password-1') is None  # one recorded failure
    before, _ = account(admin, f)
    assert before['failed_attempts'] == 1
    new = f['tmp'] / 'new-password'
    request = 'pw-reset-' + str(uuid.uuid4())
    code, out, err = tool(f, '--generate-password-file', str(new), '--request-id', request)
    assert code == 0, (out, err)
    result = json.loads(out)
    password = new.read_text().strip()
    assert len(password) == 20 and new.stat().st_mode & 0o777 == 0o600
    assert password not in out + err and old not in out + err
    assert result['reset'] is True and result['replayed'] is False and result['revision'] == before['revision'] + 1
    assert result['session_epoch'] == before['session_epoch'] + 1
    after, history = account(admin, f)
    assert (after['failed_attempts'], after['lockout_level'], after['locked_until']) == (0, 0, None)
    assert after['updated_at'] > before['updated_at'] and len(history) == 1  # the previous hash, nothing else
    assert not session_reads(f, digest)                    # the session created before the reset is gone
    assert login(f, 'explicit-user', old) is None          # the old password no longer signs in
    assert login(f, 'explicit-user', password) is not None  # the new one does
    events = admin.execute("select operator_principal_id, request_id, details from control.nexloop_browser_login_events "
                           "where tenant_id=%s and event_type='operator_password_reset'", (f['tenant'],)).fetchall()
    assert [(e[0], e[1], e[2]['operator_label'], e[2]['account_revision']) for e in events] == [('nexloop_identity', request, 'stage-owner', result['revision'])]
    assert password not in json.dumps([e[2] for e in events])
    # Replay: the same request with the same password changes nothing.
    current, _ = account(admin, f)
    code, out, err = tool(f, '--password-file', str(new), '--request-id', request)
    assert code == 0 and json.loads(out)['replayed'] is True and json.loads(out)['revision'] == current['revision'], (out, err)
    assert account(admin, f)[0]['revision'] == current['revision']
    # A new request with the current password is password reuse, not a reset.
    code, out, err = tool(f, '--password-file', str(new), '--request-id', 'pw-reset-' + str(uuid.uuid4()))
    assert code == 2 and json.loads(err)['reason'] == 'password reuse or request conflict'
    # A password from the history is refused before any write.
    old_file = f['tmp'] / 'old-password'
    old_file.write_text(old)
    old_file.chmod(0o600)
    code, out, err = tool(f, '--password-file', str(old_file), '--request-id', 'pw-reset-' + str(uuid.uuid4()))
    assert code == 2 and json.loads(err)['reason'] == 'password reuse'
    assert account(admin, f)[0]['revision'] == current['revision']


def test_refusals_change_nothing(human, admin):
    f = human
    before, _ = account(admin, f)
    loose = f['tmp'] / 'loose-password'
    loose.write_text(secrets.token_urlsafe(16))
    loose.chmod(0o644)
    code, out, err = tool(f, '--password-file', str(loose), '--request-id', 'pw-reset-loose')
    assert code == 2 and json.loads(err) == {'ok': False, 'reason': 'PermissionError'}
    private = f['tmp'] / 'private-password'
    private.write_text(secrets.token_urlsafe(16))
    private.chmod(0o600)
    code, out, err = tool(f, '--password-file', str(private), '--request-id', 'pw-reset-unconfirmed', confirm=False)
    assert code == 2 and json.loads(err)['reason'] == '--confirm-reset required'
    code, out, err = tool(f, '--password-file', str(private), '--request-id', 'pw-reset-api', dsn_user='nexloop_api')  # not the identity role
    assert code == 2 and json.loads(err)['ok'] is False
    existing = f['tmp'] / 'existing'
    existing.write_text('x')
    code, out, err = tool(f, '--generate-password-file', str(existing), '--request-id', 'pw-reset-existing')
    assert code == 2 and json.loads(err)['reason'] == 'generated password file exists' and existing.read_text() == 'x'
    assert account(admin, f)[0] == before
    # The SQL function itself: no role but nexloop_identity may execute it.
    for role in ('nexloop_api', 'nexloop_configurator'):
        with psycopg.connect(make_conninfo(f['pg'], user=role)) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('select control.nexloop_operator_reset_browser_password(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                          (f['tenant'], 'explicit-local-login', role, before['local_account_id'], before['revision'], '$argon2id$x', 'r', 't', 'l', False))
    assert account(admin, f)[0] == before
    latest = admin.execute("select max(version) from control.schema_migrations").fetchone()[0]
    catalog = json.loads((ROOT / 'packages/eios-core/src/eios/migrations/catalog.json').read_text())['migrations'][-1]['version']
    assert latest == catalog

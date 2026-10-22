"""G-PW-RESET (owner-approved, 2026-10-15): an identity operator resets a local browser account's password.

For accounts whose password nobody knows and whose e-mail reset cannot be delivered (the stage's simulated WebChat customers have
.invalid addresses). Runs as the identity operator (nexloop_identity, the API's identity DSN) through the governed SQL function
control.nexloop_operator_reset_browser_password (migration 0198, temporary number): optimistic lock on the account revision, the new
argon2id hash, the previous hash into the history, session_epoch + 1 (every existing browser session fails), failures and lockout
cleared, one evidence row (operator label, request id; never the password). No table is written directly.

    python -m nexloop_eios.identity_password_reset --identity-database-url-file F --tenant T --application-id A
        (--username U | --local-account-id ID) (--password-file F | --generate-password-file NEW_F)
        --request-id R --operator-label L --confirm-reset

* The new password comes from a private file (0600, owned by this process user; read_private_text), or the tool generates 20
  random characters into a NEW private file (0600; an existing file is refused) for the owner to read, store and shred.
* Only an active local account of an active human subject (SQL checks it); the initial identity allowance quota is not touched.
* --request-id is a stable business-intent id: the same request again replays (nothing changes, `replayed: true`).
* The new password must be at least 12 characters and differ from the current one and from the history (a match with the
  current one is accepted only as the replay of the request that set it).
Output: one JSON object of ids, revision and epoch; never the password or a hash. Exit 0 reset or replayed, 2 refused.
"""
import argparse
import json
import os
import re
import secrets
import string
import sys
import uuid
from pathlib import Path

GENERATED_LENGTH = 20
MINIMUM_LENGTH = 12
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,319}')


class PasswordResetRefused(ValueError):
    pass


def generate_password_file(path):
    """20 random letters and digits into a new private file (0600, never overwritten)."""
    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for _ in range(GENERATED_LENGTH))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(password + '\n')
    os.chmod(path, 0o600)


def _matches(encoded, password):
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError
    try:
        return bool(PasswordHasher().verify(encoded, password))
    except (InvalidHashError, VerificationError, ValueError):
        return False


def password_problem(password, current_hash, history_hashes):
    """The rules every password reset shares (0198 tool, NX-058 owner-issued codes): 12 to 1024 characters, not the current password
    and none of the history. Returns a problem code, or None."""
    if type(password) is not str or len(password) < MINIMUM_LENGTH or len(password) > 1024:
        return 'password_length'
    if (current_hash is not None and _matches(current_hash, password)) or any(_matches(h, password) for h in history_hashes):
        return 'password_reuse'
    return None


def reset_password(*, identity_database_url_file, tenant, application_id, password_file, request_id, operator_label, username=None,
                   local_account_id=None):
    from argon2 import PasswordHasher
    from nexloop_eios.browser_identity import PostgresBrowserIdentityReader, open_browser_identity, verify_identity_role
    from nexloop_eios.private_configuration import read_private_text
    for name, value in (('tenant', tenant), ('application_id', application_id), ('request_id', request_id), ('operator_label', operator_label)):
        if type(value) is not str or not _ID.fullmatch(value):
            raise PasswordResetRefused(name)
    if (username is None) == (local_account_id is None):
        raise PasswordResetRefused('exactly one of username / local_account_id')
    password = read_private_text(password_file, maximum=4096).strip()
    if len(password) < MINIMUM_LENGTH or len(password) > 1024:
        raise PasswordResetRefused('password length')
    with open_browser_identity(read_private_text(identity_database_url_file, maximum=16384).strip(), pool_max=1) as pool:
        reader = PostgresBrowserIdentityReader(pool, tenant_id=tenant, application_id=application_id)
        account = reader.find_by_username(tenant, username) if username is not None else reader.get_local_account(tenant, local_account_id)
        if account is None:
            raise PasswordResetRefused('account not found')
        current = account.password_hash.get_secret_value() if account.password_hash is not None else None
        if any(_matches(h.get_secret_value(), password) for h in account.password_history):
            raise PasswordResetRefused('password reuse')
        replay_only = current is not None and _matches(current, password)
        encoded = PasswordHasher().hash(password)
        with pool.connection() as db, db.transaction():
            verify_identity_role(db)
            try:
                result = db.execute('select control.nexloop_operator_reset_browser_password(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                    (tenant, application_id, 'nexloop_identity', account.local_account_id, account.revision, encoded, request_id,
                                     str(uuid.uuid4()), operator_label, replay_only)).fetchone()[0]
            except Exception as error:
                if getattr(error, 'sqlstate', None) == '22023':
                    raise PasswordResetRefused('password reuse or request conflict') from None
                raise
    return {'reset': not result['replayed'], 'replayed': result['replayed'], 'tenant_id': tenant, 'application_id': application_id,
            'account_id': account.local_account_id, 'revision': result['revision'], 'session_epoch': result['session_epoch']}


def main(argv=None):
    p = argparse.ArgumentParser(description='Identity operator password reset (G-PW-RESET)')
    p.add_argument('--identity-database-url-file', type=Path, required=True)
    p.add_argument('--tenant', required=True)
    p.add_argument('--application-id', required=True)
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument('--username')
    who.add_argument('--local-account-id')
    secret = p.add_mutually_exclusive_group(required=True)
    secret.add_argument('--password-file', type=Path)
    secret.add_argument('--generate-password-file', type=Path)
    p.add_argument('--request-id', required=True)
    p.add_argument('--operator-label', required=True)
    p.add_argument('--confirm-reset', action='store_true')
    a = p.parse_args(argv)
    try:
        if not a.confirm_reset:
            raise PasswordResetRefused('--confirm-reset required')
        path = a.password_file
        if a.generate_password_file is not None:
            if a.generate_password_file.exists() or a.generate_password_file.is_symlink():
                raise PasswordResetRefused('generated password file exists')
            generate_password_file(a.generate_password_file)
            path = a.generate_password_file
        out = reset_password(identity_database_url_file=a.identity_database_url_file, tenant=a.tenant, application_id=a.application_id,
                             password_file=path, request_id=a.request_id, operator_label=a.operator_label, username=a.username,
                             local_account_id=a.local_account_id)
        print(json.dumps(out, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - fixed text only: never a password, hash or connection detail
        print(json.dumps({'ok': False, 'reason': str(error) if isinstance(error, PasswordResetRefused) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

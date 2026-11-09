#!/usr/bin/env python3
"""NX-065: workbench single sign-on (OIDC) acceptance against a synthetic IdP on stage (docs/release/run-nx065-oidc-acceptance.md,
design docs/design/nx065-synthetic-idp.md). It drives the deployed workbench through its public routes only (owner login, the governed
link/unlink Actions, /oidc/start and /oidc/callback, /auth/session); it reads no database and changes nothing but the one synthetic
member's link, which it removes at the end.

    security find-generic-password -s <keychain service> -a <owner account> -w | \\
      python3 scripts/acceptance/nx065_oidc_acceptance.py --config <private config 0600>

The owner's password comes only from stdin (never argv, output or exception text); run the script file, never `python -`.
The IdP sign-in is done by the person at the terminal (it is the IdP's own page, whatever product the owner picks): the script prints
the authorization URL on the terminal (/dev/tty, not stdout), the person signs in as the named synthetic user and pastes back the URL
the browser was sent to (only its code and state are used), or types `refused` when the IdP refused the sign-in.

Configuration (private JSON, 0600, owned by the runner):
    {"schema": "nexloop-nx065-oidc-acceptance/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>",
     "provider_id": "<PROVIDER_ID>", "owner_username_file": "<0600 file>",
     "member": {"principal_id": "<synthetic operator or reviewer principal>"},
     "users": {"A": {"subject": "<IdP sub of A>"}, "B": {"subject": "<IdP sub of B, never linked>"}},
     "owner_group": "<name of the IdP group A is put in that would mean owner, if groups mapped to roles>"}

Checks (one JSON line each, then a summary; no subject, code, state, cookie, token, username or password is printed):
  login.unlinked_refused      B (never linked) is sent back to /workbench?sso=failed and has no session
  login.linked_signs_in       the owner links the member to A (sso_or_password); A signs in as the member, methods ["oidc"]
  login.callback_one_time     the same callback a second time is refused and opens no session
  groups.no_role_from_idp     A, placed in owner_group at the IdP, still cannot run an owner Action (403)
  groups.mapping              GAP: the product maps no IdP group to a role (roles come only from the workbench roles manifest)
  disable.idp_refuses         A disabled at the IdP: a new SSO sign-in does not complete
  disable.sync                GAP: an existing SSO session outlives the IdP-side disable (no back-channel, no periodic check)
  epoch.unlink_ends_sessions  the owner unlinks the member: both SSO sessions end (401)
  epoch.relink_new_only       A re-enabled and re-linked signs in again; the sessions ended before stay ended
Exit: 0 every check passed and no gap; 3 every check passed but gaps remain (NX-065 is not complete); 1 a check failed;
2 input refused.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from urllib.parse import parse_qs, urlsplit

SCHEMA = 'nexloop-nx065-oidc-acceptance/1'
AUTH = '/api/v1/workbench/auth'
START, CALLBACK = AUTH + '/oidc/start', AUTH + '/oidc/callback'
FAILED = '/workbench?sso=failed'


class InputRefused(Exception):
    pass


class IdpRefused(Exception):
    """The IdP did not let the user sign in (disabled, wrong password, ...)."""


def read_private(path, maximum=65536):
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid() or st.st_size > maximum:
        raise InputRefused(f'{os.path.basename(str(path))}: a regular file, 0600, owned by the runner')
    with open(path, encoding='utf-8') as f:
        return f.read()


def load_config(path):
    try:
        config = json.loads(read_private(path))
    except InputRefused:
        raise
    except Exception:
        raise InputRefused('configuration: not JSON') from None
    needed = {'schema', 'origin', 'provider_id', 'owner_username_file', 'member', 'users', 'owner_group'}
    if type(config) is not dict or not needed <= set(config) or not set(config) <= needed | {'ca_file'} or config['schema'] != SCHEMA:
        raise InputRefused('configuration: fields ' + ', '.join(sorted(needed)) + ' (and ca_file)')
    if not str(config['origin']).startswith('https://') or type(config['member']) is not dict or not config['member'].get('principal_id'):
        raise InputRefused('configuration: https origin and member.principal_id')
    users = config['users']
    if type(users) is not dict or set(users) != {'A', 'B'} or any(type(u) is not dict or not u.get('subject') for u in users.values()):
        raise InputRefused('configuration: users A and B with their subjects')
    if users['A']['subject'] == users['B']['subject']:
        raise InputRefused('configuration: A and B are different IdP users')
    return config


class TerminalIdp:
    """The person at the terminal signs in at the IdP. Prompts go to /dev/tty so stdout stays the JSON report."""

    def __init__(self, tty=None):
        self.tty = tty

    def _ask(self, text):
        tty = self.tty or open('/dev/tty', 'r+', encoding='utf-8')
        try:
            tty.write(text)
            tty.flush()
            return tty.readline().strip()
        finally:
            if self.tty is None:
                tty.close()

    def authorize(self, location, user):
        answer = self._ask(f'\nOpen in the browser and sign in at the IdP as synthetic user {user}:\n{location}\n'
                           'Paste the URL the browser was sent back to (or type refused): ')
        if answer == 'refused':
            raise IdpRefused(user)
        query = {k: v[0] for k, v in parse_qs(urlsplit(answer).query).items()}
        if not {'code', 'state'} <= set(query):
            raise IdpRefused(user)
        return {'code': query['code'], 'state': query['state']}

    def set_group(self, user, group, member):
        self._ask(f'\nAt the IdP admin console {"add" if member else "remove"} synthetic user {user} '
                  f'{"to" if member else "from"} group {group}, then press Enter: ')

    def set_enabled(self, user, enabled):
        self._ask(f'\nAt the IdP admin console {"enable" if enabled else "disable"} synthetic user {user}, then press Enter: ')


def http_client(config):
    import httpx
    return httpx.Client(base_url=config['origin'], verify=config.get('ca_file') or True, follow_redirects=False, trust_env=False, timeout=15.0)


class Acceptance:
    def __init__(self, config, owner_password, idp, client_factory):
        self.config, self.idp, self.new_client = config, idp, client_factory
        self._password = owner_password
        self.origin = config['origin'].rstrip('/')
        self.results = []

    # -- workbench calls ------------------------------------------------------------------------------------------------------
    def _headers(self, csrf=None):
        headers = {'Origin': self.origin}
        if csrf:
            headers.update({'X-CSRF-Token': csrf, 'Idempotency-Key': 'nx065-' + secrets.token_hex(12)})
        return headers

    def owner(self):
        client = self.new_client()
        username = read_private(self.config['owner_username_file'], 512).strip()
        r = client.post(AUTH + '/login', headers=self._headers(), json={'username': username, 'password': self._password})
        if r.status_code != 200:
            raise InputRefused(f'owner login: HTTP {r.status_code}')
        return client, r.json()['csrf_token']

    def act(self, client, csrf, operation, body):
        return client.post('/api/v1/workbench/actions/' + operation, headers=self._headers(csrf), json=body)

    def link(self, client, csrf, subject):
        return self.act(client, csrf, 'link_workbench_external', {'target_principal': self.config['member']['principal_id'],
                        'provider_id': self.config['provider_id'], 'external_subject': subject, 'login_mode': 'sso_or_password'})

    def unlink(self, client, csrf):
        return self.act(client, csrf, 'unlink_workbench_external', {'target_principal': self.config['member']['principal_id']})

    def sign_in(self, user):
        """A fresh browser: start, sign in at the IdP, come back. Returns (client, callback response, query) or raises IdpRefused."""
        client = self.new_client()
        started = client.get(START, params={'provider': self.config['provider_id']}, follow_redirects=False)
        if started.status_code != 302:
            raise AssertionError(f'start: HTTP {started.status_code}')
        query = self.idp.authorize(started.headers['location'], user)
        return client, client.get(CALLBACK, params=query, follow_redirects=False), query

    @staticmethod
    def session(client):
        r = client.get(AUTH + '/session')
        return r.status_code, (r.json() if r.status_code == 200 else None)

    # -- report ---------------------------------------------------------------------------------------------------------------
    def record(self, check, status, **observed):
        self.results.append({'check': check, 'status': status, **observed})
        print(json.dumps(self.results[-1], sort_keys=True), flush=True)

    def expect(self, check, ok, **observed):
        self.record(check, 'passed' if ok else 'failed', **observed)
        return ok

    # -- the run --------------------------------------------------------------------------------------------------------------
    def run(self):
        member = self.config['member']['principal_id']
        users = self.config['users']
        owner, csrf = self.owner()
        try:
            self._run(owner, csrf, member, users)
        finally:
            self.unlink(owner, csrf)  # leave the synthetic member unlinked (404/409 when already unlinked is fine)
            try:
                self.idp.set_enabled('A', True)
                self.idp.set_group('A', self.config['owner_group'], False)
            finally:
                owner.post(AUTH + '/logout', headers=self._headers(csrf))
        return self.summary()

    def _run(self, owner, csrf, member, users):
        # login
        b, refused, _ = self.sign_in('B')
        code, _ = self.session(b)
        self.expect('login.unlinked_refused', refused.status_code == 303 and refused.headers.get('location') == FAILED and code == 401,
                    http=refused.status_code, session_http=code)
        linked = self.link(owner, csrf, users['A']['subject'])
        if not self.expect('login.linked_signs_in.link', linked.status_code == 200, http=linked.status_code):
            return
        first, done, query = self.sign_in('A')
        code, view = self.session(first)
        self.expect('login.linked_signs_in', done.status_code == 303 and done.headers.get('location') == '/workbench' and code == 200
                    and view['principal_id'] == member and view.get('authentication_methods') == ['oidc'],
                    http=done.status_code, session_http=code, methods=(view or {}).get('authentication_methods'))
        replay = self.new_client()
        again = replay.get(CALLBACK, params=query, follow_redirects=False)
        code, _ = self.session(replay)
        self.expect('login.callback_one_time', again.status_code == 303 and again.headers.get('location') == FAILED and code == 401,
                    http=again.status_code, session_http=code)
        # groups: an IdP group never grants a workbench role
        self.idp.set_group('A', self.config['owner_group'], True)
        grouped, done, _ = self.sign_in('A')
        member_csrf = grouped.post(AUTH + '/csrf', headers=self._headers())
        attempt = self.act(grouped, member_csrf.json().get('csrf_token', '') if member_csrf.status_code == 200 else '', 'link_workbench_external',
                           {'target_principal': member, 'provider_id': self.config['provider_id'], 'external_subject': users['B']['subject'],
                            'login_mode': 'sso_or_password'})
        self.expect('groups.no_role_from_idp', done.status_code == 303 and attempt.status_code == 403, http=attempt.status_code)
        self.record('groups.mapping', 'gap', observed='roles come only from the workbench roles manifest; no ID token group claim is read')
        # disable at the IdP
        self.idp.set_enabled('A', False)
        try:
            _, done, _ = self.sign_in('A')
            completed = done.status_code == 303 and done.headers.get('location') == '/workbench'
        except IdpRefused:
            completed = False
        self.expect('disable.idp_refuses', not completed)
        code, _ = self.session(first)
        self.record('disable.sync', 'gap' if code == 200 else 'passed', session_http=code,
                    observed='an SSO session outlives the IdP-side disable until the owner unlinks the member' if code == 200 else None)
        # session epoch: unlinking ends every SSO session of the member
        unlinked = self.unlink(owner, csrf)
        codes = [self.session(c)[0] for c in (first, grouped)]
        self.expect('epoch.unlink_ends_sessions', unlinked.status_code == 200 and codes == [401, 401], http=unlinked.status_code, session_http=codes)
        self.idp.set_enabled('A', True)
        self.idp.set_group('A', self.config['owner_group'], False)
        relinked = self.link(owner, csrf, users['A']['subject'])
        fresh, done, _ = self.sign_in('A')
        codes = [self.session(c)[0] for c in (fresh, first, grouped)]
        self.expect('epoch.relink_new_only', relinked.status_code == 200 and done.status_code == 303 and codes == [200, 401, 401],
                    http=relinked.status_code, session_http=codes)

    def summary(self):
        statuses = [r['status'] for r in self.results]
        exit_code = 1 if 'failed' in statuses else 3 if 'gap' in statuses else 0
        print(json.dumps({'summary': {s: statuses.count(s) for s in ('passed', 'failed', 'gap')}, 'exit': exit_code}, sort_keys=True), flush=True)
        return exit_code


def main(argv=None, *, stdin=None, idp=None, client_factory=None):
    parser = argparse.ArgumentParser(description='NX-065 OIDC acceptance against a synthetic IdP (owner password on stdin)')
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        password = (stdin or sys.stdin).readline().rstrip('\n')
        if not password:
            raise InputRefused('owner password: one line on stdin')
        run = Acceptance(config, password, idp or TerminalIdp(), client_factory or (lambda: http_client(config)))
        return run.run()
    except InputRefused as refused:
        print(json.dumps({'refused': str(refused)}), flush=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())

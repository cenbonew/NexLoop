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
     "owner_group": "<name of the IdP group A is put in that would mean owner, if groups mapped to roles>",
     "group_mapping": null | {"operator_group": "<IdP group mapped to operator>", "reviewer_group": "<IdP group mapped to reviewer>",
                              "expected_revision": <the provider's current mapping revision, 0 before the first>},
     "backchannel": true | false, "backchannel_wait_seconds": 60, "max_session_seconds": <the provider's, 900-43200>}
With group_mapping the script sets {operator_group: operator, reviewer_group: reviewer} by the owner's governed Action and clears it at
the end (NX-067); without it groups.mapping is reported not_configured. With backchannel false disable.sync.backchannel is reported
not_configured. disable.sync.max_session waits max_session_seconds + 60 seconds (use an acceptance provider with 900).

Checks (one JSON line each, then a summary; no subject, code, state, cookie, token, username or password is printed):
  login.unlinked_refused      B (never linked) is sent back to /workbench?sso=failed and has no session
  login.linked_signs_in       the owner links the member to A (sso_or_password); A signs in as the member, methods ["oidc"]
  login.callback_one_time     the same callback a second time is refused and opens no session
  groups.no_role_from_idp     A, placed in owner_group at the IdP, still cannot run an owner Action: its session and CSRF fetch are 200,
                              a control call with a wrong CSRF token is 401 (so the Origin was accepted) and the attempt is 403 forbidden
  groups.mapping              with group_mapping: A in operator_group signs in and its operator Action is not refused; A in
                              reviewer_group only is refused (operator and reviewer share no Action) and the session it had loses the
                              operator Action (403); back in operator_group it signs in again. Without group_mapping: GAP not_configured
  disable.idp_refuses         A disabled at the IdP: a new SSO sign-in does not complete
  disable.sync.backchannel    the first SSO session is 200 before the disable and 401 within backchannel_wait_seconds after the IdP's
                              back-channel logout (passed); still 200 is failed; backchannel false: GAP not_configured
  disable.sync.max_session    a fresh SSO session is 200 and 401 after max_session_seconds + 60 seconds (the upper bound without a
                              back-channel)
  epoch.unlink_ends_sessions  the owner unlinks the member: both SSO sessions end (401)
  epoch.relink_new_only       A re-enabled and re-linked signs in again; the sessions ended before stay ended
All ten checks must appear, else run.incomplete fails the run; an exception ends the run with one failed line (its type
only) and the clean-up steps still run one by one.
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
REQUIRED = frozenset({'login.unlinked_refused', 'login.linked_signs_in', 'login.callback_one_time', 'groups.no_role_from_idp', 'groups.mapping',
                      'disable.idp_refuses', 'disable.sync.backchannel', 'disable.sync.max_session', 'epoch.unlink_ends_sessions',
                      'epoch.relink_new_only'})
QUESTION_PROBE = '00000000-0000-4000-8000-000000000065'  # an operator Action on a question that does not exist: 403 only when refused


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
    needed = {'schema', 'origin', 'provider_id', 'owner_username_file', 'member', 'users', 'owner_group', 'group_mapping', 'backchannel',
              'max_session_seconds'}
    if type(config) is not dict or not needed <= set(config) or not set(config) <= needed | {'ca_file', 'backchannel_wait_seconds'} or config['schema'] != SCHEMA:
        raise InputRefused('configuration: fields ' + ', '.join(sorted(needed)) + ' (and ca_file, backchannel_wait_seconds)')
    mapping = config['group_mapping']
    if mapping is not None and (type(mapping) is not dict or set(mapping) != {'operator_group', 'reviewer_group', 'expected_revision'}
                                or not all(type(mapping[k]) is str and mapping[k] for k in ('operator_group', 'reviewer_group'))
                                or mapping['operator_group'] == mapping['reviewer_group']
                                or type(mapping['expected_revision']) is not int or mapping['expected_revision'] < 0):
        raise InputRefused('configuration: group_mapping null or {operator_group, reviewer_group, expected_revision}')
    if type(config['backchannel']) is not bool or type(config['max_session_seconds']) is not int or not 900 <= config['max_session_seconds'] <= 43200:
        raise InputRefused('configuration: backchannel true or false, max_session_seconds 900-43200')
    wait = config.get('backchannel_wait_seconds', 60)
    if type(wait) is not int or not 1 <= wait <= 600:
        raise InputRefused('configuration: backchannel_wait_seconds 1-600')
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
        what = 'enable' if enabled else 'disable (and end every IdP session of)'
        self._ask(f'\nAt the IdP admin console {what} synthetic user {user}, then press Enter: ')


def http_client(config):
    import httpx
    return httpx.Client(base_url=config['origin'], verify=config.get('ca_file') or True, follow_redirects=False, trust_env=False, timeout=15.0)


class Acceptance:
    def __init__(self, config, owner_password, idp, client_factory, sleep=None):
        import time
        self.config, self.idp, self.new_client, self.sleep = config, idp, client_factory, sleep or time.sleep
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

    def set_mapping(self, owner, csrf, mappings, revision):
        return self.act(owner, csrf, 'set_sso_group_mapping', {'provider_id': self.config['provider_id'], 'group_claim': 'groups',
                                                                'mappings': mappings, 'expected_revision': revision})

    def clear_mapping(self, owner, csrf):
        if getattr(self, 'mapping_revision', None) is not None:
            if self.set_mapping(owner, csrf, {}, self.mapping_revision).status_code == 200:
                self.mapping_revision = None

    def operator_action(self, client):
        """403 when the member may not run its operator Action; anything else (the question does not exist) when it may."""
        token = client.post(AUTH + '/csrf', headers=self._headers())
        if token.status_code != 200:
            return token.status_code
        return self.act(client, token.json()['csrf_token'], 'answer_human_question',
                        {'question_id': QUESTION_PROBE, 'answer_kind': 'withdraw', 'option_index': None, 'text': None}).status_code

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
        except Exception as failure:  # L6 X3: one failed line with the exception type only (httpx text may carry the callback URL)
            self.record('run.aborted', 'failed', error=type(failure).__name__)
        finally:
            # Each clean-up step on its own: one that raises must not skip the others.
            for step in (lambda: self.unlink(owner, csrf),  # leave the synthetic member unlinked (404/409 when already unlinked is fine)
                         lambda: self.clear_mapping(owner, csrf),
                         lambda: self.config['group_mapping'] and self.idp.set_group('A', self.config['group_mapping']['operator_group'], False),
                         lambda: self.idp.set_enabled('A', True),
                         lambda: self.idp.set_group('A', self.config['owner_group'], False),
                         lambda: owner.post(AUTH + '/logout', headers=self._headers(csrf))):
                try:
                    step()
                except Exception as failure:
                    self.record('cleanup', 'failed', error=type(failure).__name__)
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
        grouped_session, _ = self.session(grouped)
        member_csrf = grouped.post(AUTH + '/csrf', headers=self._headers())
        token = member_csrf.json().get('csrf_token', '') if member_csrf.status_code == 200 else ''
        owner_only = {'target_principal': member, 'provider_id': self.config['provider_id'], 'external_subject': users['B']['subject'],
                      'login_mode': 'sso_or_password'}
        # L6 X2: a 403 is a permission refusal only when the session and its CSRF token were good. The control request (same session,
        # same Origin, a wrong CSRF token) must be refused as unauthenticated (401): the Origin check (403) comes before the CSRF check,
        # so the real attempt's 403 can then only be the grant check.
        control = self.act(grouped, 'not-' + secrets.token_hex(16), 'link_workbench_external', owner_only)
        attempt = self.act(grouped, token, 'link_workbench_external', owner_only)
        body = attempt.json() if attempt.headers.get('content-type', '').startswith('application/json') else {}
        self.expect('groups.no_role_from_idp', done.status_code == 303 and grouped_session == 200 and member_csrf.status_code == 200
                    and control.status_code == 401 and attempt.status_code == 403 and body.get('code') == 'forbidden',
                    http=attempt.status_code, csrf_http=member_csrf.status_code, control_http=control.status_code, session_http=grouped_session)
        self.idp.set_group('A', self.config['owner_group'], False)
        # disable at the IdP (L6 X1: the session must be live before the disable, else the result proves nothing)
        before, _ = self.session(first)
        self.idp.set_enabled('A', False)
        try:
            _, done, _ = self.sign_in('A')
            completed = done.status_code == 303 and done.headers.get('location') == '/workbench'
        except IdpRefused:
            completed = False
        self.expect('disable.idp_refuses', not completed)
        if not self.config['backchannel']:
            after, _ = self.session(first)
            self.record('disable.sync.backchannel', 'gap' if before == 200 else 'failed', observed='not_configured', session_http=[before, after])
        else:
            waited, after = 0, self.session(first)[0]
            while after == 200 and waited < self.config.get('backchannel_wait_seconds', 60):
                self.sleep(5)
                waited += 5
                after = self.session(first)[0]
            self.expect('disable.sync.backchannel', before == 200 and after == 401, session_http=[before, after], waited_seconds=waited)
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
        # Groups after the epoch checks: setting a mapping ends the provider's sessions (Z2). A stays in operator_group from here on.
        self.groups_mapping(owner, csrf)
        # The upper bound without a back-channel: a new SSO session ends max_session_seconds (+ 60 s leeway) after sign-in.
        last, done, _ = self.sign_in('A')
        live = self.session(last)[0]
        self.sleep(self.config['max_session_seconds'] + 60)
        ended = self.session(last)[0]
        self.expect('disable.sync.max_session', live == 200 and ended == 401, session_http=[live, ended],
                    waited_seconds=self.config['max_session_seconds'] + 60)

    def groups_mapping(self, owner, csrf):
        mapping = self.config['group_mapping']
        if mapping is None:
            self.record('groups.mapping', 'gap', observed='not_configured')
            return
        set_ = self.set_mapping(owner, csrf, {mapping['operator_group']: 'operator', mapping['reviewer_group']: 'reviewer'}, mapping['expected_revision'])
        if set_.status_code != 200:
            self.record('groups.mapping', 'failed', http=set_.status_code)
            return
        self.mapping_revision = set_.json()['revision']
        self.idp.set_group('A', mapping['operator_group'], True)
        mapped, done, _ = self.sign_in('A')
        allowed = self.operator_action(mapped) if done.status_code == 303 and done.headers.get('location') == '/workbench' else None
        self.idp.set_group('A', mapping['operator_group'], False)
        self.idp.set_group('A', mapping['reviewer_group'], True)
        _, narrowed, _ = self.sign_in('A')
        refused_after = self.operator_action(mapped)
        self.idp.set_group('A', mapping['reviewer_group'], False)
        self.idp.set_group('A', mapping['operator_group'], True)
        _, again, _ = self.sign_in('A')
        self.expect('groups.mapping', allowed not in (None, 401, 403, 503) and narrowed.headers.get('location') == FAILED and refused_after == 403
                    and again.headers.get('location') == '/workbench', operator_action_http=[allowed, refused_after],
                    narrowed=narrowed.headers.get('location') == FAILED)

    def summary(self):
        statuses = [r['status'] for r in self.results]
        missing = sorted(REQUIRED - {r['check'] for r in self.results})
        if missing:  # every one of the nine checks must have run
            self.record('run.incomplete', 'failed', missing=missing)
            statuses.append('failed')
        exit_code = 1 if 'failed' in statuses else 3 if 'gap' in statuses else 0
        print(json.dumps({'summary': {s: statuses.count(s) for s in ('passed', 'failed', 'gap')}, 'exit': exit_code}, sort_keys=True), flush=True)
        return exit_code


def main(argv=None, *, stdin=None, idp=None, client_factory=None, sleep=None):
    parser = argparse.ArgumentParser(description='NX-065 OIDC acceptance against a synthetic IdP (owner password on stdin)')
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        password = (stdin or sys.stdin).readline().rstrip('\n')
        if not password:
            raise InputRefused('owner password: one line on stdin')
        run = Acceptance(config, password, idp or TerminalIdp(), client_factory or (lambda: http_client(config)), sleep)
        return run.run()
    except InputRefused as refused:
        print(json.dumps({'refused': str(refused)}), flush=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())

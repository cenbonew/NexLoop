#!/usr/bin/env python3
"""NX-065: workbench single sign-on (OIDC) acceptance against a synthetic IdP on stage (docs/release/run-nx065-oidc-acceptance.md,
design docs/design/nx065-synthetic-idp.md). It drives the deployed workbench through its public routes only (owner login, the governed
link/unlink and group-mapping Actions, /oidc/start and /oidc/callback, /auth/session). Use an exclusive synthetic
provider, the agreed initial state and the password pipe documented in run-nx065-oidc-acceptance.md. Only the
runner's private stdin supplies the owner password and (when back-channel is tested) M1's password on the next line.
The IdP sign-in is done by the person at the terminal (it is the IdP's own page, whatever product the owner picks): the script prints
the authorization URL on the terminal (/dev/tty, not stdout), the person signs in as the named synthetic user and pastes back the URL
the browser was sent to (only its code and state are used), or types `refused` when the IdP refused the sign-in.

Configuration (private JSON, 0600, owned by the runner):
    {"schema": "nexloop-nx065-oidc-acceptance/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>",
     "provider_id": "<PROVIDER_ID>", "owner_username_file": "<0600 file>",
     "member": {"principal_id": "<synthetic operator principal>", "username_file": "<0600 file>"},
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
  groups.unmapped             A is in neither mapped group: a fresh login is refused and its prior authorized session loses the Action
  disable.idp_refuses         A disabled at the IdP: a new SSO sign-in does not complete
  disable.sync.backchannel    both the SSO and password sessions for M1 are 200 before the disable and 401 within backchannel_wait_seconds after the IdP's
                              back-channel logout (passed); still 200 is failed; backchannel false: GAP not_configured
  disable.sync.max_session    a fresh SSO session is 200 and 401 after max_session_seconds + 60 seconds (the upper bound without a
                              back-channel)
  epoch.unlink_ends_sessions  the owner unlinks the member: both SSO sessions end (401)
  epoch.relink_new_only       A re-enabled and re-linked signs in again; the sessions ended before stay ended
All eleven checks must appear, else run.incomplete fails the run; an exception ends the run with one failed line (its type
only) and the clean-up steps still run one by one.
Exit: 0 every check passed and no gap; 3 every check passed but gaps remain (NX-065 is not complete); 1 a check failed;
2 input refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from urllib.parse import parse_qs, urlsplit

SCHEMA = 'nexloop-nx065-oidc-acceptance/1'
AUTH = '/api/v1/workbench/auth'
START, CALLBACK = AUTH + '/oidc/start', AUTH + '/oidc/callback'
FAILED = '/workbench?sso=failed'
REQUIRED = frozenset({'login.unlinked_refused', 'login.linked_signs_in', 'login.callback_one_time', 'groups.no_role_from_idp', 'groups.mapping', 'groups.unmapped',
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
    if type(config['provider_id']) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', config['provider_id']):
        raise InputRefused('configuration: invalid provider identifier')
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
        if self.tty is not None:
            self.tty.write(text)
            self.tty.flush()
            line = self.tty.readline()
        else:
            with open('/dev/tty', 'w', encoding='utf-8') as prompt:
                prompt.write(text)
                prompt.flush()
            with open('/dev/tty', 'r', encoding='utf-8') as reply:
                line = reply.readline()
        if line == '':
            raise EOFError
        return line.strip()

    def verify_state(self, phase):
        if phase == 'unmapped':
            return self._ask('Read A in the IdP console and confirm neither mapped group is present. Type verified: ') == 'verified'
        scope = ('Confirm the exclusive test provider has no other links, its mapping is empty at the configured revision, '
                 'and M1 is unlinked. ' if phase == 'before' else
                 'Read back the M1 unlink and empty provider mapping at its final revision from the governed records. ')
        return self._ask(scope + 'In the IdP console verify A enabled and absent from all three test groups. '
                         'Type verified only after checking every item: ') == 'verified'

    def authorize(self, location, user):
        answer = self._ask(f'\nOpen in the browser and sign in at the IdP as synthetic user {user}:\n{location}\n'
                           'Paste the URL the browser was sent back to (or type refused): ')
        if answer == 'refused':
            raise IdpRefused(user)
        query = {k: v[0] for k, v in parse_qs(urlsplit(answer).query).items()}
        if not {'code', 'state'} <= set(query):
            raise InputRefused('callback requires code and state')
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
    def __init__(self, config, owner_password, idp, client_factory, sleep=None, member_password=None):
        import time
        self.config, self.idp, self.new_client, self.sleep = config, idp, client_factory, sleep or time.sleep
        self._password = owner_password
        self.member_password = member_password
        self.origin = config['origin'].rstrip('/')
        self.results = []
        self.link_state = 'unlinked'
        self.mapping_revision = None
        self.mapping_unknown = False
        self.idp_unknown = False
        self.dirty_groups = set()
        self.disabled_a = False

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

    def change_link(self, owner, csrf, subject=None):
        self.link_state = 'unknown'
        response = self.link(owner, csrf, subject) if subject is not None else self.unlink(owner, csrf)
        if response.status_code == 200:
            receipt = response.json()
            if (receipt.get('target_principal') == self.config['member']['principal_id']
                    and receipt.get('provider_id') == self.config['provider_id']):
                self.link_state = 'linked' if subject is not None else 'unlinked'
        if self.link_state == 'unknown':
            raise RuntimeError('link receipt unavailable')
        return response

    def change_group(self, group, enabled):
        self.idp_unknown = True
        self.idp.set_group('A', group, enabled)
        self.idp_unknown = False
        if enabled:
            self.dirty_groups.add(group)
        else:
            self.dirty_groups.discard(group)

    def enable_a(self, enabled):
        self.idp_unknown = True
        self.idp.set_enabled('A', enabled)
        self.idp_unknown = False
        self.disabled_a = not enabled

    def password_session(self):
        client = self.new_client()
        username = read_private(self.config['member']['username_file'], 512).strip()
        response = client.post(AUTH + '/login', headers=self._headers(), json={'username': username, 'password': self.member_password})
        if response.status_code != 200:
            raise InputRefused('synthetic member password login refused')
        return client

    def restore_mapping(self, owner, csrf):
        if self.mapping_unknown:
            return False
        if self.mapping_revision is None:
            return True
        revision = self.mapping_revision
        self.mapping_unknown = True
        response = self.set_mapping(owner, csrf, {}, revision)
        if response.status_code != 200:
            return False
        body = response.json()
        if body.get('provider_id') != self.config['provider_id'] or body.get('cleared') is not True or body.get('revision') != revision + 1:
            return False
        self.mapping_unknown = False
        self.mapping_revision = None
        return True

    def restore_link(self, owner, csrf):
        if self.link_state == 'linked':
            self.change_link(owner, csrf)
        return self.link_state == 'unlinked'

    def restore_idp(self):
        if self.idp_unknown:
            return False
        for group in sorted(self.dirty_groups):
            self.change_group(group, False)
        if self.disabled_a:
            self.enable_a(True)
        return True

    def operator_action(self, client):
        """(HTTP, known business probe). Missing question is 22023 -> 409 with reason=not_found;
        a generic 404 can also mean the Action was not published, before the grant check.
        """
        token = client.post(AUTH + '/csrf', headers=self._headers())
        if token.status_code != 200:
            return None, False  # No Action was attempted; not permission evidence.
        response = self.act(client, token.json()['csrf_token'], 'answer_human_question',
                            {'question_id': QUESTION_PROBE, 'answer_kind': 'withdraw', 'option_index': None, 'text': None})
        known_probe = False
        if response.status_code in (409, 403) and response.headers.get('content-type', '').startswith('application/json'):
            try:
                body = response.json()
                known_probe = (response.status_code == 409 and type(body) is dict and body.get('code') == 'not_allowed_in_state'
                               and type(body.get('details')) is dict and body['details'].get('reason') == 'not_found')
                if response.status_code == 403:
                    known_probe = type(body) is dict and body.get('code') == 'forbidden'
            except (ValueError, TypeError):
                pass
        return response.status_code, known_probe  # Never return/log raw response fields.

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
        try:
            if not self.idp.verify_state('before'):
                self.record('run.aborted', 'failed', error='baseline_unconfirmed')
                return self.summary()
        except (EOFError, KeyboardInterrupt):
            self.record('run.aborted', 'failed', error='terminal_interrupted')
            return self.summary()
        owner, csrf = self.owner()
        try:
            self._run(owner, csrf, member, users)
        except (EOFError, KeyboardInterrupt):
            self.record('run.aborted', 'failed', error='terminal_interrupted', manual_cleanup_required=True)
            return self.summary()
        except Exception as failure:
            self.record('run.aborted', 'failed', error=type(failure).__name__)

        def logout():
            return owner.post(AUTH + '/logout', headers=self._headers(csrf)).status_code == 200 and owner.get(AUTH + '/session').status_code == 401

        for name, step in (('unlink', lambda: self.restore_link(owner, csrf)),
                           ('clear_mapping', lambda: self.restore_mapping(owner, csrf)),
                           ('idp', self.restore_idp),
                           ('readback', lambda: self.idp.verify_state('after')),
                           ('logout', logout)):
            try:
                if not step():
                    self.record('cleanup', 'failed', step=name, manual_cleanup_required=True)
            except (EOFError, KeyboardInterrupt):
                self.record('cleanup', 'failed', step=name, error='terminal_interrupted', manual_cleanup_required=True)
                break
            except Exception as failure:
                self.record('cleanup', 'failed', step=name, error=type(failure).__name__, manual_cleanup_required=True)
        if not any(row['check'] == 'cleanup' for row in self.results):
            self.record('cleanup', 'passed', readback='operator_verified', owner_session_http=401)
        return self.summary()

    def _run(self, owner, csrf, member, users):
        # login
        b, refused, _ = self.sign_in('B')
        code, _ = self.session(b)
        self.expect('login.unlinked_refused', refused.status_code == 303 and refused.headers.get('location') == FAILED and code == 401,
                    http=refused.status_code, session_http=code)
        linked = self.change_link(owner, csrf, users['A']['subject'])
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
        self.change_group(self.config['owner_group'], True)
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
        self.change_group(self.config['owner_group'], False)
        # disable at the IdP (L6 X1: the session must be live before the disable, else the result proves nothing)
        password_client = self.password_session() if self.config['backchannel'] else None
        before, first_view = self.session(first)
        password_before, password_view = self.session(password_client) if password_client is not None else (None, None)
        def matches(view, method):
            return bool(view and view.get('principal_id') == member and view.get('authentication_methods') == [method])
        self.enable_a(False)
        # Sample both sessions before another interactive login can add unmeasured waiting time.
        after = self.session(first)[0]
        if password_client is None:
            self.record('disable.sync.backchannel', 'gap' if before == 200 else 'failed', observed='not_configured', session_http=[before, after])
        else:
            waited, limit = 0, self.config.get('backchannel_wait_seconds', 60)
            password_after = self.session(password_client)[0]
            while all(code in (200, 401) for code in (after, password_after)) and 200 in (after, password_after) and waited < limit:
                delay = min(5, limit - waited)
                self.sleep(delay)
                waited += delay
                after, password_after = self.session(first)[0], self.session(password_client)[0]
            self.expect('disable.sync.backchannel',
                        (before, password_before, after, password_after) == (200, 200, 401, 401)
                        and matches(first_view, 'oidc') and matches(password_view, 'password'),
                        session_http=[before, after], password_http=[password_before, password_after], waited_seconds=waited)
        try:
            self.sign_in('A')
        except IdpRefused:
            idp_refused = True
        else:
            idp_refused = False
        self.expect('disable.idp_refuses', idp_refused, provider_ref=self.config['provider_id'], user_slot='A',
                    source='operator_browser_observation' if idp_refused else 'application_callback')
        # Obtain a live control after the earlier back-channel may already have ended the original sessions.
        self.enable_a(True)
        epoch_client, _, _ = self.sign_in('A')
        epoch_before, epoch_view = self.session(epoch_client)
        unlinked = self.change_link(owner, csrf)
        codes = [self.session(c)[0] for c in (first, grouped)]
        epoch_after = self.session(epoch_client)[0]
        self.expect('epoch.unlink_ends_sessions', unlinked.status_code == 200 and codes == [401, 401]
                    and (epoch_before, epoch_after) == (200, 401) and matches(epoch_view, 'oidc'),
                    http=unlinked.status_code, session_http=codes, control_http=[epoch_before, epoch_after])
        self.change_group(self.config['owner_group'], False)
        relinked = self.change_link(owner, csrf, users['A']['subject'])
        fresh, done, _ = self.sign_in('A')
        codes = [self.session(c)[0] for c in (fresh, first, grouped)]
        self.expect('epoch.relink_new_only', relinked.status_code == 200 and done.status_code == 303 and codes == [200, 401, 401],
                    http=relinked.status_code, session_http=codes)
        # Groups after the epoch checks: setting a mapping ends the provider's sessions (Z2). A stays in operator_group from here on.
        self.groups_mapping(owner, csrf)
        self.unmapped_groups()
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
        self.mapping_unknown = True
        set_ = self.set_mapping(owner, csrf, {mapping['operator_group']: 'operator', mapping['reviewer_group']: 'reviewer'}, mapping['expected_revision'])
        if set_.status_code != 200:
            self.record('groups.mapping', 'failed', http=set_.status_code)
            return
        receipt = set_.json()
        revision = receipt.get('revision')
        if (receipt.get('provider_id') != self.config['provider_id'] or receipt.get('cleared') is not False
                or type(revision) is not int or revision != mapping['expected_revision'] + 1):
            raise RuntimeError('mapping receipt unavailable')
        self.mapping_revision, self.mapping_unknown = revision, False
        self.change_group(mapping['operator_group'], True)
        mapped, done, _ = self.sign_in('A')
        allowed, known_probe = self.operator_action(mapped) if done.status_code == 303 and done.headers.get('location') == '/workbench' else (None, False)
        self.change_group(mapping['operator_group'], False)
        self.change_group(mapping['reviewer_group'], True)
        _, narrowed, _ = self.sign_in('A')
        refused_after, known_refusal = self.operator_action(mapped)
        self.change_group(mapping['reviewer_group'], False)
        self.change_group(mapping['operator_group'], True)
        _, again, _ = self.sign_in('A')
        self.expect('groups.mapping', allowed == 409 and known_probe and narrowed.status_code == 303 and narrowed.headers.get('location') == FAILED and refused_after == 403
                    and known_refusal and again.status_code == 303 and again.headers.get('location') == '/workbench', operator_action_http=[allowed, refused_after],
                    narrowed=narrowed.headers.get('location') == FAILED, operator_probe_verified=known_probe)

    def unmapped_groups(self):
        mapping = self.config['group_mapping']
        if mapping is None:
            self.record('groups.unmapped', 'gap', observed='not_configured')
            return
        if self.mapping_revision is None:
            self.record('groups.unmapped', 'failed', observed='mapping_unconfirmed')
            return
        # Start from a fresh authorized session: the reviewer-only check has already denied its previous one.
        active, sign_in, _ = self.sign_in('A')
        prior = self.operator_action(active) if sign_in.headers.get('location') == '/workbench' else (None, False)
        self.change_group(mapping['operator_group'], False)
        empty_groups_verified = self.idp.verify_state('unmapped')
        empty, denied, _ = self.sign_in('A')
        refused = self.operator_action(active)
        self.expect('groups.unmapped', empty_groups_verified and prior == (409, True) and denied.status_code == 303 and denied.headers.get('location') == FAILED
                    and self.session(empty)[0] == 401 and refused == (403, True),
                    operator_action_http=[prior[0], refused[0]], empty_groups_verified=empty_groups_verified)
        self.change_group(mapping['operator_group'], True)

    def summary(self):
        statuses = [r['status'] for r in self.results]
        missing = sorted(REQUIRED - {r['check'] for r in self.results})
        if missing:
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
        member_password = None
        if config['backchannel']:
            if not config['member'].get('username_file'):
                raise InputRefused('member.username_file required for two-session back-channel evidence')
            member_password = (stdin or sys.stdin).readline().rstrip('\n')
            if not member_password:
                raise InputRefused('synthetic member password required on the second stdin line')
        run = Acceptance(config, password, idp or TerminalIdp(), client_factory or (lambda: http_client(config)), sleep, member_password)
        return run.run()
    except InputRefused as refused:
        print(json.dumps({'refused': str(refused)}), flush=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())

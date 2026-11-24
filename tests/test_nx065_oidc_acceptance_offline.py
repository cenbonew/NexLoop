"""Offline evidence judgment only. No app, IdP, HTTP transport, database or credentials."""
from collections import deque
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import socket
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location('nx065_offline', Path(__file__).parents[1] / 'scripts/acceptance/nx065_oidc_acceptance.py')
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)
REAL_ASK = A.TerminalIdp._ask


@pytest.fixture(autouse=True)
def forbid_external_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('offline OIDC judgment must not perform external operations')
    monkeypatch.setattr(A, 'http_client', forbidden)
    monkeypatch.setattr(A, 'read_private', forbidden)
    monkeypatch.setattr(A.TerminalIdp, '_ask', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


class Response:
    def __init__(self, code=200, body=None, location='/workbench'):
        self.status_code, self.body = code, body or {}
        self.headers = {'location': location, 'content-type': 'application/json'}
    def json(self):
        return self.body


class Client:
    def __init__(self, csrf=(200,), action=(404, 403)):
        self.csrf, self.action = deque(csrf), deque(action)
    def post(self, *args, **kwargs):
        return Response(self.csrf.popleft(), {'csrf_token': 'synthetic-only'})
    def get(self, *args, **kwargs):
        return Response(303, location=A.FAILED)


class GroupRun(A.Acceptance):
    def __init__(self, initial_csrf=200, narrowed_csrf=200, initial_action=409, initial_body=None, json_error=False, content_type='application/json'):
        config = {'origin': 'https://synthetic.invalid', 'provider_id': 'synthetic-provider', 'group_mapping':
                  {'operator_group': 'synthetic-ops', 'reviewer_group': 'synthetic-review', 'expected_revision': 0}}
        self.client = Client((initial_csrf, narrowed_csrf), (initial_action, 403))
        self.denial_body = {'code': 'forbidden'}
        self.initial_body = initial_body if initial_body is not None else {'code': 'not_allowed_in_state', 'details': {'reason': 'not_found'}}
        self.json_error, self.content_type = json_error, content_type
        idp = SimpleNamespace(set_group=lambda *args: None)
        super().__init__(config, '', idp, lambda: None, sleep=lambda _: None)
        self.logins = deque([Response(303), Response(303, location=A.FAILED), Response(303)])
    def set_mapping(self, *args):
        return Response(200, {'revision': 1, 'provider_id': self.config['provider_id'], 'cleared': False})
    def sign_in(self, user):
        return self.client, self.logins.popleft(), {}
    def act(self, client, csrf, operation, body):
        body = self.initial_body if len(client.action) == 2 else self.denial_body
        response = Response(client.action.popleft(), body)
        response.headers['content-type'] = self.content_type
        if self.json_error:
            def malformed():
                raise ValueError('synthetic malformed response')
            response.json = malformed
        return response
    def record(self, check, status, **observed):
        self.results.append({'check': check, 'status': status, **observed})


def test_groups_mapping_valid_action_then_refusal_passes():
    run = GroupRun()
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'passed'
    assert run.results[0]['operator_action_http'] == [409, 403]
    assert run.results[0]['operator_probe_verified'] is True


@pytest.mark.parametrize('change', [{'narrowed_csrf': 403}, {'initial_csrf': 500}], ids=['csrf-is-not-grant-refusal', 'csrf-error-is-not-permission'])
def test_groups_mapping_csrf_failure_is_not_action_evidence(change):
    run = GroupRun(**change)
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'
    assert None in run.results[0]['operator_action_http']


@pytest.mark.parametrize('code', [500, 502, 504])
def test_groups_mapping_server_error_is_not_positive_permission_evidence(code):
    run = GroupRun(initial_action=code)
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'


class SessionRun(A.Acceptance):
    def __init__(self, before=200, after=401, live=200, ended=401, backchannel=True):
        config = {'origin': 'https://synthetic.invalid', 'provider_id': 'synthetic-provider', 'member': {'principal_id': 'synthetic-member'},
                  'users': {'A': {'subject': 'synthetic-A'}, 'B': {'subject': 'synthetic-B'}},
                  'owner_group': 'synthetic-owner', 'group_mapping': None, 'backchannel': backchannel,
                  'backchannel_wait_seconds': 5, 'max_session_seconds': 900}
        self.disabled = False
        self.waits = []
        self.signins = deque(['first', 'grouped', 'epoch', 'fresh', 'last'])
        self.states = {'B': deque([401]), 'first': deque([200, before, after, 401, 401]),
                       'grouped': deque([200, 401, 401]), 'replay': deque([401]), 'fresh': deque([200]),
                       'last': deque([live, ended])}
        self.states['epoch'] = deque([200, 401])
        self.states['password'] = deque([200, 401, 401])
        self.clients = {key: Client() for key in self.states}
        self.names = {id(value): key for key, value in self.clients.items()}
        idp = SimpleNamespace(set_group=lambda *args: None, set_enabled=self.enabled)
        super().__init__(config, '', idp, lambda: self.clients['replay'], sleep=self.waits.append)
    def enabled(self, user, value):
        self.disabled = not value
    def sign_in(self, user):
        if self.disabled:
            raise A.IdpRefused('synthetic refusal')
        key = 'B' if user == 'B' else self.signins.popleft()
        return self.clients[key], Response(303, location=A.FAILED if key == 'B' else '/workbench'), {}
    def session(self, client):
        code = self.states[self.names[id(client)]].popleft()
        method = 'password' if self.names[id(client)] == 'password' else 'oidc'
        return code, {'principal_id': 'synthetic-member', 'authentication_methods': [method]} if code == 200 else None
    def password_session(self):
        return self.clients['password']
    def link(self, *args):
        return Response(200, {'provider_id': self.config['provider_id'], 'target_principal': self.config['member']['principal_id']})
    def unlink(self, *args):
        return self.link()
    def act(self, client, csrf, operation, body):
        return Response(401 if csrf.startswith('not-') else 403, {'code': 'forbidden'})
    def record(self, check, status, **observed):
        self.results.append({'check': check, 'status': status, **observed})
    def checks(self):
        self._run(None, None, self.config['member']['principal_id'], self.config['users'])
        return {row['check']: row for row in self.results}


@pytest.mark.parametrize('before,after,expected', [(200, 401, 'passed'), (401, 401, 'failed'), (200, 500, 'failed')])
def test_disable_backchannel_requires_live_then_expired(before, after, expected):
    checks = SessionRun(before=before, after=after).checks()
    assert checks['disable.sync.backchannel']['status'] == expected
    assert checks['disable.sync.backchannel']['session_http'] == [before, after if before == 200 else None]


@pytest.mark.parametrize('live,ended,expected', [(200, 401, 'passed'), (401, 401, 'failed'), (200, 500, 'failed'), (200, 200, 'failed')])
def test_max_session_requires_live_then_expired(live, ended, expected):
    run = SessionRun(live=live, ended=ended)
    checks = run.checks()
    assert checks['disable.sync.max_session']['status'] == expected
    assert run.waits == ([960] if live == 200 else [])  # no wait after a failed live precondition


def test_unconfigured_mechanism_is_gap_not_pass_and_epochs_need_exact_statuses():
    checks = SessionRun(backchannel=False).checks()
    assert checks['disable.sync.backchannel']['status'] == 'gap'
    assert checks['groups.mapping']['status'] == 'gap'
    assert checks['epoch.unlink_ends_sessions']['session_http'] == [401, 401]
    assert checks['epoch.relink_new_only']['session_http'] == [200, 401, 401]


@pytest.mark.parametrize('code', [408, 422])
def test_pre_auth_request_error_is_not_operator_permission(code):
    run = GroupRun(initial_action=code)
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'


@pytest.mark.parametrize('code', [200, 204, 301, 302, 307, 400, 401, 403, 404, 410, 429])
def test_non_probe_status_cannot_prove_permission(code):
    run = GroupRun(initial_action=code)
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'
    assert run.results[0]['operator_probe_verified'] is False


def test_generic_404_not_found_then_403_is_ambiguous_not_positive():
    # Same generic error as an unpublished Action (pre-grant LookupError).
    run = GroupRun(initial_action=404, initial_body={'code': 'not_found', 'details': {}})
    run.groups_mapping(None, None)
    assert run.results[0]['operator_action_http'] == [404, None]
    assert run.results[0]['status'] == 'failed'


@pytest.mark.parametrize('body', [{}, {'code': 'not_found', 'details': {'reason': 'not_found'}},
                                 {'code': 'not_allowed_in_state', 'details': {}},
                                 {'code': 'not_allowed_in_state', 'details': {'reason': 'other'}},
                                 {'code': 'not_allowed_in_state', 'details': 'not_found'}, ['not_found']])
def test_409_requires_exact_business_code_and_reason(body):
    run = GroupRun(initial_body=body)
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'


@pytest.mark.parametrize('change', [{'json_error': True}, {'content_type': 'text/html'}])
def test_non_json_or_malformed_probe_fails_without_retaining_body(change):
    run = GroupRun(**change)
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'
    assert 'details' not in run.results[0] and 'code' not in run.results[0]


def test_malformed_browser_input_is_not_an_idp_refusal(monkeypatch):
    idp = A.TerminalIdp()
    monkeypatch.setattr(idp, '_ask', lambda _: 'incomplete callback')
    with pytest.raises(A.InputRefused):
        idp.authorize('unused', 'A')


def test_callback_error_is_not_an_idp_refusal(monkeypatch):
    run = SessionRun()
    sign_in = run.sign_in

    def disabled_callback(user):
        if run.disabled:
            return None, Response(500), {}
        return sign_in(user)

    monkeypatch.setattr(run, 'sign_in', disabled_callback)
    assert run.checks()['disable.idp_refuses']['status'] == 'failed'


def test_generic_denial_is_not_evidence_of_group_revocation():
    run = GroupRun()
    run.denial_body = {'code': 'proxy_denied'}
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'failed'


@pytest.mark.parametrize('line', ['', '\n'])
def test_terminal_eof_and_enter(line):
    tty = SimpleNamespace(write=lambda text: None, flush=lambda: None, readline=lambda: line)
    if line:
        assert REAL_ASK(A.TerminalIdp(tty), 'confirm') == ''
    else:
        with pytest.raises(EOFError):
            REAL_ASK(A.TerminalIdp(tty), 'confirm')


def test_manual_refusal_evidence():
    row = SessionRun().checks()['disable.idp_refuses']
    assert row == {'check': 'disable.idp_refuses', 'status': 'passed', 'source': 'operator_browser_observation',
                   'provider_ref': 'synthetic-provider', 'user_slot': 'A'}


def test_terminal_interruption_stops_before_cleanup(monkeypatch):
    run = SessionRun()
    monkeypatch.setattr(run, 'owner', lambda: (None, None))
    def interrupted(*args):
        raise EOFError
    run.idp.verify_state = lambda phase: True
    monkeypatch.setattr(run, '_run', interrupted)
    monkeypatch.setattr(run, 'unlink', lambda *args: pytest.fail('operation after EOF'))
    assert run.run() == 1
    assert run.results[0]['manual_cleanup_required'] is True


@pytest.mark.parametrize('provider', ['https://synthetic.invalid', 'invalid\nref', 7, 'a' * 33])
def test_provider_reference_is_validated_before_use(monkeypatch, provider):
    run = SessionRun()
    config = dict(run.config, schema=A.SCHEMA, owner_username_file='unused', provider_id=provider)
    monkeypatch.setattr(A, 'read_private', lambda path: A.json.dumps(config))
    with pytest.raises(A.InputRefused, match='invalid provider identifier'):
        A.load_config('unused')


@pytest.mark.parametrize('step,code', [('unlink', 503), ('mapping', 409), ('logout', 401)])
def test_cleanup_refused_is_never_a_pass(monkeypatch, step, code):
    run = SessionRun()
    owner = SimpleNamespace(post=lambda *a, **k: Response(code if step == 'logout' else 200),
                            get=lambda *a, **k: Response(401))
    run.config.update(provider_id='synth', group_mapping={'operator_group': 'ops', 'reviewer_group': 'review', 'expected_revision': 0})
    run.idp.verify_state = lambda *a: True
    monkeypatch.setattr(run, 'owner', lambda: (owner, 'csrf'))
    def completed(*args):
        for check in A.REQUIRED:
            run.record(check, 'passed')
        run.link_state = 'linked'
        run.mapping_revision = 1
    monkeypatch.setattr(run, '_run', completed)
    monkeypatch.setattr(run, 'unlink', lambda *a: Response(code if step == 'unlink' else 200,
        {'target_principal': run.config['member']['principal_id'], 'provider_id': 'synth'}))
    monkeypatch.setattr(run, 'set_mapping', lambda *a: Response(code if step == 'mapping' else 200,
        {'provider_id': 'synth', 'revision': 2, 'cleared': True}))
    assert run.run() == 1
    assert any(r['status'] == 'failed' and r.get('manual_cleanup_required') for r in run.results)


def test_surviving_password_session_fails_even_when_sso_ended():
    run = SessionRun()
    run.states['password'] = deque([200, 200, 200])
    run.states['first'].append(401)
    row = run.checks()['disable.sync.backchannel']
    assert row['session_http'] == [200, 401]
    assert row['password_http'] == [200, 200]
    assert row['status'] == 'failed'


def test_unknown_mutation_is_left_for_reconciliation(monkeypatch):
    run = SessionRun()
    calls = []
    def lost(*args):
        calls.append('sent')
        raise TimeoutError
    monkeypatch.setattr(run, 'link', lost)
    with pytest.raises(TimeoutError):
        run.change_link(None, None, 'unused')
    assert not run.restore_link(None, None)
    monkeypatch.setattr(run, 'set_mapping', lost)
    run.mapping_revision = 1
    with pytest.raises(TimeoutError):
        run.restore_mapping(None, None)
    assert not run.restore_mapping(None, None)
    run.idp.set_group = lost
    with pytest.raises(TimeoutError):
        run.change_group('synthetic-reviewer', True)
    assert not run.restore_idp()
    assert calls == ['sent', 'sent', 'sent']


def test_reviewer_restored_from_verified_initial_absence():
    run = SessionRun()
    calls = []
    run.idp.set_group = lambda user, group, joined: calls.append(joined)
    run.change_group('synthetic-reviewer', True)
    assert run.restore_idp()
    assert calls == [True, False]

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
    def __init__(self, initial_csrf=200, narrowed_csrf=200, initial_action=404):
        config = {'origin': 'https://synthetic.invalid', 'group_mapping':
                  {'operator_group': 'synthetic-ops', 'reviewer_group': 'synthetic-review', 'expected_revision': 0}}
        self.client = Client((initial_csrf, narrowed_csrf), (initial_action, 403))
        idp = SimpleNamespace(set_group=lambda *args: None)
        super().__init__(config, '', idp, lambda: None, sleep=lambda _: None)
        self.logins = deque([Response(303), Response(303, location=A.FAILED), Response(303)])
    def set_mapping(self, *args):
        return Response(200, {'revision': 1})
    def sign_in(self, user):
        return self.client, self.logins.popleft(), {}
    def act(self, client, csrf, operation, body):
        return Response(client.action.popleft())
    def record(self, check, status, **observed):
        self.results.append({'check': check, 'status': status, **observed})


def test_groups_mapping_valid_action_then_refusal_passes():
    run = GroupRun()
    run.groups_mapping(None, None)
    assert run.results[0]['status'] == 'passed'
    assert run.results[0]['operator_action_http'] == [404, 403]


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
        self.signins = deque(['first', 'grouped', 'fresh', 'last'])
        self.states = {'B': deque([401]), 'first': deque([200, before, after, 401, 401]),
                       'grouped': deque([200, 401, 401]), 'replay': deque([401]), 'fresh': deque([200]),
                       'last': deque([live, ended])}
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
        return code, {'principal_id': 'synthetic-member', 'authentication_methods': ['oidc']} if code == 200 else None
    def link(self, *args):
        return Response()
    def unlink(self, *args):
        return Response()
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
    assert checks['disable.sync.backchannel']['session_http'] == [before, after]


@pytest.mark.parametrize('live,ended,expected', [(200, 401, 'passed'), (401, 401, 'failed'), (200, 500, 'failed'), (200, 200, 'failed')])
def test_max_session_requires_live_then_expired(live, ended, expected):
    run = SessionRun(live=live, ended=ended)
    checks = run.checks()
    assert checks['disable.sync.max_session']['status'] == expected
    assert run.waits[-1] == 960  # memory recorder, no real sleep


def test_unconfigured_mechanism_is_gap_not_pass_and_epochs_need_exact_statuses():
    checks = SessionRun(backchannel=False).checks()
    assert checks['disable.sync.backchannel']['status'] == 'gap'
    assert checks['groups.mapping']['status'] == 'gap'
    assert checks['epoch.unlink_ends_sessions']['session_http'] == [401, 401]
    assert checks['epoch.relink_new_only']['session_http'] == [200, 401, 401]

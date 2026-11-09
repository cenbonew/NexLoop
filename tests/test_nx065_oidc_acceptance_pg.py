"""NX-065: scripts/acceptance/nx065_oidc_acceptance.py runs end to end against the actual app and the in-process IdP of
tests/support/mock_oidc_idp.py (nothing leaves the process): login, an IdP group that grants no role, the IdP-side disable and the
session epoch on unlink and relink. The two gaps the product has today (no group-to-role mapping, no IdP-side disable sync) are reported
as gaps (exit 3), never as passed; an IdP that ignores the disable fails the run (exit 1); bad input is refused (exit 2)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support.mock_oidc_idp import MockIdp  # noqa: F401
from test_browser_http import ORIGIN
from test_workbench_oidc_pg import app, private, sso  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

_spec = importlib.util.spec_from_file_location('nx065_oidc_acceptance', Path(__file__).parents[1] / 'scripts/acceptance/nx065_oidc_acceptance.py')
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

SUBJECTS = {'A': 'idp-sub-nx065-a', 'B': 'idp-sub-nx065-b'}


class MockAdapter:
    """The synthetic IdP's admin console and sign-in page, played by the mock: disabled users cannot sign in, groups land in the ID token."""

    def __init__(self, idp, *, honour_disable=True):
        self.idp, self.honour_disable = idp, honour_disable
        self.disabled, self.groups, self.calls = set(), {}, []

    def authorize(self, location, user):
        if user in self.disabled and self.honour_disable:
            raise A.IdpRefused(user)
        self.idp.tamper = {'groups': sorted(self.groups.get(user, ()))} if self.groups.get(user) else {}
        return self.idp.authorize(location, SUBJECTS[user])

    def set_group(self, user, group, member):
        self.calls.append(('group', user, member))
        (self.groups.setdefault(user, set()).add if member else self.groups.setdefault(user, set()).discard)(group)

    def set_enabled(self, user, enabled):
        self.calls.append(('enabled', user, enabled))
        (self.disabled.discard if enabled else self.disabled.add)(user)


def configure(c, tmp_path):
    owner = c['owner_member']
    username = private(tmp_path / 'owner-username', owner['username'])
    config = {'schema': A.SCHEMA, 'origin': ORIGIN, 'provider_id': 'corp', 'owner_username_file': str(username),
              'member': {'principal_id': c['operator_member']['principal_id']},
              'users': {u: {'subject': s} for u, s in SUBJECTS.items()}, 'owner_group': 'nexloop-owner'}
    return private(tmp_path / 'nx065.json', json.dumps(config))


def run(c, tmp_path, adapter, capsys):
    stack = contextlib.ExitStack()
    with stack:
        factory = lambda: stack.enter_context(TestClient(app(c), base_url=ORIGIN))  # noqa: E731
        code = A.main(['--config', str(configure(c, tmp_path))], stdin=io.StringIO(c['owner_member']['password'] + '\n'),
                      idp=adapter, client_factory=factory)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return code, {r['check']: r for r in lines if 'check' in r}, lines


def test_the_acceptance_passes_its_checks_and_reports_the_two_gaps(sso, admin, tmp_path, capsys):
    adapter = MockAdapter(sso['idp'])
    code, checks, lines = run(sso, tmp_path, adapter, capsys)
    assert code == 3, lines
    assert {k: v['status'] for k, v in checks.items()} == {
        'login.unlinked_refused': 'passed', 'login.linked_signs_in.link': 'passed', 'login.linked_signs_in': 'passed',
        'login.callback_one_time': 'passed', 'groups.no_role_from_idp': 'passed', 'groups.mapping': 'gap',
        'disable.idp_refuses': 'passed', 'disable.sync': 'gap', 'epoch.unlink_ends_sessions': 'passed', 'epoch.relink_new_only': 'passed'}
    assert checks['login.linked_signs_in']['methods'] == ['oidc'] and checks['disable.sync']['session_http'] == 200
    assert lines[-1] == {'summary': {'passed': 8, 'failed': 0, 'gap': 2}, 'exit': 3}
    # The groups claim really reached the app (it is ignored, not missing) and the run left A enabled, out of the group, unlinked.
    assert adapter.disabled == set() and adapter.groups == {'A': set()}
    assert admin.execute('select count(*) from control.nexloop_workbench_external_identities').fetchone() == (0,)
    printed = json.dumps(lines)
    for secret in (*SUBJECTS.values(), sso['owner_member']['password'], sso['owner_member']['username'], sso['idp'].client_secret):
        assert secret not in printed


def test_an_idp_that_ignores_the_disable_fails_the_run(sso, admin, tmp_path, capsys):
    code, checks, lines = run(sso, tmp_path, MockAdapter(sso['idp'], honour_disable=False), capsys)
    assert code == 1 and checks['disable.idp_refuses']['status'] == 'failed', lines
    assert lines[-1]['summary']['failed'] == 1


@pytest.mark.parametrize('change, mode', [
    ({'schema': 'other/1'}, None), ({'origin': 'http://synthetic.example'}, None),
    ({'users': {'A': {'subject': 'same'}, 'B': {'subject': 'same'}}}, None), ({'extra': 1}, None), (None, 0o644)])
def test_bad_input_is_refused_before_any_request(tmp_path, capsys, change, mode):
    config = {'schema': A.SCHEMA, 'origin': ORIGIN, 'provider_id': 'corp', 'owner_username_file': str(tmp_path / 'u'),
              'member': {'principal_id': 'p'}, 'users': {u: {'subject': s} for u, s in SUBJECTS.items()}, 'owner_group': 'g'}
    config.update(change or {})
    path = private(tmp_path / 'c.json', json.dumps(config))
    if mode:
        path.chmod(mode)
    def no_client():
        raise AssertionError('no request before the input is accepted')
    assert A.main(['--config', str(path)], stdin=io.StringIO('pw\n'), idp=MockAdapter(None), client_factory=no_client) == 2
    assert 'refused' in json.loads(capsys.readouterr().out)


def test_an_empty_password_is_refused(sso, tmp_path, capsys):
    assert A.main(['--config', str(configure(sso, tmp_path))], stdin=io.StringIO('\n'), idp=MockAdapter(sso['idp']),
                  client_factory=lambda: (_ for _ in ()).throw(AssertionError)) == 2

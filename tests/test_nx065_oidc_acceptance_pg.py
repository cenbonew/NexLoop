"""NX-065 / NX-067: scripts/acceptance/nx065_oidc_acceptance.py runs end to end against the actual app and the in-process IdP of
tests/support/mock_oidc_idp.py (nothing leaves the process): login, an IdP group that grants no role, the group mapping, the IdP-side
disable (back-channel and the maximum session) and the session epoch on unlink and relink. With every NX-067 mechanism configured the run
exits 0; a provider without a mapping or a back-channel is reported not_configured (gaps, exit 3), never passed; an IdP that ignores the
disable fails the run (exit 1); bad input is refused (exit 2)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from support.mock_oidc_idp import MockIdp  # noqa: F401
from commercial_fixture import publish_registry_actions
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
MAPPING = {'operator_group': 'nexloop-ops', 'reviewer_group': 'nexloop-review', 'expected_revision': 0}


class MockAdapter:
    """The synthetic IdP's admin console and sign-in page, played by the mock: disabled users cannot sign in, groups land in the ID token,
    and with a back-channel the disable posts a logout token for the user (as an IdP ending the user's sessions does)."""

    def __init__(self, idp, *, honour_disable=True, backchannel=None):
        self.idp, self.honour_disable, self.backchannel = idp, honour_disable, backchannel
        self.disabled, self.groups, self.calls, self.logouts = set(), {}, [], []

    def authorize(self, location, user):
        if user in self.disabled and self.honour_disable:
            raise A.IdpRefused(user)
        self.idp.tamper = {'groups': sorted(self.groups.get(user, ()))}
        return self.idp.authorize(location, SUBJECTS[user])

    def set_group(self, user, group, member):
        self.calls.append(('group', user, member))
        (self.groups.setdefault(user, set()).add if member else self.groups.setdefault(user, set()).discard)(group)

    def set_enabled(self, user, enabled):
        self.calls.append(('enabled', user, enabled))
        (self.disabled.discard if enabled else self.disabled.add)(user)
        if not enabled and self.backchannel is not None:
            answer = self.backchannel().post('/api/v1/workbench/auth/oidc/backchannel/corp',
                                             content=urlencode({'logout_token': self.idp.logout_token(SUBJECTS[user])}),
                                             headers={'content-type': 'application/x-www-form-urlencoded'})
            self.logouts.append(answer.status_code)


def configure(c, tmp_path, **changes):
    owner = c['owner_member']
    username = private(tmp_path / 'owner-username', owner['username'])
    config = {'schema': A.SCHEMA, 'origin': ORIGIN, 'provider_id': 'corp', 'owner_username_file': str(username),
              'member': {'principal_id': c['operator_member']['principal_id']},
              'users': {u: {'subject': s} for u, s in SUBJECTS.items()}, 'owner_group': 'nexloop-owner',
              'group_mapping': None, 'backchannel': False, 'max_session_seconds': 900, **changes}
    return private(tmp_path / 'nx065.json', json.dumps(config))


def expiring(admin, c):
    """The script's wait: instead of sleeping, every session of the member reaches its absolute deadline (as time would make it)."""
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if seconds > 5:
            admin.execute("""update control.nexloop_browser_sessions set payload=jsonb_set(payload,'{absolute_expires_at}',to_jsonb(now()-interval '1 second'))
                where payload->>'principal_id'=%s""", (c['operator_member']['principal_id'],))
    sleep.waits = waits
    return sleep


def run(c, tmp_path, adapter, capsys, admin=None, client=TestClient, **changes):
    stack = contextlib.ExitStack()
    with stack:
        factory = lambda: stack.enter_context(client(app(c), base_url=ORIGIN))  # noqa: E731
        if adapter.backchannel is True:
            adapter.backchannel = factory
        code = A.main(['--config', str(configure(c, tmp_path, **changes))], stdin=io.StringIO(c['owner_member']['password'] + '\n'),
                      idp=adapter, client_factory=factory, sleep=expiring(admin, c) if admin is not None else (lambda s: None))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return code, {r['check']: r for r in lines if 'check' in r}, lines


def enable_everything(c, admin):
    publish_registry_actions(admin, ('nexloop.workbench_member.set_sso_group_mapping', 'nexloop.human_question.answer'))
    document = json.loads(c['providers'].read_text())
    document['providers'][0].update(backchannel_logout=True, max_session_seconds=900)
    c['providers'].write_text(json.dumps(document))


def test_with_every_mechanism_configured_the_acceptance_exits_0(sso, admin, tmp_path, capsys):
    enable_everything(sso, admin)
    adapter = MockAdapter(sso['idp'], backchannel=True)
    code, checks, lines = run(sso, tmp_path, adapter, capsys, admin, group_mapping=MAPPING, backchannel=True)
    assert code == 0, lines
    assert set(checks) == A.REQUIRED | {'login.linked_signs_in.link'} and {v['status'] for v in checks.values()} == {'passed'}
    assert checks['groups.mapping']['operator_action_http'][1] == 403 and checks['groups.mapping']['narrowed'] is True
    assert checks['disable.sync.backchannel']['session_http'] == [200, 401] and adapter.logouts == [200]
    assert checks['disable.sync.max_session']['session_http'] == [200, 401] and checks['disable.sync.max_session']['waited_seconds'] == 960
    assert lines[-1] == {'summary': {'passed': 11, 'failed': 0, 'gap': 0}, 'exit': 0}
    # Clean-up: the mapping cleared (a revision with {}), A enabled and in no group, the member unlinked.
    assert admin.execute('select mappings from control.nexloop_workbench_oidc_group_mappings order by revision desc limit 1').fetchone() == ({},)
    assert adapter.disabled == set() and all(not g for g in adapter.groups.values())
    assert admin.execute('select count(*) from control.nexloop_workbench_external_identities').fetchone() == (0,)
    printed = json.dumps(lines)
    for secret in (*SUBJECTS.values(), sso['owner_member']['password'], sso['owner_member']['username'], sso['idp'].client_secret, 'nexloop-ops'):
        assert secret not in printed


def test_without_a_mapping_or_a_back_channel_both_are_reported_not_configured(sso, admin, tmp_path, capsys):
    adapter = MockAdapter(sso['idp'])
    code, checks, lines = run(sso, tmp_path, adapter, capsys, admin)
    assert code == 3, lines
    assert {k: v['status'] for k, v in checks.items()} == {
        'login.unlinked_refused': 'passed', 'login.linked_signs_in.link': 'passed', 'login.linked_signs_in': 'passed',
        'login.callback_one_time': 'passed', 'groups.no_role_from_idp': 'passed', 'groups.mapping': 'gap',
        'disable.idp_refuses': 'passed', 'disable.sync.backchannel': 'gap', 'epoch.unlink_ends_sessions': 'passed',
        'epoch.relink_new_only': 'passed', 'disable.sync.max_session': 'passed'}
    assert checks['groups.mapping']['observed'] == checks['disable.sync.backchannel']['observed'] == 'not_configured'
    assert checks['login.linked_signs_in']['methods'] == ['oidc'] and checks['disable.sync.backchannel']['session_http'] == [200, 200]
    assert lines[-1] == {'summary': {'passed': 9, 'failed': 0, 'gap': 2}, 'exit': 3}
    assert adapter.disabled == set() and adapter.groups == {'A': set()}
    assert admin.execute('select count(*) from control.nexloop_workbench_external_identities').fetchone() == (0,)


def test_a_back_channel_that_never_arrives_fails_and_a_session_outliving_its_maximum_fails(sso, admin, tmp_path, capsys):
    """backchannel true in the configuration but the IdP posts nothing; and the wait does not expire the session (no sleep effect)."""
    enable_everything(sso, admin)
    code, checks, lines = run(sso, tmp_path, MockAdapter(sso['idp']), capsys, None, backchannel=True)
    assert code == 1, lines
    assert checks['disable.sync.backchannel']['status'] == 'failed' and checks['disable.sync.backchannel']['session_http'] == [200, 200]
    assert checks['disable.sync.max_session']['status'] == 'failed' and checks['disable.sync.max_session']['session_http'] == [200, 200]


def test_an_idp_that_ignores_the_disable_fails_the_run(sso, admin, tmp_path, capsys):
    code, checks, lines = run(sso, tmp_path, MockAdapter(sso['idp'], honour_disable=False), capsys, admin)
    assert code == 1 and checks['disable.idp_refuses']['status'] == 'failed', lines
    assert lines[-1]['summary']['failed'] == 1


@pytest.mark.parametrize('change, mode', [
    ({'schema': 'other/1'}, None), ({'origin': 'http://synthetic.example'}, None),
    ({'users': {'A': {'subject': 'same'}, 'B': {'subject': 'same'}}}, None), ({'extra': 1}, None), (None, 0o644),
    ({'max_session_seconds': 899}, None), ({'backchannel': 'yes'}, None), ({'backchannel_wait_seconds': 0}, None),
    ({'group_mapping': {'operator_group': 'g', 'reviewer_group': 'g', 'expected_revision': 0}}, None),
    ({'group_mapping': {'operator_group': 'a', 'reviewer_group': 'b'}}, None)])
def test_bad_input_is_refused_before_any_request(tmp_path, capsys, change, mode):
    config = {'schema': A.SCHEMA, 'origin': ORIGIN, 'provider_id': 'corp', 'owner_username_file': str(tmp_path / 'u'),
              'member': {'principal_id': 'p'}, 'users': {u: {'subject': s} for u, s in SUBJECTS.items()}, 'owner_group': 'g',
              'group_mapping': None, 'backchannel': False, 'max_session_seconds': 900}
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


# -- L6 X1-X3 on 8f24da4f -------------------------------------------------------------------------------------------------------------
class Wrapped(TestClient):
    """A browser whose CSRF answer or callback can be broken on purpose."""
    csrf_status = None
    callback_error = None
    bad_origin = False

    def post(self, url, *a, **k):
        if Wrapped.bad_origin and "/actions/" in str(url) and (k.get("json") or {}).get("external_subject") == SUBJECTS["B"]:
            k["headers"] = {**k.get("headers", {}), "Origin": "https://elsewhere.example"}
        if Wrapped.csrf_status and str(url).endswith('/auth/csrf'):
            import httpx
            return httpx.Response(Wrapped.csrf_status, json={'code': 'forbidden'}, request=httpx.Request('POST', ORIGIN + str(url)))
        return super().post(url, *a, **k)

    def get(self, url, *a, **k):
        if Wrapped.callback_error and str(url).endswith('/oidc/callback'):
            import httpx
            raise Wrapped.callback_error(f"failed {ORIGIN}{url}?code=SECRETCODE-nx065&state=SECRETSTATE-nx065")
        return super().get(url, *a, **k)


def run_wrapped(c, tmp_path, adapter, capsys, **broken):
    for name, value in broken.items():
        setattr(Wrapped, name, value)
    try:
        return run(c, tmp_path, adapter, capsys, None, client=Wrapped)
    finally:
        Wrapped.csrf_status = Wrapped.callback_error = None
        Wrapped.bad_origin = False


class ExpiringAdapter(MockAdapter):
    """Ends the member's existing sessions when A is put in the group, i.e. before the disable step reads the first session."""

    def __init__(self, idp, admin):
        super().__init__(idp)
        self.admin = admin

    def set_group(self, user, group, member):
        if member:
            self.admin.execute("update control.nexloop_browser_sessions set payload=jsonb_set(payload,'{revoked_at}',to_jsonb(now()))")
        super().set_group(user, group, member)


def test_x1_a_session_dead_before_the_disable_is_a_failure_not_a_pass(sso, admin, tmp_path, capsys):
    code, checks, lines = run(sso, tmp_path, ExpiringAdapter(sso['idp'], admin), capsys, admin)
    assert code == 1 and checks['disable.sync.backchannel']['status'] == 'failed' and checks['disable.sync.backchannel']['session_http'][0] == 401, lines


def test_x2_a_failed_csrf_fetch_does_not_count_as_a_permission_refusal(sso, admin, tmp_path, capsys):
    code, checks, lines = run_wrapped(sso, tmp_path, MockAdapter(sso['idp']), capsys, csrf_status=403)
    assert code == 1 and checks['groups.no_role_from_idp']['status'] == 'failed' and checks['groups.no_role_from_idp']['csrf_http'] == 403, lines
    # The control request (wrong CSRF token) is what tells a CSRF refusal (401) from the grant check (403).
    assert checks['groups.no_role_from_idp']['control_http'] == 401


def test_x3_a_network_error_in_the_callback_is_one_failed_line_and_cleanup_still_runs(sso, admin, tmp_path, capsys):
    import httpx
    adapter = MockAdapter(sso['idp'])
    code, checks, lines = run_wrapped(sso, tmp_path, adapter, capsys, callback_error=httpx.ConnectError)
    assert code == 1 and checks['run.aborted'] == {'check': 'run.aborted', 'status': 'failed', 'error': 'ConnectError'}, lines
    assert checks['run.incomplete']['status'] == 'failed' and 'disable.sync.backchannel' in checks['run.incomplete']['missing']
    assert 'SECRETCODE' not in json.dumps(lines) and 'SECRETSTATE' not in json.dumps(lines)
    assert ('enabled', 'A', True) in adapter.calls and ('group', 'A', False) in adapter.calls


def test_x3_an_unlink_that_raises_in_cleanup_skips_neither_the_idp_restore_nor_the_logout(sso, admin, tmp_path, capsys, monkeypatch):
    adapter = MockAdapter(sso['idp'])
    real = A.Acceptance.unlink
    calls = []

    def unlink(self, client, csrf):
        calls.append(1)
        if len(calls) == 2:  # the clean-up unlink (the first is the epoch check)
            raise RuntimeError('boom')
        return real(self, client, csrf)
    monkeypatch.setattr(A.Acceptance, 'unlink', unlink)
    code, checks, lines = run(sso, tmp_path, adapter, capsys, admin)
    assert code == 1 and checks['cleanup'] == {'check': 'cleanup', 'status': 'failed', 'error': 'RuntimeError'}, lines
    assert adapter.calls[-2:] == [('enabled', 'A', True), ('group', 'A', False)]


def test_x2_an_origin_refusal_403_is_told_apart_from_the_grant_check(sso, admin, tmp_path, capsys):
    """The member's owner-only attempt and its control carry a foreign Origin: the attempt is 403 forbidden like a grant refusal, but so is the control request
    (the Origin check comes before the CSRF check), so the check fails instead of passing."""
    code, checks, lines = run_wrapped(sso, tmp_path, MockAdapter(sso['idp']), capsys, bad_origin=True)
    check = checks['groups.no_role_from_idp']
    assert code == 1 and check['status'] == 'failed' and check['http'] == 403 and check['control_http'] == 403, lines

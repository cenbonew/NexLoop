"""NX-056 stage approval drill driver (scripts/stage/approval_drill.py): arguments, the password file, what is sent and what is
shown. A fake transport stands in for the workbench (no network); the routes and field names are those of workbench_http,
browser_http and workbench_actions_http (login, approvals, actions/<operation>, logout)."""
import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import importlib.util
_spec = importlib.util.spec_from_file_location('approval_drill', ROOT / 'scripts/stage/approval_drill.py')
D = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(D)

SECRET_PASSWORD = 'pw-zz-never-shown-QQ'
CSRF = 'csrf-zz-never-shown-WW'
APPROVAL = '0f0e0d0c-0b0a-4908-8706-050403020100'


def pwfile(tmp_path, mode=0o600, value=SECRET_PASSWORD):
    p = tmp_path / 'pw'
    p.write_text(value + '\n')
    os.chmod(p, mode)
    return str(p)


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode()); self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Fake:
    """Answers by (method, path); records every request (headers and body)."""

    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def open(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.sent.append((request.get_method(), request.full_url, dict(request.header_items()), body))
        path = request.full_url.split('://', 1)[1].split('/', 1)[1]
        status, answer = self.answers[(request.get_method(), '/' + path)]
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, 'x', {}, io.BytesIO(json.dumps(answer).encode()))
        return Response(status, answer)


BASE = ['--base-url', 'https://workbench.stage.example', '--username', 'nx-approver3', '--account-label', 'approver3']
LOGIN = {('POST', '/api/v1/workbench/auth/login'): (200, {'csrf_token': CSRF, 'principal_id': 'stage-principal-approver3'}),
         ('POST', '/api/v1/workbench/auth/logout'): (200, {})}


def run(tmp_path, capsys, args, answers, **kw):
    fake = Fake({**LOGIN, **answers})
    code = D.main(BASE + ['--password-file', pwfile(tmp_path, **kw)] + args, opener=fake)
    out = capsys.readouterr()
    return code, [json.loads(line) for line in out.out.splitlines()], out.err, fake


def assert_no_secret(text):
    for s in (SECRET_PASSWORD, CSRF, 'drill-', 'nx-approver3', 'Cookie'):
        assert s not in text, s


def test_arguments_and_target_are_checked_before_anything_is_sent(tmp_path, capsys):
    for bad in (['--base-url', 'http://workbench.stage.example'], ['--base-url', 'https://workbench.stage.example/api'],
                ['--base-url', 'https://user:x@workbench.stage.example'], ['--base-url', 'https://workbench.stage.example/?q=1']):
        assert D.main(BASE[2:] + bad + ['--password-file', pwfile(tmp_path), 'list'], opener=Fake({})) == 2
    assert D.main(BASE + ['--password-file', pwfile(tmp_path), 'decide', '--approval-id', 'nope', '--decision', 'approve', '--reason', 'r'], opener=Fake({})) == 2
    assert D.main(BASE + ['--password-file', pwfile(tmp_path), 'request', '--operation', 'set_real_dispatch', '--body-json', '[1]'], opener=Fake({})) == 2
    # The label must not be the username (the evidence never names the account).
    assert D.main(BASE[:4] + ['--account-label', 'nx-approver3', '--password-file', pwfile(tmp_path), 'list'], opener=Fake({})) == 2
    # The password file: a regular 0600 file of the caller, one line.
    assert D.main(BASE + ['--password-file', pwfile(tmp_path, mode=0o644), 'list'], opener=Fake({})) == 2
    assert D.main(BASE + ['--password-file', str(tmp_path / 'missing'), 'list'], opener=Fake({})) == 2
    assert_no_secret(capsys.readouterr().err)


def test_dry_run_sends_nothing_and_shows_paths_and_keys_only(tmp_path, capsys):
    fake = Fake({})
    body = json.dumps({'scope_kind': 'action_type', 'action_type': 'eios:action:nexloop.service.request:1', 'enabled': True, 'reason': '演练一'})
    assert D.main(BASE + ['--password-file', pwfile(tmp_path), '--dry-run', 'request', '--operation', 'set_real_dispatch', '--body-json', body], opener=fake) == 0
    out = capsys.readouterr().out
    assert fake.sent == [] and '演练一' not in out
    (record,) = [json.loads(x) for x in out.splitlines()]
    assert record['dry_run'] is True and [r['path'] for r in record['requests']] == [
        '/api/v1/workbench/auth/login', '/api/v1/workbench/actions/set_real_dispatch', '/api/v1/workbench/auth/logout']
    assert record['requests'][1]['body_keys'] == ['action_type', 'enabled', 'reason', 'scope_kind']
    assert_no_secret(out)


def test_request_decide_cancel_execute_show_ids_statuses_and_codes_only(tmp_path, capsys, monkeypatch):
    evidence = tmp_path / 'evidence.jsonl'
    body = json.dumps({'scope_kind': 'action_type', 'action_type': 'eios:action:nexloop.service.request:1', 'enabled': True, 'reason': '演练一'})
    code, (r,), _, fake = run(tmp_path, capsys, ['--evidence-file', str(evidence), 'request', '--operation', 'set_real_dispatch', '--body-json', body],
        {('POST', '/api/v1/workbench/actions/set_real_dispatch'): (200, {'status': 'waiting_approval', 'approval_id': APPROVAL, 'approval': 'required'})})
    assert code == 0 and r['http'] == 200 and r['approval_id'] == APPROVAL and r['status'] == 'waiting_approval' and r['account'] == 'approver3'
    method, url, headers, sent = fake.sent[1]
    assert url == 'https://workbench.stage.example/api/v1/workbench/actions/set_real_dispatch' and sent['reason'] == '演练一'
    assert headers['Origin'] == 'https://workbench.stage.example' and headers['X-csrf-token'] == CSRF and headers['Idempotency-key'].startswith('drill-')
    assert fake.sent[0][3] == {'username': 'nx-approver3', 'password': SECRET_PASSWORD} and fake.sent[-1][1].endswith('/auth/logout')
    # A refusal: the HTTP status and the fixed reason code; the free-text message is not shown.
    code, (r,), _, _ = run(tmp_path, capsys, ['--evidence-file', str(evidence), 'decide', '--approval-id', APPROVAL, '--decision', 'approve', '--reason', '同意'],
        {('POST', '/api/v1/workbench/actions/decide_approval'): (409, {'code': 'not_allowed_in_state', 'message': '请求未完成', 'details': {'reason': 'self_approval'}})})
    assert code == 1 and r == {**r, 'http': 409, 'code': 'not_allowed_in_state', 'details_reason': 'self_approval', 'approval_id': APPROVAL} and 'message' not in r
    code, (r,), _, _ = run(tmp_path, capsys, ['cancel', '--approval-id', APPROVAL, '--reason', '撤回'],
        {('POST', '/api/v1/workbench/actions/cancel_approval'): (200, {'approval_id': APPROVAL, 'status': 'cancelled'})})
    assert code == 0 and r['status'] == 'cancelled'
    # Execute resubmits the frozen inputs of 待我执行; a code an execution shows (an owner reset) is never printed.
    inputs = {'scope_kind': 'action_type', 'action_type': 'eios:action:nexloop.service.request:1', 'enabled': True, 'reason': '演练一'}
    code, (r,), _, fake = run(tmp_path, capsys, ['execute', '--approval-id', APPROVAL], {
        ('GET', '/api/v1/workbench/approvals'): (200, {'to_decide': [], 'mine': [], 'solo_mode': {'enabled': False, 'others_eligible': True},
            'to_execute': [{'approval_id': APPROVAL, 'operation': 'set_real_dispatch', 'status': 'approved', 'inputs': inputs}]}),
        ('POST', '/api/v1/workbench/actions/set_real_dispatch'): (200, {'approval': 'consumed', 'replayed': False, 'code': '123456 789', 'token': 'tok-zz'})})
    assert code == 0 and r['approval'] == 'consumed' and r['approval_id'] == APPROVAL and r['operation'] == 'set_real_dispatch'
    assert fake.sent[2][3] == inputs and r['code'] == '<omitted>' and 'token' not in r
    code, (r,), _, _ = run(tmp_path, capsys, ['execute', '--approval-id', APPROVAL],
        {('GET', '/api/v1/workbench/approvals'): (200, {'to_decide': [], 'mine': [], 'to_execute': [], 'solo_mode': {}})})
    assert code == 1 and r['error'] == 'not_in_to_execute'
    lines = evidence.read_text().splitlines()
    assert len(lines) == 2 and oct(evidence.stat().st_mode)[-3:] == '600'
    assert_no_secret(evidence.read_text())


def test_list_shows_columns_without_bodies_and_login_failure_is_reported(tmp_path, capsys):
    row = {'approval_id': APPROVAL, 'capability': 'real_dispatch.set', 'operation': 'set_real_dispatch', 'status': 'waiting', 'consumed': False, 'result': None,
           'approvals': 0, 'quorum': 1,
           'policy_version': 1, 'due_by': '2026-10-28T01:00:00+00:00', 'requested_by': 'stage-human-approver3', 'summary': {'scope_kind': 'action_type'},
           'inputs': {'reason': '演练一'}}
    code, (r,), _, _ = run(tmp_path, capsys, ['list'], {('GET', '/api/v1/workbench/approvals'): (200, {
        'to_decide': [row], 'to_execute': [], 'mine': [], 'solo_mode': {'enabled': False, 'others_eligible': True}})})
    assert code == 0 and r['columns']['to_decide'] == [{k: row[k] for k in D.ROW_KEYS}] and '演练一' not in json.dumps(r, ensure_ascii=False)
    fake = Fake({('POST', '/api/v1/workbench/auth/login'): (401, {'code': 'unauthenticated', 'message': '登录失败'})})
    assert D.main(BASE + ['--password-file', pwfile(tmp_path), 'list'], opener=fake) == 1
    out = capsys.readouterr().out
    assert 'HTTP 401' in out and 'unauthenticated' in out and len(fake.sent) == 1
    assert_no_secret(out)


def test_redirects_are_never_followed():
    handler = D._NoRedirect()
    assert handler.redirect_request(None, None, 302, 'Found', {}, 'https://elsewhere.example/') is None


class AppTransport:
    """urllib-shaped opener over the production create_app (a TestClient per invocation: its cookie jar is the session)."""

    def __init__(self, client):
        self.client = client

    def open(self, request, timeout):
        path = '/' + request.full_url.split('://', 1)[1].split('/', 1)[1]
        r = self.client.request(request.get_method(), path, headers=dict(request.header_items()), content=request.data)
        if r.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, r.status_code, 'x', {}, io.BytesIO(r.content))
        return Response(r.status_code, r.json() if r.content else {})


from test_nx056_approval_gate_pg import gated, tenant_switch  # noqa: E402,F401
from commitment_fixture import commitments  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401
from test_browser_identity_reads import identity  # noqa: E402,F401
from test_browser_session_creation import uow  # noqa: E402,F401
from test_real_dispatch_switch_pg import switching  # noqa: E402,F401


@pytest.mark.parametrize("execution_plan", ["commitment-service"], indirect=True)
def test_the_drill_against_the_real_workbench_request_decide_execute(gated, admin, tmp_path, capsys):
    """Against the production app and the real governed entry: one owner requests (waiting), the other approves (and sees the
    request in 待我审批), the requester executes from 待我执行; the switch opens once. Only ids, statuses and codes are printed."""
    c = gated
    from test_browser_http import ORIGIN

    def drill(member, label, *args):
        p = tmp_path / f'pw-{label}'
        p.write_text(member['password'] + '\n'); os.chmod(p, 0o600)
        from fastapi.testclient import TestClient
        from nexloop_eios.http_api import create_app
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            code = D.main(['--base-url', ORIGIN, '--username', member['username'], '--account-label', label, '--password-file', str(p),
                           '--evidence-file', str(tmp_path / 'evidence.jsonl'), *args], opener=AppTransport(client))
        out = capsys.readouterr().out
        for secret in (member['password'], member['username']):
            assert secret not in out
        return code, json.loads(out.splitlines()[-1])

    body = json.dumps({'scope_kind': 'tenant', 'enabled': True, 'data_mode': 'real', 'action_type': None, 'reason': '演练一'})
    code, r = drill(c['owner_member'], 'requester', 'request', '--operation', 'set_real_dispatch', '--body-json', body)
    assert code == 0 and r['status'] == 'waiting_approval' and r['approval'] == 'required', r
    approval = r['approval_id']
    code, r = drill(c['second'], 'approver', 'list')
    assert code == 0 and [x['approval_id'] for x in r['columns']['to_decide']] == [approval]
    code, r = drill(c['owner_member'], 'requester', 'decide', '--approval-id', approval, '--decision', 'approve', '--reason', '自批')
    assert code == 1 and r['http'] in (403, 409)        # never one's own
    code, r = drill(c['second'], 'approver', 'decide', '--approval-id', approval, '--decision', 'approve', '--reason', '同意')
    assert code == 0 and r['http'] == 200, r
    assert tenant_switch(admin) is None
    code, r = drill(c['owner_member'], 'requester', 'execute', '--approval-id', approval)
    assert code == 0 and r['approval'] == 'consumed' and r['approval_id'] == approval, r
    assert tenant_switch(admin) is True
    assert len((tmp_path / 'evidence.jsonl').read_text().splitlines()) == 5


def test_environment_proxies_are_ignored(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:3128')
    handlers = D.default_opener().handlers
    # An empty ProxyHandler replaces the default one (which would read HTTPS_PROXY); no handler carries a proxy.
    assert not [h for h in handlers if isinstance(h, D.urllib.request.ProxyHandler) and h.proxies]

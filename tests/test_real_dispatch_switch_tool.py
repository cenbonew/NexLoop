"""scripts/deploy/real_dispatch_switch.py (owner 2026-10-17: the real dispatch switches from a private configuration, not the
browser console). Unit part: configuration, plan, request keys, private files and refusals before any request. PG part: the
tool against the production workbench app (create_app, real login realm, governed owner Action set_real_dispatch, metrics read)."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import real_dispatch_switch as T  # noqa: E402

DELIVER = 'eios:action:nexloop.enterprise.deliver:1'
OTHER = 'eios:action:nexloop.incentive.issue:1'


def private(tmp_path, name, text, mode=0o600):
    path = tmp_path / name
    path.write_text(text)
    path.chmod(mode)
    return path


def config(**override):
    value = {'schema': T.SCHEMA, 'reason': 'synthetic trial window', 'tenant': {'enabled': True, 'data_mode': 'real'},
             'actions': [{'action_type': DELIVER, 'enabled': True}], **override}
    return json.dumps(value)


def test_the_example_configuration_is_valid():
    text = (ROOT / 'deploy/examples/real-dispatch-switch.example.json').read_text()
    assert T.load_config(text)['actions'] == [{'action_type': DELIVER, 'enabled': True}]


@pytest.mark.parametrize('bad', [
    {'schema': 'other'}, {'reason': ''}, {'reason': ' padded '}, {'tenant': {'enabled': True}}, {'tenant': {'enabled': 'yes', 'data_mode': 'real'}},
    {'tenant': {'enabled': True, 'data_mode': 'prod'}}, {'actions': [{'action_type': 'deliver', 'enabled': True}]},
    {'actions': [{'action_type': DELIVER, 'enabled': True}, {'action_type': DELIVER, 'enabled': False}]},
    {'actions': [{'action_type': DELIVER, 'enabled': True, 'extra': 1}]}, {'tenant': None, 'actions': []}, {'unknown': 1}])
def test_a_bad_configuration_is_refused(bad):
    with pytest.raises(T.Refused):
        T.load_config(config(**bad))


def test_the_plan_changes_only_what_differs_and_close_all_closes_every_open_switch():
    desired = T.load_config(config())
    closed = {'world': 'real', 'tenant': None, 'actions': []}
    assert [(c['scope_kind'], c['enabled'], c['action_type'], c['data_mode'], c['from_revision']) for c in T.plan(closed, desired)] == [
        ('tenant', True, None, 'real', 0), ('action_type', True, DELIVER, None, 0)]
    opened = {'tenant': {'enabled': True, 'data_mode': 'real', 'revision': 7},
              'actions': [{'action_type': DELIVER, 'enabled': True, 'revision': 8}, {'action_type': OTHER, 'enabled': True, 'revision': 3}]}
    assert T.plan(opened, desired) == []  # an Action the configuration does not name is left as it is
    assert [c['data_mode'] for c in T.plan({**opened, 'tenant': {'enabled': True, 'data_mode': 'test', 'revision': 7}}, desired)] == ['real']
    assert [(c['scope_kind'], c['action_type'], c['enabled'], c['from_revision']) for c in T.plan(opened, close_all=True)] == [
        ('tenant', None, False, 7), ('action_type', DELIVER, False, 8), ('action_type', OTHER, False, 3)]
    assert T.plan(closed, close_all=True) == []


def test_the_request_key_replays_a_retry_and_differs_for_a_later_change():
    change = {'scope_kind': 'tenant', 'enabled': True, 'action_type': None, 'data_mode': 'real', 'from_revision': 4}
    key = T.idempotency_key(change, 'r')
    assert key == T.idempotency_key(dict(change), 'r') and T._KEY_FORMAT.fullmatch(key)
    assert key != T.idempotency_key({**change, 'from_revision': 6}, 'r') and key != T.idempotency_key(change, 'other')


def test_inputs_are_refused_before_any_request(tmp_path, capsys):
    class NoRequest:
        def request(self, *unused):
            raise AssertionError('no request may be sent')
    user = private(tmp_path, 'user', 'owner\n')
    good = private(tmp_path, 'switch.json', config())
    common = ['--origin', 'https://app.stage.invalid:8443', '--username-file', str(user), '--password-stdin']
    cases = [
        ([*common, '--config', str(private(tmp_path, 'open.json', config(), 0o644))], 'private file must be a regular 0600 file of the caller'),
        ([*common, '--config', str(good), '--reason', 'x'], '--reason only with --close-all (the configuration carries it)'),
        ([*common, '--close-all'], 'reason: 1-500 characters'),
        (['--origin', 'https://app.stage.invalid:8443', '--username-file', str(private(tmp_path, 'user-open', 'owner', 0o644)), '--password-stdin', '--config', str(good)],
         'private file must be a regular 0600 file of the caller'),
    ]
    for argv, detail in cases:
        assert T.main(argv, stdin=io.StringIO('secret\n'), transport=NoRequest()) == 2
        assert json.loads(capsys.readouterr().out)['detail'] == detail
    assert T.main([*common, '--config', str(good)], stdin=io.StringIO('\n'), transport=NoRequest()) == 2
    assert json.loads(capsys.readouterr().out)['detail'] == 'username and password required'
    os.symlink(good, tmp_path / 'link.json')
    assert T.main([*common, '--config', str(tmp_path / 'link.json')], stdin=io.StringIO('secret\n'), transport=NoRequest()) == 2
    with pytest.raises(T.Refused):
        T.HttpsTransport('http://app.stage.invalid:8443')


from test_real_dispatch_switch_pg import ORIGIN, switching, owner_sql  # noqa: E402,F401
from commitment_fixture import TENANT, commitments  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401
from test_browser_identity_reads import identity  # noqa: E402,F401
from test_browser_session_creation import uow  # noqa: E402,F401


class AppTransport:
    """The production app (create_app) in process: same routes, realm, cookie and governed entry as over HTTPS."""

    def __init__(self, client):
        self.client, self.requests = client, []

    def request(self, method, path, headers, body=None):
        self.requests.append((method, path))
        response = self.client.request(method, path, headers=headers, content=body)
        return response.status_code, list(response.headers.multi_items()), response.content


def test_the_owner_opens_reruns_and_closes_all_through_the_governed_workbench_entry(switching, admin, tmp_path, capsys):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import create_app
    c = switching
    owner, operator = c['owner_member'], c['operator_member']
    switch = private(tmp_path, 'switch.json', config())
    base = ['--origin', ORIGIN, '--password-stdin']

    def tool(member, *argv):
        user = private(tmp_path, 'user-' + member['username'], member['username'] + '\n')
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            transport = AppTransport(client)
            code = T.main([*base, '--username-file', str(user), *argv], stdin=io.StringIO(member['password'] + '\n'), transport=transport)
        out = json.loads(capsys.readouterr().out)
        assert member['password'] not in json.dumps(out) and member['username'] not in json.dumps(out)
        return code, out, transport.requests

    # Dry run: the plan from the current state, nothing changed (no POST to the action entry), logged out at the end.
    code, out, requests = tool(owner, '--config', str(switch), '--dry-run')
    assert code == 0 and out['dry_run'] and [c_['scope_kind'] for c_ in out['changes']] == ['tenant', 'action_type']
    assert ('POST', '/api/v1/workbench/actions/set_real_dispatch') not in requests and requests[-1] == ('POST', '/api/v1/workbench/auth/logout')
    assert owner_sql(admin, 'select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT, 'real'))['real_dispatch']['tenant'] is None

    code, out, _ = tool(owner, '--config', str(switch))
    assert code == 0 and out['converged'] and [r['status'] for r in out['results']] == [200, 200]
    revisions = [r['control_revision'] for r in out['results']]
    assert revisions[1] == revisions[0] + 1
    assert out['after']['tenant'] == {'enabled': True, 'data_mode': 'real', 'revision': revisions[0]}
    assert out['after']['actions'] == [{'action_type': DELIVER, 'enabled': True, 'revision': revisions[1]}]
    assert 'REAL_DISPATCH_ENABLED' in out['deployment_layer']

    # Rerun: nothing differs, nothing is sent.
    code, out, requests = tool(owner, '--config', str(switch))
    assert code == 0 and out['changes'] == [] and ('POST', '/api/v1/workbench/actions/set_real_dispatch') not in requests

    # The operator is not the owner: the governed entry refuses (403), the state is unchanged, exit 1.
    code, out, _ = tool(operator, '--close-all', '--reason', 'synthetic close by the operator')
    assert code == 1 and out['results'] == [{'scope_kind': 'tenant', 'enabled': False, 'status': 403, 'code': 'forbidden'}]
    assert out['after']['tenant']['enabled'] is True

    # Close all: the tenant and every open Action switch, each a new control revision.
    code, out, _ = tool(owner, '--close-all', '--reason', 'synthetic end of the trial window')
    assert code == 0 and out['converged'] and [r['control_revision'] for r in out['results']] == [revisions[1] + 1, revisions[1] + 2]
    assert out['after']['tenant'] == {'enabled': False, 'data_mode': 'real', 'revision': revisions[1] + 1}
    assert out['after']['actions'] == [{'action_type': DELIVER, 'enabled': False, 'revision': revisions[1] + 2}]

    # Reopen with the same configuration and reason: new requests (another starting revision), not a replay of the first ones.
    code, out, _ = tool(owner, '--config', str(switch))
    assert code == 0 and out['converged'] and [r['control_revision'] for r in out['results']] == [revisions[1] + 3, revisions[1] + 4]

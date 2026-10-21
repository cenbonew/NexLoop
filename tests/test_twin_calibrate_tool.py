"""scripts/deploy/twin_calibrate.py (owner rule: a configuration file and one command, the password piped from the Keychain): the
NX-042 T5 calibration of a consumer twin version through the workbench actions entry. Unit part: the example configuration, the
checks mirroring the entry, the dry run (nothing read from stdin, nothing sent), the password only from stdin and never in the
output. PG part: the tool against the production workbench app (create_app, real login realm, governed owner Action) calibrates a
twin version over three windows of seeded real aggregates (passed), replays it, and the operator is refused."""
from datetime import timedelta
import io
import json
import logging
import sys
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import twin_calibrate as T  # noqa: E402

SECRET = 'zz-owner-pass-marker'
WINDOWS = [{'split_at': '2026-07-01', 'span_days': 28}, {'split_at': '2026-07-29', 'span_days': 28}, {'split_at': '2026-08-26', 'span_days': 28}]


def private(tmp_path, name, value, mode=0o600):
    path = tmp_path / name
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(mode)
    return path


def cfg(**over):
    return json.dumps({'schema': T.SCHEMA, 'twin_id': 'trial.calibrated', 'version': 1, 'windows': WINDOWS, **over})


def test_the_example_configuration_is_valid():
    body = T.load_config((ROOT / 'deploy/examples/twin-calibrate.example.json').read_text())
    assert body['twin_id'] == 'trial.service_consumer' and len(body['windows']) == 3


def test_the_fields_are_those_of_the_workbench_entry():
    from nexloop_eios.workbench_actions_http import OPERATIONS
    spec = OPERATIONS[T.OPERATION]
    body = T.load_config(cfg())
    assert set(body) == set(spec) and {k: check(body[k]) for k, check in spec.items()} == body
    for bad in ([], WINDOWS + WINDOWS[:1], [{'split_at': '2026-07-01', 'span_days': 30}], [{'split_at': '1 July', 'span_days': 28}]):
        with pytest.raises(ValueError):
            spec['windows'](bad)
        with pytest.raises(T.Refused):
            T.load_config(cfg(windows=bad))


@pytest.mark.parametrize('over,detail', [({'twin_id': 'Bad'}, 'twin_id'), ({'version': 0}, 'version'), ({'version': '1'}, 'version'),
                                         ({'tenant_id': 'x'}, 'fields'), ({'schema': 'x'}, 'schema')])
def test_a_bad_configuration_is_refused(over, detail):
    with pytest.raises(T.Refused, match=detail):
        T.load_config(cfg(**over))


class NoNetwork:
    def request(self, *unused):
        raise AssertionError('no request')


class NoStdin:
    def readline(self):
        raise AssertionError('the dry run reads no password')


def test_the_dry_run_prints_the_request_without_reading_a_password_or_sending(tmp_path, capsys):
    conf = private(tmp_path, 'c.json', cfg())
    assert T.main(['--config', str(conf), '--dry-run'], stdin=NoStdin(), transport=NoNetwork()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] and out['operation'] == 'calibrate_consumer_twin' and out['request']['windows'] == WINDOWS
    assert out['idempotency_key'] == T.idempotency_key(T.load_config(cfg())) != T.idempotency_key(T.load_config(cfg(version=2)))


def test_the_password_is_never_an_argument_and_never_echoed(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    conf, user = private(tmp_path, 'c.json', cfg()), private(tmp_path, 'u', 'owner\n')
    base = ['--config', str(conf), '--origin', 'https://app.example.invalid', '--username-file', str(user)]
    for argv in ([*base, '--password', SECRET], [*base, SECRET], base):
        assert T.main(argv, stdin=NoStdin(), transport=NoNetwork()) == 2
    assert T.main([*base, '--password-stdin'], stdin=io.StringIO('\n'), transport=NoNetwork()) == 2
    assert T.main(['--config', str(private(tmp_path, 'loose.json', cfg(), 0o644)), '--dry-run']) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + caplog.text


class AppTransport:
    def __init__(self, client):
        self.client, self.requests = client, []

    def request(self, method, path, headers, body=None):
        self.requests.append((method, path))
        response = self.client.request(method, path, headers=headers, content=body)
        return response.status_code, list(response.headers.multi_items()), response.content


# PG part --------------------------------------------------------------------------------------------------------------------------
from test_real_dispatch_switch_pg import switching  # noqa: E402,F401
from commercial_fixture import publish_registry_actions  # noqa: E402
from commitment_fixture import TENANT, commitments  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401
from test_browser_identity_reads import identity  # noqa: E402,F401
from test_browser_session_creation import uow  # noqa: E402,F401
from test_browser_http import ORIGIN  # noqa: E402
from test_nx042_twin_calibration_pg import INSIDE, SPLIT, definition  # noqa: E402
from test_nx042_twin_calibration_read_pg import seed  # noqa: E402


def test_the_owner_calibrates_a_twin_version_end_to_end_through_the_governed_workbench_entry(switching, admin, tmp_path, capsys, caplog):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import create_app
    caplog.set_level(logging.DEBUG)
    c = switching
    publish_registry_actions(admin, ('nexloop.consumer_twin.calibrate',))
    owner, operator = c['owner_member'], c['operator_member']
    # A published twin version (moved before the windows as trusted seeding) and real aggregates in three windows.
    admin.execute("select control.nexloop_governed_publish_consumer_twin(%s,'real','owner-principal','owner-subject','i',%s)",
                  (TENANT, Jsonb({'operation': 'publish_consumer_twin', 'request_id': uuid.uuid4().hex, 'definition': definition()})))
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update control.nexloop_consumer_twins set published_at=%s where tenant_id=%s and twin_id='trial.calibrated'", (SPLIT - timedelta(days=2), TENANT))
    for w in range(3):
        for d, (replied, refused) in enumerate(INSIDE):
            seed(admin, f'cli-w{w}d{d}', 30, SPLIT + timedelta(days=28 * w + 3, hours=6 * d + 1), replied=replied, refused=refused, tenant=TENANT)
    conf = private(tmp_path, 'calibrate.json', cfg())

    def tool(member, *extra):
        user = private(tmp_path, 'user-' + member['username'], member['username'] + '\n')
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            transport = AppTransport(client)
            code = T.main(['--config', str(conf), '--origin', ORIGIN, '--username-file', str(user), '--password-stdin', *extra],
                          stdin=io.StringIO(member['password'] + '\n'), transport=transport)
        captured = capsys.readouterr()
        for secret in (member['password'], member['username']):
            assert secret not in captured.out + captured.err + caplog.text
        assert 'csrf' not in captured.out.lower() and 'cookie' not in captured.out.lower() and 'postgres' not in captured.out.lower()
        return code, json.loads(captured.out), transport.requests

    code, out, requests = tool(owner, '--dry-run')
    assert code == 0 and out['dry_run'] and requests == []
    code, out, requests = tool(owner)
    assert code == 0 and out['status'] == 200, out
    r = out['result']
    assert r['conclusion'] == 'passed' and r['calibration'] == 'calibrated' and r['report_ref'].startswith('twin-calibration:') and r['created'] is True
    assert [p for _, p in requests] == ['/api/v1/workbench/auth/login', '/api/v1/workbench/actions/calibrate_consumer_twin', '/api/v1/workbench/auth/logout']
    code, again, _ = tool(owner)  # the same configuration replays (the governed outcome); no second report
    assert code == 0 and again['result']['replayed'] is True and again['result']['outcome_id']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    assert admin.execute('select count(*) from control.nexloop_twin_calibration_reports').fetchone()[0] == 1
    code, out, _ = tool(operator)
    assert code == 1 and out['status'] == 403 and out['code'] == 'forbidden'

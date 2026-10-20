"""scripts/deploy/sim_world.py (owner rule: a configuration file and one command, the password piped from the Keychain): the four
simulation world Actions (create, delete, generate, clock) through the workbench actions entry. Unit part: the four example
configurations, field and range checks mirroring the entry, the dry run (nothing read from stdin, nothing sent), the password only
from stdin and never in the output. PG part: the tool against the production workbench app (create_app, real login realm, governed
owner Actions) runs all four end to end; the operator is refused."""
import io
import json
import logging
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import sim_world as T  # noqa: E402

WORLD = 'sim:' + str(uuid.UUID(int=1, version=4))
SECRET = 'zz-owner-pass-marker'


def private(tmp_path, name, value, mode=0o600):
    path = tmp_path / name
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(mode)
    return path


def cfg(**fields):
    return json.dumps({'schema': T.SCHEMA, **fields})


@pytest.mark.parametrize('command', sorted(T.OPERATIONS))
def test_each_example_configuration_is_valid_and_has_no_real_identifiers(command):
    text = (ROOT / f'deploy/examples/sim-world-{command}.example.json').read_text()
    operation, body = T.load_config(command, text)
    assert operation == T.OPERATIONS[command][0]
    assert 'tenant' not in text and '192.168.' not in text and '10.0.' not in text
    if 'world' in body:
        assert body['world'].endswith('00000000-0000-4000-8000-000000000000')


def test_the_fields_and_ranges_are_those_of_the_workbench_entry():
    from nexloop_eios.workbench_actions_http import OPERATIONS
    for command, (operation, spec) in T.OPERATIONS.items():
        assert set(spec) == set(OPERATIONS[operation]), command
    # Values both sides accept or refuse alike (the entry's checks raise ValueError, the tool's Refused).
    probes = {('create', 'expires_in_days'): ([1, 30], [0, 366, '7', 7.0]), ('generate', 'consumers'): ([1, 2000], [0, 2001]),
              ('clock', 'seconds'): ([1, 315360000], [0, 315360001]), ('clock', 'rate'): ([0.5, 100000], [0, 100001]),
              ('delete', 'world'): ([WORLD, 'shadow:' + str(uuid.uuid4())], ['real', 'test', 'sim:1']),
              ('generate', 'world'): ([WORLD], ['shadow:' + str(uuid.uuid4())]), ('generate', 'seed'): (['s.1-x'], ['', 'a b'])}
    for (command, field), (good, bad) in probes.items():
        operation, spec = T.OPERATIONS[command]
        for v in good:
            assert spec[field][0](v) == OPERATIONS[operation][field](v) == v
        for v in bad:
            with pytest.raises(T.Refused):
                spec[field][0](v)
            with pytest.raises(ValueError):
                OPERATIONS[operation][field](v)


@pytest.mark.parametrize('command,fields,detail', [
    ('create', {'kind': 'real', 'purpose': 'x', 'expires_in_days': 3}, 'kind'),
    ('create', {'kind': 'simulation', 'purpose': 'x', 'expires_in_days': 31}, 'expires_in_days'),
    ('create', {'kind': 'simulation', 'purpose': 'x'}, 'missing'),
    ('create', {'kind': 'simulation', 'purpose': 'x', 'expires_in_days': 3, 'tenant_id': 'x'}, 'unknown'),
    ('delete', {'world': 'real', 'reason': 'x'}, 'world'),
    ('generate', {'world': WORLD, 'kind': 'synthetic', 'seed': 's', 'consumers': 3, 'events_per_consumer': {'min': 0, 'max': 2}}, 'generate'),
    ('generate', {'world': WORLD, 'kind': 'structural', 'seed': 's', 'consumers': 3, 'events_per_consumer': {'min': 0, 'max': 2},
                  'segments': {'a': 1}, 'max_objects': 3}, 'generate'),
    ('generate', {'world': WORLD, 'kind': 'structural', 'seed': 's', 'consumers': 3, 'events_per_consumer': {'min': 3, 'max': 2}, 'max_objects': 3}, 'events'),
    ('clock', {'world': WORLD, 'action': 'advance', 'reason': 'x'}, 'clock'),
    ('clock', {'world': WORLD, 'action': 'pause', 'seconds': 5, 'reason': 'x'}, 'clock'),
])
def test_a_bad_configuration_is_refused(command, fields, detail):
    with pytest.raises(T.Refused, match=detail):
        T.load_config(command, cfg(**fields))


def test_nullable_fields_left_out_are_sent_as_null_and_the_key_is_stable():
    op, body = T.load_config('clock', cfg(world=WORLD, action='pause', reason='停一下'))
    assert op == 'control_sim_clock' and body == {'world': WORLD, 'action': 'pause', 'seconds': None, 'rate': None, 'reason': '停一下'}
    assert T.idempotency_key(op, body) == T.idempotency_key(op, dict(body)) != T.idempotency_key(op, {**body, 'reason': '再停'})
    assert __import__('re').fullmatch(r'[A-Za-z0-9._~-]{16,160}', T.idempotency_key(op, body))


class NoNetwork:
    def request(self, *unused):
        raise AssertionError('no request')


class NoStdin:
    def readline(self):
        raise AssertionError('the dry run reads no password')


def test_the_dry_run_checks_and_prints_the_request_without_reading_a_password_or_sending(tmp_path, capsys):
    conf = private(tmp_path, 'c.json', cfg(kind='shadow', purpose='影子实验', expires_in_days=7))
    assert T.main(['create', '--config', str(conf), '--dry-run'], stdin=NoStdin(), transport=NoNetwork()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] and out['operation'] == 'create_sim_world' and out['request']['kind'] == 'shadow' and out['idempotency_key'].startswith('simcli-')


def test_inputs_are_refused_before_any_request_and_the_password_is_never_an_argument(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    conf = private(tmp_path, 'c.json', cfg(world=WORLD, reason='x'))
    user = private(tmp_path, 'u', 'owner\n')
    base = ['delete', '--config', str(conf), '--origin', 'https://app.example.invalid', '--username-file', str(user)]
    # No password option exists: a password on the command line is a usage error, before anything is read or sent.
    for argv in ([*base, '--password', SECRET], [*base, '--password-file', str(user)], [*base, SECRET]):
        assert T.main(argv, stdin=NoStdin(), transport=NoNetwork()) == 2
    assert T.main(base, stdin=NoStdin(), transport=NoNetwork()) == 2  # without --password-stdin
    assert T.main([*base, '--password-stdin'], stdin=io.StringIO('\n'), transport=NoNetwork()) == 2  # empty password
    loose = private(tmp_path, 'loose.json', cfg(world=WORLD, reason='x'), mode=0o644)
    assert T.main(['delete', '--config', str(loose), '--dry-run']) == 2
    with pytest.raises(T.Refused):
        T.HttpsTransport('http://app.example.invalid:8443')
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + caplog.text


class AppTransport:
    """The production app (create_app) in process: same routes, realm, cookie and governed entry as over HTTPS."""

    def __init__(self, client):
        self.client, self.requests = client, []

    def request(self, method, path, headers, body=None):
        self.requests.append((method, path, body))
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

SIM_ACTIONS = ('nexloop.sim_world.create', 'nexloop.sim_world.delete', 'nexloop.sim_world.generate', 'nexloop.sim_clock.control')


def test_the_owner_runs_all_four_actions_end_to_end_through_the_governed_workbench_entry(switching, admin, tmp_path, capsys, caplog):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import create_app
    caplog.set_level(logging.DEBUG)
    c = switching
    publish_registry_actions(admin, SIM_ACTIONS)
    owner, operator = c['owner_member'], c['operator_member']

    def tool(member, command, fields, *extra):
        conf = private(tmp_path, f'{command}-{uuid.uuid4().hex[:6]}.json', cfg(**fields))
        user = private(tmp_path, 'user-' + member['username'], member['username'] + '\n')
        with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
            transport = AppTransport(client)
            code = T.main([command, '--config', str(conf), '--origin', ORIGIN, '--username-file', str(user), '--password-stdin', *extra],
                          stdin=io.StringIO(member['password'] + '\n'), transport=transport)
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        for secret in (member['password'], member['username']):
            assert secret not in captured.out + captured.err + caplog.text
        assert 'csrf' not in captured.out.lower() and 'postgres' not in captured.out.lower() and 'cookie' not in captured.out.lower()
        return code, out, transport.requests

    def world_status(world):
        admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        row = admin.execute('select status from control.nexloop_sim_worlds where world=%s', (world,)).fetchone()
        return row and row[0]

    create = {'kind': 'simulation', 'purpose': '命令行工具端到端', 'expires_in_days': 7}
    # Dry run: nothing sent at all, nothing registered.
    code, out, requests = tool(owner, 'create', create, '--dry-run')
    assert code == 0 and out['dry_run'] and requests == []
    assert admin.execute('select count(*) from control.nexloop_sim_worlds').fetchone()[0] == 0
    # Create, then rerun the same configuration: the same world (the request replays).
    code, out, requests = tool(owner, 'create', create)
    assert code == 0 and out['status'] == 200 and out['result']['world'].startswith('sim:'), out
    assert [r[:2] for r in requests] == [('POST', '/api/v1/workbench/auth/login'), ('POST', '/api/v1/workbench/actions/create_sim_world'),
                                         ('POST', '/api/v1/workbench/auth/logout')]
    world = out['result']['world']
    assert world_status(world) == 'active'
    code, again, _ = tool(owner, 'create', create)
    assert code == 0 and again['result'].get('world', world) == world
    assert admin.execute('select count(*) from control.nexloop_sim_worlds').fetchone()[0] == 1
    # Generate synthetic data and move the clock.
    code, out, _ = tool(owner, 'generate', {'world': world, 'kind': 'synthetic', 'seed': 'cli-1', 'consumers': 5,
                                            'events_per_consumer': {'min': 0, 'max': 2}, 'segments': {'loyal': 1}})
    assert code == 0 and out['status'] == 200 and out['result']['world'] == world and out['result']['generation_id'], out
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    assert admin.execute("select count(*) from control.nexloop_sim_generations where world=%s", (world,)).fetchone()[0] == 1
    before = admin.execute('select runtime.nexloop_world_now(%s,%s)', (TENANT, world)).fetchone()[0]
    code, out, _ = tool(owner, 'clock', {'world': world, 'action': 'advance', 'seconds': 86400, 'reason': '推进一天'})
    assert code == 0 and out['status'] == 200 and out['result']['action'] == 'advance' and out['result']['revision'] >= 1, out
    assert (admin.execute('select runtime.nexloop_world_now(%s,%s)', (TENANT, world)).fetchone()[0] - before).days >= 1
    # The operator is not the owner: refused (403), exit 1, nothing changed.
    code, out, _ = tool(operator, 'delete', {'world': world, 'reason': '操作员无权删除'})
    assert code == 1 and out['status'] == 403 and out['code'] == 'forbidden' and world_status(world) == 'active'
    # A refusal of the handler names its code; a bad configuration exits 2 before any request.
    code, out, requests = tool(owner, 'clock', {'world': 'sim:' + str(uuid.uuid4()), 'action': 'pause', 'reason': '不存在的世界'})
    assert code == 1 and out['status'] == 409 and out.get('reason'), out
    code, out, requests = tool(owner, 'create', {**create, 'expires_in_days': 31})
    assert code == 2 and requests == []
    # Delete: the whole world goes.
    code, out, _ = tool(owner, 'delete', {'world': world, 'reason': '实验结束'})
    assert code == 0 and out['result']['status'] == 'deleted' and world_status(world) == 'deleted', out

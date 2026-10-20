"""NX-041 S6b: the simulation group's Agent tools reach the stand-in (S4) through the guard, never the real effect path.

- The guard's stand-in routes over actual HTTPS (fake workers prove the route only).
- The payload: a stable intent per Run, Action and business key, digests of the parameters, no simulated clock, simulation world
  only.
- Real PostgreSQL: the guard of a simulation world (its one credential, S2 D10) records stand-in intents only (no real effect row),
  a retry is the same intent, a changed payload is a conflict, a find reads only this Run's reply intent; the guard of world real
  refuses before anything. The live activation is re-authorized by the existing guard function (covered by the runtime activation
  tests); here it is observed to be called with operation tool.
"""
import hashlib
import secrets
import threading
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.backend import open_backend
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.runtime_worker import _FreshGuard
from nexloop_eios.stand_in_tools import present, stand_in_intent_id, stand_in_payload
from support.tenants import TENANT_A
from test_agent_host import files
from test_runtime_guard_transport import client

RUN = '11111111-1111-4111-8111-111111111111'
SIM = 'sim:' + '0' * 8 + '-0000-4000-8000-0000000000f1'
REAL_TABLES = ('runtime.nexloop_effect_intents', 'runtime.nexloop_effect_outbox', 'runtime.nexloop_incentive_issues', 'runtime.nexloop_outreach_sends',
               'runtime.nexloop_outbound_messages')


# 1. The payload -------------------------------------------------------------------------------------------------------------
def test_the_payload_is_stable_per_run_action_and_key_and_carries_no_clock():
    cmd = {'run_id': RUN, 'world_id': SIM, 'consumer_ref': 'consumer:x'}
    one = stand_in_payload(cmd, SIM, 'nexloop.service.request', {'message': '您好'})
    assert one == stand_in_payload(cmd, SIM, 'nexloop.service.request', {'message': '您好'}) and 'sim_clock' not in one
    assert one['intent_id'] == stand_in_intent_id(RUN, 'nexloop.service.request') and one['provider_kind'] == 'http' and one['origin'] == 'twin'
    changed = stand_in_payload(cmd, SIM, 'nexloop.service.request', {'message': '再见'})
    assert changed['intent_id'] == one['intent_id'] and changed['request_digest'] != one['request_digest']  # same intent, another payload
    a = stand_in_payload(cmd, SIM, 'nexloop.outreach.send', {'purpose': 'followup', 'body': 'x'})
    b = stand_in_payload(cmd, SIM, 'nexloop.outreach.send', {'purpose': 'renewal', 'body': 'x'})
    assert a['intent_id'] != b['intent_id'] and a['provider_kind'] == 'outreach_outbox'
    offer = {'incentive_offering_id': 'd' * 64, 'offering_revision': 1, 'binding_id': 'e' * 64, 'amount_minor': '1', 'currency': 'CNY', 'notification_text': ''}
    assert stand_in_payload(cmd, SIM, 'nexloop.incentive.issue', offer)['provider_kind'] == 'incentive_ledger'
    for world, command, action, params in (('real', {**cmd, 'world_id': 'real'}, 'nexloop.service.request', {'message': 'x'}),
                                           ('test', {**cmd, 'world_id': 'test'}, 'nexloop.service.request', {'message': 'x'}),
                                           (SIM, {**cmd, 'world_id': 'real'}, 'nexloop.service.request', {'message': 'x'}),
                                           (SIM, cmd, 'nexloop.enterprise.deliver', {'summary': 'x'}),
                                           (SIM, cmd, 'nexloop.service.request', {'message': 'x', 'sim_clock': '2026-01-01T00:00:00Z'}),
                                           (SIM, cmd, 'nexloop.service.request', {'message': ''}),
                                           (SIM, cmd, 'nexloop.outreach.send', {'purpose': 'bad purpose!', 'body': 'x'})):
        with pytest.raises(ValueError):
            stand_in_payload(command, world, action, params)
    with pytest.raises(ValueError):
        present({'simulated': False})


# 2. The route ---------------------------------------------------------------------------------------------------------------
class Refused(psycopg.errors.InvalidParameterValue):
    def __init__(self, message):
        super().__init__(message)
        self._message = message

    @property
    def diag(self):
        return SimpleNamespace(message_primary=self._message)


class StandInWorker:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def _answer(self, **arguments):
        self.calls.append(arguments)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome
    submit_run_stand_in = find_run_stand_in = _answer


@pytest.fixture
def route(tmp_path):
    from nexloop_eios.runtime_control import create_runtime_guard_server
    _, key = files(tmp_path)
    servers = []

    def start(outcome):
        worker = StandInWorker(outcome)
        server = create_runtime_guard_server(worker, port=0, key_file=key, certificate_file=tmp_path / 'host-cert.pem', tls_key_file=tmp_path / 'host-key.pem')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))

        def post(path, body):
            with client(key) as http:
                return http.post(f'https://127.0.0.1:{server.server_port}/internal/v1/runtime/stand-ins/{path}', headers={'Authorization': 'Bearer ' + key.read_text()}, json=body)
        return worker, post
    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(3)


RECEIPT = {'intent_id': str(uuid.uuid4()), 'world': SIM, 'state': 'accepted', 'stand_in_id': 'service.request.http', 'stand_in_version': '1.0.0',
           'simulated': True, 'output_kind': 'simulated_output'}
SUBMIT = {'activation_ref': 'activation_' + 'a' * 36, 'command': {'run_id': RUN}, 'action': 'nexloop.service.request', 'parameters': {'message': 'x'}}


def test_the_stand_in_route_passes_exact_arguments_and_maps_refusals_to_fixed_codes(route):
    worker, post = route({'run_id': RUN, 'receipt': RECEIPT})
    response = post('submit', SUBMIT)
    assert response.status_code == 200 and response.json() == {'run_id': RUN, 'receipt': RECEIPT}
    assert worker.calls == [{k: SUBMIT[k] for k in ('activation_ref', 'command', 'action', 'parameters')}]
    assert post('find', {'activation_ref': SUBMIT['activation_ref'], 'command': {'run_id': RUN}, 'intent_id': RECEIPT['intent_id']}).status_code == 200
    for bad in ({**SUBMIT, 'extra': 1}, {k: v for k, v in SUBMIT.items() if k != 'action'}, {**SUBMIT, 'parameters': 'x'}):
        assert post('submit', bad).json() == {'code': 'invalid_effect_request'}
    for outcome, status, code in ((ValueError('stand-in: simulation world required'), 400, 'invalid_effect_request'),
                                  (PermissionError('effect_intent_unavailable'), 403, 'effect_intent_unavailable'),
                                  (Refused('stand-in: intent_conflict'), 409, 'intent_payload_conflict'),
                                  (RuntimeError('SYNTHETIC_SECRET_SENTINEL'), 403, 'effect_intent_unavailable'),
                                  ({'run_id': 'other', 'receipt': RECEIPT}, 503, 'runtime_effect_unavailable')):
        _, post = route(outcome)
        response = post('submit', SUBMIT)
        assert (response.status_code, response.json()) == (status, {'code': code}) and 'SENTINEL' not in response.text


# 3. The guard on real PostgreSQL ---------------------------------------------------------------------------------------------
@pytest.fixture
def sim_guard(admin, pg, tmp_path):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    world = admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner','owner','i',%s)",
                          (TENANT_A, Jsonb({'operation': 'create_sim_world', 'request_id': 's6b', 'kind': 'simulation', 'purpose': 'S6b', 'expires_in_days': 7}))).fetchone()[0]['world']
    token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'a' * 32, world=world, identity_suffix='-s6b-guard')
    real_token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'b' * 32, world='real', identity_suffix='-s6b-real')
    material = secrets.token_bytes(32)
    keyfile = tmp_path / 'signing'
    keyfile.write_bytes(material)
    keyfile.chmod(0o600)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)', ('synthetic-s6b', material))

    def credential(value, name):
        path = tmp_path / name
        path.write_text(value)
        path.chmod(0o600)
        return path
    with open_backend(database_url=make_conninfo(pg, user='nexloop_domain_worker'), artifact_root=tmp_path / 'artifacts',
                      signing_key_file=keyfile, signing_key_id='synthetic-s6b') as backend:
        authorized = []

        def guard_for(world_, token_, name):
            g = _FreshGuard(backend, credential(token_, name), world_)
            real_service = g.service

            def service():
                session = real_service()
                session.authorize_runtime_activation = lambda **arguments: authorized.append(arguments) or {'authorized': True}
                return session
            g.service = service
            return g
        yield {'admin': admin, 'world': world, 'sim': guard_for(world, token, 'sim-credential'), 'real': guard_for('real', real_token, 'real-credential'),
               'authorized': authorized, 'digest': hashlib.sha256(token.encode()).hexdigest()}


def test_the_simulation_guard_records_stand_in_intents_only(sim_guard):
    f = sim_guard
    admin, world = f['admin'], f['world']
    before = {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in REAL_TABLES}
    cmd = {'run_id': str(uuid.uuid4()), 'world_id': world, 'consumer_ref': 'consumer:x'}
    activation = 'activation_' + 'b' * 30
    first = f['sim'].submit_run_stand_in(activation_ref=activation, command=cmd, action='nexloop.service.request', parameters={'message': '您好'})
    receipt = first['receipt']
    assert receipt == {'intent_id': stand_in_intent_id(cmd['run_id'], 'nexloop.service.request'), 'world': world, 'state': 'accepted',
                       'stand_in_id': 'service.request.http', 'stand_in_version': '1.0.0', 'simulated': True, 'output_kind': 'simulated_output'}
    assert f['authorized'][-1] == {'activation_ref': activation, 'command': cmd, 'operation': 'tool'}
    again = f['sim'].submit_run_stand_in(activation_ref=activation, command=cmd, action='nexloop.service.request', parameters={'message': '您好'})
    assert again['receipt']['intent_id'] == receipt['intent_id']
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='intent_conflict'):
        f['sim'].submit_run_stand_in(activation_ref=activation, command=cmd, action='nexloop.service.request', parameters={'message': '另一条'})
    f['sim'].submit_run_stand_in(activation_ref=activation, command=cmd, action='nexloop.outreach.send', parameters={'purpose': 'followup', 'body': '明天见'})
    rows = admin.execute('select stand_in_id,origin,data_mode,submitted_at>now()-interval \'1 minute\' from runtime.nexloop_sim_effect_intents where world=%s order by 1',
                         (world,)).fetchall()
    assert rows == [('outreach.send', 'twin', 'simulation', True), ('service.request.http', 'twin', 'simulation', True)]  # the server's world time
    assert {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in REAL_TABLES} == before  # nothing on the real effect path
    # find: only this Run's reply intent, as a simulated outcome (no receipt yet: unavailable until the stand-in executor ran).
    with pytest.raises(PermissionError):
        f['sim'].find_run_stand_in(activation_ref=activation, command=cmd, intent_id=str(uuid.uuid4()))
    with pytest.raises(PermissionError):
        f['sim'].find_run_stand_in(activation_ref=activation, command=cmd, intent_id=receipt['intent_id'])
    with psycopg.connect(make_conninfo(admin.info.dsn, user='nexloop_action_worker'), autocommit=True) as worker:
        worker.execute('select authz.nexloop_sim_effect_execute(%s,%s,%s)', (f['digest'], world, receipt['intent_id']))
    found = f['sim'].find_run_stand_in(activation_ref=activation, command=cmd, intent_id=receipt['intent_id'])['receipt']
    assert (found['state'], found['simulated'], found['output_kind']) == ('fulfilled', True, 'simulated_output')


def test_the_real_guard_and_a_mismatched_command_never_reach_the_stand_in(sim_guard):
    f = sim_guard
    cmd = {'run_id': str(uuid.uuid4()), 'world_id': 'real', 'consumer_ref': 'consumer:x'}
    with pytest.raises(ValueError):
        f['real'].submit_run_stand_in(activation_ref='activation_' + 'c' * 30, command=cmd, action='nexloop.service.request', parameters={'message': 'x'})
    with pytest.raises(ValueError):
        f['real'].find_run_stand_in(activation_ref='activation_' + 'c' * 30, command=cmd, intent_id=stand_in_intent_id(cmd['run_id'], 'nexloop.service.request'))
    with pytest.raises(ValueError):  # a command of another world at the simulation guard
        f['sim'].submit_run_stand_in(activation_ref='activation_' + 'c' * 30, command=cmd, action='nexloop.service.request', parameters={'message': 'x'})
    assert f['authorized'] == []  # refused before any authorization or database call
    assert f['admin'].execute('select count(*) from runtime.nexloop_sim_effect_intents').fetchone()[0] == 0

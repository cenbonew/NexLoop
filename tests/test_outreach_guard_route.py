"""Outreach slice 5: the runtime guard's outreach route over actual HTTPS (fake workers prove the route only; the SQL port that
decides is covered by tests/test_outreach_runs_pg.py). Synthetic transport keys only."""
import threading
from types import SimpleNamespace

import psycopg
import pytest

from test_agent_host import files
from test_runtime_guard_transport import client

PATH = '/internal/v1/runtime/outreach/send'
RUN = '11111111-1111-4111-8111-111111111111'
ASK = {'purpose': 'commitment_followup', 'body': '您好'}
BODY = {'activation_ref': 'activation_' + 'a' * 36, 'command': {'run_id': RUN}, 'parameters': ASK}
INTENT = 'outreach:' + 'f' * 64


class Refused(psycopg.errors.InvalidParameterValue):
    def __init__(self, message):
        super().__init__(message); self._message = message

    @property
    def diag(self):
        return SimpleNamespace(message_primary=self._message)


class OutreachWorker:
    def __init__(self, outcome): self.outcome, self.calls = outcome, []

    def send_run_outreach(self, **arguments):
        self.calls.append(arguments)
        if isinstance(self.outcome, Exception): raise self.outcome
        return self.outcome


@pytest.fixture
def guard(tmp_path):
    from nexloop_eios.runtime_control import create_runtime_guard_server
    _, key = files(tmp_path); servers = []

    def start(outcome):
        worker = OutreachWorker(outcome)
        server = create_runtime_guard_server(worker, port=0, key_file=key, certificate_file=tmp_path / 'host-cert.pem', tls_key_file=tmp_path / 'host-key.pem')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); servers.append((server, thread))

        def post(body):
            with client(key) as http:
                return http.post(f'https://127.0.0.1:{server.server_port}{PATH}', headers={'Authorization': 'Bearer ' + key.read_text()}, json=body)
        return worker, post
    yield start
    for server, thread in servers: server.shutdown(); server.server_close(); thread.join(3)


def test_the_route_passes_exact_arguments_and_returns_a_fixed_receipt(guard):
    worker, post = guard({'run_id': RUN, 'intent_id': INTENT, 'state': 'accepted', 'replayed': False, 'origin_ref': 'plan:x', 'debug': 'x'})
    response = post(BODY)
    assert response.status_code == 200
    assert response.json() == {'run_id': RUN, 'intent_id': INTENT, 'state': 'accepted', 'replayed': False, 'business_action_success': False}
    assert worker.calls == [{'activation_ref': BODY['activation_ref'], 'command': BODY['command'], 'parameters': ASK}]


@pytest.mark.parametrize('outcome,status,code', [(Refused('outreach send: purpose_not_allowed'), 409, 'purpose_not_allowed'),
    (Refused('outreach send: intent_exists'), 409, 'intent_exists'), (Refused('outreach send: invalid'), 400, 'invalid_outreach_request'),
    (Refused('something else'), 400, 'invalid_outreach_request'), (ValueError('x'), 400, 'invalid_outreach_request'),
    (RuntimeError('secret'), 403, 'outreach_unavailable')])
def test_the_route_maps_refusals_to_fixed_codes_without_detail(guard, outcome, status, code):
    _, post = guard(outcome)
    response = post(BODY)
    assert response.status_code == status and response.json() == {'code': code}


@pytest.mark.parametrize('body', [{**BODY, 'extra': 1}, {'activation_ref': BODY['activation_ref'], 'command': BODY['command']}, {**BODY, 'parameters': 'x'}])
def test_the_route_refuses_malformed_bodies_before_the_worker(guard, body):
    worker, post = guard({})
    assert post(body).status_code == 400 and worker.calls == []


def test_a_receipt_for_another_run_is_never_returned(guard):
    _, post = guard({'run_id': '22222222-2222-4222-8222-222222222222', 'intent_id': INTENT, 'state': 'accepted', 'replayed': False})
    assert post(BODY).status_code == 503

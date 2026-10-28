"""NX-062 C8 G-5 (docs/design/c8-route-capability-inventory.md v2 §4): no credential, no access - on real PostgreSQL.

Every route of the inventory outside public, public_static and pre_session is requested once without its credential: cookie-session
routes without a cookie must answer 401 or 403; the service-token route without Authorization and with an invalid token 401; the
webhook without a signature is refused. Path parameters are synthetic values of the shape each handler accepts, so the answer is
the credential check, not input validation. Real PG (the workbench fixture's identity and business databases) is what makes a 401
real: without a database a missing session check would still fail as 503 and look like a refusal."""
import dataclasses
import secrets
import tempfile

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import api_route_inventory as I
from nexloop_eios import commercial
from nexloop_eios.http_api import create_app
from test_workbench_read_pg import TENANT
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

UUID = '00000000-0000-4000-8000-000000000001'
HEX64 = 'c' * 64
# One accepted shape per path parameter name (the handlers' own patterns: workbench_http, web_chat_http, review_http, ...).
PARAMETERS = {
    'artifact_id': 'd' * 32, 'conversation_id': HEX64, 'message_id': HEX64, 'record_id': HEX64, 'commitment_id': HEX64, 'consumer_id': HEX64,
    'run_id': UUID, 'candidate_id': UUID, 'proposal_id': UUID, 'publication_id': UUID, 'remediation_id': UUID, 'intent_id': UUID,
    'call_sequence': '1', 'goal_id': 'synthetic-goal', 'goal_version': '1', 'kr_key': 'synthetic-kr', 'export_id': 'synthetic-export',
    'connector_id': 'synthetic-probe-shop', 'operation': 'set_control', 'type_name': 'Consumer', 'object_id': 'synthetic-object',
    'stable_name': 'synthetic.set',
}
EXEMPT = {'public', 'public_static', 'pre_session'}
PROBE = 'synthetic probe'
# A body of the accepted shape for every write, so the handler's input check passes and the answer is the credential check.
BODIES = {
    '/api/v1/conversations': {},
    '/api/v1/conversations/{conversation_id}/messages': {'body': PROBE},
    '/api/v1/conversations/{conversation_id}/native-messages': {'schema_version': 'nexloop.native-message.v1', 'provider_event_id': UUID, 'body': PROBE},
    '/api/v1/review/candidates/{candidate_id}/decisions': {'decision': 'reject', 'expected_revision': 1, 'rationale': PROBE},
    '/api/v1/workbench/review-decisions/candidates/{candidate_id}/decisions': {'decision': 'reject', 'expected_revision': 1, 'rationale': PROBE},
    '/api/v1/workbench/actions/{operation}': {'scope_kind': 'tenant', 'scope_ref': '*', 'paused': True, 'reason': PROBE},
    '/api/v1/workbench/schema-proposals/{proposal_id}/publish': {'rationale': PROBE},
    '/api/v1/workbench/schema-proposals/{proposal_id}/hide': {'rationale': PROBE},
    '/api/v1/webhooks/commercial/{connector_id}': {'event_id': 'evt-probe'},
}
WRITE_HEADERS = {'Idempotency-Key': 'synthetic-probe-key-0001'}


def concrete(path):
    for name, value in PARAMETERS.items():
        path = path.replace('{' + name + '}', value)
    assert '{' not in path, path
    return path


@pytest.mark.parametrize('mode', I.MODES)
def test_g5_no_credential_no_access_on_every_inventoried_route(workbench, tmp_path, mode):
    inventory = I.load()
    # A configured connector, so the webhook answer is its signature check (an unknown connector is a 404 before any signature).
    key = tmp_path / 'probe-connector-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    commercial.configure_connector(workbench['configurator'], tenant=TENANT, world='real', connector_id=PARAMETERS['connector_id'], data_mode='real',
                                   event_types=['order.paid'], currencies=['CNY'], key_id='k1', key_file=key, owner_confirmation='synthetic owner confirmation')
    # The fixture's own configuration in each deployment mode (L6 S-5): what a mode mounts is what is probed.
    client = TestClient(create_app(dataclasses.replace(workbench['config'], mode=mode)), base_url=workbench['client']().base_url)
    mounted = I.routes(client.app)
    with tempfile.TemporaryDirectory() as directory:
        complete = I.routes(I.complete_app(directory, mode))
    # The fixture's application is the complete configuration without a web root: nothing else differs.
    assert set(complete) - set(mounted) == {('GET', '/workbench'), ('GET', '/workbench/{rest:path}'), ('MOUNT', '/')}
    assert set(mounted) <= set(complete)
    answers, probed = {}, 0
    with client:
        for route in I.listed(inventory, mode):
            if route['realm'] in EXEMPT:
                continue
            path = concrete(route['path'])
            if route['method'] == 'GET':
                send = lambda **kw: client.request('GET', path, **kw)  # noqa: E731
            else:
                body = BODIES.get(route['path'], {})
                send = lambda headers=None, body=body: client.request(route['method'], path, json=body, headers={**WRITE_HEADERS, **(headers or {})})  # noqa: E731
            probed += 1
            status = send().status_code
            if route['realm'] == 'service_bearer':
                answers[(route['method'], route['path'], 'no token')] = status
                answers[(route['method'], route['path'], 'invalid token')] = send(headers={'Authorization': 'Bearer not-a-service-token'}).status_code
            else:
                answers[(route['method'], route['path'], 'none')] = status
    # Every route outside public, public_static and pre_session; the test-world Artifact read in --mode test only (F-2).
    assert probed == {'test': 96 - 3 - 2 - 6, 'stage': 95 - 3 - 2 - 6}[mode]  # batch 32 added the members read, batch 50 the experiments read
    refused = {key: status for key, status in answers.items() if status in (401, 403)}
    leftover = sorted(f'{m} {p} ({how}): {status}' for (m, p, how), status in answers.items() if (m, p, how) not in refused)
    assert leftover == [], '\n'.join(leftover)
    bearer = {key[2]: status for key, status in answers.items() if key[1] == '/api/v1/artifacts/{artifact_id}'}
    assert bearer == ({'no token': 401, 'invalid token': 401} if mode == 'test' else {})

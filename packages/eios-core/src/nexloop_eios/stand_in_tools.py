"""NX-041 S6b (design §3.2, §6): the simulation group's Agent tools reach the stand-in, never the real effect path.

In a simulation world the Host's effect tools (service request, incentive issue, outreach send) post to the guard's stand-in routes;
the guard re-authorizes the live activation of the Run and records a stand-in intent through authz.nexloop_sim_effect_submit (S4)
with its own simulation world credential. The intent id is stable per Run, Action and business key, so a retried tool call is the
same intent; a changed payload for the same key is a conflict. No simulated clock is sent: the server takes the world's time.
Every answer is a simulated output (simulated: true, output_kind simulated_output), never a fact.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from nexloop_eios.worlds import world_kind

NAMESPACE = uuid.UUID('6f1c2b8e-41d0-5a3e-9c7d-0e4b5a6c7d8e')
SIMULATION_KINDS = ('simulation', 'shadow')
# Action -> (stand-in provider kind of the S4 catalog, the parameter that is the business key within one Run, allowed parameters).
ACTIONS = {
    'nexloop.service.request': ('http', None, {'message', 'request_scope'}),
    'nexloop.incentive.issue': ('incentive_ledger', 'incentive_offering_id',
                                {'incentive_offering_id', 'offering_revision', 'binding_id', 'amount_minor', 'currency', 'notification_text'}),
    'nexloop.outreach.send': ('outreach_outbox', 'purpose', {'purpose', 'body'}),
}
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}')


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def stand_in_intent_id(run_id: str, action: str, key: str = '') -> str:
    return str(uuid.uuid5(NAMESPACE, f'nexloop:stand-in:{run_id}:{action}:{key}'))


def stand_in_payload(command: dict, world: str, action: str, parameters: dict) -> dict:
    """The S4 submit payload for one tool call of a simulation Run (no sim_clock). ValueError when anything does not fit."""
    if world_kind(world) not in SIMULATION_KINDS or type(command) is not dict or command.get('world_id') != world:
        raise ValueError('stand-in: simulation world required')
    if action not in ACTIONS or type(parameters) is not dict:
        raise ValueError('stand-in: invalid')
    provider, key_field, allowed = ACTIONS[action]
    if not parameters or set(parameters) - allowed:
        raise ValueError('stand-in: invalid')
    key = ''
    if key_field is not None:
        key = parameters.get(key_field)
        if type(key) is not str or not _KEY.fullmatch(key):
            raise ValueError('stand-in: invalid')
    if action == 'nexloop.service.request' and (type(parameters.get('message')) is not str or not 1 <= len(parameters['message']) <= 8192):
        raise ValueError('stand-in: invalid')
    if action == 'nexloop.outreach.send' and (type(parameters.get('body')) is not str or not 1 <= len(parameters['body']) <= 2000):
        raise ValueError('stand-in: invalid')
    run_id = str(command.get('run_id'))
    return {'intent_id': stand_in_intent_id(run_id, action, key), 'action_name': action, 'action_version': 1, 'provider_kind': provider,
            'business_digest': hashlib.sha256(_canonical([run_id, command.get('consumer_ref'), action, key])).hexdigest(),
            'request_digest': hashlib.sha256(_canonical(parameters)).hexdigest(), 'origin': 'twin'}


def present(row: dict | None) -> dict | None:
    """What the guard returns for one stand-in intent: the simulated outcome only (status, ids, version), never a fact."""
    if row is None:
        return None
    if row.get('simulated') is not True:
        raise ValueError('not a stand-in answer')
    return {'intent_id': str(row['intent_id']), 'world': row['world'], 'state': row.get('status') or row.get('state'),
            'stand_in_id': row['stand_in_id'], 'stand_in_version': row['stand_in_version'], 'simulated': True, 'output_kind': 'simulated_output'}

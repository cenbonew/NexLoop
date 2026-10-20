"""NX-041 Host simulation context protocol (S7b; L6 nx041-s7b-review 3.4): a simulation Run's input.

A simulation world's Run reads exactly the simulation pack (nexloop.context-pack.sim.v1) that S7b's own entry
authz.nexloop_sim_context_command assembles from simulation sources, with that world's own credential (S2 D10) and - in the
simulation group - the simulation event intake's role (nexloop_sim_api, S6c). There is no v6 path here and no fallback to one:
a real world is refused before anything is called. The simulation Agent Host parses the Run's input with parseSimContextPack and
renders it with its simulation preamble (apps/agent-host/src/sim-context.ts).

By reference (L6 nx041-host-sim-context-review 4.3): a simulation Run's input is never a pack the caller assembled. The caller
names the Run, the Consumer and the limit; SQL (authz.nexloop_sim_context_register) assembles the pack in the same transaction,
fixes its text and digest to the Run, records the manifest of its simulated sources, and returns the text. That text is the Run's
input; the Run registration of a simulation world refuses any other input (its digest must be the fixed pack's). The guard's input
digest check is unchanged. assemble_sim_context stays as a read-only preview; it is never a Run's input.
"""
from __future__ import annotations

import json

from nexloop_eios.worlds import world_kind

SIM_CONTEXT_PROTOCOL = 'nexloop.context-pack.sim.v1'


def assemble_sim_context(connection, token_digest: str, world: str, consumer_id: str, limit: int = 64) -> str:
    """The simulation pack for one synthetic Consumer of a simulation world, as the Run input text (JSON)."""
    if world_kind(world) not in ('simulation', 'shadow'):
        raise ValueError('sim context: simulation world required')
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('sim context: limit')
    pack = connection.execute('select authz.nexloop_sim_context_command(%s,%s,%s,%s)', (token_digest, world, consumer_id, limit)).fetchone()[0]
    if type(pack) is not dict or pack.get('schema_version') != SIM_CONTEXT_PROTOCOL or pack.get('world') != world or pack.get('data_mode') != 'simulation':
        raise ValueError('sim context: not a simulation pack')
    return json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def register_sim_context(connection, token_digest: str, world: str, run_id: str, consumer_id: str, limit: int = 64) -> str:
    """Fix the simulation pack of one Run by reference and return its text (the Run's input). A repeat returns the same text."""
    if world_kind(world) not in ('simulation', 'shadow'):
        raise ValueError('sim context: simulation world required')
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('sim context: limit')
    bound = connection.execute('select authz.nexloop_sim_context_register(%s,%s,%s,%s,%s)', (token_digest, world, run_id, consumer_id, limit)).fetchone()[0]
    text = bound['pack_text']
    pack = json.loads(text)
    if pack.get('schema_version') != SIM_CONTEXT_PROTOCOL or pack.get('world') != world or pack.get('data_mode') != 'simulation':
        raise ValueError('sim context: not a simulation pack')
    return text


def sim_run_input(session, run_id: str, consumer_id: str, limit: int = 64) -> str:
    """The Run's input for an authenticated service session of a simulation world: the pack fixed to that Run by reference."""
    with session._backend._pool.connection() as connection, connection.transaction():
        return register_sim_context(connection, session._session.token_digest, session._session.world, run_id, consumer_id, limit)

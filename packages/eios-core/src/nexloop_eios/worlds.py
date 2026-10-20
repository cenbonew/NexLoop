"""NX-041 S1 (design D1, D9): the one world check, mirrored from SQL runtime.nexloop_world_valid / nexloop_world_kind.

A world is real, test, sim:<uuid> or shadow:<uuid>; no other reserved name. 'simulation' is a data mode (costs, Claims), not a world.
"""
from __future__ import annotations

import re

_EXPERIMENT = re.compile(r'(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
KINDS = ('real', 'test', 'simulation', 'shadow')


def valid_world(world) -> bool:
    return isinstance(world, str) and (world in ('real', 'test') or _EXPERIMENT.fullmatch(world) is not None)


def world_kind(world) -> str | None:
    """real | test | simulation | shadow, or None for anything that is not a world."""
    if not valid_world(world):
        return None
    if world in ('real', 'test'):
        return world
    return 'simulation' if world.startswith('sim:') else 'shadow'


def data_mode(world) -> str:
    """The cost / Claim data mode: real, simulation (sim:* and shadow:*), else test."""
    return {'real': 'real', 'simulation': 'simulation', 'shadow': 'simulation'}.get(world_kind(world), 'test')


# NX-041 S6 (D3): a process serves one world group. real (the default): real and test, as before; simulation: one sim:/shadow:
# world, in the separate simulation process group only.
WORLD_GROUPS = ('real', 'simulation')


def group_accepts(group, world) -> bool:
    kind = world_kind(world)
    if group == 'simulation':
        return kind in ('simulation', 'shadow')
    return group == 'real' and kind in ('real', 'test')

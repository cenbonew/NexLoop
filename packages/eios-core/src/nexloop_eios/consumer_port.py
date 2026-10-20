"""NX-041 S7 (design §5): the consumer port and a deterministic twin stub.

The port is the one way a twin (NX-042) produces inbound events in a simulation world: act() records what a synthetic Consumer did
(nexloop-consumer-port-event/1), observe() returns what it was shown. It takes only a connection factory (as nexloop_twin_port, the
port's own role), the simulation world's one credential and a sim:/shadow: world; real and test are refused before anything is read.
It holds no real credential, no provider and no network client, and it calls no Action: the database answers (authz.
nexloop_sim_consumer_act / _observe), and the world time is the server's.

TwinStub is the minimal twin for tests: fixed version and seed, every event a function of (seed, consumer, step) only, so the same
frozen input gives byte-identical events. The real twin (behaviour model, calibration) is NX-042.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from nexloop_eios.worlds import valid_world, world_kind

SIMULATION_KINDS = ('simulation', 'shadow')
EVENT_KINDS = ('message', 'refuse_contact', 'payment_intent', 'visit')


def simulation_world(world: str) -> str:
    if not valid_world(world) or world_kind(world) not in SIMULATION_KINDS:
        raise ValueError('consumer port: simulation world required')
    return world


class ConsumerPort:
    """One simulation world, one credential (D10). connection_factory gives a connection as nexloop_twin_port."""

    def __init__(self, connection_factory, token: str, world: str):
        self.world = simulation_world(world)
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            raise ValueError('consumer port: credential unavailable')
        self._digest = hashlib.sha256(token.encode()).hexdigest()
        self._connect = connection_factory

    def _one(self, sql, params):
        with self._connect() as connection, connection.transaction():
            return connection.execute(sql, params).fetchone()[0]

    def act(self, request: dict) -> dict:
        """request: the request part of nexloop-consumer-port-event/1; returns the event with the server's frozen part."""
        from psycopg.types.json import Jsonb
        if type(request) is not dict or set(request) - {'consumer_id', 'event_kind', 'content', 'twin'}:
            raise ValueError('consumer port: request fields')
        return self._one('select authz.nexloop_sim_consumer_act(%s,%s,%s)', (self._digest, self.world, Jsonb(request)))

    def observe(self, consumer_id: str, after: int = 0) -> list:
        return self._one('select authz.nexloop_sim_consumer_observe(%s,%s,%s,%s)', (self._digest, self.world, consumer_id, after))


PHRASES = ('合成消息：请问明天上午还能预约吗？', '合成消息：价格能再优惠一点吗？', '合成消息：上次的服务很好，谢谢。',
           '合成消息：我想改一下预约时间。', '合成消息：请把发票发给我。')
WEIGHTS = (('message', 6), ('visit', 2), ('payment_intent', 1), ('refuse_contact', 1))


@dataclass(frozen=True)
class TwinStub:
    """A deterministic minimal twin: (seed, consumer, step) -> one request of the port contract."""
    seed: str
    version: str = 'twin-stub/1'

    def _draw(self, consumer_id: str, step: int, label: str) -> int:
        return int(hashlib.sha256(f'{self.version}|{self.seed}|{consumer_id}|{step}|{label}'.encode()).hexdigest()[:8], 16)

    def act(self, consumer_id: str, step: int) -> dict:
        u = self._draw(consumer_id, step, 'kind') % sum(w for _, w in WEIGHTS)
        kind = next(k for k, w in _cumulative(WEIGHTS) if u < w)
        content: dict = {'attributes': {'mood': ('calm', 'busy', 'curious')[self._draw(consumer_id, step, 'mood') % 3]}}
        if kind == 'message':
            content['text'] = PHRASES[self._draw(consumer_id, step, 'text') % len(PHRASES)]
        if kind == 'payment_intent':
            content['attributes']['amount_band'] = ('low', 'mid', 'high')[self._draw(consumer_id, step, 'amount') % 3]
        return {'consumer_id': consumer_id, 'event_kind': kind, 'content': content, 'twin': {'version': self.version, 'seed': self.seed, 'step': step}}

    def run(self, port: ConsumerPort, consumers, steps: int) -> list:
        return [port.act(self.act(c, step)) for step in range(1, steps + 1) for c in consumers]


def _cumulative(weights):
    total = 0
    for kind, weight in weights:
        total += weight
        yield kind, total

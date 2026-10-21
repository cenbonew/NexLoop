"""NX-042 T3 (design §5; NX-041 S7, S7b): the consumer twin on the consumer port.

A twin (ConsumerTwin) is its twin id, published version and seed: it holds no credential and no connection. The port (TwinPort) is the
one way it reaches a simulation world: a connection factory as nexloop_twin_port and the world's one credential; real and test are
refused before anything is read. bind() fixes the twin version to the world, observe() returns what the Consumer sees before a step
(the S7b simulation context pack, fixed by reference, and the observations offered to it), act() returns the step's event - decided by
the server from the twin version's decision layer, written through the S7 port with the twin's provenance and calibration status.
No network client, no Action.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from nexloop_eios.consumer_port import simulation_world


# NX-042 T6 (D1): the text layer. The default generator is the deterministic template (rendered by the server in act); a generator
# that calls a model is an interface only - an experiment's own approved budget is required and no slice provides one yet.
TEMPLATE_GENERATOR = 'twin-template/1'
SIMULATION_MARKER = '【模拟消息】'


class ModelBudgetRequired(PermissionError):
    """A model text generator needs an experiment's approved budget (D1); there is none."""


class ModelTextGenerator:
    """The model path of the text layer: interface only. render() always refuses (model_budget_required); it opens no connection."""

    def __init__(self, generator_version: str):
        self.generator_version = generator_version

    def render(self, *, behavior: str, kind: str, seed: str, step: int) -> str:
        raise ModelBudgetRequired('model_budget_required')


@dataclass(frozen=True)
class ConsumerTwin:
    twin_id: str
    version: int
    seed: str


class TwinPort:
    def __init__(self, connection_factory, token: str, world: str):
        self.world = simulation_world(world)
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            raise ValueError('twin port: credential unavailable')
        self._digest = hashlib.sha256(token.encode()).hexdigest()
        self._connect = connection_factory

    def _one(self, sql, params):
        with self._connect() as connection, connection.transaction():
            return connection.execute(sql, params).fetchone()[0]

    def bind(self, twin: ConsumerTwin) -> dict:
        return self._one('select authz.nexloop_twin_bind(%s,%s,%s,%s,%s)', (self._digest, self.world, twin.twin_id, twin.version, twin.seed))

    def observe(self, binding_id: str, consumer_id: str, step: int, limit: int = 32) -> dict:
        return self._one('select authz.nexloop_twin_observe(%s,%s,%s,%s,%s,%s)', (self._digest, self.world, binding_id, consumer_id, step, limit))

    def act(self, binding_id: str, consumer_id: str, step: int) -> dict:
        return self._one('select authz.nexloop_twin_act(%s,%s,%s,%s,%s)', (self._digest, self.world, binding_id, consumer_id, step))

    def run(self, twin: ConsumerTwin, consumers, steps: int, limit: int = 32) -> list:
        """N steps of observe then act for every Consumer; returns the acts in order."""
        binding = self.bind(twin)['binding_id']
        acts = []
        for step in range(1, steps + 1):
            for consumer in consumers:
                self.observe(binding, consumer, step, limit)
                acts.append(self.act(binding, consumer, step))
        return acts

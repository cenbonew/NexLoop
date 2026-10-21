"""NX-041 S4 (design §3.2, D4, D11, D12): the stand-in executor of a simulation world.

It takes only a database URL file, the simulation world's service credential file and a sim:/shadow: world. There is no provider
configuration, no routes file, no channel credential and no network client in this module: the only thing it calls is the database
(authz.nexloop_sim_effect_pending / _execute / _query), which answers with a deterministic stand-in receipt marked simulated.
real and test are refused before any file is read.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import signal
import sys
import threading

import psycopg

from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.worlds import valid_world, world_kind

SIMULATION_KINDS = ('simulation', 'shadow')


@dataclass(frozen=True)
class StandInReceipt:
    """The shape of effect_provider.EffectProviderReceipt (status, external reference, latency) plus the explicit simulated marker,
    the stand-in version and the output kind; a stand-in receipt is a simulated output, never a fact."""
    intent_id: str
    world: str
    status: str
    external_ref: str
    latency_ms: int
    stand_in_id: str
    stand_in_version: str
    failure_code: str | None
    executions: int
    repeated: bool
    simulated: bool = True
    data_mode: str = 'simulation'
    output_kind: str = 'simulated_output'

    @classmethod
    def from_row(cls, value: dict) -> 'StandInReceipt':
        if value.get('simulated') is not True or value.get('output_kind') != 'simulated_output' or value.get('data_mode') != 'simulation':
            raise ValueError('not a stand-in receipt')
        return cls(intent_id=str(value['intent_id']), world=value['world'], status=value['status'], external_ref=value['external_ref'],
                   latency_ms=int(value['latency_ms']), stand_in_id=value['stand_in_id'], stand_in_version=value['stand_in_version'],
                   failure_code=value.get('failure_code'), executions=int(value['executions']), repeated=bool(value['repeated']))


def simulation_world(world: str) -> str:
    if not valid_world(world) or world_kind(world) not in SIMULATION_KINDS:
        raise ValueError('stand-in executor: simulation world required')
    return world


class StandInExecutor:
    """One simulation world, one credential (D10). connection_factory gives a connection as nexloop_stand_in_worker, the executor's
    own role (it executes the stand-in entries only; never the real executor's role)."""

    def __init__(self, connection_factory, token: str, world: str):
        self.world = simulation_world(world)
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            raise ValueError('stand-in executor: credential unavailable')
        self._digest = hashlib.sha256(token.encode()).hexdigest()
        self._connect = connection_factory

    def _call(self, sql, params):
        with self._connect() as connection, connection.transaction():
            return connection.execute(sql, params).fetchall()

    def pending(self, limit=50):
        return [row[0] for row in self._call('select authz.nexloop_sim_effect_pending(%s,%s,%s)', (self._digest, self.world, limit))]

    def execute(self, intent_id) -> StandInReceipt:
        return StandInReceipt.from_row(self._call('select authz.nexloop_sim_effect_execute(%s,%s,%s)', (self._digest, self.world, intent_id))[0][0])

    def query(self, intent_id) -> StandInReceipt | None:
        row = self._call('select authz.nexloop_sim_effect_query(%s,%s,%s)', (self._digest, self.world, intent_id))[0][0]
        return None if row is None else StandInReceipt.from_row(row)

    def run_once(self, limit=50) -> list[StandInReceipt]:
        return [self.execute(intent) for intent in self.pending(limit)]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Stand-in executor configuration unavailable\n')


def _arguments(argv):
    parser = _Parser(description=__doc__)
    parser.add_argument('--database-url-file', type=Path, required=True)
    parser.add_argument('--service-credential-file', type=Path, required=True)
    parser.add_argument('--world', required=True)
    parser.add_argument('--tick-seconds', type=float, default=1.0)
    parser.add_argument('--once', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        simulation_world(arguments.world)
    except ValueError:
        parser.error('world')
    if not 0 < arguments.tick_seconds <= 60:
        parser.error('tick')
    return arguments


def main(argv=None):
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    dsn = read_private_text(arguments.database_url_file, maximum=16384)
    token = read_private_text(arguments.service_credential_file, maximum=16384).strip()
    executor = StandInExecutor(lambda: psycopg.connect(dsn, autocommit=True), token, arguments.world)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while True:
        executor.run_once()
        if arguments.once or stop.wait(arguments.tick_seconds):
            return 0


if __name__ == '__main__':
    raise SystemExit(main())

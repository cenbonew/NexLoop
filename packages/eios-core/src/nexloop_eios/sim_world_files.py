"""NX-041 S6 (D2, D3, D6): the simulation process group's file purger.

When a simulation world's deletion starts (S2), the database queues its directory on the simulation Agent Hosts
(runtime.nexloop_sim_world_file_purges). This process serves one Host (--host-id), connected as its own role nexloop_sim_files
(review 1.2/3.1) with no world credential (the world's own was revoked at deletion). For each world its Host has not confirmed, the
database gives a purge permit (HMAC with the Host's purge key, minutes); the Host removes the directory only with that permit and
answers with a signed deletion proof, which the database verifies before it records anything (review 2.3/3.2). This process holds
no Host key: it can neither make a permit nor a proof. A failed call is retried on the next tick.
"""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import re
import signal
import ssl
import sys
import threading

import psycopg

from psycopg.types.json import Jsonb

from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.worlds import world_kind


class HostWorldPurger:
    """POST /internal/v1/worlds/purge on the simulation Host (127.0.0.1 only, TLS, internal key); bounded reply."""

    RESPONSE_LIMIT = 4096

    def __init__(self, *, port, key_file, ca_file):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('host port')
        self.port = port
        self.key = read_private_text(key_file, maximum=128).strip()
        self.context = ssl.create_default_context(cafile=str(ca_file))

    def __call__(self, world, permit):
        connection = http.client.HTTPSConnection('127.0.0.1', self.port, context=self.context, timeout=10)
        try:
            connection.request('POST', '/internal/v1/worlds/purge', body=json.dumps({'world': world, 'permit': permit}),
                               headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json', 'Host': f'127.0.0.1:{self.port}'})
            response = connection.getresponse()
            body = response.read(self.RESPONSE_LIMIT + 1)
            if response.status != 200 or len(body) > self.RESPONSE_LIMIT:
                return None
            answer = json.loads(body)
            proof = answer.get('proof') if type(answer) is dict else None
            return proof if type(proof) is dict and proof.get('world') == world else None
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close()


class SimWorldFilePurger:
    """One tick for one Host: every world that Host has not confirmed, purged with the database's permit, then the Host's signed
    proof handed to the database (which verifies it). Returns {world: outcome or 'failed'}."""

    def __init__(self, connection_factory, host, host_id):
        self._connect, self.host, self.host_id = connection_factory, host, host_id

    def tick(self, limit=20):
        with self._connect() as connection:
            pending = connection.execute('select world,permit from authz.nexloop_sim_world_files_pending(%s,%s)', (self.host_id, limit)).fetchall()
        done = {}
        for world, permit in pending:
            if world_kind(world) not in ('simulation', 'shadow'):
                continue
            proof = self.host(world, permit)
            if proof is None:
                done[world] = 'failed'
                continue
            with self._connect() as connection:
                connection.execute('select authz.nexloop_sim_world_files_confirm(%s,%s,%s)', (self.host_id, world, Jsonb(proof)))
            done[world] = proof.get('outcome')
        return done


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Simulation world file purger configuration unavailable\n')


def _arguments(argv):
    parser = _Parser(description=__doc__)
    for name in ('database-url-file', 'host-control-key-file', 'host-ca-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--host-port', type=int, required=True)
    parser.add_argument('--host-id', required=True)
    parser.add_argument('--tick-seconds', type=float, default=60.0)
    parser.add_argument('--once', action='store_true')
    arguments = parser.parse_args(argv)
    if (not 1024 <= arguments.host_port <= 65535 or not 1 <= arguments.tick_seconds <= 3600
            or re.fullmatch(r'[a-z][a-z0-9-]{2,62}', arguments.host_id) is None):
        parser.error('configuration')
    return arguments


def main(argv=None):
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    dsn = read_private_text(arguments.database_url_file, maximum=16384)
    purger = SimWorldFilePurger(lambda: psycopg.connect(dsn, autocommit=True),
                                HostWorldPurger(port=arguments.host_port, key_file=arguments.host_control_key_file, ca_file=arguments.host_ca_file),
                                arguments.host_id)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while True:
        purger.tick()
        if arguments.once or stop.wait(arguments.tick_seconds):
            return 0


if __name__ == '__main__':
    raise SystemExit(main())

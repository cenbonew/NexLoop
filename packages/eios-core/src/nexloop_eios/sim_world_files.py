"""NX-041 S6 (D2, D3, D6): the simulation process group's file purger.

When a simulation world's deletion starts (S2), the database queues its directory on the simulation Agent Host
(runtime.nexloop_sim_world_file_purges). This process, connected as nexloop_scheduler with no world credential (the world's own was
revoked at deletion), asks the simulation Host to remove the world's directory over its loopback internal API and confirms the
outcome (purged or absent). A failed call is retried on the next tick; nothing is confirmed that the Host did not report.
"""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import signal
import ssl
import sys
import threading

import psycopg

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

    def __call__(self, world):
        connection = http.client.HTTPSConnection('127.0.0.1', self.port, context=self.context, timeout=10)
        try:
            connection.request('POST', '/internal/v1/worlds/purge', body=json.dumps({'world': world}),
                               headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json', 'Host': f'127.0.0.1:{self.port}'})
            response = connection.getresponse()
            body = response.read(self.RESPONSE_LIMIT + 1)
            if response.status != 200 or len(body) > self.RESPONSE_LIMIT:
                return 'failed'
            outcome = json.loads(body).get('outcome')
            return outcome if outcome in ('purged', 'absent') else 'failed'
        except (OSError, ValueError, http.client.HTTPException):
            return 'failed'
        finally:
            connection.close()


class SimWorldFilePurger:
    """One tick: every queued deleted world, purged on the Host, then confirmed. Returns {world: outcome}."""

    def __init__(self, connection_factory, host):
        self._connect, self.host = connection_factory, host

    def tick(self, limit=20):
        with self._connect() as connection:
            worlds = [row[0] for row in connection.execute('select authz.nexloop_sim_world_files_pending(%s)', (limit,)).fetchall()]
        done = {}
        for world in worlds:
            if world_kind(world) not in ('simulation', 'shadow'):
                continue
            outcome = self.host(world)
            done[world] = outcome
            if outcome in ('purged', 'absent'):
                with self._connect() as connection:
                    connection.execute('select authz.nexloop_sim_world_files_confirm(%s,%s)', (world, outcome))
        return done


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Simulation world file purger configuration unavailable\n')


def _arguments(argv):
    parser = _Parser(description=__doc__)
    for name in ('database-url-file', 'host-control-key-file', 'host-ca-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--host-port', type=int, required=True)
    parser.add_argument('--tick-seconds', type=float, default=60.0)
    parser.add_argument('--once', action='store_true')
    arguments = parser.parse_args(argv)
    if not 1024 <= arguments.host_port <= 65535 or not 1 <= arguments.tick_seconds <= 3600:
        parser.error('configuration')
    return arguments


def main(argv=None):
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    dsn = read_private_text(arguments.database_url_file, maximum=16384)
    purger = SimWorldFilePurger(lambda: psycopg.connect(dsn, autocommit=True),
                                HostWorldPurger(port=arguments.host_port, key_file=arguments.host_control_key_file, ca_file=arguments.host_ca_file))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while True:
        purger.tick()
        if arguments.once or stop.wait(arguments.tick_seconds):
            return 0


if __name__ == '__main__':
    raise SystemExit(main())

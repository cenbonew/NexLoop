#!/usr/bin/env python3
"""NX-037 gap D: the "result temporarily unknown" drill hook for the 3 `unknown_reconciliation` interactions (D-i).

The owner must be able to make one real delivery's outcome unknown to NexLoop *after* the enterprise endpoint has committed
it, and then check that NexLoop queries and reconciles instead of resending. This relay sits between the effect worker and
the enterprise endpoint and copies bytes in both directions (TLS stays end to end; it never sees plaintext, keys or
credentials). When armed, the next connection's request still reaches the endpoint, but the endpoint's answer is withheld
for --hold-seconds (longer than the worker's 2 s deadline) and the connection is then closed: the worker records the
attempt as unknown, and its next step must be the query (GET), which passes through untouched once the arm is used up.

    python scripts/nx037/unknown_drill_relay.py serve --listen 127.0.0.1:<ORIGIN_PORT> --target <ENDPOINT_IP>:<ORIGIN_PORT> \\
        --control <PRIVATE_CONTROL_FILE> [--hold-seconds 10]
    python scripts/nx037/unknown_drill_relay.py arm --control <PRIVATE_CONTROL_FILE> [--count 1]
    python scripts/nx037/unknown_drill_relay.py status --control <PRIVATE_CONTROL_FILE>

The effect worker reaches the relay by its provider configuration's numeric connect address (the origin host name stays the
TLS SNI and certificate name), so the relay listens on the origin's port. Withholding starts only after the client has sent
application data: for TLS, its first application_data record (after the handshake in TLS 1.2 and 1.3); for plain loopback
HTTP (tests), its first bytes. The control file is a private JSON file {"armed": n, "withheld": total}; `arm` adds to the
count, every withheld connection uses one. Standard library only; it records counts, never content.
"""
import argparse
import asyncio
import fcntl
import ipaddress
import json
import os
import sys
from pathlib import Path

TLS_HANDSHAKE, TLS_APPLICATION_DATA = 0x16, 0x17


def _endpoint(value):
    host, _, port = value.rpartition(':')
    host = host.strip('[]')
    ipaddress.ip_address(host)
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('port')
    return host, port


def _update(control, change):
    """Read-modify-write the control file under an exclusive lock; returns the new state."""
    path = Path(control)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('control file must be private')
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 4096)
        state = json.loads(raw) if raw.strip() else {'armed': 0, 'withheld': 0}
        if type(state) is not dict or set(state) != {'armed', 'withheld'} or not all(type(state[k]) is int and state[k] >= 0 for k in state):
            raise ValueError('control file invalid')
        state = change(dict(state))
        data = json.dumps(state).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)
        os.fsync(fd)
        return state
    finally:
        os.close(fd)


def arm(control, count=1):
    if not 1 <= count <= 10:
        raise ValueError('count')
    return _update(control, lambda s: {**s, 'armed': s['armed'] + count})


def status(control):
    return _update(control, lambda s: s)


def _take(control):
    taken = []

    def change(state):
        if state['armed'] > 0:
            taken.append(True)
            return {'armed': state['armed'] - 1, 'withheld': state['withheld'] + 1}
        return state
    _update(control, change)
    return bool(taken)


class _ClientWatch:
    """Tells when the client has started sending application data."""

    def __init__(self):
        self.mode, self.buffer, self.started = None, b'', False

    def feed(self, data):
        if self.started:
            return
        if self.mode is None:
            self.mode = 'tls' if data[:1] == bytes([TLS_HANDSHAKE]) else 'plain'
        if self.mode == 'plain':
            self.started = True
            return
        self.buffer += data
        while len(self.buffer) >= 5:
            kind, length = self.buffer[0], int.from_bytes(self.buffer[3:5], 'big')
            if kind == TLS_APPLICATION_DATA:
                self.started = True
                self.buffer = b''
                return
            if len(self.buffer) < 5 + length:
                return
            self.buffer = self.buffer[5 + length:]


async def _client_to_upstream(reader, writer, watch, started):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            watch.feed(data)
            writer.write(data)
            await writer.drain()
            if watch.started:
                started.set()
    except (ConnectionError, OSError):
        pass
    finally:
        started.set()
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _upstream_to_client(reader, writer, watch, withhold, hold_seconds):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if withhold and watch.started:
                # The endpoint has answered the request: drop the answer, keep the connection silent, then close.
                await asyncio.sleep(hold_seconds)
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve(listen, target, control, hold_seconds=10.0, ready=None):
    async def handle(reader, writer):
        withhold = _take(control)
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(*target), timeout=5)
        except (OSError, asyncio.TimeoutError):
            writer.close()
            return
        watch, started = _ClientWatch(), asyncio.Event()
        await asyncio.gather(_client_to_upstream(reader, up_writer, watch, started),
                             _upstream_to_client(up_reader, writer, watch, withhold, hold_seconds))
        try:
            up_writer.close()
        except Exception:
            pass
    server = await asyncio.start_server(handle, *listen)
    if ready is not None:
        ready(server.sockets[0].getsockname()[1])
    async with server:
        await server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description='NX-037 unknown-result drill relay')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--listen', required=True)
    s.add_argument('--target', required=True)
    s.add_argument('--control', required=True)
    s.add_argument('--hold-seconds', type=float, default=10.0)
    a = sub.add_parser('arm')
    a.add_argument('--control', required=True)
    a.add_argument('--count', type=int, default=1)
    t = sub.add_parser('status')
    t.add_argument('--control', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            if not 3 <= args.hold_seconds <= 120:
                raise ValueError('hold seconds')
            listen, target = _endpoint(args.listen), _endpoint(args.target)
            status(args.control)
            asyncio.run(serve(listen, target, args.control, args.hold_seconds))
            return 0
        state = arm(args.control, args.count) if args.command == 'arm' else status(args.control)
        print(json.dumps(state))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 2


if __name__ == '__main__':
    sys.exit(main())

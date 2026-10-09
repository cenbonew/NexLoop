"""Runtime guard child processes (NX-049 multi-process prototype, off by default).

`nexloop-runtime-worker --guard-workers N` (N > 1) keeps the dispatcher in the
parent and serves the loopback guard from N child processes. The parent creates
and binds the listener; each child inherits the file descriptor, opens its own
Backend, authenticates afresh and runs the unchanged guard handler. No child binds
a port (no SO_REUSEPORT). Secrets reach a child only as the same private file
paths the parent was given. A child stops on SIGTERM/SIGINT or when the parent's
pipe closes; it stops accepting, answers every accepted connection, then closes
its Backend (LifecycleLock waits for in-flight commits). The parent restarts a
child that exits unexpectedly and gives up (stops claiming, exits non-zero) when
children keep exiting.
"""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

READY = 'Runtime guard worker ready'


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Runtime guard worker configuration unavailable\n')


def _arguments(argv):
    parser = _Parser(description=__doc__)
    parser.add_argument('--listen-fd', type=int, required=True)
    parser.add_argument('--guard-port', type=int, required=True)
    for option in ('database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root',
                   'guard-key-file', 'guard-certificate-file', 'guard-tls-key-file'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    parser.add_argument('--world', required=True)
    args = parser.parse_args(argv)
    if args.listen_fd < 3 or not 1024 <= args.guard_port <= 65535 or not args.world or len(args.world) > 255:
        parser.error('configuration')
    return args


def run_child(arguments, stop):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text
    from nexloop_eios.runtime_control import create_runtime_guard_server
    from nexloop_eios.runtime_worker import _FreshGuard
    listener = socket.socket(fileno=arguments.listen_fd)
    try:
        dsn = read_private_text(arguments.database_url_file, maximum=16384)
        read_private_text(arguments.service_credential_file, maximum=16384)
        with open_backend(database_url=dsn, artifact_root=arguments.artifact_root,
                          signing_key_file=arguments.signing_key_file, signing_key_id=arguments.signing_key_id) as backend:
            with backend._pool.connection() as connection:
                if verify_application_role(connection) not in {'nexloop_domain_worker', 'nexloop_scheduler'}:
                    raise ValueError('restricted Worker role required')
            guard = _FreshGuard(backend, arguments.service_credential_file, arguments.world)
            guard.service()  # current authentication before accepting any connection
            server = create_runtime_guard_server(guard, port=arguments.guard_port, key_file=arguments.guard_key_file,
                certificate_file=arguments.guard_certificate_file, tls_key_file=arguments.guard_tls_key_file,
                listen_socket=listener)
            thread = threading.Thread(target=server.serve_forever, name='nexloop-runtime-guard', daemon=True)
            thread.start()
            try:
                print(READY, flush=True)
                stop.wait()
            finally:
                server.shutdown()
                drained = server.drain(15)
                server.server_close()
                thread.join(5)
            if not drained or thread.is_alive():
                raise RuntimeError('guard drain unavailable')
        return 0
    finally:
        listener.close()


def main(argv=None):
    arguments = _arguments(argv)
    stop = threading.Event()
    import logging
    logging.getLogger('psycopg.pool').disabled = True

    def stop_serving(signum, frame):
        stop.set()

    def parent_watch():
        # The parent never writes; EOF means it exited (or closed us on purpose).
        try:
            while os.read(0, 4096):
                pass
        except OSError:
            pass
        stop.set()

    signal.signal(signal.SIGTERM, stop_serving)
    signal.signal(signal.SIGINT, stop_serving)
    threading.Thread(target=parent_watch, name='nexloop-guard-parent-watch', daemon=True).start()
    try:
        return run_child(arguments, stop)
    except Exception:
        print('Runtime guard worker unavailable', file=sys.stderr, flush=True)
        return 1


@dataclass(frozen=True)
class GuardFiles:
    database_url_file: Path
    signing_key_file: Path
    signing_key_id: str
    service_credential_file: Path
    artifact_root: Path
    world: str
    guard_key_file: Path
    guard_certificate_file: Path
    guard_tls_key_file: Path


class GuardWorkerPool:
    """Parent side: one bound loopback listener shared by N supervised guard children."""

    def __init__(self, files, *, port, workers, ready_timeout=30.0, stop_timeout=20.0,
                 max_restarts=4, restart_window=60.0, on_failure=None):
        if type(workers) is not int or not 2 <= workers <= 16:
            raise ValueError('guard worker count must be 2..16')
        if type(port) is not int or not (port == 0 or 1024 <= port <= 65535):
            raise ValueError('unprivileged loopback port required')
        self.files, self.workers, self.port = files, workers, port
        self.ready_timeout, self.stop_timeout = ready_timeout, stop_timeout
        self.max_restarts, self.restart_window = max_restarts, restart_window
        self.on_failure = on_failure
        self.failed = False
        self._listener = None
        self._children = []
        self._restarts = []
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._monitor = None

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', self.port))
            listener.listen(64)
            listener.set_inheritable(True)
            self._listener = listener
            self.port = listener.getsockname()[1]
            for _ in range(self.workers):
                self._children.append(self._spawn())
        except BaseException:
            self.stop()
            raise
        self._monitor = threading.Thread(target=self._supervise, name='nexloop-guard-supervisor', daemon=True)
        self._monitor.start()
        return self.port

    def pids(self):
        with self._lock:
            return [child.pid for child in self._children if child.poll() is None]

    def _spawn(self):
        f = self.files
        fd = self._listener.fileno()
        child = subprocess.Popen([sys.executable, '-m', 'nexloop_eios.runtime_guard_worker',
            '--listen-fd', str(fd), '--guard-port', str(self.port),
            '--database-url-file', str(f.database_url_file), '--signing-key-file', str(f.signing_key_file),
            '--signing-key-id', f.signing_key_id, '--service-credential-file', str(f.service_credential_file),
            '--artifact-root', str(f.artifact_root), '--world', f.world, '--guard-key-file', str(f.guard_key_file),
            '--guard-certificate-file', str(f.guard_certificate_file), '--guard-tls-key-file', str(f.guard_tls_key_file)],
            pass_fds=(fd,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, close_fds=True, start_new_session=True)
        ready = threading.Event()

        def read():
            for line in child.stdout:
                if line.strip() == READY:
                    ready.set()
        threading.Thread(target=read, name='nexloop-guard-child-stdout', daemon=True).start()
        deadline = time.monotonic() + self.ready_timeout
        while not ready.wait(.05):
            if child.poll() is not None or time.monotonic() >= deadline:
                self._terminate([child], kill_after=2)
                raise RuntimeError('guard worker unavailable')
        return child

    def _supervise(self):
        while not self._stopping.wait(.1):
            with self._lock:
                exited = [child for child in self._children if child.poll() is not None]
            for child in exited:
                if self._stopping.is_set():
                    return
                now = time.monotonic()
                self._restarts = [t for t in self._restarts if now - t < self.restart_window] + [now]
                replacement = None
                if len(self._restarts) <= self.max_restarts:
                    try:
                        replacement = self._spawn()
                    except Exception:
                        replacement = None
                for stream in (child.stdin, child.stdout):
                    try:
                        stream.close()
                    except Exception:
                        pass
                with self._lock:
                    self._children.remove(child)
                    if replacement is not None:
                        self._children.append(replacement)
                if replacement is None:
                    self.failed = True
                    if self.on_failure is not None:
                        self.on_failure()
                    return

    def _terminate(self, children, *, kill_after):
        for child in children:
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + kill_after
        clean = True
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                clean = False
                child.kill()
                child.wait(timeout=5)
            for stream in (child.stdin, child.stdout):
                try:
                    stream.close()
                except Exception:
                    pass
        return clean

    def stop(self):
        """Stop accepting, let every child drain, then release the port."""
        self._stopping.set()
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(5)
        with self._lock:
            children, self._children = self._children, []
        clean = self._terminate(children, kill_after=self.stop_timeout)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if not clean:
            raise RuntimeError('guard shutdown unavailable')


if __name__ == '__main__':
    sys.exit(main())

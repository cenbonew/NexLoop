"""pytest plugin: verify a backend request never holds more than two pooled connections.

LifecycleLock's capacity = pool_max_size // 2 assumes one outer transaction connection
plus at most one nested connection per request. This plugin measures that assumption:
every psycopg_pool getconn/putconn in a thread that is inside a backend request
(nexloop_eios.backend.request_depth() > 0) is counted per thread; a third concurrent
connection fails at once. Nesting call paths (depth 2) are aggregated and written to
NEXLOOP_POOL_DEPTH_REPORT when set. Enable with `-p support.pool_depth_plugin`.
"""
import collections
import json
import os
import threading
import traceback

import psycopg_pool

from nexloop_eios.backend import request_depth

LIMIT = 2
_held = threading.local()
_paths = collections.Counter()
_maximum = [0]
_lock = threading.Lock()
_original_getconn = psycopg_pool.ConnectionPool.getconn
_original_putconn = psycopg_pool.ConnectionPool.putconn


class PoolDepthExceeded(AssertionError):
    pass


def _path():
    frames = [f for f in traceback.extract_stack()[:-3] if '/nexloop_eios/' in f.filename]
    return ' > '.join(f"{os.path.basename(f.filename)}:{f.name}" for f in frames[-6:])


def _getconn(self, *args, **kwargs):
    inside = request_depth() > 0
    count = getattr(_held, 'count', 0) + 1 if inside else getattr(_held, 'count', 0)
    if inside and count > LIMIT:
        raise PoolDepthExceeded(f'backend request holds {count} pooled connections: {_path()}')
    connection = _original_getconn(self, *args, **kwargs)
    if inside:
        _held.count = count
        connections = getattr(_held, 'connections', set()); connections.add(id(connection)); _held.connections = connections
        with _lock:
            _maximum[0] = max(_maximum[0], count)
            if count == LIMIT:
                _paths[_path()] += 1
    return connection


def _putconn(self, connection, *args, **kwargs):
    connections = getattr(_held, 'connections', set())
    if id(connection) in connections:
        connections.discard(id(connection)); _held.count = getattr(_held, 'count', 1) - 1
    return _original_putconn(self, connection, *args, **kwargs)


def pytest_configure(config):
    psycopg_pool.ConnectionPool.getconn = _getconn
    psycopg_pool.ConnectionPool.putconn = _putconn


def pytest_unconfigure(config):
    psycopg_pool.ConnectionPool.getconn = _original_getconn
    psycopg_pool.ConnectionPool.putconn = _original_putconn
    target = os.environ.get('NEXLOOP_POOL_DEPTH_REPORT')
    if target:
        with open(target, 'w') as handle:
            json.dump({'limit': LIMIT, 'maximum_observed': _maximum[0],
                       'nesting_paths': [{'path': path, 'count': count} for path, count in _paths.most_common()]}, handle, indent=1)

"""NX-031 D7 measurement only (never imported unless this directory is put first on PYTHONPATH by scripts/load/measure_pools.py).

With NEXLOOP_POOL_PROBE set, every psycopg_pool.ConnectionPool created in this interpreter (and in child interpreters that
inherit the environment) that matches the selector gets max_size forced to the probe size, and its get_stats() is appended to
NEXLOOP_POOL_PROBE_OUT when it closes. Selectors:
  module:<python module>=<size>   pools opened while a frame of that module is on the stack (a service's own entry point)
  role:<database login role>=<size>   pools that log in as that role (every pool of the role in the test)
Only counters are recorded (no DSN, no credentials)."""
import os

_SPEC = os.environ.get('NEXLOOP_POOL_PROBE')
if _SPEC:
    import json
    import sys
    import psycopg_pool

    _KIND, _REST = _SPEC.split(':', 1)
    _TARGET, _SIZE = _REST.rsplit('=', 1)
    _SIZE = int(_SIZE)
    _OUT = os.environ['NEXLOOP_POOL_PROBE_OUT']
    _init, _close = psycopg_pool.ConnectionPool.__init__, psycopg_pool.ConnectionPool.close

    def _matches(kwargs):
        if _KIND == 'role':
            return (kwargs.get('kwargs') or {}).get('user') == _TARGET
        frame = sys._getframe(2)
        while frame is not None:
            spec = frame.f_globals.get('__spec__')
            if frame.f_globals.get('__name__') == _TARGET or getattr(spec, 'name', None) == _TARGET:  # also `python -m <module>`
                return True
            frame = frame.f_back
        return False

    def __init__(self, conninfo='', **kwargs):
        hit = _matches(kwargs)
        if hit:
            kwargs['max_size'] = _SIZE
            kwargs['min_size'] = min(kwargs.get('min_size', 1) or 1, _SIZE)
        self._nexloop_probe = hit
        _init(self, conninfo, **kwargs)

    def close(self, *args, **kwargs):
        if getattr(self, '_nexloop_probe', False):
            try:
                stats = self.get_stats()
                with open(_OUT, 'a') as handle:
                    handle.write(json.dumps({'pid': os.getpid(), 'selector': _SPEC, 'max_size': self.max_size,
                        **{k: stats.get(k, 0) for k in ('requests_num', 'requests_queued', 'requests_wait_ms', 'requests_errors', 'connections_num')}}) + '\n')
            except Exception:
                pass
        return _close(self, *args, **kwargs)

    psycopg_pool.ConnectionPool.__init__ = __init__
    psycopg_pool.ConnectionPool.close = close

"""Test-only: record runtime guard calls of guard child processes (no effect unless enabled).

Loaded only when a test puts this directory on PYTHONPATH and sets NEXLOOP_TEST_GUARD_CALL_LOG
(tests/test_agent_host_run_limit.py). Each guard request appends one JSON line
{"at": wall-clock seconds, "run_id", "operation"} with O_APPEND, so lines from several guard
processes interleave whole. Only the Run id and operation name are written; no command body,
activation reference, credential or result.
"""
import json
import os

_LOG = os.environ.get('NEXLOOP_TEST_GUARD_CALL_LOG')
if _LOG:
    import time
    from nexloop_eios.backend import AuthenticatedServices

    def _record(operation, run_id):
        line = (json.dumps({'at': time.time(), 'run_id': run_id, 'operation': operation}) + '\n').encode()
        fd = os.open(_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    _authorize = AuthenticatedServices.authorize_runtime_activation

    def authorize_runtime_activation(self, **kwargs):
        command = kwargs.get('command') or {}
        _record(kwargs.get('operation'), command.get('run_id'))
        return _authorize(self, **kwargs)

    AuthenticatedServices.authorize_runtime_activation = authorize_runtime_activation

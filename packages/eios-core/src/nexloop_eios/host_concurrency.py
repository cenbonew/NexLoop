"""Agent Host global active-Run limit of a deployment (ADR-022 §4, ADR-024).

The NX-049 gate is measured with 4 guard processes and assumes production Agent Hosts
run at most 4 Runs at once. MAXIMUM_HOST_ACTIVE_RUNS is fixed here, not read from the
deployment: raising it requires re-measuring the gate and amending the ADR first.
The defaults mirror apps/agent-host/src/run-admission-gate.ts.
"""
import json

MAXIMUM_HOST_ACTIVE_RUNS = 4
HOST_DEFAULT_ACTIVE_RUNS = 4
HOST_DEFAULT_ADMISSION_WAIT_MS = 500


class HostConcurrencyInvalid(ValueError):
    pass


def evaluate(config):
    """Check a parsed Agent Host runtime configuration (the private runtime-config JSON)."""
    if type(config) is not dict:
        raise HostConcurrencyInvalid('Agent Host configuration is invalid')
    limit = config.get('maximum_active_runs', HOST_DEFAULT_ACTIVE_RUNS)
    wait = config.get('run_admission_wait_ms', HOST_DEFAULT_ADMISSION_WAIT_MS)
    if type(limit) is not int or not 1 <= limit <= 64:
        raise HostConcurrencyInvalid('maximum_active_runs must be an integer 1..64')
    if type(wait) is not int or not 0 <= wait <= 30000:
        raise HostConcurrencyInvalid('run_admission_wait_ms must be an integer 0..30000')
    return {'passed': limit <= MAXIMUM_HOST_ACTIVE_RUNS,
            'details': {'maximum_active_runs': limit, 'explicit': 'maximum_active_runs' in config,
                        'run_admission_wait_ms': wait, 'deployment_limit': MAXIMUM_HOST_ACTIVE_RUNS}}


def load(text):
    try:
        return json.loads(text)
    except ValueError:
        raise HostConcurrencyInvalid('Agent Host configuration is unreadable') from None

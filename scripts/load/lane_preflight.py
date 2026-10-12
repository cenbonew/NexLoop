#!/usr/bin/env python3
"""NX-031 lane preflight (read-only): may the full matrix run start on this lane now?

    uv run --frozen python scripts/load/lane_preflight.py [--template <run_dir>/template]

Reads files, probes the CI lock without holding it, and inspects the repository's load and deployment configuration. It writes
nothing, starts nothing and needs no database. Prints one JSON object {checks: {name: bool}, details: {...}, passed: bool};
exit 0 when every check passes, 2 otherwise. Parameters come from the environment (values live on the lane, never in git):
  NEXLOOP_CI_LOCK          lock file shared with the regular CI (required)
  LOAD_SEED / LOAD_CONSUMERS          default 31 / 1000
  LOAD_MATRIX              matrix file (default deploy/load/matrix.v1.json)
  LOAD_MIN_FREE_GIB        free disk under .ci-results/load (default 20)
  LOAD_MIN_FREE_MEM_GIB    available memory (default 8)
On a host without distinct loopback sources (macOS without aliases) distinct_loopback_sources fails: the run still works, but
each cell's warm-up then waits out the login limit (about 50 minutes per cell at 1000 customers).
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/load'))
import synthetic  # noqa: E402


def _git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True).stdout.strip()


def lock_idle(path):
    """True when nobody holds the CI lock right now (probed with a non-blocking lock that is released at once)."""
    if not path or not Path(path).is_file():
        return False, 'missing'
    with open(path, 'rb') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, 'held'
        fcntl.flock(handle, fcntl.LOCK_UN)
    return True, 'idle'


def available_memory_gib():
    meminfo = Path('/proc/meminfo')
    if meminfo.is_file():
        values = dict(line.split(':', 1) for line in meminfo.read_text().splitlines() if ':' in line)
        return int(values['MemAvailable'].split()[0]) / 1048576
    out = subprocess.run(['vm_stat'], capture_output=True, text=True).stdout  # macOS: free + inactive + speculative pages
    page = int(re.search(r'page size of (\d+)', out).group(1))
    pages = sum(int(re.search(rf'{name}:\s+(\d+)', out).group(1)) for name in ('Pages free', 'Pages inactive', 'Pages speculative'))
    return pages * page / 2 ** 30


def deployment_premises():
    """guard 4, dispatch_loops 4, Host <= 4 on the gate cells, and the cells' chain built with those values."""
    from nexloop_eios import connection_budget
    budget = json.loads((ROOT / 'deploy/stage/connection-budget.v1.json').read_text())
    runtime = next(s for s in budget['services'] if s['name'] == 'runtime-worker')
    evaluated = connection_budget.evaluate(budget)
    # The lane's matrix (run_on_lane.sh: LOAD_MATRIX, default v1).
    matrix = json.loads((ROOT / os.environ.get('LOAD_MATRIX', 'deploy/load/matrix.v1.json')).read_text())
    gates = {c['cell']: c['host_concurrency'] for c in matrix['cells'] if c['role'] == 'gate'}
    stack = (ROOT / 'scripts/load/stack.py').read_text()
    chain = {'guard_workers_4': "'--guard-workers', '4'" in stack,
             'dispatch_loops_per_host_slot': "'--dispatch-loops', str(host_concurrency)" in stack,
             'host_limit_per_cell': "'maximum_active_runs': host_concurrency" in stack,
             'one_host_all_runs': "'deterministic_protocols': True" in stack}
    details = {'runtime_worker': {k: runtime[k] for k in ('guard_workers', 'dispatch_loops', 'dispatcher_pool_max', 'guard_pool_max')},
               'application_pool_total': evaluated['details']['application_pool_total'], 'gate_host_concurrency': gates, 'chain': chain,
               'model_latency': matrix.get('model_latency')}
    ok = (runtime['guard_workers'] == 4 and runtime['dispatch_loops'] == 4 and evaluated['passed'] and gates and all(v <= 4 for v in gates.values())
          and all(chain.values()) and matrix.get('model_latency', {}).get('distribution') == 'lognormal')
    return ok, details


def login_limit_untouched():
    """No load script or helper writes a login rate policy (the cells assert the template's recorded one at start)."""
    sources = [*(ROOT / 'scripts/load').glob('*.py'), *(ROOT / 'scripts/load').glob('*.sh'), ROOT / 'tests/load_template.py', ROOT / 'tests/load_chain.py']
    writers = [p.name for p in sources if re.search(r'(insert into|update)\s+control\.nexloop_browser_rate_policies', p.read_text())]
    return not writers, writers


def template_matches(template, seed, consumers, head):
    meta = json.loads((Path(template) / 'template.json').read_text())
    expected = synthetic.digest(synthetic.lines(seed, consumers))
    policy = meta.get('login_rate_policy') or {}
    details = {'seed': meta.get('seed'), 'consumers': meta.get('manifest_totals', {}).get('consumers'), 'digest_matches': meta.get('manifest_digest') == expected,
               'commit_matches': meta.get('commit') == head, 'login_rate_policy': policy, 'pgdata': (Path(template) / 'pgdata').is_dir()}
    ok = (details['seed'] == seed and details['consumers'] == consumers and details['digest_matches'] and details['commit_matches']
          and policy.get('active') is True and type(policy.get('maximum_attempts')) is int and details['pgdata'])
    return ok, details


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--template', help='an existing template directory to check against the seed (resume or reuse)')
    args = parser.parse_args(argv)
    seed, consumers = int(os.environ.get('LOAD_SEED', '31')), int(os.environ.get('LOAD_CONSUMERS', '1000'))
    checks, details = {}, {}
    checks['ci_lock_idle'], details['ci_lock'] = lock_idle(os.environ.get('NEXLOOP_CI_LOCK'))
    results = ROOT / '.ci-results/load'
    free = shutil.disk_usage(results if results.exists() else ROOT).free / 2 ** 30
    checks['disk_free'] = free >= float(os.environ.get('LOAD_MIN_FREE_GIB', '20'))
    memory = available_memory_gib()
    checks['memory_available'] = memory >= float(os.environ.get('LOAD_MIN_FREE_MEM_GIB', '8'))
    details['free_gib'], details['available_memory_gib'] = round(free, 1), round(memory, 1)
    head = _git('rev-parse', 'HEAD')
    checks['clean_checkout'] = _git('status', '--porcelain', '--untracked-files=no') == ''
    details['commit'] = head
    details['manifest'] = {'seed': seed, 'consumers': consumers, 'digest': synthetic.digest(synthetic.lines(seed, consumers))}
    checks['deployment_premises'], details['deployment'] = deployment_premises()
    checks['login_limit_untouched'], details['rate_policy_writers'] = login_limit_untouched()
    leftovers = subprocess.run(['pgrep', '-f', str(results)], capture_output=True, text=True).stdout.split()
    checks['no_leftover_processes'] = not leftovers
    details['leftover_processes'] = len(leftovers)
    # The driver's warm-up logs each customer in from its own loopback source (driver.source_address); without them every login
    # shares one rate-limit bucket and 1000 customers wait out Retry-After for about 50 minutes per cell.
    import socket
    sources = 0
    for address in ('127.0.0.3', '127.0.3.233'):
        try:
            with socket.socket() as probe:
                probe.bind((address, 0))
            sources += 1
        except OSError:
            pass
    checks['distinct_loopback_sources'] = sources == 2
    checks['safety_environment'] = (os.environ.get('REAL_DISPATCH_ENABLED', 'false') == 'false'
                                    and not any(k.startswith(('MODEL_', 'EMBEDDING_', 'DEEPSEEK_')) for k in os.environ))
    if args.template:
        checks['template_matches_seed'], details['template'] = template_matches(args.template, seed, consumers, head)
    passed = all(checks.values())
    print(json.dumps({'checks': checks, 'details': details, 'passed': passed}, indent=2, ensure_ascii=False))
    return 0 if passed else 2


if __name__ == '__main__':
    sys.exit(main())

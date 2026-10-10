"""NX-031 D7: the smallest workable PostgreSQL pool of every long-running service, measured (dispatcher ruling 3).

For each service and pool size (1, 2) the service's functional tests run with its pools forced to that size by
tests/pool_probe (selector: the service's own entry module, or its database role); a size is workable when the tests pass and
no probed pool timed out (requests_errors 0). Waits (requests_queued, requests_wait_ms) are reported. Writes JSON lines and a
summary to --out. Local: pytest -n <= 3, Pi cases serial (LINE-RULES)."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# service -> list of (selector, test files, serial?)
MATRIX = {
    'claim-extraction-scheduler': [('module:nexloop_eios.background_services', ['tests/test_background_services_pg.py'], False),
                                   ('role:nexloop_api', ['tests/test_claim_extraction_jobs_pg.py'], False)],
    'claim-extraction-worker': [('role:nexloop_domain_worker', ['tests/test_claim_extraction_jobs_pg.py'], False)],
    'claim-matcher': [('role:nexloop_domain_worker', ['tests/test_claim_matching_pg.py'], False)],
    'recall-indexer': [('role:nexloop_domain_worker', ['tests/test_recall_pg.py'], False)],
    'plan-reevaluator': [('role:nexloop_domain_worker', ['tests/test_plan_reevaluation_pg.py'], False)],
    'reply-guarantor': [('role:nexloop_domain_worker', ['tests/test_takeover_pg.py', 'tests/test_contact_refusal_pg.py'], False)],
    'commitment-keeper': [('role:nexloop_domain_worker', ['tests/test_commitments_pg.py'], False)],
    'commercial-recorder': [('role:nexloop_domain_worker', ['tests/test_commercial_intake_pg.py'], False)],
    'retention-keeper': [('role:nexloop_domain_worker', ['tests/test_retention_pg.py'], False)],
    'alert-evaluator': [('role:nexloop_domain_worker', ['tests/test_observability_pg.py'], False)],
    'background-services-cli': [('module:nexloop_eios.background_services', ['tests/test_background_services_pg.py', 'tests/test_connection_budget_live_pg.py'], False)],
    'message-relay': [('module:nexloop_eios.message_relay_cli', ['tests/test_message_relay.py', 'tests/test_takeover_relay_pg.py'], True)],
    'outbound-recorder': [('module:nexloop_eios.outbound_messages', ['tests/test_outbound_messages_pg.py'], True)],
    'api': [('module:nexloop_eios.http_api', ['tests/test_http_api.py', 'tests/test_workbench_ui_pg.py', 'tests/test_workbench_actions_http.py',
                                             'tests/test_takeover_relay_pg.py'], True)],
    'domain-worker': [('module:nexloop_eios.domain_worker', ['tests/test_domain_worker.py'], False)],
    'action-worker': [('module:nexloop_eios.effect_worker', ['tests/test_effect_worker_cli.py'], True)],
    'guard-child': [('module:nexloop_eios.runtime_guard_worker', ['tests/test_takeover_e2e_pg.py', 'tests/test_agent_host_run_limit.py'], True)],
}


def run(service, selector, files, serial, size, out):
    probe = out / f'{service}-{size}-{selector.split(":")[1].replace(".", "_")}.jsonl'
    env = {**os.environ, 'NEXLOOP_POOL_PROBE': f'{selector}={size}', 'NEXLOOP_POOL_PROBE_OUT': str(probe),
           'PYTHONPATH': os.pathsep.join([str(ROOT / 'tests/pool_probe'), str(ROOT / 'packages/eios-core/src'), str(ROOT / 'tests')])}
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:randomly', *(['-p', 'no:xdist'] if serial else ['-n', '3']), *files]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    tail = [line for line in result.stdout.splitlines() if ' passed' in line or ' failed' in line or line.startswith(('FAILED', 'ERROR'))]
    pools = [json.loads(line) for line in probe.read_text().splitlines()] if probe.exists() else []
    return {'service': service, 'selector': selector, 'size': size, 'files': files, 'exit': result.returncode, 'summary': tail[-6:],
            'seconds': round(time.monotonic() - started), 'probed_pools': len(pools),
            'requests_errors': sum(p['requests_errors'] for p in pools), 'requests_queued': sum(p['requests_queued'] for p in pools),
            'requests_wait_ms': sum(p['requests_wait_ms'] for p in pools)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--services', nargs='*')
    parser.add_argument('--sizes', nargs='*', type=int, default=[1, 2])
    a = parser.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    for service, entries in MATRIX.items():
        if a.services and service not in a.services:
            continue
        for selector, files, serial in entries:
            for size in a.sizes:
                record = run(service, selector, files, serial, size, a.out)
                with open(a.out / 'results.jsonl', 'a') as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                print(json.dumps(record, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

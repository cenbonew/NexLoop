"""NX-032 run sheet 3.4 §4.7 (gap G8): scripts/deploy/running_snapshot.py builds the deploy gate's running.json from the APP_HOST
Compose project's running containers (labels) and their process lists. Fed with a capture shaped like the docker CLI's output for
every service of deploy/stage/compose.app.example.yaml, the gate's process checks pass with the stage connection budget; a missing
guard child, a missing runtime worker, an unlabelled extra container or a stopped container change the verdict as they should."""
import json
import subprocess
import sys
from pathlib import Path

import yaml

from nexloop_eios import deploy_checks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import running_snapshot as R  # noqa: E402

BUDGET = json.loads((ROOT / 'deploy/stage/connection-budget.v1.json').read_text())
COMPOSE = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
GUARD = '/usr/local/bin/python -m nexloop_eios.runtime_guard_worker --database-url-file /run/secrets/runtime_worker_dsn'


def capture(guard_children=4, drop=(), extra=(), stopped=()):
    """Every Compose service as one running container with its labels; the runtime worker with its guard children."""
    containers, tops = [], {}
    for name, spec in COMPOSE['services'].items():
        if name in drop:
            continue
        labels = {R.PROJECT_LABEL: 'nexloop-app', R.SERVICE_LABEL: name, **{k: str(v) for k, v in (spec.get('labels') or {}).items()}}
        cid = 'c-' + name
        containers.append({'id': cid, 'state': 'exited' if name in stopped else 'running', 'labels': labels})
        tops[cid] = ['python -m nexloop_eios.' + name.replace('-', '_')] + ([GUARD] * guard_children if name == 'runtime-worker' else [])
    for name in extra:
        containers.append({'id': 'c-' + name, 'state': 'running', 'labels': {R.PROJECT_LABEL: 'nexloop-app', R.SERVICE_LABEL: name}})
        tops['c-' + name] = ['python -m something']
    # A container of another project is never counted.
    containers.append({'id': 'other', 'state': 'running', 'labels': {R.PROJECT_LABEL: 'unrelated', R.SERVICE_LABEL: 'api'}})
    return {'project': 'nexloop-app', 'containers': containers, 'tops': tops}


def gate(cap):
    return deploy_checks.guard_and_registration(BUDGET, R.snapshot(cap['containers'], cap['tops'], cap['project']))


def test_the_stage_compose_running_passes_the_gate_process_checks():
    value = R.snapshot(**{k: v for k, v in capture().items() if k != 'project'}, project='nexloop-app')
    assert value['services']['runtime-worker'] == {'pg': True, 'processes': 1, 'guard_children': 4}
    assert value['services']['api']['processes'] == 2 and value['services']['message-relay']['processes'] == 2
    assert value['services']['plan-reevaluator-launcher'] == {'pg': True, 'processes': 1}
    assert {s['name']: s['processes'] for s in BUDGET['services']} == {n: r['processes'] for n, r in value['services'].items()}
    out = gate(capture())
    assert out['checks']['running_processes_registered'] is True and out['checks']['running_guard_children_exact'] is True, out


def test_missing_processes_fail_the_gate():
    assert gate(capture(guard_children=3))['checks']['running_guard_children_exact'] is False
    assert gate(capture(drop=('runtime-worker',)))['checks']['running_guard_children_exact'] is False
    assert gate(capture(stopped=('runtime-worker',)))['checks']['running_guard_children_exact'] is False  # stopped = not running
    unregistered = gate(capture(extra=('mystery-worker',)))
    assert unregistered['checks']['running_processes_registered'] is False and unregistered['details']['process_shape']['unregistered'] == ['mystery-worker']


def test_docker_ps_label_strings_keep_the_budget_list():
    labels = R._labels(f'{R.PROJECT_LABEL}=nexloop-app,nexloop.budget-service=plan-reevaluator,plan-reevaluator-launcher,{R.SERVICE_LABEL}=plan-reevaluator')
    assert labels['nexloop.budget-service'] == 'plan-reevaluator,plan-reevaluator-launcher' and labels[R.SERVICE_LABEL] == 'plan-reevaluator'


def test_the_cli_writes_a_private_file_from_a_capture_and_never_overwrites(tmp_path):
    cap = tmp_path / 'capture.json'
    cap.write_text(json.dumps(capture()))
    out = tmp_path / 'running.json'
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy/running_snapshot.py'), '--capture', str(cap), '--out', str(out)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and json.loads(run.stdout) == {'ok': True, 'services': len(BUDGET['services']),
                                                         'processes': sum(s['processes'] for s in BUDGET['services']), 'guard_children': 4}
    assert out.stat().st_mode & 0o777 == 0o600 and json.loads(out.read_text())['schema'] == 'nexloop-running/1'
    again = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy/running_snapshot.py'), '--capture', str(cap), '--out', str(out)],
                           capture_output=True, text=True)
    assert again.returncode == 2 and 'output exists' in again.stdout

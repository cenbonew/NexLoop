"""Host health probe (scripts/deploy/host_health_probe.py) offline: docker and systemctl answers come from a scripted runner. Restart
counts of containers and looping services, one-shot units skipped, a timer's run classified scheduled or manual against its last
trigger, blindness against the configured list, configuration refusal, and a failed recording reported by type only."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
import host_health_probe as P  # noqa: E402

T0 = 1761100000  # a unix time standing for the timer's last trigger


def config(tmp_path, **over):
    value = {'schema': P.SCHEMA, 'host_role': 'data_host', 'recorder_dsn_file': str(tmp_path / 'dsn'), 'grace_seconds': 120,
             'containers': [{'name': 'postgres', 'container': 'stage-postgres-1'}],
             'services': [{'name': 'runtime-worker', 'unit': 'nexloop-runtime-worker.service'}, {'name': 'once', 'unit': 'nexloop-once.service'}],
             'timers': [{'name': 'pg-diff', 'timer': 'nexloop-pg-diff.timer', 'service': 'nexloop-pg-diff.service'}], **over}
    path = tmp_path / 'host-health.json'
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
    return str(path)


def runner(*, docker=None, services=None, timer=None, run=None):
    def answer(command):
        if command[0] == 'docker':
            return json.dumps(docker if docker is not None else [{'Name': '/stage-postgres-1', 'Id': 'a' * 64, 'RestartCount': 4, 'State': {'Running': True}}])
        if '--timestamp=unix' in command:
            t = timer if timer is not None else {'Id': 'nexloop-pg-diff.timer', 'LastTriggerUSec': f'@{T0}'}
            s = run if run is not None else {'Id': 'nexloop-pg-diff.service', 'ActiveState': 'inactive', 'Result': 'success',
                                             'InactiveExitTimestamp': f'@{T0 + 3}', 'StateChangeTimestamp': f'@{T0 + 200}'}
            return '\n\n'.join('\n'.join(f'{k}={v}' for k, v in b.items()) for b in (t, s) if b)
        blocks = services if services is not None else [
            {'Id': 'nexloop-runtime-worker.service', 'NRestarts': '7', 'ActiveState': 'active', 'Type': 'simple'},
            {'Id': 'nexloop-once.service', 'NRestarts': '0', 'ActiveState': 'inactive', 'Type': 'oneshot'}]
        return '\n\n'.join('\n'.join(f'{k}={v}' for k, v in b.items()) for b in blocks)
    return answer


def probe(path, run, *args, connect=None):
    out = io.StringIO()
    code = P.main(['--config', path, *args], runner=run, connect=connect, out=out)
    return code, json.loads(out.getvalue())


def test_restart_counts_of_containers_and_looping_services_one_shot_skipped(tmp_path):
    code, doc = probe(config(tmp_path), runner(), '--dry-run')
    assert code == 0 and doc['blind'] is False
    units = {u['name']: u for u in doc['entry']['units']}
    assert units['postgres'] == {'name': 'postgres', 'kind': 'container', 'instance_id': 'a' * 64, 'restart_count': 4, 'running': True}
    assert units['runtime-worker'] == {'name': 'runtime-worker', 'kind': 'service', 'instance_id': 'nexloop-runtime-worker.service',
                                       'restart_count': 7, 'running': True}
    assert 'once' not in units  # one-shot: no restart semantics
    assert doc['entry']['expected_units'] == ['postgres', 'runtime-worker', 'once']


def test_a_run_right_after_the_trigger_is_scheduled_a_later_one_is_manual(tmp_path):
    path = config(tmp_path)
    _, doc = probe(path, runner(), '--dry-run')
    assert doc['entry']['timers'] == [{'name': 'pg-diff', 'run_started_at': '2025-10-22T02:26:43+00:00', 'run_finished_at': '2025-10-22T02:30:00+00:00',
                                       'result': 'success', 'scheduled': True, 'last_trigger_at': '2025-10-22T02:26:40+00:00'}]
    manual = {'Id': 'nexloop-pg-diff.service', 'ActiveState': 'inactive', 'Result': 'success', 'InactiveExitTimestamp': f'@{T0 + 3600}',
              'StateChangeTimestamp': f'@{T0 + 3700}'}
    _, doc = probe(path, runner(run=manual), '--dry-run')
    assert doc['entry']['timers'][0]['scheduled'] is False
    failed = {**manual, 'ActiveState': 'failed', 'Result': 'exit-code', 'InactiveExitTimestamp': f'@{T0 + 1}'}
    _, doc = probe(path, runner(run=failed), '--dry-run')
    assert doc['entry']['timers'][0] | {'run_started_at': None, 'run_finished_at': None} == {
        'name': 'pg-diff', 'result': 'failure', 'scheduled': True, 'last_trigger_at': '2025-10-22T02:26:40+00:00', 'run_started_at': None, 'run_finished_at': None}
    # Running now, or never ran: seen, nothing to record.
    _, doc = probe(path, runner(run={**manual, 'ActiveState': 'activating'}), '--dry-run')
    assert doc['entry']['timers'] == [] and doc['blind'] is False


def test_seeing_less_than_half_of_what_is_expected_is_blind(tmp_path):
    path = config(tmp_path)
    _, doc = probe(path, lambda command: '', '--dry-run')  # docker and systemctl unavailable: nothing seen
    assert doc['blind'] is True and doc['units'] == 0
    _, doc = probe(path, runner(docker=[]), '--dry-run')  # the container is gone, the rest is seen
    assert doc['blind'] is False


def test_recording_and_its_failure(tmp_path):
    path = config(tmp_path)
    calls = []

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args):
            calls.append(json.loads(args[0]))
            return self

        def fetchone(self):
            return ({'recorded': True, 'new_runs': 1},)
    code, doc = probe(path, runner(), connect=Db)
    assert code == 0 and doc['recorded'] is True and doc['new_runs'] == 1 and calls[0]['host_role'] == 'data_host'

    def broken():
        raise OSError('connection to host=db.example.invalid password=hunter2 refused')
    code, doc = probe(path, runner(), connect=broken)
    assert code == 1 and doc == {**doc, 'recorded': False, 'error': 'OSError'} and 'hunter2' not in json.dumps(doc)


@pytest.mark.parametrize('over', [{'host_role': 'App Host'}, {'recorder_dsn_file': 'relative'}, {'grace_seconds': 1},
                                  {'containers': [{'name': 'x', 'container': 'a b'}]}, {'timers': [{'name': 'x', 'timer': 'a.service', 'service': 'b.service'}]},
                                  {'services': [{'name': 'dup', 'unit': 'a.service'}, {'name': 'dup', 'unit': 'b.service'}]}])
def test_configuration_is_refused(tmp_path, over):
    code, doc = probe(config(tmp_path, **over), runner())
    assert code == 2 and 'refused' in doc


def test_a_configuration_readable_by_others_is_refused(tmp_path):
    path = config(tmp_path)
    os.chmod(path, 0o644)
    code, doc = probe(path, runner())
    assert code == 2

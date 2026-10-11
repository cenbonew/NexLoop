"""NX-033 §4.1: scripts/ci/check split into stage scripts. scripts/ci/check stays the whole entry with the same behaviour and exit
status (the stage scripts in a fixed order; the deadline gate recorded and reported at the end); .ci-policy/stages.v2.json runs the
same scripts one stage at a time and v1 is unchanged. Every external command is a stand-in that records its argv, so nothing is
built or tested for real here."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import nexloop_ci as ci  # noqa: E402

STAGES_V2 = ['dependency-python', 'static', 'dependency-node', 'licenses', 'build-artifacts', 'unit-contract', 'serial-build',
             'deadline-gate', 'integration', 'pi-runtime']
# Stages v3 (D-j): what scripts/ci/check runs.
STAGES = ['dependency-python', 'static', 'dependency-node', 'licenses', 'build-artifacts', 'unit-contract', 'serial-build',
          'deadline-gate', 'integration-parallel', 'integration-heavy', 'pi-runtime']


def shell_list(name):
    """An array of scripts/ci/stages/lib.sh, as bash sees it."""
    out = subprocess.run(['bash', '-c', f'source scripts/ci/stages/lib.sh; printf "%s\\n" "${{{name}[@]}}"'], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for tool in ('uv', 'pnpm', 'node'):
        (bin_dir / tool).write_text(f'''#!/bin/sh
if [ -n "$NEXLOOP_TEST_GUARD_WORKERS" ]; then echo "GUARD=$NEXLOOP_TEST_GUARD_WORKERS {tool} $*" >> "$TRACE"; else echo "{tool} $*" >> "$TRACE"; fi
if [ "{tool}" = node ] && [ "$1" = -p ]; then echo 24; fi
n=$(grep -c . "$TRACE")
if [ -n "$FAIL_AT" ] && [ "$n" = "$FAIL_AT" ]; then exit "${{FAIL_RC:-3}}"; fi
exit 0
''')
        (bin_dir / tool).chmod(0o755)

    def run(argv, fail_at='', fail_rc=3, trace=None):
        trace = trace or tmp_path / f'trace-{os.urandom(4).hex()}'
        env = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'TRACE': str(trace), 'FAIL_AT': str(fail_at), 'FAIL_RC': str(fail_rc), 'HOME': str(tmp_path)}
        done = subprocess.run(['bash', *argv], cwd=ROOT, env=env, capture_output=True, text=True)
        lines = re.sub(r'/[^ \n]*/tmp\.[A-Za-z0-9]+', '<tmp>', trace.read_text()).splitlines() if trace.exists() else []
        return done.returncode, lines, done.stderr
    return run


def test_v2_is_unchanged_and_still_names_its_own_scripts():
    v2 = ci.load_policy(ROOT / '.ci-policy', 2)
    assert [s['name'] for s in v2['stages']['stages']] == STAGES_V2 + ['isolation-probe'] and v2['stages_version'] == 2
    assert all((ROOT / s['argv'][1]).is_file() for s in v2['stages']['stages'] if s['argv'][0] == 'bash')
    # integration.sh (v2) still runs every remaining test: nothing is deselected there.
    assert '--deselect' not in (ROOT / 'scripts/ci/stages/integration.sh').read_text()
    assert set(v2['suites']['suites']) == {Path(j).stem for s in v2['stages']['stages'] for j in s.get('junit', [])}


def test_v3_runs_the_stage_scripts_in_the_order_of_scripts_ci_check():
    v3 = ci.load_policy(ROOT / '.ci-policy', 3)
    names = [s['name'] for s in v3['stages']['stages']]
    assert names == STAGES + ['isolation-probe'] and v3['stages_version'] == 3 and v3['version'] == 2
    called = re.findall(r'"\$stages/([a-z-]+)\.sh"', (ROOT / 'scripts/ci/check').read_text())
    assert called == STAGES
    stages = {s['name']: s for s in v3['stages']['stages']}
    assert stages['deadline-gate']['timeout_seconds'] == 300  # D-j: the gate is at most 5 minutes
    v2 = v3
    assert {n for n, s in stages.items() if s['network'] == 'dependency'} == {'dependency-python', 'dependency-node'}
    assert all(s['env'] == {'UV_OFFLINE': '1', 'npm_config_offline': 'true'} for n, s in stages.items() if s['network'] == 'test' and n != 'isolation-probe')
    assert [n for n, s in stages.items() if s.get('stop_on_failure') is False] == ['deadline-gate']
    # Each stage declares exactly the JUnit files its script writes; together they are the required suites, names unchanged.
    for name in STAGES:
        written = sorted(re.findall(r'(\.ci-results/[a-z0-9_-]+\.xml)', (ROOT / f'scripts/ci/stages/{name}.sh').read_text()))
        assert sorted(set(written)) == sorted(stages[name].get('junit', [])), name
    assert stages['deadline-gate']['junit'] == ['.ci-results/python-deadline-gate.xml']
    suites = {Path(j).stem for s in stages.values() for j in s.get('junit', [])}
    assert suites == set(v2['suites']['suites']) and 'python-heavy' in suites
    (group,) = v3['suites']['count_groups']
    assert group['suites'] == ['python', 'python-heavy', 'python-deadline-gate'] and group['allowed_drop'] == 2  # the 2 double-counted gate cases
    assert 'two consecutive full runs' in v3['stages']['decision']  # the baseline rule is in the trusted policy


def test_the_gate_and_the_heavy_cases_run_in_their_own_stages_only(stubs):
    gate, heavy = shell_list('DEADLINE_GATE_TESTS'), shell_list('HEAVY_TESTS')
    assert len(gate) == 4 and sum('test_agent_host_run_limit.py' in t for t in gate) == 2 and len(heavy) == 19
    _, whole, _ = stubs(['scripts/ci/check'])
    gate_run = next(line for line in whole if 'python-deadline-gate.xml' in line and 'pytest' in line)
    assert gate_run.startswith('GUARD=4 uv run --frozen pytest -q -p no:xdist ') and all(t in gate_run for t in gate)  # 4 guard processes
    parallel = next(line for line in whole if line.endswith('--junitxml=.ci-results/python.xml'))
    assert all('--deselect=' + t in parallel for t in gate + heavy) and '-n 6' in parallel
    heavy_run = next(line for line in whole if line.endswith('--junitxml=.ci-results/python-heavy.xml'))
    assert '-n 2 ' in heavy_run and all(t in heavy_run for t in heavy) and not any(t in heavy_run for t in gate)
    # Every id names collected tests (a stale id would silently select nothing).
    collected = subprocess.run([sys.executable, '-m', 'pytest', '--co', '-q', '-p', 'no:cacheprovider', *gate, *heavy], cwd=ROOT, capture_output=True, text=True)
    assert collected.returncode == 0 and re.search(r'(\d+) tests? collected', collected.stdout).group(1) == '26', collected.stdout[-500:]


def test_moving_tests_between_grouped_suites_is_not_a_drop_but_losing_one_is():
    v3 = ci.load_policy(ROOT / '.ci-policy', 3)
    clean = lambda n: {'tests': n, 'passed': n, 'failed': 0, 'errors': 0, 'skipped': 0}
    others = {name: clean(5) for name in v3['suites']['suites'] if name not in ('python', 'python-heavy', 'python-deadline-gate')}
    baseline = {**others, 'python': clean(1000), 'python-deadline-gate': clean(2)}
    moved = {**others, 'python': clean(974), 'python-heavy': clean(22), 'python-deadline-gate': clean(4)}
    assert ci.judge(v3, [], moved, baseline) == []  # 1000 vs 1002 - 2 (the gate cases also counted in python under v1)
    lost = {**moved, 'python-heavy': clean(19)}
    assert ci.judge(v3, [], lost, baseline) == ['test_count_dropped:python+python-heavy+python-deadline-gate']
    # Ungrouped suites keep the per-suite rule; v1 has no groups (python alone may not drop).
    assert ci.judge(v3, [], {**moved, 'web': clean(4)}, baseline) == ['test_count_dropped:web']
    v1 = ci.load_policy(ROOT / '.ci-policy')
    assert 'test_count_dropped:python' in ci.judge(v1, [], {**others, 'python': clean(974), 'python-deadline-gate': clean(4)}, baseline)


def test_v1_is_unchanged_and_stays_the_default():
    v1 = ci.load_policy(ROOT / '.ci-policy')
    assert v1['stages_version'] == 1 and [s['name'] for s in v1['stages']['stages']] == ['dependency-python', 'dependency-node', 'suite', 'isolation-probe']
    with pytest.raises(ci.ConfigError):
        ci.load_policy(ROOT / '.ci-policy', 4)


def test_check_is_the_stage_scripts_in_sequence(stubs):
    code, whole, _ = stubs(['scripts/ci/check'])
    assert code == 0 and len(whole) == 36
    pieces = []
    for name in STAGES:
        rc, lines, _ = stubs([f'scripts/ci/stages/{name}.sh'])
        assert rc == 0, name
        pieces += lines
    assert whole == pieces


def test_a_failing_command_stops_check_with_its_status_except_the_deadline_gate(stubs):
    _, whole, _ = stubs(['scripts/ci/check'])
    gate = [i + 1 for i, line in enumerate(whole) if 'python-deadline-gate.xml' in line]
    # The Node version probe (`node -p`, inside a [[ ]] test) is not a failing command in either form; skip it.
    probe = next(i + 1 for i, line in enumerate(whole) if line.startswith('node -p'))
    for position in (1, 6, probe + 1, 20, len(whole) - 2, len(whole)):
        for rc in (1, 2, 5):
            code, lines, _ = stubs(['scripts/ci/check'], position, rc)
            assert code == rc and lines == whole[:position], (position, rc)
    for position in gate:
        code, lines, err = stubs(['scripts/ci/check'], position, 5)
        expected = 5 if 'pytest' in whole[position - 1] else 1
        assert code == 1 and lines == whole and f'NX-049 deadline gate failed (rc={expected})' in err
    code, lines, _ = stubs(['scripts/ci/stages/deadline-gate.sh'], 1, 4)
    assert code == 4 and len(lines) == 2  # standalone: the gate's own status, the report still checked

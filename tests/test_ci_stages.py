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
        return done.returncode, lines, done.stderr + done.stdout
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
    # scripts/ci/check lists its stages in two loops: preparation, then the test stages.
    called = [n for line in re.findall(r'^for name in ([a-z -]+); do', (ROOT / 'scripts/ci/check').read_text(), re.M) for n in line.split()]
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
    assert len(gate) == 4 and sum('test_agent_host_run_limit.py' in t for t in gate) == 2 and len(heavy) == 32
    _, whole, _ = stubs(['scripts/ci/check'])
    gate_run = next(line for line in whole if 'python-deadline-gate.xml' in line and 'pytest' in line)
    assert gate_run.startswith('GUARD=4 uv run --frozen pytest -q -p no:xdist ') and all(t in gate_run for t in gate)  # 4 guard processes
    parallel = next(line for line in whole if line.endswith('--junitxml=.ci-results/python.xml'))
    assert all('--deselect=' + t in parallel for t in gate + heavy) and '-n 6' in parallel
    heavy_run = next(line for line in whole if line.endswith('--junitxml=.ci-results/python-heavy.xml'))
    assert '-n 2 ' in heavy_run and all(t in heavy_run for t in heavy) and not any(t in heavy_run for t in gate)
    # Every id names collected tests (a stale id would silently select nothing).
    collected = subprocess.run([sys.executable, '-m', 'pytest', '--co', '-q', '-p', 'no:cacheprovider', *gate, *heavy], cwd=ROOT, capture_output=True, text=True)
    assert collected.returncode == 0 and re.search(r'(\d+) tests? collected', collected.stdout).group(1) == '48', collected.stdout[-500:]


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
        ci.load_policy(ROOT / '.ci-policy', 5)


def test_check_is_the_stage_scripts_in_sequence(stubs):
    code, whole, _ = stubs(['scripts/ci/check'])
    assert code == 0 and len(whole) == 36
    pieces = []
    for name in STAGES:
        rc, lines, _ = stubs([f'scripts/ci/stages/{name}.sh'])
        assert rc == 0, name
        pieces += lines
    assert whole == pieces


PREPARATION = ['dependency-python', 'static', 'dependency-node', 'licenses', 'build-artifacts']
TEST_STAGES = ['unit-contract', 'serial-build', 'deadline-gate', 'integration-parallel', 'integration-heavy', 'pi-runtime']
STAGE_LINE = re.compile(r'NEXLOOP-STAGE name=([a-z-]+) rc=(\d+) seconds=(\d+) junit=(\S+)')


def spans(stubs):
    """(first, last) command positions (1-based) of every stage in the whole check trace."""
    out, at = {}, 0
    for name in STAGES:
        _, lines, _ = stubs([f'scripts/ci/stages/{name}.sh'])
        out[name] = (at + 1, at + len(lines))
        at += len(lines)
    return out


def test_v4_runs_every_test_stage_to_the_end_and_judges_per_stage():
    v4 = ci.load_policy(ROOT / '.ci-policy', 4)
    stages = {s['name']: s for s in v4['stages']['stages']}
    assert [s['name'] for s in v4['stages']['stages']] == STAGES + ['isolation-probe'] and v4['suites']['version'] == 2
    assert {n for n, s in stages.items() if s.get('stop_on_failure') is False} == set(TEST_STAGES)
    assert all(stages[n].get('stop_on_failure', True) for n in PREPARATION)
    # Same stages, scripts and JUnit as v3; v3 itself is unchanged.
    v3 = ci.load_policy(ROOT / '.ci-policy', 3)
    assert [{k: v for k, v in s.items() if k != 'stop_on_failure'} for s in v4['stages']['stages']] == \
        [{k: v for k, v in s.items() if k != 'stop_on_failure'} for s in v3['stages']['stages']]
    assert [n for n, s in {s['name']: s for s in v3['stages']['stages']}.items() if s.get('stop_on_failure') is False] == ['deadline-gate']
    # The judgement is per stage and per suite: a failed or skipped test in any suite fails, whatever ran before or after it.
    clean = lambda n: {'tests': n, 'passed': n, 'failed': 0, 'errors': 0, 'skipped': 0}
    junit = {name: clean(5) for name in v4['suites']['suites']}
    ran = [{'name': n, 'status': 'passed'} for n in STAGES]
    assert ci.judge(v4, ran, junit, None) == []
    assert ci.judge(v4, ran, {**junit, 'pi': {**clean(5), 'skipped': 1}}, None) == ['suite_not_clean:pi']
    failed_parallel = [{**s, 'status': 'failed'} if s['name'] == 'integration-parallel' else s for s in ran]
    assert ci.judge(v4, failed_parallel, {**junit, 'python': {**clean(5), 'failed': 2}}, None) == ['stage_failed:integration-parallel', 'suite_not_clean:python']
    assert 'suite_missing:runtime' in ci.judge(v4, ran, {k: v for k, v in junit.items() if k != 'runtime'}, None)


def test_preparation_stops_at_its_first_failure_and_test_stages_all_run(stubs):
    _, whole, _ = stubs(['scripts/ci/check'])
    span = spans(stubs)
    # The Node version probe (`node -p`, inside a [[ ]] test) is not a failing command; skip it.
    probe = next(i + 1 for i, line in enumerate(whole) if line.startswith('node -p'))
    for position in (1, 6, probe + 1, span['build-artifacts'][1]):
        for rc in (1, 2, 5):
            code, lines, _ = stubs(['scripts/ci/check'], position, rc)
            assert code == rc and lines == whole[:position], (position, rc)
    for name in ('unit-contract', 'serial-build', 'integration-parallel', 'integration-heavy', 'pi-runtime'):
        first, last = span[name]
        for position in (first, last):
            code, lines, out = stubs(['scripts/ci/check'], position, 5)
            # The failing stage stops inside itself; every later stage still runs; the status is that stage's.
            assert code == 5 and lines == whole[:position] + whole[last:], (name, position)
            assert f'NEXLOOP-STAGE name={name} rc=5 ' in out


def test_a_parallel_failure_still_runs_heavy_and_pi_runtime_and_returns_the_parallel_status(stubs):
    span = spans(stubs)
    code, lines, out = stubs(['scripts/ci/check'], span['integration-parallel'][0], 3)
    assert code == 3
    assert any(line.endswith('--junitxml=.ci-results/python-heavy.xml') for line in lines)
    assert any('.ci-results/pi.xml' in line for line in lines) and any('.ci-results/runtime.xml' in line for line in lines)
    results = {m.group(1): int(m.group(2)) for m in STAGE_LINE.finditer(out)}
    assert results['integration-parallel'] == 3 and results['integration-heavy'] == 0 and results['pi-runtime'] == 0
    # A later failure does not replace the first one's status.
    both = stubs(['scripts/ci/check'], span['integration-parallel'][0], 3)
    assert both[0] == 3


def test_the_gate_is_reported_on_its_own_and_counts_as_status_one(stubs):
    _, whole, _ = stubs(['scripts/ci/check'])
    gate = [i + 1 for i, line in enumerate(whole) if 'python-deadline-gate.xml' in line]
    for position in gate:
        code, lines, out = stubs(['scripts/ci/check'], position, 5)
        expected = 5 if 'pytest' in whole[position - 1] else 1
        assert code == 1 and lines == whole and f'NX-049 deadline gate failed (rc={expected})' in out
    code, lines, _ = stubs(['scripts/ci/stages/deadline-gate.sh'], 1, 4)
    assert code == 4 and len(lines) == 2  # standalone: the gate's own status, the report still checked


def test_every_stage_prints_one_result_line_with_its_junit(stubs):
    code, _, out = stubs(['scripts/ci/check'])
    found = [(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)) for m in STAGE_LINE.finditer(out)]
    assert code == 0 and [f[0] for f in found] == STAGES and all(f[1] == 0 and f[2] >= 0 for f in found)
    declared = {s['name']: ','.join(s.get('junit', [])) or '-' for s in ci.load_policy(ROOT / '.ci-policy', 4)['stages']['stages']}
    assert {f[0]: f[3] for f in found} == {n: declared[n] for n in STAGES}

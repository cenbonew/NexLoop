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

STAGES = ['dependency-python', 'static', 'dependency-node', 'licenses', 'build-artifacts', 'unit-contract', 'serial-build',
          'deadline-gate', 'integration', 'pi-runtime']


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for tool in ('uv', 'pnpm', 'node'):
        (bin_dir / tool).write_text(f'''#!/bin/sh
echo "{tool} $*" >> "$TRACE"
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


def test_v2_runs_the_stage_scripts_in_the_order_of_scripts_ci_check():
    v2 = ci.load_policy(ROOT / '.ci-policy', 2)
    names = [s['name'] for s in v2['stages']['stages']]
    assert names == STAGES + ['isolation-probe'] and v2['stages_version'] == 2
    called = re.findall(r'"\$stages/([a-z-]+)\.sh"', (ROOT / 'scripts/ci/check').read_text())
    assert called == STAGES
    stages = {s['name']: s for s in v2['stages']['stages']}
    assert {n for n, s in stages.items() if s['network'] == 'dependency'} == {'dependency-python', 'dependency-node'}
    assert all(s['env'] == {'UV_OFFLINE': '1', 'npm_config_offline': 'true'} for n, s in stages.items() if s['network'] == 'test' and n != 'isolation-probe')
    assert [n for n, s in stages.items() if s.get('stop_on_failure') is False] == ['deadline-gate']
    # Each stage declares exactly the JUnit files its script writes; together they are the required suites, names unchanged.
    for name in STAGES:
        written = sorted(re.findall(r'(\.ci-results/[a-z0-9_-]+\.xml)', (ROOT / f'scripts/ci/stages/{name}.sh').read_text()))
        assert sorted(set(written)) == sorted(stages[name].get('junit', [])), name
    assert stages['deadline-gate']['junit'] == ['.ci-results/python-deadline-gate.xml']
    suites = {Path(j).stem for s in stages.values() for j in s.get('junit', [])}
    assert suites == set(v2['suites']['suites'])


def test_v1_is_unchanged_and_stays_the_default():
    v1 = ci.load_policy(ROOT / '.ci-policy')
    assert v1['stages_version'] == 1 and [s['name'] for s in v1['stages']['stages']] == ['dependency-python', 'dependency-node', 'suite', 'isolation-probe']
    with pytest.raises(ci.ConfigError):
        ci.load_policy(ROOT / '.ci-policy', 3)


def test_check_is_the_stage_scripts_in_sequence(stubs):
    code, whole, _ = stubs(['scripts/ci/check'])
    assert code == 0 and len(whole) == 34
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

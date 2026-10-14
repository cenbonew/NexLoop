"""NX-034 release CI on the existing CI lane (docs/implementation/NX-034-release-ci-queue.md), simulated on temporary directories.

The runbook's commands, in order, through the real CLIs: a bare repository without NX-033 hooks receives the candidate as
refs/ci/<sha>; the lane identity points an integration ref at it (update-ref, no push); the lane policy instance is derived
from the trusted policy (scripts/ci/lane_policy.py: without isolation-probe, with the lane's stage env); enqueue, run-once,
result; release.py build_manifest accepts the result file. The candidate is this repository's HEAD so that build_manifest
can resolve it; the trusted policy here is synthetic (stage commands write the required JUnit; the v4-shaped isolation-probe
stage is the real one's argv). A policy that keeps isolation-probe fails off the runner and the release refuses it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
import release  # noqa: E402
from test_release_local import DIGEST_API, oci_archive  # noqa: E402

CI = ROOT / 'scripts/ci/nexloop_ci.py'
LANE = ROOT / 'scripts/ci/lane_policy.py'
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
# Writes the two required suites; the deadline gate suite only when the lane env reached the stage.
STAGE = ('mkdir -p .ci-results && printf \'<testsuite><testcase name="a"/><testcase name="b"/></testsuite>\' > .ci-results/unit.xml && '
         'test "$NEXLOOP_LANE_MARK" = lane && printf \'<testsuite><testcase name="gate"/></testsuite>\' > .ci-results/gate.xml')


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check, env={**os.environ, **GIT_ENV})


def cli(script, *args):
    run = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    return run.returncode, json.loads(run.stdout)


@pytest.fixture
def lane(tmp_path):
    sha = sh('git', '-C', str(ROOT), 'rev-parse', 'HEAD').stdout.strip()
    bare, queue = tmp_path / 'git' / 'nexloop.git', tmp_path / 'ci' / 'queue'
    sh('git', 'init', '-q', '--bare', str(bare))
    # What the dispatcher does: push the candidate commit to refs/ci/<sha> (no NX-033 hooks on this repository). The target is
    # a temporary local directory, so this checkout's own pre-push publication hook does not apply (--no-verify).
    sh('git', '-C', str(ROOT), 'push', '-q', '--no-verify', str(bare), f'{sha}:refs/ci/{sha}')
    trusted = tmp_path / 'trusted' / '.ci-policy'
    trusted.mkdir(parents=True)
    (trusted / 'stages.v4.json').write_text(json.dumps({'schema': 'nexloop-ci-stages/1', 'version': 4, 'decision': 'synthetic', 'stages': [
        {'name': 'suites', 'network': 'test', 'argv': ['sh', '-c', STAGE], 'junit': ['.ci-results/unit.xml', '.ci-results/gate.xml'], 'stop_on_failure': False},
        {'name': 'isolation-probe', 'network': 'none', 'argv': ['python3', '/trusted/nexloop_ci.py', 'isolation-probe', '--targets', '/trusted/probe-targets.json'],
         'timeout_seconds': 120}]}))
    (trusted / 'required-suites.v2.json').write_text(json.dumps({'schema': 'nexloop-ci-required-suites/1', 'version': 2, 'deadline_gate_suite': 'gate',
        'suites': {'unit': {'min_tests': 2}, 'gate': {'min_tests': 1}}}))
    identities = tmp_path / 'private' / 'identities.json'
    identities.parent.mkdir()
    identities.write_text(json.dumps({'identities': {'release-ci': 'release'}}))
    env = tmp_path / 'private' / 'lane.env'
    env.write_text('# non-secret settings the lane stages need\nNEXLOOP_LANE_MARK=lane\nLC_ALL=C.UTF-8\n')
    return dict(sha=sha, bare=bare, queue=queue, trusted=trusted, identities=identities, env=env, tmp=tmp_path)


def run_lane(lane, policy, tag):
    sha, bare, queue = lane['sha'], str(lane['bare']), str(lane['queue'])
    sh('git', '-C', bare, 'update-ref', f'refs/heads/integration/release-{tag}', sha)
    code, out = cli(CI, 'enqueue', '--repo', bare, '--queue', queue, '--ref', f'refs/heads/integration/release-{tag}', '--sha', sha,
                    '--identity', 'release-ci', '--identities', str(lane['identities']))
    assert code == 0 and out['enqueued'] and out['sha'] == sha
    code, ran = cli(CI, 'run-once', '--repo', bare, '--queue', queue, '--policy', str(policy), '--identities', str(lane['identities']), '--stages-version', '4')
    code_r, result = cli(CI, 'result', '--queue', queue, '--sha', sha)
    return code, ran, code_r, result


def images(tmp):
    spec = tmp / 'images.json'
    spec.write_text(json.dumps({'nexloop-api': {'archive': str(oci_archive(tmp / 'api.tar', DIGEST_API)), 'digest': DIGEST_API}}))
    return spec


def test_the_lane_produces_a_passed_queue_result_that_the_release_manifest_accepts(lane):
    policy = lane['tmp'] / 'ci' / 'lane-policy'
    code, derived = cli(LANE, '--source', str(lane['trusted']), '--stages-version', '4', '--without', 'isolation-probe',
                        '--stage-env', str(lane['env']), '--out', str(policy))
    assert code == 0 and derived['stages'] == ['suites'] and derived['env_names'] == ['LC_ALL', 'NEXLOOP_LANE_MARK']
    code, ran, code_r, result = run_lane(lane, policy, 'r1')
    assert code == 0 and ran['ran'] and ran['outcome']['status'] == 'passed', ran
    assert code_r == 0 and result['status'] == 'passed' and result['sha'] == lane['sha']
    assert result['schema'] == 'nexloop-ci-result/1' and result['policy_version'] == 2 and result['deadline_gate'] == 'passed'
    assert result['counts'] == {'tests': 3, 'passed': 3, 'failed': 0, 'errors': 0, 'skipped': 0} and result['isolation'] is None
    path = lane['queue'] / 'results' / lane['sha'] / 'attempt-1' / 'result.json'
    assert json.loads(path.read_text()) == result
    manifest = release.build_manifest(sha=lane['sha'], images=images(lane['tmp']), ci_result=path, out=lane['tmp'] / 'release')
    assert manifest['commit'] == lane['sha'] and manifest['ci_policy_version'] == 2 and manifest['deadline_gate'] == 'passed'
    assert manifest['ci_result_digest'] == release.sha256_file(path) and manifest['tests'] == result['counts']


def test_the_trusted_policy_as_is_fails_off_the_runner_and_the_release_refuses_it(lane):
    code, ran, code_r, result = run_lane(lane, lane['trusted'], 'r2')
    assert code == 1 and code_r == 1 and result['status'] == 'failed' and 'stage_failed:isolation-probe' in result['reasons']
    path = lane['queue'] / 'results' / lane['sha'] / 'attempt-1' / 'result.json'
    with pytest.raises(release.Refused):
        release.build_manifest(sha=lane['sha'], images=images(lane['tmp']), ci_result=path, out=lane['tmp'] / 'release')


@pytest.mark.parametrize('line, reason', [('DATABASE_URL=x', 'secret'), ('MODEL_API_KEY=x', 'secret'), ('PATH=/x', 'controller'),
                                          ('lower=x', 'KEY=VALUE'), ('NO_EQUALS', 'KEY=VALUE')])
def test_the_lane_policy_refuses_secret_or_controller_environment(lane, line, reason):
    lane['env'].write_text(line + '\n')
    code, out = cli(LANE, '--source', str(lane['trusted']), '--stages-version', '4', '--stage-env', str(lane['env']), '--out', str(lane['tmp'] / 'p'))
    assert code == 2 and not out['written'] and reason in out['detail'] and not (lane['tmp'] / 'p').exists()


def test_the_lane_policy_refuses_an_unknown_stage_or_an_existing_output(lane):
    code, out = cli(LANE, '--source', str(lane['trusted']), '--stages-version', '4', '--without', 'no-such-stage', '--out', str(lane['tmp'] / 'p'))
    assert code == 2 and 'unknown stage' in out['detail']
    (lane['tmp'] / 'q').mkdir()
    code, out = cli(LANE, '--source', str(lane['trusted']), '--stages-version', '4', '--out', str(lane['tmp'] / 'q'))
    assert code == 2 and 'exists' in out['detail']


def test_the_repository_trusted_v4_policy_derives_to_the_stages_of_scripts_ci_check():
    """The lane instance of the real .ci-policy v4 runs exactly the stages scripts/ci/check runs, in its order."""
    import re
    import tempfile
    check = (ROOT / 'scripts/ci/check').read_text()
    order = [n for group in re.findall(r'for name in ([a-z -]+); do', check) for n in group.split()]
    with tempfile.TemporaryDirectory() as tmp:
        code, out = cli(LANE, '--source', str(ROOT / '.ci-policy'), '--stages-version', '4', '--without', 'isolation-probe', '--out', str(Path(tmp) / 'p'))
    assert code == 0 and out['stages'] == order

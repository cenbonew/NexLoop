"""NX-033 local part: ref rules through real hooks, the immutable-SHA queue, the trusted judgement, merge by the controller only,
the CI result as NX-034's input, and GitHub sync as pending when unreachable (AT-053 local evidence).

Everything runs on temporary bare repositories and queue directories; identities and policies are synthetic."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import nexloop_ci as ci  # noqa: E402

IDENTITIES = {'identities': {'line-a': 'developer', 'line-b': 'developer', 'integrator-a': 'integrator', 'maintainer-a': 'maintainer',
                             'merge-controller': 'controller', 'release-tool': 'release'}}
GIT_ENV = {'GIT_AUTHOR_NAME': 'Synthetic', 'GIT_AUTHOR_EMAIL': 'synthetic@example.invalid', 'GIT_COMMITTER_NAME': 'Synthetic',
           'GIT_COMMITTER_EMAIL': 'synthetic@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
# The candidate's own test script: writes one junit file with N passing tests (N from tests.count), or a failure/skip.
CI_TEST = """#!/bin/sh
set -e
test "$GIT_ALLOW_PROTOCOL" = file
mkdir -p .ci-results
n=$(cat tests.count)
{ echo '<testsuite>'; i=0; while [ $i -lt $n ]; do echo "<testcase name=\\"t$i\\"/>"; i=$((i+1)); done
  if [ -f fail.flag ]; then echo '<testcase name="bad"><failure/></testcase>'; fi
  if [ -f skip.flag ]; then echo '<testcase name="skipped"><skipped/></testcase>'; fi
  echo '</testsuite>'; } > .ci-results/unit.xml
"""


def run(*args, cwd=None, env=None, check=True, input=None):
    return subprocess.run(args, cwd=cwd, env={**os.environ, **GIT_ENV, **(env or {})}, capture_output=True, text=True, check=check, input=input)


@pytest.fixture
def lab(tmp_path):
    bare, work, queue = tmp_path / 'nexloop.git', tmp_path / 'work', tmp_path / 'queue'
    run('git', 'init', '-q', '--bare', '-b', 'main', str(bare))
    run('git', 'init', '-q', '-b', 'main', str(work))
    identities = tmp_path / 'identities.json'
    identities.write_text(json.dumps(IDENTITIES))
    policy = tmp_path / 'trusted-policy'
    policy.mkdir()
    (policy / 'stages.v1.json').write_text(json.dumps({'schema': 'nexloop-ci-stages/1', 'version': 1, 'stages': [
        {'name': 'suite', 'network': 'test', 'argv': ['sh', 'ci-test.sh'], 'junit': ['.ci-results/*.xml']}]}))
    (policy / 'required-suites.v1.json').write_text(json.dumps({'schema': 'nexloop-ci-required-suites/1', 'version': 7,
        'deadline_gate_suite': 'unit', 'suites': {'unit': {'min_tests': 1, 'allowed_drop': 0}}}))
    hooks = bare / 'hooks'
    for name in ('pre-receive', 'post-receive'):
        extra = f'--identities {identities}' if name == 'pre-receive' else f'--queue {queue}'
        (hooks / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/scripts/ci/nexloop_ci.py" {name} --repo "$PWD" {extra}\n')
        (hooks / name).chmod(0o755)
    run('git', 'remote', 'add', 'origin', str(bare), cwd=work)

    def commit(message, files=None, parent=None):
        if parent:
            run('git', 'checkout', '-q', '--detach', parent, cwd=work)
        for name, content in (files or {}).items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(content)
        run('git', 'add', '-A', cwd=work)
        run('git', 'commit', '-q', '--allow-empty', '-m', message, cwd=work)
        return run('git', 'rev-parse', 'HEAD', cwd=work).stdout.strip()

    def push(identity, refspec, force=False):
        return run('git', 'push', '-q', *(['--force'] if force else []), 'origin', refspec, cwd=work, env={'NEXLOOP_PUSH_IDENTITY': identity}, check=False)

    base = commit('base', {'ci-test.sh': CI_TEST, 'tests.count': '3\n'})
    assert push('merge-controller', f'{base}:refs/heads/main').returncode == 0
    return dict(bare=bare, work=work, queue=queue, identities=ci.load_identities(identities), policy=ci.load_policy(policy), commit=commit,
                push=push, base=base, tmp=tmp_path)


def pending(lab):
    return sorted(p.name for p in (lab['queue'] / 'pending').glob('*.json')) if (lab['queue'] / 'pending').exists() else []


def once(lab, **kw):
    return ci.run_once(lab['bare'], lab['queue'], lab['policy'], lab['identities'], workroot=lab['tmp'] / 'builds', **kw)


def test_ref_namespaces_are_enforced_by_the_real_hooks_and_public_pr_refs_never_queue(lab):
    c, push = lab['commit'], lab['push']
    sha = c('feature', parent=lab['base'])
    assert push('line-a', f'{sha}:refs/heads/incoming/line-a/topic').returncode == 0
    assert [p.split('-', 1)[1] for p in pending(lab)] == [f'{sha}.json']
    refused = {
        'other identity': push('line-a', f'{sha}:refs/heads/incoming/line-b/topic'),
        'developer to main': push('line-a', f'{sha}:refs/heads/main'),
        'developer integration': push('line-a', f'{sha}:refs/heads/integration/s9'),
        'pull ref': push('maintainer-a', f'{sha}:refs/pull/7/head'),
        'unknown identity': push('stranger', f'{sha}:refs/heads/incoming/stranger/x'),
        'tag by developer': push('line-a', f'{sha}:refs/tags/v0.1.0'),
    }
    for case, result in refused.items():
        assert result.returncode != 0, case
    # A maintainer's review copy of a public PR is accepted but never queued (D3: unreviewed code never runs here).
    before = pending(lab)
    assert push('maintainer-a', f'{sha}:refs/heads/review/github-pr-7').returncode == 0
    assert pending(lab) == before
    # Integration is queued; deleting it is refused; tags are created once by the release identity and never moved.
    assert push('integrator-a', f'{sha}:refs/heads/integration/s9').returncode == 0 and len(pending(lab)) == 2
    assert push('integrator-a', ':refs/heads/integration/s9').returncode != 0
    assert push('release-tool', f'{sha}:refs/tags/v0.1.0').returncode == 0
    other = c('other', parent=lab['base'])
    assert push('release-tool', f'{other}:refs/tags/v0.1.0', force=True).returncode != 0
    # main: the controller only, fast-forward only.
    assert push('merge-controller', f'{other}:refs/heads/main').returncode == 0
    assert push('merge-controller', f'{sha}:refs/heads/main', force=True).returncode != 0
    assert ci.check_update(lab['bare'], lab['identities'], 'line-a', ci.ZERO, sha, 'refs/heads/incoming/line-a/x') is None


def test_the_queued_sha_is_immutable_built_in_a_fresh_checkout_and_reused(lab):
    c, push = lab['commit'], lab['push']
    first = c('first', parent=lab['base'])
    push('line-a', f'{first}:refs/heads/incoming/line-a/topic')
    # The ref moves (non-fast-forward) after enqueue: the queued SHA is still the one built.
    second = c('second', {'tests.count': '4\n'}, parent=lab['base'])
    push('line-a', f'{second}:refs/heads/incoming/line-a/topic', force=True)
    out = once(lab)
    assert out['outcome']['sha'] == first and out['outcome']['status'] == 'passed', out
    r = out['outcome']
    assert r['counts'] == {'tests': 3, 'passed': 3, 'failed': 0, 'errors': 0, 'skipped': 0} and r['deadline_gate'] == 'passed'
    assert r['parents'] == [lab['base']] and r['policy_version'] == 7 and r['controller_version'] == ci.CONTROLLER_VERSION and r['identity'] == 'line-a'
    assert set(r) >= {'started_at', 'finished_at', 'load', 'stages', 'suites', 'policy_change', 'attempt'}
    assert not (lab['tmp'] / 'builds' / f'{first}-1').exists()  # the fresh checkout is removed
    assert once(lab)['outcome']['sha'] == second
    # The same SHA again: the passed result is reused, no second build.
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/incoming/line-a/topic', first, 'line-a')
    again = once(lab)['outcome']
    assert again['reused'] is True and again['attempt'] == 1
    assert sorted(p.name for p in (lab['queue'] / 'results' / first).iterdir()) == ['attempt-1']


def test_the_trusted_policy_judges_even_when_the_candidate_changes_ci_or_hides_tests(lab):
    c, push = lab['commit'], lab['push']
    # A clean main result is the baseline (3 tests).
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/base', lab['base'], 'integrator-a')
    assert once(lab)['outcome']['status'] == 'passed'
    # The candidate rewrites its own CI policy to require nothing and drops a test: the trusted policy still decides.
    sneaky = c('sneaky', {'tests.count': '2\n', '.ci-policy/required-suites.v1.json': '{"suites": {}}', 'scripts/ci/check': 'exit 0\n'}, parent=lab['base'])
    push('line-a', f'{sneaky}:refs/heads/incoming/line-a/sneaky')
    r = once(lab)['outcome']
    assert r['status'] == 'failed' and r['reasons'] == ['test_count_dropped:unit'] and r['policy_change'] is True
    # Failures and skips are never a pass, and a re-run is a new attempt that keeps the first failure.
    failing = c('failing', {'fail.flag': 'x'}, parent=lab['base'])
    push('line-a', f'{failing}:refs/heads/incoming/line-a/failing')
    assert once(lab)['outcome']['reasons'] == ['suite_not_clean:unit']
    skipping = c('skipping', {'skip.flag': 'x', 'tests.count': '3\n'}, parent=lab['base'])
    push('line-a', f'{skipping}:refs/heads/incoming/line-a/skipping')
    assert once(lab)['outcome']['reasons'] == ['suite_not_clean:unit']
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/incoming/line-a/failing', failing, 'line-a')
    retry = once(lab)['outcome']
    assert retry['attempt'] == 2 and retry['first_failure'] == {'attempt': 1, 'reasons': ['suite_not_clean:unit']}


def test_only_the_controller_merges_a_passed_sha_whose_first_parent_is_main(lab):
    c, push = lab['commit'], lab['push']
    cand = c('candidate', {'feature.txt': 'x'}, parent=lab['base'])
    push('integrator-a', f'{cand}:refs/heads/integration/s1')
    # A developer cannot request a merge; a merge before any result is refused.
    assert ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/s1', cand, 'line-a', 'merge', lab['identities'])['reason'] == 'merge_requires_integrator'
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/s1', cand, 'integrator-a', 'merge', lab['identities'])
    # Queue order: the check first, then the merge request.
    assert once(lab)['outcome']['status'] == 'passed'
    merged = once(lab)['outcome']
    assert merged['merged'] is True and run('git', '-C', str(lab['bare']), 'rev-parse', 'main').stdout.strip() == cand
    assert merged['previous_main'] == lab['base'] and merged['result']['policy_version'] == 7
    # Built on an old main: refused, the integrator must rebuild on the new main.
    stale = c('stale', {'other.txt': 'y'}, parent=lab['base'])
    push('integrator-a', f'{stale}:refs/heads/integration/s2')
    assert once(lab)['outcome']['status'] == 'passed'
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/s2', stale, 'integrator-a', 'merge', lab['identities'])
    assert once(lab)['outcome']['reasons'] == ['first_parent_is_not_main']
    # A policy change needs a maintainer's acknowledgement first.
    policy = c('policy', {'deploy/stage/x.json': '{}'}, parent=cand)
    push('integrator-a', f'{policy}:refs/heads/integration/s3')
    assert once(lab)['outcome']['policy_change'] is True
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/s3', policy, 'integrator-a', 'merge', lab['identities'])
    assert once(lab)['outcome']['reasons'] == ['policy_change_not_acknowledged']
    assert ci.ack_policy(lab['queue'], policy, 'integrator-a', lab['identities'])['acknowledged'] is False
    assert ci.ack_policy(lab['queue'], policy, 'maintainer-a', lab['identities'])['acknowledged'] is True
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/s3', policy, 'integrator-a', 'merge', lab['identities'])
    done = once(lab)['outcome']
    assert done['merged'] is True and done['policy_change_ack']['maintainer'] == 'maintainer-a'
    # A failing merge gate (e.g. the prepush scan) refuses.
    nxt = c('next', {'n.txt': 'z'}, parent=policy)
    push('integrator-a', f'{nxt}:refs/heads/integration/s4')
    once(lab)
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/s4', nxt, 'integrator-a', 'merge', lab['identities'])
    assert once(lab, gates=[['false']])['outcome']['reasons'] == ['merge_gate_failed:false']


def test_a_dependency_cache_miss_fails_explicitly(lab):
    stages = {'schema': 'nexloop-ci-stages/1', 'version': 1, 'stages': [{'name': 'dependency-python', 'network': 'dependency', 'argv': ['false']},
              {'name': 'suite', 'network': 'test', 'argv': ['sh', 'ci-test.sh'], 'junit': ['.ci-results/*.xml']}]}
    policy = {**lab['policy'], 'stages': stages}
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/dep', lab['base'], 'integrator-a')
    r = ci.run_once(lab['bare'], lab['queue'], policy, lab['identities'], workroot=lab['tmp'] / 'builds')['outcome']
    assert r['status'] == 'failed' and r['stages'][0]['status'] == 'dependency_unavailable' and len(r['stages']) == 1
    assert 'stage_failed:dependency-python' in r['reasons'] and 'suite_missing:unit' in r['reasons']  # never a skipped pass


def test_the_ci_result_is_accepted_by_the_release_manifest(lab, tmp_path):
    """Contract with NX-034: release.py manifest accepts the controller's result.json for exactly this commit."""
    sys.path.insert(0, str(ROOT / 'scripts/release'))
    import release
    from test_release_local import oci_archive
    # The release reads the commit's own lock file (base image digests); the synthetic commit carries a copy.
    sha = lab['commit']('release candidate', {'versions.lock.json': (ROOT / 'versions.lock.json').read_text()}, parent=lab['base'])
    lab['push']('integrator-a', f'{sha}:refs/heads/integration/rel')
    assert once(lab)['outcome']['status'] == 'passed'
    result = next((lab['queue'] / 'results' / sha).glob('attempt-*/result.json'))
    digest = 'sha256:' + 'a' * 64
    archive = oci_archive(tmp_path / 'api.tar', digest)
    images = tmp_path / 'images.json'
    images.write_text(json.dumps({'nexloop-api': {'archive': str(archive), 'digest': digest}}))
    manifest = release.build_manifest(sha=sha, images=images, ci_result=result, out=tmp_path / 'rel', root=lab['work'])
    assert manifest['commit'] == sha and manifest['ci_policy_version'] == 7 and manifest['deadline_gate'] == 'passed'
    assert manifest['tests'] == {'tests': 3, 'passed': 3, 'failed': 0, 'errors': 0, 'skipped': 0}
    # Another commit's result, or a failed one, is refused.
    with pytest.raises(release.Refused):
        release.build_manifest(sha=lab['commit']('x', parent=sha), images=images, ci_result=result, out=tmp_path / 'rel2', root=lab['work'])


def test_at053_github_unreachable_leaves_ci_and_merge_working_and_sync_pending(lab, tmp_path):
    c, push = lab['commit'], lab['push']
    github = tmp_path / 'github-mirror.git'
    run('git', 'init', '-q', '--bare', '-b', 'main', str(github))
    # The sync remote is unreachable (no route: the URL leads nowhere).
    run('git', '-C', str(lab['bare']), 'remote', 'add', 'github', str(tmp_path / 'unreachable' / 'nexloop.git'))
    state = ci.sync(lab['bare'], lab['queue'], 'github', timeout=10)
    assert state['status'] == 'sync_pending' and state['backlog'] == 1
    # Meanwhile local CI and merges proceed from the local repository only.
    cand = c('during outage', {'feature.txt': 'x'}, parent=lab['base'])
    push('integrator-a', f'{cand}:refs/heads/integration/outage')
    assert once(lab)['outcome']['status'] == 'passed'
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/outage', cand, 'integrator-a', 'merge', lab['identities'])
    assert once(lab)['outcome']['merged'] is True
    state = ci.sync(lab['bare'], lab['queue'], 'github', timeout=10)
    assert state['status'] == 'sync_pending' and state['backlog'] == 2 and state['failures'] == 2
    assert ci.status(lab['queue'])['sync'] == {'github': {'status': 'sync_pending', 'backlog': 2}}
    # Reachable again: one fast-forward push, the backlog clears.
    run('git', '-C', str(lab['bare']), 'remote', 'set-url', 'github', str(github))
    state = ci.sync(lab['bare'], lab['queue'], 'github', timeout=10)
    assert state['status'] == 'synced' and state['backlog'] == 0 and state['last_synced'] == cand
    assert run('git', '-C', str(github), 'rev-parse', 'main').stdout.strip() == cand


def test_cli_status_and_result(lab, capsys):
    ci.enqueue(lab['bare'], lab['queue'], 'refs/heads/integration/cli', lab['base'], 'integrator-a')
    assert ci.main(['status', '--queue', str(lab['queue'])]) == 0
    assert json.loads(capsys.readouterr().out)['pending'] == [lab['base']]
    assert ci.main(['result', '--queue', str(lab['queue']), '--sha', lab['base']]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'not_available'
    once(lab)
    assert ci.main(['result', '--queue', str(lab['queue']), '--sha', lab['base']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'passed'


# A candidate whose suite fails with a message carrying credential-looking text, and errors in another case.
CI_TEST_FAILING = """#!/bin/sh
set -e
mkdir -p .ci-results
cat > .ci-results/unit.xml <<'XML'
<testsuite>
 <testcase classname="tests.test_ok" name="t_ok"/>
 <testcase classname="tests.test_pay" name="test_settles_once"><failure message="assert 1 == 2 while connecting postgresql://svc:hunter2@db/x password=hunter2 token: abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP0123">Traceback line one
line two with the raw log</failure></testcase>
 <testcase classname="tests.test_pay" name="test_refund"><error>RuntimeError: boom
second line</error></testcase>
 <testcase classname="tests.test_pay" name="test_skip"><skipped/></testcase>
</testsuite>
XML
"""


def test_a_failed_run_keeps_the_failed_names_and_the_junit_files_and_release_refuses_it(lab, tmp_path):
    sys.path.insert(0, str(ROOT / 'scripts/release'))
    import release
    from test_release_local import oci_archive
    sha = lab['commit']('failing candidate', {'ci-test.sh': CI_TEST_FAILING, 'versions.lock.json': (ROOT / 'versions.lock.json').read_text()},
                        parent=lab['base'])
    lab['push']('integrator-a', f'{sha}:refs/heads/integration/failing')
    result = once(lab)['outcome']
    assert result['status'] == 'failed' and 'suite_not_clean:unit' in result['reasons']
    attempt = lab['queue'] / 'results' / sha / 'attempt-1'
    assert not (lab['tmp'] / 'builds' / f'{sha}-1').exists()  # the work directory is still removed
    # result.json: additive summary only (schema unchanged); the names are in failures.json next to it.
    assert result['schema'] == 'nexloop-ci-result/1' and result['failures'] == {'file': 'failures.json', 'count': 2, 'truncated': False}
    failures = json.loads((attempt / 'failures.json').read_text())
    assert failures['schema'] == 'nexloop-ci-failures/1' and failures['sha'] == sha and failures['attempt'] == 1
    unit = failures['suites']['unit']
    assert unit['stage'] == 'suite' and unit['count'] == 2 and unit['junit'] == 'junit/suite/unit.xml'
    cases = {(c['classname'], c['name'], c['kind']): c['message'] for c in unit['cases']}
    assert set(cases) == {('tests.test_pay', 'test_settles_once', 'failure'), ('tests.test_pay', 'test_refund', 'error')}
    message = cases[('tests.test_pay', 'test_settles_once', 'failure')]
    assert message.startswith('assert 1 == 2') and 'hunter2' not in message and 'abcdefghijklmnopqrstuvwxyzABCDEF' not in message
    assert 'postgresql://<redacted>@' in message and 'password=<redacted>' in message and len(message) <= 300
    assert cases[('tests.test_pay', 'test_refund', 'error')] == 'RuntimeError: boom'  # first line only, never the raw log
    # The JUnit file itself is kept, 0640, byte for byte.
    copy = attempt / 'junit' / 'suite' / 'unit.xml'
    assert copy.stat().st_mode & 0o777 == 0o640 and hashlib.sha256(copy.read_bytes()).hexdigest() == result['stages'][0]['junit']['unit']['sha256']
    # release.build_manifest still accepts only a passed result of this commit.
    digest = 'sha256:' + 'a' * 64
    images = tmp_path / 'images.json'
    images.write_text(json.dumps({'nexloop-api': {'archive': str(oci_archive(tmp_path / 'api.tar', digest)), 'digest': digest}}))
    with pytest.raises(release.Refused):
        release.build_manifest(sha=sha, images=images, ci_result=attempt / 'result.json', out=tmp_path / 'rel', root=lab['work'])


def test_a_passed_run_has_an_empty_failures_file_and_long_messages_and_lists_are_bounded(lab):
    sha = lab['commit']('passing', parent=lab['base'])
    lab['push']('integrator-a', f'{sha}:refs/heads/integration/passing')
    result = once(lab)['outcome']
    assert result['status'] == 'passed' and result['failures'] == {'file': 'failures.json', 'count': 0, 'truncated': False}
    assert json.loads((lab['queue'] / 'results' / sha / 'attempt-1' / 'failures.json').read_text())['suites'] == {}
    case = ci.ET.fromstring('<failure message="' + 'x ' * 400 + '"/>')
    assert len(ci._failure_message(case)) == ci.FAILURE_MESSAGE_LIMIT
    body = '<testsuite>' + ''.join(f'<testcase classname="c" name="t{i}"><failure/></testcase>' for i in range(ci.FAILURES_PER_SUITE + 5)) + '</testsuite>'
    path = lab['tmp'] / 'many.xml'
    path.write_text(body)
    assert len(ci.junit_failures(path)) == ci.FAILURES_PER_SUITE + 5  # bounded when stored (FAILURES_PER_SUITE, truncated flag)

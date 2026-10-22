"""Exercise publication gates in disposable repositories without printing secrets."""
import pathlib
import subprocess
import tempfile

SCAN = pathlib.Path(__file__).with_name('prepush-scan.sh').resolve()

def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)

with tempfile.TemporaryDirectory(prefix='nexloop-prepush-') as temp:
    root = pathlib.Path(temp)
    # Synthetic restricted identifiers: the real list is private and never part of the repository.
    identifiers = pathlib.Path(temp) / 'restricted-identifiers.txt'
    identifiers.write_text('# synthetic\n10\\.0\\.99\\.(7|8)\nexample-host-[0-9]+\nSHA256:Zq[A-Za-z]{4}\n')
    import os
    os.environ['NEXLOOP_RESTRICTED_IDENTIFIERS_FILE'] = str(identifiers)
    run('git', 'init', '-b', 'main', cwd=root)
    run('git', 'config', 'user.email', 'test@example.invalid', cwd=root)
    run('git', 'config', 'user.name', 'Publication test', cwd=root)
    (root / 'safe').write_text('baseline\n')
    # Gate 4 trusts the configuration and pin of the base commit, so the disposable repository's base carries the real ones.
    (root / '.gitleaks.toml').write_text((SCAN.parents[2] / '.gitleaks.toml').read_text())
    (root / 'versions.lock.json').write_text((SCAN.parents[2] / 'versions.lock.json').read_text())
    run('git', 'add', 'safe', '.gitleaks.toml', 'versions.lock.json', cwd=root)
    run('git', 'commit', '-m', 'baseline', cwd=root)
    baseline = run('git', 'rev-parse', 'HEAD', cwd=root).stdout.strip()
    run('git', 'update-ref', 'refs/remotes/origin/main', baseline, cwd=root)
    def check(expected, hook=False):
        args = ['bash', str(SCAN)]
        kwargs = {}
        if hook:
            args += ['--hook', 'origin']
            head = run('git', 'rev-parse', 'HEAD', cwd=root).stdout.strip()
            kwargs['input'] = f'refs/heads/feature {head} refs/heads/feature {"0" * 40}\n'
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, **kwargs)
        assert (result.returncode == 0) == expected, result.stdout
    check(True)
    values = ['10.0.99.' + '7', 'example-host-' + '42',
              'SHA256:' + 'ZqWxYz', 'MODEL_API_KEY=' + 'abcdefgh1234',
              'sk-' + 'a' * 24, 'xz' + 'zn']
    for value in values:
        run('git', 'reset', '--hard', baseline, cwd=root)
        (root / 'safe').write_text(value + '\n')
        run('git', 'add', 'safe', cwd=root)
        run('git', 'commit', '-m', 'unsafe historic content', cwd=root)
        (root / 'safe').write_text('removed\n')
        run('git', 'add', 'safe', cwd=root)
        run('git', 'commit', '-m', 'remove current content', cwd=root)
        check(False)
        check(False, hook=True)
    run('git', 'reset', '--hard', baseline, cwd=root)
    (root / 'safe').write_text('MODEL_API_KEY=${MODEL_API_KEY}\nEMBEDDING_API_KEY=""\n')
    run('git', 'add', 'safe', cwd=root)
    run('git', 'commit', '-m', 'references without values', cwd=root)
    check(True)
    check(True, hook=True)
    (root / '.env').write_text('')
    run('git', 'add', '.env', cwd=root)
    check(False)
    # A missing restricted identifier list fails closed.
    run('git', 'reset', '--hard', baseline, cwd=root)
    (root / 'safe').write_text('harmless\n')
    run('git', 'add', 'safe', cwd=root)
    run('git', 'commit', '-m', 'harmless change', cwd=root)
    os.environ['NEXLOOP_RESTRICTED_IDENTIFIERS_FILE'] = str(pathlib.Path(temp) / 'absent.txt')
    check(False)
    os.environ['NEXLOOP_RESTRICTED_IDENTIFIERS_FILE'] = str(identifiers)
    check(True)
    # Gate 4 (NX-060), gitleaks. Synthetic credentials are assembled at run time so that this file never contains one.
    def commit(path, text, message):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        run('git', 'add', path, cwd=root)
        run('git', 'commit', '-m', message, cwd=root)
    def full(expected):
        result = subprocess.run(['bash', str(SCAN), '--full', 'HEAD'], cwd=root, capture_output=True, text=True)
        assert (result.returncode == 0) == expected, result.stdout
        return result.stdout
    fakes = {'aws.cfg': 'aws_access_key_id = ' + 'AKIA' + 'Q4RT7WX2' + 'ZV3PMN6Y' + '\n',
             'deploy.env': 'GITHUB_TOKEN=' + 'ghp_' + 'r8Kq2Lm5Nv7Px3Tz9Wb4Yc6Hd1Fg0Js2Ae8U' + '\n'}
    for path, text in fakes.items():
        run('git', 'reset', '--hard', baseline, cwd=root)
        commit(path, text, 'fake credential')
        commit(path, 'removed\n', 'remove it again')
        result = subprocess.run(['bash', str(SCAN)], cwd=root, capture_output=True, text=True)
        assert result.returncode != 0 and 'FAIL gitleaks: ' in result.stdout, result.stdout
        assert text.split('=')[-1].strip() not in result.stdout + result.stderr  # counted, never printed
        check(False, hook=True)
        full(False)
    # A synthetic idempotency key that the default generic rule does flag is allowed only in a test file: the allowlist
    # is limited by path as well as by shape.
    synthetic = "headers = {'Idempotency-Key': '" + 'synthetic-wb-' + "owner-reject-0001'}\n"
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('tests/test_demo.py', synthetic, 'synthetic key in a test')
    check(True)
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('service/demo.py', synthetic, 'same value outside tests')
    check(False)
    # The same leak only before the base passes the outgoing range but not the full mode.
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('aws.cfg', fakes['aws.cfg'], 'old leak')
    commit('aws.cfg', 'removed\n', 'removed long ago')
    run('git', 'update-ref', 'refs/remotes/origin/main', 'HEAD', cwd=root)
    commit('safe', 'later\n', 'later change')
    check(True)
    assert 'FAIL gitleaks: ' in full(False)
    run('git', 'update-ref', 'refs/remotes/origin/main', baseline, cwd=root)
    # Fail closed: no tool, another version, a tool that detects nothing (sentinel; on linux_amd64 the digest pin stops it first).
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('safe', 'harmless again\n', 'harmless')
    check(True)
    def stub(name, body):
        path = pathlib.Path(temp) / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
        return str(path)
    for tool in (str(pathlib.Path(temp) / 'absent-gitleaks'), stub('old-gitleaks', 'echo 0.0.1\n'),
                 stub('blind-gitleaks', 'echo 8.30.1\nexit 0\n')):
        os.environ['NEXLOOP_GITLEAKS'] = tool
        check(False)
    del os.environ['NEXLOOP_GITLEAKS']
    def scan_output():
        return subprocess.run(['bash', str(SCAN)], cwd=root, capture_output=True, text=True)
    aws = fakes['aws.cfg']
    # The outgoing commits cannot loosen their own scan: a dropped, neutered or wide-open configuration in them is reported
    # for owner ack and the base configuration still applies.
    wide = ('minVersion = "8.30.1"\n[extend]\nuseDefault = true\n[[allowlists]]\ndescription = "everything"\n'
            "regexes = ['" + "''.''" + "']\n")
    loosened = {'drop': None, 'neutered': 'title = "detects nothing"\n[extend]\nuseDefault = false\n', 'wide': wide}
    for name, text in loosened.items():
        run('git', 'reset', '--hard', baseline, cwd=root)
        if text is None:
            run('git', 'rm', '-q', '.gitleaks.toml', cwd=root)
            run('git', 'commit', '-q', '-m', 'drop the configuration', cwd=root)
        else:
            commit('.gitleaks.toml', text, name + ' configuration')
        commit('aws.cfg', aws, 'fake credential under a loosened configuration')
        result = scan_output()
        assert result.returncode != 0 and 'FAIL gitleaks: ' in result.stdout and 'owner ack required' in result.stdout, (name, result.stdout)
    # A base whose own configuration breaks the structure rules is not trusted either.
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('.gitleaks.toml', wide, 'wide configuration published')
    run('git', 'update-ref', 'refs/remotes/origin/main', 'HEAD', cwd=root)
    commit('safe', 'harmless on a loosened base\n', 'harmless')
    result = scan_output()
    assert result.returncode != 0 and 'breaks the structure rules' in result.stdout, result.stdout
    # A base without a configuration (first introduction) falls back to this checkout's configuration.
    run('git', 'reset', '--hard', baseline, cwd=root)
    run('git', 'rm', '-q', '.gitleaks.toml', cwd=root)
    run('git', 'commit', '-q', '-m', 'base without configuration', cwd=root)
    run('git', 'update-ref', 'refs/remotes/origin/main', 'HEAD', cwd=root)
    commit('safe', 'harmless again\n', 'harmless')
    result = scan_output()
    assert result.returncode == 0 and '(checkout configuration)' in result.stdout, result.stdout
    commit('aws.cfg', aws, 'fake credential')
    check(False)
    run('git', 'update-ref', 'refs/remotes/origin/main', baseline, cwd=root)
    # Neither a .gitleaksignore (tracked or not) nor a gitleaks:allow comment hides a finding.
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('aws.cfg', aws.rstrip('\n') + ' # gitleaks:' + 'allow\n', 'inline allow comment')
    check(False)
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('aws.cfg', aws, 'fake credential')
    report = pathlib.Path(temp) / 'fingerprints.json'
    subprocess.run(['gitleaks', 'git', '--no-banner', '--log-level', 'error', '--redact', '--log-opts', 'HEAD~1..HEAD',
                    '--report-format', 'json', '--report-path', str(report), '.'], cwd=root, capture_output=True)
    import json
    fingerprints = ''.join(f['Fingerprint'] + '\n' for f in json.loads(report.read_text()))
    assert fingerprints
    (root / '.gitleaksignore').write_text(fingerprints)
    result = scan_output()
    assert result.returncode != 0 and 'a .gitleaksignore is present' in result.stdout, result.stdout
    commit('.gitleaksignore', fingerprints, 'tracked ignore file')
    check(False)
    run('git', 'reset', '--hard', baseline, cwd=root)
    (root / '.gitleaksignore').unlink(missing_ok=True)
    # From a trusted CI installation (scripts/ci only, OPERATIONS §7.2: no versions.lock.json, no .gitleaks.toml) the pin and
    # the configuration come from the base commit; without them in the base as well the gate fails closed with a message.
    import shutil
    trusted = pathlib.Path(temp) / 'trusted'
    shutil.copytree(SCAN.parent, trusted / 'scripts' / 'ci')
    trusted_scan = ['bash', str(trusted / 'scripts' / 'ci' / 'prepush-scan.sh')]
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('safe', 'harmless from the trusted installation\n', 'harmless')
    result = subprocess.run(trusted_scan, cwd=root, capture_output=True, text=True)
    assert result.returncode == 0 and '(base configuration)' in result.stdout, result.stdout
    commit('aws.cfg', aws, 'fake credential')
    result = subprocess.run(trusted_scan, cwd=root, capture_output=True, text=True)
    assert result.returncode != 0 and 'FAIL gitleaks: ' in result.stdout, result.stdout
    run('git', 'reset', '--hard', baseline, cwd=root)
    run('git', 'rm', '-q', 'versions.lock.json', cwd=root)
    run('git', 'commit', '-q', '-m', 'base without the pin', cwd=root)
    run('git', 'update-ref', 'refs/remotes/origin/main', 'HEAD', cwd=root)
    commit('safe', 'harmless again\n', 'harmless')
    result = subprocess.run(trusted_scan, cwd=root, capture_output=True, text=True)
    assert result.returncode != 0 and 'FAIL gitleaks pin unreadable' in result.stdout, result.stdout
    run('git', 'update-ref', 'refs/remotes/origin/main', baseline, cwd=root)
    run('git', 'reset', '--hard', baseline, cwd=root)
print('PASS: gitleaks gate blocks fake AWS/GitHub credentials (outgoing, hook, full), allows synthetic test keys only in tests, full mode sees old history, trusts only the base/checkout configuration (dropped, neutered, wide-open outgoing configurations still blocked; broken base refused), ignores .gitleaksignore and gitleaks:allow, runs from a scripts/ci-only trusted installation, fails closed without tool/pinned version/sentinel')
print('PASS: missing list fails closed, clean/no-ahead, historical identifiers and credentials, feature hook, variable references, tracked private paths')

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
    # Gate 4 reads the configuration from the scanned commit, so the disposable repository carries the real one.
    (root / '.gitleaks.toml').write_text((SCAN.parents[2] / '.gitleaks.toml').read_text())
    run('git', 'add', 'safe', '.gitleaks.toml', cwd=root)
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
    # Fail closed: no tool, another version, no configuration in the scanned commit, a configuration that detects nothing.
    run('git', 'reset', '--hard', baseline, cwd=root)
    commit('safe', 'harmless again\n', 'harmless')
    check(True)
    stub = pathlib.Path(temp) / 'stub-gitleaks'
    stub.write_text('#!/bin/sh\necho 0.0.1\n')
    stub.chmod(0o755)
    for tool in (str(pathlib.Path(temp) / 'absent-gitleaks'), str(stub)):
        os.environ['NEXLOOP_GITLEAKS'] = tool
        check(False)
    del os.environ['NEXLOOP_GITLEAKS']
    run('git', 'rm', '-q', '.gitleaks.toml', cwd=root)
    run('git', 'commit', '-m', 'drop the configuration', cwd=root)
    check(False)
    commit('.gitleaks.toml', 'title = "detects nothing"\n[extend]\nuseDefault = false\n', 'neutered configuration')
    result = subprocess.run(['bash', str(SCAN)], cwd=root, capture_output=True, text=True)
    assert result.returncode != 0 and 'no longer detects the sentinel' in result.stdout, result.stdout
print('PASS: gitleaks gate blocks fake AWS/GitHub credentials (outgoing, hook, full), allows synthetic test keys, full mode sees old history, fails closed without tool/pinned version/configuration/sentinel')
print('PASS: missing list fails closed, clean/no-ahead, historical identifiers and credentials, feature hook, variable references, tracked private paths')

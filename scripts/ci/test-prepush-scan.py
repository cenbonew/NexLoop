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
    run('git', 'add', 'safe', cwd=root)
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
print('PASS: missing list fails closed, clean/no-ahead, historical identifiers and credentials, feature hook, variable references, tracked private paths')

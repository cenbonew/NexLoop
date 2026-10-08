"""Exercise publication gates in disposable repositories without printing secrets."""
import pathlib
import subprocess
import tempfile

SCAN = pathlib.Path(__file__).with_name('prepush-scan.sh').resolve()

def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)

with tempfile.TemporaryDirectory(prefix='nexloop-prepush-') as temp:
    root = pathlib.Path(temp)
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
    values = ['192.' + '168.31.140', 'openclaw-' + 'thinkpad',
              'SHA256:' + 'Ncgbuz', 'MODEL_API_KEY=' + 'abcdefgh1234',
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
print('PASS: clean/no-ahead, historical identifiers and credentials, feature hook, variable references, tracked private paths')

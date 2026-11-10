"""scripts/dev/keychain_new_password.py: the password goes only to `security -i` on stdin, never argv or output; an item that exists
is refused."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/dev'))
import keychain_new_password as K  # noqa: E402


class Fake:
    def __init__(self, exists=False):
        self.calls, self.exists = [], exists

    def __call__(self, argv, input=None, **kw):
        self.calls.append((argv, input))
        found = self.exists or any(a == ['security', '-i'] for a, _ in self.calls[:-1])
        code = 0 if argv[1] == '-i' or (argv[1] == 'find-generic-password' and found) else 44
        return type('R', (), {'returncode': code})()


def test_the_password_goes_on_stdin_only_and_is_never_printed(capsys):
    fake = Fake()
    assert K.main(['--service', 'nexloop/stage/walk-op-20261110', '--account', 'walk-op-20261110'], run=fake) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {'stored': True, 'service': 'nexloop/stage/walk-op-20261110', 'account': 'walk-op-20261110'}
    (argv, stdin), = [c for c in fake.calls if c[0] == ['security', '-i']]
    password = stdin.split(' -w ', 1)[1].strip()
    assert len(password) >= 24 and all(password not in ' '.join(a) for a, _ in fake.calls) and password not in json.dumps(out)


def test_an_existing_item_or_a_bad_name_is_refused(capsys):
    fake = Fake(exists=True)
    assert K.main(['--service', 'nexloop/stage/x', '--account', 'x'], run=fake) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'exists' and not any(a == ['security', '-i'] for a, _ in fake.calls)
    assert K.main(['--service', 'bad name', '--account', 'x'], run=Fake()) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'bad_name'

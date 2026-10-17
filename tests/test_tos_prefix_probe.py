"""scripts/deploy/tos_prefix_probe.py (NX-035 repo2 §1.3): SigV4 against the published AWS example, the checks a prefix-scoped
sub-user must pass, and that keys stay out of argv and output."""
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import tos_prefix_probe as P  # noqa: E402

# The AWS Signature Version 4 example of the S3 documentation ("GET Object" with a Range header): its published example key pair
# and expected signature (an independent reference, not a credential).
AWS_EXAMPLE_ACCESS = 'AKIAIOSFODNN7EXAMPLE'
AWS_EXAMPLE_SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'


def test_sigv4_matches_the_published_s3_example():
    headers = P.sign('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', [], {'Range': 'bytes=0-9'}, P.EMPTY_SHA256,
                     access_key=AWS_EXAMPLE_ACCESS, secret_key=AWS_EXAMPLE_SECRET, region='us-east-1', now=datetime(2013, 5, 24, tzinfo=UTC))
    assert headers['authorization'] == ('AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, '
                                        'SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, '
                                        'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')


class Fake:
    """A prefix-scoped TOS: 200/204 inside the allowed prefixes, 403 elsewhere; records hosts and paths, never answers bodies."""

    def __init__(self, allowed, path_style_status=200):
        self.allowed, self.path_style_status, self.requests = allowed, path_style_status, []

    def request(self, method, host, path, query, headers, body=b''):
        self.requests.append((method, host, path, dict(query), headers))
        if not host.startswith('synthetic-bucket.'):
            return self.path_style_status
        target = dict(query).get('prefix', '') if method == 'GET' else path.lstrip('/')
        inside = any(target.startswith(p) for p in self.allowed) and target != ''
        return (200 if method != 'DELETE' else 204) if inside else 403


def test_a_correctly_scoped_sub_user_passes_and_a_wide_one_fails(capsys):
    fake = Fake(P.PREFIXES['data'])
    stdin = io.StringIO('synthetic-access-id\nsynthetic-secret-value\n')
    code = P.main(['--bucket', 'synthetic-bucket', '--role', 'data', '--credentials-stdin', '--write'], stdin=stdin, transport=fake)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out['ok'] and [c['check'] for c in out['checks']] == [
        'list_own:nexloop/pgbackrest/', 'list_own:nexloop/restic-journal/', 'list_bucket_root', 'list_other:nexloop/restic-files/',
        'list_path_style:nexloop/pgbackrest/', 'put_own:nexloop/pgbackrest/', 'delete_own:nexloop/pgbackrest/', 'put_other:nexloop/restic-files/']
    text = json.dumps(out)
    assert 'synthetic-secret-value' not in text and 'synthetic-access-id' not in text and '.nexloop-probe-' not in text
    assert all(h['authorization'].startswith('AWS4-HMAC-SHA256 Credential=synthetic-access-id/') for *_, h in fake.requests)
    assert fake.requests[0][1] == 'synthetic-bucket.tos-s3-cn-shanghai.volces.com' and fake.requests[4][1] == 'tos-s3-cn-shanghai.volces.com'
    # A sub-user allowed on the whole bucket: the root listing and the other role's prefix answer 200, exit 1.
    wide = Fake(('nexloop/', ''))
    wide.request = lambda method, host, path, query, headers, body=b'': 200
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'app', '--credentials-stdin'], stdin=io.StringIO('a\nb\n'), transport=wide) == 1
    failed = [c['check'] for c in json.loads(capsys.readouterr().out)['checks'] if not c['ok']]
    assert failed == ['list_bucket_root', 'list_other:nexloop/pgbackrest/', 'list_other:nexloop/restic-journal/']


def test_keys_come_from_the_keychain_by_item_name_never_from_argv(capsys):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return type('Done', (), {'returncode': 0, 'stdout': 'value-of-' + argv[3].rsplit('/', 1)[1] + '\n'})()
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'app'], transport=Fake(P.PREFIXES['app']), runner=runner) == 0
    assert calls == [['security', 'find-generic-password', '-s', 'nexloop/backup/tos-app-access-key', '-w'],
                     ['security', 'find-generic-password', '-s', 'nexloop/backup/tos-app-secret-key', '-w']]
    assert 'value-of-' not in capsys.readouterr().out
    missing = lambda argv, **kw: type('Done', (), {'returncode': 44, 'stdout': ''})()  # noqa: E731
    assert P.main(['--bucket', 'synthetic-bucket', '--role', 'data'], transport=Fake(()), runner=missing) == 2
    assert json.loads(capsys.readouterr().out)['detail'] == 'keychain item unreadable: nexloop/backup/tos-data-access-key'
    for argv in (['--bucket', 'Bad_Bucket', '--role', 'data'], ['--bucket', 'synthetic-bucket', '--role', 'data', '--region', 'x'],
                 ['--bucket', 'synthetic-bucket', '--role', 'data', '--access-key-item', 'a b']):
        assert P.main(argv, transport=Fake(()), runner=runner) == 2
        capsys.readouterr()

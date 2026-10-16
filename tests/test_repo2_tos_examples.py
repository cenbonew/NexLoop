"""NX-035 repo2 on Volcengine TOS (docs/implementation/NX-035-repo2-tos.md): the configuration examples carry placeholders only
and the invariants of the design: pgBackRest repo2 on the TOS S3 endpoint with its own cipher; restic repo2 environment keys the
executors accept; a TLS-only bucket policy in the TOS spellings NEX-EIOS measured; a prefix-scoped IAM policy without bucket
configuration actions; no lifecycle expiration under the repositories."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG, TOS = ROOT / 'deploy/stage/config', ROOT / 'deploy/examples/tos'
SECRET_LIKE = re.compile(r'(?i)(AKLT|AKTP)[A-Za-z0-9]{8,}|[A-Za-z0-9+/]{40,}')


def settings(text):
    return dict(re.findall(r'^([A-Za-z0-9_-]+)=(.*)$', text, re.M))


def test_the_pgbackrest_fragment_points_repo2_at_the_tos_s3_endpoint_with_its_own_cipher():
    s = settings((CONFIG / 'pgbackrest-repo2-tos.conf.example').read_text())
    assert s['repo2-type'] == 's3' and s['repo2-s3-endpoint'] == 'tos-s3-__TOS_REGION__.volces.com' and s['repo2-s3-region'] == '__TOS_REGION__'
    assert s['repo2-s3-uri-style'] == 'host' and s['repo2-storage-verify-tls'] == 'y' and s['repo2-path'] == '/nexloop/pgbackrest'
    assert s['repo2-cipher-type'] == 'aes-256-cbc' and s['repo2-cipher-pass'] == '__REPO2_CIPHER_PASS__'
    assert {s['repo2-s3-key'], s['repo2-s3-key-secret']} == {'__TOS_DATA_ACCESS_KEY_ID__', '__TOS_DATA_SECRET_ACCESS_KEY__'}
    assert (s['repo2-retention-full-type'], s['repo2-retention-full'], s['repo2-retention-diff']) == ('time', '35', '14')
    assert all(k.startswith('repo2-') for k in s) and 'ivolces' not in s['repo2-s3-endpoint']


def test_the_restic_environments_hold_only_the_keys_their_readers_accept():
    from nexloop_eios.file_backup import _ENVIRONMENT_KEYS
    app = settings((CONFIG / 'file-backup-repo2.env.example').read_text())
    assert set(app) == set(_ENVIRONMENT_KEYS) and app['AWS_ACCESS_KEY_ID'] == '__TOS_APP_ACCESS_KEY_ID__'
    data = settings((CONFIG / 'restic-repo2.env.example').read_text())
    assert set(data) == {'JOURNAL_DIR', 'RESTIC_REPOSITORY_FILE', 'RESTIC_PASSWORD_FILE', *_ENVIRONMENT_KEYS}
    assert data['AWS_ACCESS_KEY_ID'] == '__TOS_DATA_ACCESS_KEY_ID__' and data['AWS_DEFAULT_REGION'] == '__TOS_REGION__'


def test_the_bucket_policy_is_tls_only_in_the_tos_spellings():
    policy = json.loads((TOS / 'bucket-policy.example.json').read_text())
    [deny] = policy['Statement']
    assert policy['Version'] == '1.0' and deny['Effect'] == 'Deny' and deny['Principal'] == ['*'] and deny['Action'] == 'tos:*'
    assert deny['Condition'] == {'Bool': {'tos:securetransport': ['false']}}
    assert deny['Resource'] == ['trn:tos:::__TOS_BUCKET__', 'trn:tos:::__TOS_BUCKET__/*']


def test_the_iam_policy_is_prefix_scoped_and_never_configures_the_bucket():
    policy = json.loads((TOS / 'iam-policy-data-host.example.json').read_text())
    listing, objects = policy['Statement']
    assert listing['Action'] == ['tos:ListObjects'] and listing['Resource'] == ['trn:tos:::__TOS_BUCKET__']
    assert listing['Condition'] == {'StringLike': {'tos:prefix': ['nexloop/pgbackrest/*', 'nexloop/restic-journal/*']}}
    assert all(r.startswith('trn:tos:::__TOS_BUCKET__/nexloop/') for r in objects['Resource'])
    actions = {a for s in policy['Statement'] for a in s['Action']}
    assert not [a for a in actions if re.search(r'Bucket(Policy|Acl|Encryption|Lifecycle|Versioning)|PutBucket|DeleteBucket|\*', a)]


def test_no_lifecycle_rule_expires_repository_objects():
    rules = json.loads((TOS / 'lifecycle.example.json').read_text())['Rules']
    assert not [r for r in rules if 'Expiration' in r]
    assert {r['ID'] for r in rules} == {'abort-incomplete-multipart', 'noncurrent-versions-14d'}


def test_the_examples_carry_no_real_value():
    for path in [*CONFIG.glob('*repo2*.example'), *TOS.glob('*.json')]:
        text = path.read_text()
        assert not SECRET_LIKE.search(text), path.name
        assert not re.search(r'(?<![_\w])[a-z0-9][a-z0-9-]{2,62}\.tos-s3-', text), path.name  # no real bucket host

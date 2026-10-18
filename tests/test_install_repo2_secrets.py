"""scripts/deploy/install_repo2_secrets.sh (repo2 activation run sheet, 2026-10-18): secrets from stdin only, the pgBackRest repo2 block
after [global] with no placeholder left, the restic repo2 files of the journal and the file backup, the optional bucket lookup, the
refusal to overwrite without REPLACE=1, and a .pre-repo2 copy for the rollback. Runs the script with PATH shims for the root-only
tools (id, install, stat): no root, no host, synthetic values."""
import os
import stat as st
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy/install_repo2_secrets.sh'
AK, SK, PASS = 'SYNTHETICACCESSID', 'synthetic/secret+value', 'p' * 40
SHIM = {
    'id': '#!/bin/sh\n[ "$1" = -u ] && { echo 0; exit 0; }\nexit 0\n',
    # install [-d] -o U -g G -m MODE SRC DST: owner ignored, mode applied.
    'install': f'#!{sys.executable}\nimport shutil,sys,os\na=sys.argv[1:];m=0o600\nwhile a and a[0].startswith("-"):\n'
               f'    o=a.pop(0)\n    if o=="-d":sys.exit(0)\n    v=a.pop(0)\n    if o=="-m":m=int(v,8)\nshutil.copyfile(a[0],a[1]);os.chmod(a[1],m)\n',
    'stat': f'#!{sys.executable}\nimport os,sys\na=sys.argv[1:];fmt=a[1];p=a[2];s=os.stat(p)\n'
            f'print(fmt.replace("%n",p).replace("%U:%G","u:g").replace("%u",str(s.st_uid)).replace("%g",str(s.st_gid)).replace("%a",oct(s.st_mode&0o777)[2:]).replace("%s",str(s.st_size)))\n',
}


def run(tmp, role, stdin, **env):
    shims = tmp / 'shims'
    shims.mkdir(exist_ok=True)
    for name, body in SHIM.items():
        (shims / name).write_text(body)
        (shims / name).chmod(0o755)
    environment = {'PATH': f'{shims}:{os.environ["PATH"]}', 'NEXLOOP_ROOT': str(tmp / 'root'), 'NEXLOOP_ETC': str(tmp / 'etc'),
                   'TOS_BUCKET': 'synthetic-bucket', 'TOS_REGION': 'cn-shanghai', **env}
    return subprocess.run(['bash', str(SCRIPT), role], input=stdin, env=environment, capture_output=True, text=True, timeout=60)


def test_pgbackrest_block_goes_after_global_and_needs_replace_to_rewrite(tmp_path):
    conf = tmp_path / 'root/secrets/pgbackrest/pgbackrest.conf'
    conf.parent.mkdir(parents=True)
    conf.write_text('[global]\nrepo1-path=/var/lib/pgbackrest\n\n[nexloop]\npg1-path=/var/lib/postgresql/data\n')
    done = run(tmp_path, 'pgbackrest', f'{AK}\n{SK}\n{PASS}\n')
    assert done.returncode == 0, done.stderr
    text = conf.read_text()
    assert text.index('repo2-type=s3') < text.index('[nexloop]') and text.index('[global]') < text.index('repo2-type=s3')
    assert 'repo2-s3-endpoint=tos-s3-cn-shanghai.volces.com' in text and 'repo2-s3-bucket=synthetic-bucket' in text and '__' not in text
    assert f'repo2-s3-key-secret={SK}' in text and f'repo2-cipher-pass={PASS}' in text and 'repo1-path=/var/lib/pgbackrest' in text
    assert st.S_IMODE(conf.stat().st_mode) == 0o600 and (conf.parent / 'pgbackrest.conf.pre-repo2').exists()
    for secret in (AK, SK, PASS):
        assert secret not in done.stdout + done.stderr
    again = run(tmp_path, 'pgbackrest', f'{AK}\n{SK}\n{PASS}\n')
    assert again.returncode == 2 and 'REPLACE=1' in again.stderr
    replaced = run(tmp_path, 'pgbackrest', f'{AK}\n{SK}\n{"q" * 40}\n', REPLACE='1')
    assert replaced.returncode == 0 and conf.read_text().count('repo2-type=s3') == 1 and 'repo2-cipher-pass=' + 'q' * 40 in conf.read_text()


def test_journal_and_files_write_restic_repo2_and_the_optional_dns_lookup(tmp_path):
    etc = tmp_path / 'etc'
    etc.mkdir()
    (etc / 'restic-repo1.env').write_text('JOURNAL_DIR=/srv/nexloop-backup/journal\nRESTIC_REPOSITORY_FILE=/x\nRESTIC_PASSWORD_FILE=/y\n')
    (etc / 'restic.env').write_text('RESTIC_REPO1_REPOSITORY_FILE=/r1\nRESTIC_REPO1_PASSWORD_FILE=/p1\n')
    (tmp_path / 'root/secrets/backup').mkdir(parents=True)
    done = run(tmp_path, 'journal', f'{AK}\n{SK}\n{PASS}\n', BUCKET_LOOKUP='dns')
    assert done.returncode == 0, done.stderr
    env2 = (etc / 'restic-repo2.env').read_text()
    assert 'JOURNAL_DIR=/srv/nexloop-backup/journal' in env2 and 'RESTIC_EXTRA_OPTIONS=-o s3.bucket-lookup=dns' in env2 and f'AWS_SECRET_ACCESS_KEY={SK}' in env2
    assert (tmp_path / 'root/secrets/backup/restic-repo2.repository').read_text() == 's3:https://tos-s3-cn-shanghai.volces.com/synthetic-bucket/nexloop/restic-journal\n'
    assert (tmp_path / 'root/secrets/backup/restic-repo2.pass').read_text() == PASS + '\n'
    maint = (etc / 'restic.env').read_text()
    assert 'RESTIC_REPO1_REPOSITORY_FILE=/r1' in maint and 'RESTIC_REPO2_OPTIONS=-o s3.bucket-lookup=dns' in maint and maint.count('AWS_ACCESS_KEY_ID=') == 1
    # files (APP_HOST): the paths come from file-backup.env; without BUCKET_LOOKUP no option line.
    (etc / 'file-backup.env').write_text(f'RESTIC_REPO2_REPOSITORY_FILE={tmp_path}/f.repository\nRESTIC_REPO2_PASSWORD_FILE={tmp_path}/f.pass\n'
                                         f'OFFSITE_ENVIRONMENT_FILE={tmp_path}/offsite.env\nFILE_BACKUP_REPO2_OPTIONS=--s3-bucket-lookup dns\n')
    files = run(tmp_path, 'files', f'{AK}\n{SK}\n{PASS}\n')
    assert files.returncode == 0, files.stderr
    assert (tmp_path / 'f.repository').read_text().endswith('/synthetic-bucket/nexloop/restic-files\n')
    assert (tmp_path / 'offsite.env').read_text() == f'AWS_ACCESS_KEY_ID={AK}\nAWS_SECRET_ACCESS_KEY={SK}\nAWS_DEFAULT_REGION=cn-shanghai\n'
    assert 'FILE_BACKUP_REPO2_OPTIONS' not in (etc / 'file-backup.env').read_text()


def test_refused_input_writes_nothing(tmp_path):
    for stdin, env, why in ((f'{AK}\n{SK}\n', {}, 'three lines'), (f'{AK}\n{SK}\nshort\n', {}, 'at least 32'),
                            (f'{AK}\n{SK}\n{PASS}\nextra\n', {}, 'more than three'), (f'{AK}\n{SK}\n{PASS}\n', {'TOS_BUCKET': 'Bad_Bucket'}, 'TOS_BUCKET'),
                            (f'{AK}\n{SK}\n{PASS}\n', {'BUCKET_LOOKUP': 'virtual'}, 'BUCKET_LOOKUP')):
        done = run(tmp_path, 'journal', stdin, **env)
        assert done.returncode == 2 and why in done.stderr, (why, done.stderr)
    assert not (tmp_path / 'etc').exists()


def test_values_with_awk_and_sed_metacharacters_are_written_literally_and_never_printed(tmp_path):
    # PR #10 review: awk sub() reads & and \ in the replacement; a value with them must land as is.
    conf = tmp_path / 'root/secrets/pgbackrest/pgbackrest.conf'
    conf.parent.mkdir(parents=True)
    conf.write_text('[global]\nrepo1-path=/var/lib/pgbackrest\n')
    ak, sk, passphrase = 'SYNTH&ID\\1', 'sec&ret/\\&value__x', 'pass&\\/' + 'z' * 32 + '&'
    done = run(tmp_path, 'pgbackrest', f'{ak}\n{sk}\n{passphrase}\n')
    assert done.returncode == 0, done.stderr
    text = conf.read_text()
    assert f'repo2-s3-key={ak}\n' in text and f'repo2-s3-key-secret={sk}\n' in text and f'repo2-cipher-pass={passphrase}\n' in text
    for value in (ak, sk, passphrase):
        assert value not in done.stdout + done.stderr

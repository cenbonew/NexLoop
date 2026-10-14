"""NX-035 / NX-034: the DATA_HOST PostgreSQL image of a release (nexloop/postgres) carries pgBackRest at the locked version.

The Dockerfile builds FROM the locked PostgreSQL + pgvector image by digest and installs exactly the locked pgBackRest package
(PGDG, or pre-downloaded .deb files checked against SHA256SUMS), nothing else, ending as the postgres user; release.py builds
it for linux/amd64 with the exact versions from versions.lock.json and records it by digest in the manifest. The docker CLI is
the stand-in of test_release_build_archive; nothing is pulled or built for real."""
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/release'))
import prepare_postgres_build as P  # noqa: E402
import release  # noqa: E402
from test_release_build_archive import Docker  # noqa: E402
from test_release_local import head_sha  # noqa: E402

DOCKERFILE = (ROOT / 'deploy/postgres/Dockerfile').read_text()
LOCK = json.loads((ROOT / 'versions.lock.json').read_text())
CONFIG = json.loads((ROOT / 'deploy/release/images.v1.json').read_text())


def instructions():
    return [line for line in DOCKERFILE.splitlines() if line and not line.startswith(('#', ' '))]


def test_the_dockerfile_builds_from_the_locked_base_installs_only_the_locked_pgbackrest_and_ends_as_postgres():
    lines = instructions()
    assert lines[:2] == ['ARG POSTGRES_IMAGE', 'FROM ${POSTGRES_IMAGE}'] and lines[-1] == 'USER postgres'
    assert [line.split()[0] for line in lines].count('FROM') == 1
    assert {'ARG PGBACKREST_VERSION', 'ARG PGBACKREST_DEB_VERSION'} <= set(lines)
    installs = re.findall(r'apt-get install[^;\\]*', DOCKERFILE)
    assert installs == ['apt-get install --yes --no-install-recommends "pgbackrest=${PGBACKREST_DEB_VERSION}"']
    assert 'test "$(pgbackrest version)" = "pgBackRest ${PGBACKREST_VERSION}"' in DOCKERFILE
    assert 'sha256sum --check --strict SHA256SUMS' in DOCKERFILE and 'curl' not in DOCKERFILE and 'wget' not in DOCKERFILE


def test_the_release_config_passes_the_locked_base_and_the_locked_pgbackrest_versions():
    entry = CONFIG['images']['postgres']
    assert entry == {'kind': 'build', 'context': 'postgres', 'base': 'postgres', 'build_args': {'POSTGRES_IMAGE': 'postgres'},
                     'tool_args': {'PGBACKREST_VERSION': ['deployment_tools', 'pgbackrest', 'version'],
                                   'PGBACKREST_DEB_VERSION': ['deployment_tools', 'pgbackrest', 'debian_version']},
                     'mirror_args': {'APT_DEBIAN_MIRROR': 'NEXLOOP_APT_DEBIAN_MIRROR', 'APT_DEBIAN_SECURITY_MIRROR': 'NEXLOOP_APT_DEBIAN_SECURITY_MIRROR',
                                     'APT_PGDG_MIRROR': 'NEXLOOP_APT_PGDG_MIRROR'}}
    assert release.pinned_tool_value(LOCK, entry['tool_args']['PGBACKREST_VERSION']) == LOCK['deployment_tools']['pgbackrest']['version']
    assert release.pinned_reference(LOCK, 'postgres') == 'docker.io/pgvector/pgvector@' + LOCK['images']['postgres']['digest']
    assert 'valkey' not in CONFIG['images']  # valkey stays the pinned upstream image of the lock


def pinned_lock(debian='2.56.0-1.pgdg120+1'):
    lock = copy.deepcopy(LOCK)
    lock['deployment_tools']['pgbackrest']['debian_version'] = debian
    return lock


def build(tmp_path, docker, lock):
    ci = tmp_path / 'ci.json'
    ci.write_text(json.dumps({'sha': head_sha(), 'status': 'passed', 'policy_version': 2, 'deadline_gate': 'passed', 'counts': {'passed': 1}}))
    import zlib
    return release.build(head_sha(), ci, tmp_path / 'release-root', sequence=7000 + zlib.crc32(str(tmp_path).encode()) % 1000, only=['postgres'],
                         root=ROOT, docker=docker, checkout_check=lambda r, s: None, lock=lock, environ={})


def test_the_release_build_makes_nexloop_postgres_for_amd64_with_the_exact_versions_and_records_it_by_digest(tmp_path):
    docker = Docker()
    out = build(tmp_path, docker, pinned_lock())
    built = next(c for c in docker.calls if c[0] == 'build')
    assert built[built.index('--platform') + 1] == 'linux/amd64' and '--pull=false' in built
    args = [built[i + 1] for i, a in enumerate(built) if a == '--build-arg']
    assert args == ['POSTGRES_IMAGE=docker.io/pgvector/pgvector@' + LOCK['images']['postgres']['digest'],
                    'PGBACKREST_DEB_VERSION=2.56.0-1.pgdg120+1', 'PGBACKREST_VERSION=' + LOCK['deployment_tools']['pgbackrest']['version']]
    manifest = json.loads((Path(out['release_dir']) / 'manifest.json').read_text())
    (image,) = manifest['images']
    assert image['name'] == 'postgres' and image['reference'] == 'nexloop/postgres@' + image['digest'] and out['images'] == {'postgres': image['reference']}
    assert image['base_digest'] == LOCK['images']['postgres']['digest']


def test_the_release_build_refuses_while_the_debian_package_version_is_not_pinned(tmp_path):
    docker = Docker()
    with pytest.raises(release.Refused, match='deployment_tools.pgbackrest.debian_version: not pinned'):
        build(tmp_path, docker, pinned_lock(None))
    assert not [c for c in docker.calls if c[0] == 'build']


def debs(directory, files, sums=None):
    directory.mkdir()
    for name, body in files.items():
        (directory / name).write_bytes(body)
    listed = sums if sums is not None else files
    (directory / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(body).hexdigest()}  {name}\n' for name, body in listed.items()))
    return directory


def test_the_context_is_the_dockerfile_and_either_no_package_or_exactly_the_checked_offline_packages(tmp_path):
    online = P.prepare_postgres(tmp_path / 'online', deb_dir='')
    assert sorted(p.relative_to(online).as_posix() for p in online.rglob('*')) == ['.dockerignore', 'Dockerfile', 'debs', 'debs/.keep']
    good = debs(tmp_path / 'debs', {'pgbackrest_2.56.0-1.pgdg120+1_amd64.deb': b'pkg', 'libssh2-1_1.10.0-3+b1_amd64.deb': b'dep'})
    offline = P.prepare_postgres(tmp_path / 'offline', deb_dir=good)
    assert sorted(p.name for p in (offline / 'debs').iterdir()) == ['SHA256SUMS', 'libssh2-1_1.10.0-3+b1_amd64.deb', 'pgbackrest_2.56.0-1.pgdg120+1_amd64.deb']
    tampered = debs(tmp_path / 'tampered', {'pgbackrest_x_amd64.deb': b'pkg'}, sums={'pgbackrest_x_amd64.deb': b'other'})
    unlisted = debs(tmp_path / 'unlisted', {'a.deb': b'a', 'b.deb': b'b'}, sums={'a.deb': b'a'})
    for bad, message in ((tampered, 'sha256 differs'), (unlisted, 'differ from SHA256SUMS')):
        with pytest.raises(ValueError, match=message):
            P.prepare_postgres(tmp_path / ('out-' + bad.name), deb_dir=bad)


# ---- package mirrors (online path) ------------------------------------------------------------------------------------------
DEB822 = """Types: deb
URIs: http://deb.debian.org/debian
Suites: bookworm bookworm-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://deb.debian.org/debian-security
Suites: bookworm-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
PGDG = 'deb [ signed-by=/usr/local/share/keyrings/postgres.gpg.asc ] http://apt.postgresql.org/pub/repos/apt/ bookworm-pgdg main 18\n'


def mirror_script(etc, **mirrors):
    """The Dockerfile's mirror step, run by sh against a temporary /etc/apt (the Dockerfile text itself, paths redirected)."""
    import subprocess
    run = DOCKERFILE[DOCKERFILE.index('        mirror() {'):DOCKERFILE.index('        apt-get update;')]
    body = '\n'.join(line.rstrip().removesuffix('\\').rstrip() for line in run.splitlines()).replace('/etc/apt', str(etc))
    env = {'PATH': '/usr/bin:/bin', **{k: v for k, v in mirrors.items()}}
    for name in ('APT_DEBIAN_MIRROR', 'APT_DEBIAN_SECURITY_MIRROR', 'APT_PGDG_MIRROR'):
        env.setdefault(name, '')
    return subprocess.run(['sh', '-c', 'set -eu\n' + body], env=env, capture_output=True, text=True)


@pytest.fixture
def apt(tmp_path):
    etc = tmp_path / 'apt'
    (etc / 'sources.list.d').mkdir(parents=True)
    (etc / 'sources.list.d' / 'debian.sources').write_text(DEB822)
    (etc / 'sources.list.d' / 'pgdg.list').write_text(PGDG)
    return etc


def test_mirrors_replace_only_the_host_and_path_and_keep_the_signing_keys_and_suites(apt):
    done = mirror_script(apt, APT_DEBIAN_MIRROR='https://mirrors.tuna.tsinghua.edu.cn/debian/',
                         APT_DEBIAN_SECURITY_MIRROR='https://mirrors.tuna.tsinghua.edu.cn/debian-security',
                         APT_PGDG_MIRROR='https://mirrors.tuna.tsinghua.edu.cn/postgresql/repos/apt')
    assert done.returncode == 0, done.stderr
    debian = (apt / 'sources.list.d' / 'debian.sources').read_text()
    assert debian == DEB822.replace('http://deb.debian.org/debian-security', 'https://mirrors.tuna.tsinghua.edu.cn/debian-security') \
                           .replace('http://deb.debian.org/debian\n', 'https://mirrors.tuna.tsinghua.edu.cn/debian\n')
    # The base URL is replaced as a whole (its trailing slash included; apt treats both forms the same).
    assert (apt / 'sources.list.d' / 'pgdg.list').read_text() == PGDG.replace('http://apt.postgresql.org/pub/repos/apt/', 'https://mirrors.tuna.tsinghua.edu.cn/postgresql/repos/apt')
    assert 'signed-by=/usr/local/share/keyrings/postgres.gpg.asc' in (apt / 'sources.list.d' / 'pgdg.list').read_text()


def test_without_mirrors_the_sources_are_untouched_and_a_bad_or_unmatched_mirror_fails(apt, tmp_path):
    assert mirror_script(apt).returncode == 0
    assert (apt / 'sources.list.d' / 'debian.sources').read_text() == DEB822 and (apt / 'sources.list.d' / 'pgdg.list').read_text() == PGDG
    assert mirror_script(apt, APT_PGDG_MIRROR='ftp://mirror.invalid/apt').returncode != 0
    assert mirror_script(apt, APT_PGDG_MIRROR='https://user:pass@mirror.invalid/apt').returncode != 0
    (apt / 'sources.list.d' / 'pgdg.list').unlink()
    unmatched = mirror_script(apt, APT_PGDG_MIRROR='https://mirror.invalid/apt')
    assert unmatched.returncode != 0 and 'no apt source matches' in unmatched.stderr


def test_the_release_build_passes_mirrors_only_from_the_build_host_environment_and_validates_them(tmp_path):
    docker = Docker()
    environ = {'NEXLOOP_APT_PGDG_MIRROR': 'https://mirrors.aliyun.com/postgresql/repos/apt/'}
    ci = tmp_path / 'ci.json'
    ci.write_text(json.dumps({'sha': head_sha(), 'status': 'passed', 'policy_version': 2, 'deadline_gate': 'passed', 'counts': {'passed': 1}}))
    release.build(head_sha(), ci, tmp_path / 'r', sequence=8123, only=['postgres'], root=ROOT, docker=docker, checkout_check=lambda r, s: None,
                  lock=pinned_lock(), environ=environ)
    built = next(c for c in docker.calls if c[0] == 'build')
    args = [built[i + 1] for i, a in enumerate(built) if a == '--build-arg']
    assert 'APT_PGDG_MIRROR=https://mirrors.aliyun.com/postgresql/repos/apt' in args and not any(a.startswith('APT_DEBIAN') for a in args)
    for bad in ('https://u:p@mirror.invalid/x', 'https://mirror.invalid/x?y=1', 'file:///srv/apt'):
        with pytest.raises(release.InputError, match='NEXLOOP_APT_PGDG_MIRROR'):
            release.build(head_sha(), ci, tmp_path / ('b' + str(len(bad))), sequence=8124 + len(bad), only=['postgres'], root=ROOT, docker=Docker(),
                          checkout_check=lambda r, s: None, lock=pinned_lock(), environ={'NEXLOOP_APT_PGDG_MIRROR': bad})

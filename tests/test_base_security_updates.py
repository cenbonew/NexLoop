"""NX-036 follow-up: the Debian fixes the upstream Node and pgvector bases still lack are exact "package=version" pins in
versions.lock.json, passed to the agent-host and postgres builds as one argument; the Dockerfiles upgrade only those packages
and refuse to finish unless each is installed at exactly its pinned version. The pin loop is run here against a stand-in
dpkg-query; no image is pulled or built."""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import release  # noqa: E402

LOCK = json.loads((ROOT / 'versions.lock.json').read_text())
CONFIG = json.loads((ROOT / 'deploy/release/images.v1.json').read_text())
HOST = (ROOT / 'deploy/community/Host.Dockerfile').read_text()
POSTGRES = (ROOT / 'deploy/postgres/Dockerfile').read_text()


@pytest.mark.parametrize('image,base', [('agent-host', 'node-host-base'), ('postgres', 'postgres')])
def test_each_image_takes_the_pins_of_its_own_base(image, base):
    path = CONFIG['images'][image]['security_args']['SECURITY_UPDATES']
    assert path == ['images', base, 'security_updates'] and CONFIG['images'][image]['base'] == base
    pins = LOCK['images'][base]['security_updates']
    assert release.pinned_package_list(LOCK, path) == ' '.join(pins)
    assert all(re.fullmatch(r'[a-z0-9.+-]+=[0-9][0-9A-Za-z.+~:_-]*', p) for p in pins)


def test_the_postgres_base_stays_on_postgresql_18_and_pgvector_0_8():
    postgres = LOCK['images']['postgres']
    assert postgres['postgres_version'].split('.')[0] == postgres['previous']['postgres_version'].split('.')[0] == '18'
    assert postgres['pgvector_version'].startswith('0.8.') and postgres['previous']['pgvector_version'].startswith('0.8.')
    assert re.fullmatch(r'docker\.io/pgvector/pgvector:0\.8\.\d+-pg18-bookworm', postgres['reference'])
    # the Node runtime is not changed by the security updates: still the locked Node the Host is built and tested with
    assert LOCK['images']['node-host-base']['reference'] == 'docker.io/library/node:24.13.0-bookworm-slim'


@pytest.mark.parametrize('value', [None, [], 'perl-base=5.36.0-7+deb12u4', ['perl-base'], ['perl-base=5.36.0-7+deb12u4', 'perl-base=5.36.0-7+deb12u5'],
                                   ['perl-base=5.36.0-7+deb12u4; rm -rf /'], ['perl-base=$(id)']])
def test_a_missing_or_malformed_pin_list_refuses_the_build(value):
    lock = copy.deepcopy(LOCK)
    lock['images']['postgres']['security_updates'] = value
    with pytest.raises(release.Refused, match='images.postgres.security_updates: not pinned'):
        release.pinned_package_list(lock, ['images', 'postgres', 'security_updates'])


def test_both_dockerfiles_upgrade_only_the_pins_and_check_each_one():
    for text in (HOST, POSTGRES):
        assert 'ARG SECURITY_UPDATES' in text and 'test -n "${SECURITY_UPDATES}"' in text
        assert 'apt-get install --yes --no-install-recommends --only-upgrade ${SECURITY_UPDATES}' in text
        assert 'apt-get upgrade' not in text and 'dist-upgrade' not in text
        assert 'for pin in ${SECURITY_UPDATES}; do' in text
    # the Host stage that is shipped is the Node stage, and the pins are applied there before anything else
    host_lines = [line for line in HOST.splitlines() if line and not line.startswith(('#', ' '))]
    assert host_lines[host_lines.index('FROM ${NODE_IMAGE}') + 1] == 'ARG SECURITY_UPDATES'
    assert 'dpkg --purge $leftovers' in POSTGRES


def pin_loop(text):
    body = text[text.index('    for pin in ${SECURITY_UPDATES}; do'):]
    body = body[:body.index('    done;') + len('    done;')]
    return body.replace('\\\n', '')


@pytest.mark.parametrize('installed,ok', [
    ({'perl-base': 'ii 5.36.0-7+deb12u4', 'libc6': 'ii 2.36-9+deb12u14'}, True),
    ({'perl-base': 'ii 5.36.0-7+deb12u3', 'libc6': 'ii 2.36-9+deb12u14'}, False),   # not upgraded
    ({'perl-base': 'ii 5.36.0-7+deb12u5', 'libc6': 'ii 2.36-9+deb12u14'}, False),   # not the pinned version either
    ({'perl-base': 'rc 5.36.0-7+deb12u4', 'libc6': 'ii 2.36-9+deb12u14'}, False),   # only configuration files left
    ({'libc6': 'ii 2.36-9+deb12u14'}, False),                                        # not installed at all
])
@pytest.mark.parametrize('dockerfile', [HOST, POSTGRES], ids=['host', 'postgres'])
def test_the_pin_loop_accepts_only_exactly_the_pinned_installed_versions(tmp_path, installed, ok, dockerfile):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    cases = ''.join(f'  {name}) printf %s "{state}";;\n' for name, state in installed.items())
    (bin_dir / 'dpkg-query').write_text(f'#!/bin/sh\nfor last; do :; done\ncase "$last" in\n{cases}  *) exit 1;;\nesac\n')
    (bin_dir / 'dpkg-query').chmod(0o755)
    script = 'set -eu\nSECURITY_UPDATES="perl-base=5.36.0-7+deb12u4 libc6=2.36-9+deb12u14"\n' + pin_loop(dockerfile) + '\necho checked\n'
    result = subprocess.run(['sh', '-c', script], env={'PATH': f'{bin_dir}:/usr/bin:/bin'}, capture_output=True, text=True)
    assert (result.returncode == 0 and result.stdout.strip() == 'checked') is ok, result.stderr
    if not ok:
        assert 'is not installed at' in result.stderr


@pytest.mark.parametrize('dockerfile', [HOST, POSTGRES], ids=['host', 'postgres'])
def test_the_run_steps_are_valid_shell(tmp_path, dockerfile):
    for block in re.findall(r'^RUN (.*?)(?<!\\)\n', dockerfile, re.S | re.M):
        assert subprocess.run(['sh', '-n', '-c', block.replace('\\\n', '')], capture_output=True).returncode == 0, block[:120]

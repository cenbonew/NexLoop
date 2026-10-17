"""NX-036 follow-up (CVE-2025-13836, fixed in CPython 3.12.13): the locked Python base is at or above the fixed patch
release, and both images that run this Python (backend, agent-host) fail their build on an older base."""
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
FLOOR=(3,12,13)
CHECK="python{} -c 'import sys; sys.exit(sys.version_info[:3] < (3, 12, 13))'"


def test_the_locked_python_base_is_at_or_above_the_fixed_patch_release():
    base=json.loads((ROOT/'versions.lock.json').read_text())['images']['python-build-base']
    match=re.fullmatch(r'docker\.io/library/python:(\d+)\.(\d+)\.(\d+)-slim-bookworm',base['reference'])
    assert match and tuple(map(int,match.groups()))>=FLOOR
    assert re.fullmatch('sha256:[0-9a-f]{64}',base['digest'])


def test_backend_and_host_builds_refuse_a_python_below_the_floor():
    backend=(ROOT/'deploy/community/Dockerfile').read_text()
    host=(ROOT/'deploy/community/Host.Dockerfile').read_text()
    # The check runs in a RUN step of the final stage, before anything is installed.
    assert 'RUN '+CHECK.format('')+' \\\n && python -m pip install' in backend
    assert 'COPY --from=python_runtime /usr/local/ /usr/local/' in host
    assert 'RUN ldconfig && '+CHECK.format('3')+' \\\n && useradd' in host

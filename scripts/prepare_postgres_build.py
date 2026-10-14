"""Prepare the Docker context of the nexloop/postgres release image (deploy/postgres/Dockerfile).

The context is the Dockerfile and a debs/ directory. It is empty unless NEXLOOP_PGBACKREST_DEB_DIR names a directory of
pre-downloaded packages (the offline path, docs/implementation/NX-034-postgres-image.md): then its *.deb files and SHA256SUMS
are copied after the same checks the build repeats (every listed file present and matching, nothing unlisted). No network."""
import hashlib
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SUMS = re.compile(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+~-]*\.deb)')


def check_debs(directory):
    """{file name: sha256} of a SHA256SUMS directory whose *.deb files are exactly the listed ones and all match."""
    directory = Path(directory)
    listed = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        match = _SUMS.fullmatch(line.strip())
        if not match or match.group(2) in listed:
            raise ValueError('SHA256SUMS: one "<sha256>  <name>.deb" line per package')
        listed[match.group(2)] = match.group(1)
    present = {p.name for p in directory.glob('*.deb')}
    if present != set(listed) or not listed:
        raise ValueError('the .deb files differ from SHA256SUMS')
    for name, digest in listed.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'{name}: sha256 differs from SHA256SUMS')
    return listed


def prepare_postgres(target, deb_dir=None):
    target = Path(target)
    if target.exists():
        raise ValueError('new build directory required')
    deb_dir = deb_dir or os.environ.get('NEXLOOP_PGBACKREST_DEB_DIR')
    (target / 'debs').mkdir(parents=True)
    shutil.copyfile(ROOT / 'deploy/postgres/Dockerfile', target / 'Dockerfile')
    (target / '.dockerignore').write_text('*\n!Dockerfile\n!debs\n')
    if deb_dir:
        for name in check_debs(deb_dir):
            shutil.copyfile(Path(deb_dir) / name, target / 'debs' / name)
        shutil.copyfile(Path(deb_dir) / 'SHA256SUMS', target / 'debs' / 'SHA256SUMS')
    else:
        (target / 'debs' / '.keep').write_text('')
    return target

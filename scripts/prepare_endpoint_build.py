"""Prepare the Docker context of the nexloop/reference-endpoint release image (apps/reference-endpoint/Dockerfile).

The context is the Dockerfile and reference_endpoint.py only (standard library, nothing installed). No network."""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('Dockerfile', 'reference_endpoint.py')


def prepare_endpoint(target):
    target = Path(target)
    if target.exists():
        raise ValueError('new build directory required')
    target.mkdir(parents=True)
    for name in FILES:
        shutil.copyfile(ROOT / 'apps/reference-endpoint' / name, target / name)
    (target / '.dockerignore').write_text('*\n' + ''.join(f'!{name}\n' for name in FILES))
    return target

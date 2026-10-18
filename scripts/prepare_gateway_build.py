"""Prepare the Docker context of the nexloop/gateway release image (deploy/gateway/Dockerfile): the Dockerfile and the Caddy
module (go.mod, go.sum, main.go). Nothing else; no network."""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('caddy/go.mod', 'caddy/go.sum', 'caddy/main.go')


def prepare_gateway(target):
    target = Path(target)
    if target.exists():
        raise ValueError('new build directory required')
    (target / 'caddy').mkdir(parents=True)
    shutil.copyfile(ROOT / 'deploy/gateway/Dockerfile', target / 'Dockerfile')
    for name in FILES:
        shutil.copyfile(ROOT / 'deploy/gateway' / name, target / name)
    (target / '.dockerignore').write_text('*\n!Dockerfile\n!caddy/go.mod\n!caddy/go.sum\n!caddy/main.go\n')
    return target

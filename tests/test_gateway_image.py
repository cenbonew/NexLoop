"""NX-036: the gateway (nexloop/gateway) is the locked official Caddy image with its caddy binary rebuilt from deploy/gateway/caddy
by the locked Go image, because the upstream Caddy 2.11.7 release binary carries fixed Critical findings (Go stdlib go1.26.8 and
golang.org/x/net v0.59.0). The module graph is fixed by the committed go.mod/go.sum; release.py passes both base images by digest
and the exact Go and Caddy versions from versions.lock.json. The docker CLI is a stand-in; nothing is pulled or built for real."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/release'))
import prepare_gateway_build as G  # noqa: E402
import release  # noqa: E402
from test_release_build_archive import Docker, ci_result  # noqa: E402,F401
from test_release_local import head_sha  # noqa: E402

from datetime import UTC, datetime  # noqa: E402

LOCK = json.loads((ROOT / 'versions.lock.json').read_text())
CONFIG = json.loads((ROOT / 'deploy/release/images.v1.json').read_text())
DOCKERFILE = (ROOT / 'deploy/gateway/Dockerfile').read_text()
GO_MOD = (ROOT / 'deploy/gateway/caddy/go.mod').read_text()
GO_SUM = (ROOT / 'deploy/gateway/caddy/go.sum').read_text()


def version(text):
    return tuple(int(x) for x in re.findall(r'\d+', text)[:3])


def test_the_lock_pins_a_go_and_x_net_at_or_above_the_fixed_versions_and_keeps_the_caddy_version():
    gateway, go = LOCK['images']['gateway'], LOCK['images']['go-build-base']
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', go['digest']) and go['reference'] == f"docker.io/library/golang:{go['go_version']}-alpine3.23"
    assert version(go['go_version']) >= (1, 26, 9)                          # GO-2026-6604, GO-2026-6612 (stdlib)
    assert gateway['reference'] == f"docker.io/library/caddy:{gateway['caddy_version']}"
    assert re.search(r'^go ' + re.escape(go['go_version']) + '$', GO_MOD, re.M)
    assert re.search(r'^require github.com/caddyserver/caddy/v2 v' + re.escape(gateway['caddy_version']) + '$', GO_MOD, re.M)
    xnet = re.search(r'^\tgolang.org/x/net (v[0-9.]+) // indirect$', GO_MOD, re.M).group(1)
    assert xnet == gateway['rebuild']['golang.org/x/net'] and version(xnet) >= (0, 60, 0)   # GO-2026-6612 (x/net)
    assert f'golang.org/x/net {xnet} h1:' in GO_SUM and f'golang.org/x/net {xnet}/go.mod h1:' in GO_SUM


def test_the_release_config_builds_the_gateway_from_both_locked_images_and_the_exact_versions():
    entry = CONFIG['images']['gateway']
    assert entry == {'kind': 'build', 'context': 'gateway', 'base': 'gateway', 'build_args': {'CADDY_IMAGE': 'gateway', 'GO_IMAGE': 'go-build-base'},
                     'tool_args': {'GO_VERSION': ['images', 'go-build-base', 'go_version'], 'CADDY_VERSION': ['images', 'gateway', 'caddy_version']},
                     'mirror_args': {'GOPROXY_MIRROR': 'NEXLOOP_GOPROXY_MIRROR'}}
    assert release.pinned_image_problems(ROOT) == []


def test_the_dockerfile_verifies_the_module_graph_and_the_result_and_ships_only_the_binary():
    lines = [line for line in DOCKERFILE.splitlines() if line and not line.startswith(('#', ' '))]
    assert lines[:4] == ['ARG GO_IMAGE', 'ARG CADDY_IMAGE', 'FROM ${GO_IMAGE} AS build', 'ARG GO_VERSION']
    assert [line for line in lines if line.startswith('FROM')] == ['FROM ${GO_IMAGE} AS build', 'FROM ${CADDY_IMAGE}']
    for check in ('GOFLAGS=-mod=readonly', 'GOTOOLCHAIN=local', 'go mod verify', 'test "$(go env GOVERSION)" = "go${GO_VERSION}"',
                  'go version -m /out/caddy | grep -q "^/out/caddy: go${GO_VERSION}\\$"', "setcap cap_net_bind_service=+ep /usr/bin/caddy",
                  'caddy version | grep -q "^v${CADDY_VERSION} "'):
        assert check in DOCKERFILE, check
    final = DOCKERFILE[DOCKERFILE.index('FROM ${CADDY_IMAGE}'):]
    assert re.findall(r'^COPY .*$', final, re.M) == ['COPY --from=build /out/caddy /usr/bin/caddy']
    assert 'apk add' not in final and 'curl' not in DOCKERFILE and 'wget' not in DOCKERFILE


def test_the_main_package_is_the_standard_distribution():
    main = (ROOT / 'deploy/gateway/caddy/main.go').read_text()
    assert '"github.com/caddyserver/caddy/v2/cmd"' in main and '_ "github.com/caddyserver/caddy/v2/modules/standard"' in main
    assert len(re.findall(r'^\s+(?:_ |caddycmd )?"[^"]+"$', main, re.M)) == 2   # no other module is compiled in


def test_the_context_is_the_dockerfile_and_the_module_only(tmp_path):
    out = G.prepare_gateway(tmp_path / 'ctx')
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()) == [
        '.dockerignore', 'Dockerfile', 'caddy/go.mod', 'caddy/go.sum', 'caddy/main.go']
    assert (out / 'caddy/go.sum').read_text() == GO_SUM


def test_the_release_build_passes_both_images_by_digest_and_the_versions(tmp_path, ci_result):
    docker = Docker()
    out = release.build(head_sha(), ci_result, tmp_path / 'releases-root', sequence=110, root=ROOT, docker=docker,
                        now=datetime(2026, 10, 11, tzinfo=UTC), checkout_check=lambda root, sha: None, only=['gateway'], environ={})
    built = next(c for c in docker.calls if c[0] == 'build')
    args = [built[i + 1] for i, a in enumerate(built) if a == '--build-arg']
    assert args == ['CADDY_IMAGE=docker.io/library/caddy@' + LOCK['images']['gateway']['digest'],
                    'GO_IMAGE=docker.io/library/golang@' + LOCK['images']['go-build-base']['digest'],
                    'CADDY_VERSION=' + LOCK['images']['gateway']['caddy_version'], 'GO_VERSION=' + LOCK['images']['go-build-base']['go_version']]
    assert '--pull=false' in built and not any(c[0] in ('tag', 'pull') for c in docker.calls)
    manifest = json.loads((Path(out['release_dir']) / 'manifest.json').read_text())
    (image,) = manifest['images']
    assert image['name'] == 'gateway' and image['base_digest'] == LOCK['images']['gateway']['digest']

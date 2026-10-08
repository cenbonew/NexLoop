# NX-009 private API Compose extension

The previous actual core Compose test was extended with the real FastAPI process. This remains an intermediate implementation step toward the full community stack; NX-009 and AT-051 remain incomplete.

## Actual changes

- `packages/eios-core/src/nexloop_eios/container_entrypoint.py`: service-owned explicit test-profile API entrypoint; HTTP checks use the Artifact ID/size/SHA256 from the actual governed write. Fixed local `HTTPConnection` avoids ambient proxies and redirects, bounds reads/timeouts, and closes sockets. No browser identity is fabricated.
- `deploy/community/compose.test.yaml`: API UID10001, readonly root/config, writable Artifact volume, dropped capabilities, healthcheck; check job shares the API namespace and waits for actual API liveness.
- `scripts/community_test.py`: requires the complete five-check HTTP result; prior core-only evidence cannot satisfy this launcher.
- `tests/test_community_container_contract.py`, `tests/test_community_launcher.py`: graph/privilege/shared-namespace checks and multiline-report missing/failed HTTP evidence negatives.
- Community README, task evidence and runtime report updated. No migration, version, upstream commit or image digest changed.

## Executed commands

```sh
uv run --frozen pytest -q tests/test_community_container_contract.py tests/test_community_launcher.py --tb=short
uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-api-runtime-20261008
uv run --frozen python scripts/community_test.py run --output .ci-results/community-api-runtime-20261008
uv run --frozen pytest -q tests/test_community_launcher.py tests/test_community_container_contract.py --tb=short
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

First focused checks: 13 passed; after adding missing/failed HTTP report negatives: 15 passed. Actual fresh-project launcher exited0 on the first run of this extension. Bootstrap/check jobs exited0; PG and API were healthy.

Read-only Docker inspection verified exact project/purpose labels, no published ports, internal network, UID10001 runtime jobs, no bootstrap-secret mount in API/check, no MODEL_ environment, and matching shared network namespace. Reading the owned API container's `/proc/net/tcp` confirmed the listener is `127.0.0.1:8000`, not a wildcard listener. Actual HTTP evidence proves liveness200, foundation ready while product readiness503, anonymous401, incorrect service credential401 and authorized Artifact content matching its recorded SHA256/size.

This loopback test API has no TLS/browser realm; no host-facing login, frontend, Worker, Host, Valkey, public gateway, Pi Run or real channel/model validation is claimed. The full community Compose and derived two-host configurations remain outstanding. Docker is available, so no external input is needed for independent implementation.

Full CI exited0:877 Python tests in196.70s (one existing Starlette warning),24 actual Pi SQLite tests,four frozen Pi builds and web/Host builds passed;zero critical skips. Dependency lock hashes match the preceding runtime evidence; versions.lock.json and all32 migration files were not edited. Planning validator69 static checks and publication scan826 paths/0 staged blobs/0 violations passed. The owned API/PG were stopped with exit0 after ownership dry-run; volumes/images/private inputs retained. No commit,push or deployment.

# NX-009 actual Valkey TLS/ACL Compose extension

This extends the real core/API Compose slice with Valkey8.1.10. The full community frontend/Host/Worker stack is still incomplete; NX-009 and AT-051 remain unaccepted.

## Files changed

- `packages/eios-core/src/nexloop_eios/cache_test_profile.py`: fresh-only root cache-file initializer; separate private server/client volumes, UID10001 ownership; TLS-verified fixed-endpoint RESP2 diagnostic, bounded replies, cache-only key prefix and expiry. No formal fact or authoritative queue is stored in Valkey.
- `container_entrypoint.py`: cache-bootstrap job and actual TLS/ACL checks alongside real PG/Artifact/API verification.
- `deploy/community/compose.test.yaml`: UID10001 Valkey, readonly server config/rootfs, no ports, internal network, no PG input; root cache initializer receives only cache materials. Check job receives client CA/credentials, never the server key.
- `scripts/community_test.py`: fresh random cache password, one-day test certificate, hashed-password least-privilege ACL, private input validation/TLS key pair verification and strict actual cache evidence requirement. Build context remains credential-free.
- Three test files cover fragmented RESP, SSLKEYLOGFILE isolation, cache material tamper/permissions, Compose mount boundaries and missing/failed cache evidence. README/planning/evidence/Valkey image lock updated.

## Commands actually run

```sh
docker info --format '{{.ServerVersion}}'
docker buildx imagetools inspect docker.io/valkey/valkey:8.1.10-alpine --raw
uv run --frozen pytest -q tests/test_cache_test_profile.py --tb=short
uv run --frozen pytest -q tests/test_cache_test_profile.py tests/test_community_launcher.py tests/test_community_container_contract.py --tb=short
uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-cache-runtime-20261008
uv run --frozen python scripts/community_test.py run --output .ci-results/community-cache-runtime-20261008
docker pull docker.io/valkey/valkey@sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507
uv run --frozen python .ci-results/fetch_valkey_oci.py
docker image load --platform linux/arm64 --input .ci-results/valkey-verified-arm64.oci.tar
uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-cache-runtime-20261008-retry
uv run --frozen python scripts/community_test.py run --output .ci-results/community-cache-runtime-20261008-retry
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

Additional read-only structured commands inspected exact project/purpose labels, image RepoDigests/architecture, private mount boundaries, binary version, network/ports and parsed the complete check report. Exact owned IDs were inspected before stopping the three running test services. All stopped exit0; volumes/images/private inputs were retained. No original production resource was read/changed.

## First failures and resolution

1. Direct registry client with ambient proxies disabled timed out. Docker buildx and daemon pull failed fetching the anonymous auth token with EOF. First Compose attempt exited1 before creating project containers. The separate pinned core image build succeeded.
2. Host HTTPS client using the existing network configuration reached Docker Hub tag metadata, auth and registry with full certificate verification. The raw registry index SHA256 exactly matched the official Hub tag digest.
3. OCI downloader refused an unexpected CloudFront redirect. Only after confirming its exact hostname in the official Docker allowlist was it added. No registry Authorization header was forwarded to the CDN; no token/signed download URL was persisted. A fresh download verified index, selected arm64 manifest, config and every layer SHA256/size. Standard `docker image load --platform linux/arm64` preserved the original multiarch index RepoDigest. No daemon/proxy/firewall/Keychain/TLS-verification setting changed. The helper and public OCI archive stay in ignored local results; this is explicit test image preloading, not a claimed fix to daemon networking or an amd64 runtime test.
4. Fragmented RESP bulk test initially failed1/passed1. A bounded full-payload read loop fixed it; the final focused run passed21. Exact NOAUTH/NOPERM/WRONGPASS denial checks were also enforced.
5. A second fresh project, containing the corrected wheel and new private credentials/certificate, passed the launcher on its first run. No existing database/config was migrated/reused.

## Real evidence and remaining work

Actual Valkey binary reports8.1.10. Ten checks passed: default user denied; ACL auth/PING/cache SET+GET success; foreign key and CONFIG denied; incorrect password denied; unknown CA and incorrect hostname rejected. Valkey is TLS-only (`port0`), default user off, cache key prefix constrained, persistence off, memory64mb with eviction. This is an expiring disposable probe, not business durability evidence.

Fresh PG revision0032, governed Artifact write/read, actual API authorized read/digest, liveness and auth negatives still passed. Product readiness remains false. Full CI exited0:883 Python tests in193.62s (one existing Starlette warning),24 Pi SQLite tests, four Pi source builds and frontend/Host builds, zero critical skips. No dependency/migration changes. `versions.lock.json` only replaces the unresolved Valkey entry with its actual official reference/index digest and scoped arm64 TLS/ACL runtime status.

Remaining independent work: full community frontend/login, Host, Worker and derived two-host configs, then the specified S2 governed instance/effect and Pi Run recovery chain. Real model/channel validations remain separate. No commit, push or deployment.

References: [Valkey official image](https://hub.docker.com/r/valkey/valkey/), [Valkey TLS](https://valkey.io/topics/encryption/), [Valkey ACL](https://valkey.io/topics/acl/), [Docker image load](https://docs.docker.com/reference/cli/docker/image/load/), [Docker Desktop allowlist](https://docs.docker.com/desktop/enterprise/allow-list/).

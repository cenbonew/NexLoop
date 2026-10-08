# NX-009/NX-011 canonical test browser + frontend HTTPS

The foundation Compose now includes packaged React assets and an actual same-origin HTTPS login backed by canonical EIOS Human/session models and protected PostgreSQL ports. The identity is explicitly synthetic and has no business Action grants. Service-authorized Artifact operations remain separate; a Human cookie cannot stand in for service authority.

## Changes

- `browser_test_profile.py`: trusted fresh-only IAM configuration after exact core catalog/session/endpoint verification. Typed Subject/membership/LocalAccount, Argon2 hash, dedicated restricted identity-role password and rate policy; no ontology/business write or Action grant. Existing identity tables or foreign/nonempty volumes are refused. Private server DSN/rate key/TLS and client test-login/CA files are separated.
- `container_entrypoint.py`: browser-bootstrap, real HTTPS API with fixed private test realm and packaged frontend; actual HTTPS static/login/cookie/session/CSRF/logout checks.
- `prepare_community_build.py`/Dockerfile: compile/copy only public HTML/JS/CSS, core wheel and frozen requirements. Programmatic Vite uses configFile:false/envDir:false and removes MODEL_/VITE_/COMPOSE_ build variables. Repository .env or private deployment material is not a build input.
- `community_test.py`: private random test password, TLS cert/key, fixed localhost origin/port, content/mode/key-pair/context verification and strict actual browser evidence; removes environment overrides of explicit Compose references.
- Compose: four initializers, API/Host/PG/Valkey/check, separate browser-server/client volumes, UID10001 runtime, API TLS8443 published only at host127.0.0.1. Data remains on internal core_test; only API attaches to local_web entry bridge. Host shares API namespace but receives no identity/browser material.
- Native real-PG bootstrap/login tests plus graph/tamper/missing-or-failed-report tests and README/planning evidence. No migration, dependency or versions.lock change.

## Actual commands

```sh
uv run --frozen pytest -q tests/test_browser_test_profile.py --tb=short
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen pytest -q tests/test_browser_test_profile.py tests/test_community_launcher.py tests/test_community_container_contract.py --tb=short
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-browser-runtime-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py run --output .ci-results/community-browser-runtime-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-browser-runtime-20261008-retry
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py run --output .ci-results/community-browser-runtime-20261008-retry
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-browser-public-runtime-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py run --output .ci-results/community-browser-public-runtime-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

Additional structured Python commands used certificate-verified HTTPX on the actual host loopback origin, parsed Docker ownership/mount/port/network/job metadata and verified no generated password in logs. No password, cookie/CSRF token or model credential was printed.

## First failures and retries

Native real-PG tests first passed4. Integration tests initially failed3/passed24: a text insertion matched a nested api-test dependency instead of the root service; Compose parse/config validation denied the first launch before any container creation. Root service matching was corrected;27 focused tests passed. Private manifest graph drift remains refused; no existing manifest was rewritten to hide the error.

The second fresh project passed12 actual container HTTPS/frontend/login checks plus PG/Artifact/cache/Host checks, but host HTTPX got ConnectionRefused on the published port. An internal-only Docker network was insufficient for the host entry. A separate API-only entry bridge was added while retaining internal-only data/Valkey/bootstrap networks and loopback-only host publication. The third fresh project passed both container checks and actual host verified HTTPS HTML/liveness/login/session/logout; Human-cookie service access was401. The internal-only project was stopped by exact ownership checks; volumes retained.

Full CI exited0:893 Python tests in224.48s (one existing Starlette warning),24 actual Pi SQLite tests,four frozen Pi builds and frontend/Host builds,zero critical skips. The final network correction occurred during this CI; final29 focused tests in33.93s subsequently verified the current graph and implementation, followed by the fresh actual Compose/host HTTPS evidence. Do not interpret the earlier broad CI alone as host-entry proof.

After recording evidence, exact ownership dry-run preceded stopping the final project's Host/API/Valkey/PG. All stopped exit0, private IAM/TLS/runtime/data volumes and images retained. No persistent-environment migration, original production change, commit/push/deployment.

## Scope limits

Current assets/HTTP authentication were verified on Compose; no new DOM/mobile/fidelity acceptance is claimed here (the earlier native browser evidence remains separate). Product readiness is503. Its missing_capabilities label still lists browser_session despite this verified login, and must be corrected before final capability reporting. Worker/full community/two-host release and S2 service/agent governed writes, Pi Run recovery/leases and real effects are incomplete. NX-009/NX-011 and AT-051 remain unpromoted. Real-model/channel calls were not executed. Test certificate lifetime is one day, not stage PKI.

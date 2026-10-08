# HTTP foundation for NX-011

Implemented nexloop_eios/http_api.py, the nexloop-api console entrypoint and Backend.foundation_readiness. The localhost-only explicit --mode test server owns the real backend via FastAPI lifespan and closes it at shutdown. It accepts only private DSN/signing-key files; does not migrate/provision or read .env/model credentials. Liveness reflects process availability. Readiness rechecks actual restricted PG/catalog, active signer and opened Artifact directory identity/mode; overall API readiness remains 503 because browser-session and Host dispatch capabilities are not yet implemented. Core readiness is never substituted for API/product readiness.

GET /api/v1/artifacts/{id} authenticates a real EIOS service token in the server-fixed test world and calls the existing authorized application service. No tenant/world/subject is accepted from client parameters and no direct business SQL is introduced. Response is private, no-store, download-only; missing/denied resources share opaque errors. Backend/storage unavailability returns retryable 503. No fake login, public static Artifact mount or pretend Host exists.

Added FastAPI/uvicorn runtime dependencies and httpx development test dependency to uv.lock. versions.lock.json records resolved FastAPI/uvicorn versions; source commits, OCI manifest-only status and bootstrap revision 0018 are unchanged. Installed wheel checks now require all five console entrypoints.

Actual commands/tests:

- `uv add --package nexloop-eios-core 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1'`; `uv add --group dev 'httpx>=0.28,<1'`: completed after transient download retries; frozen lock updated.
- First HTTP test: 1 passed/1 failed because the fixture used artifact_signing_key instead of actual artifact_key. Corrected to actual profile files.
- Expanded HTTP test: 4 passed/1 failed because the uv wrapper reports SIGTERM as 143. Replaced wrapper with direct owned Python subprocess and verify actual endpoint disappearance/PG connection closure, allowing normal SIGTERM termination.
- `uv run --frozen pytest -xq tests/test_http_api.py tests/test_wheel_install.py --tb=short`: 7 passed in 10.13s. Final closed-backend negative and dependency error mapping were then added and are covered by the full CI run.
- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`: authoritative final result is http-foundation-ci.json.

Real isolated PG evidence: backend/key revocation reflected on successive readiness requests; unauthorized/revoked service requests denied; authorized Artifact HTTP read returns actual stored bytes; directory mode drift fails readiness. A real uvicorn process bound to a temporary loopback port is probed and its owned shutdown verified; TestClient-only checks are not substituted for TCP evidence.

Starlette currently warns that httpx-based TestClient will be deprecated; tests actually execute, with no skip. FastAPI lifespan follows its [official documentation](https://fastapi.tiangolo.com/advanced/events/).

NX-009 container execution remains blocked by Docker daemon unavailability. NX-011 is partial independent work while that validation is blocked: same-origin EIOS browser sessions/CSRF, frontend and Node Host/run-bound internal auth are still required. Pi Run recovery, real-effect Action and S2 AT cases are not accepted. No LAN exposure, real model/channel call, server mutation, persistent migration, staging, commit, push or deployment.

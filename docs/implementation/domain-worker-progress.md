# Restricted Domain Worker progress

NX-009 / NX-011 remain in_progress. No S2 acceptance promotion.

The new `nexloop_eios.domain_worker` is an actual bounded maintenance process,
not a placeholder daemon. It takes private service-owned DSN, signing key and
service credential files, explicit world and bounded age/page parameters. It
invokes the existing governed final-orphan Artifact port; `nexloop_domain_worker`
role and live EIOS DELETE authority are rechecked by the underlying Python and
PostgreSQL dispatch. It does not provision roles, run migrations, directly write
business tables, or fall back to Memory. Errors return only a generic status;
credentials and per-file metadata are absent from process output.

Each invocation finishes at most one durable page. `--resume <sweep_id>` replays
an already persisted plan idempotently. Registered artifacts remain protected.
This is a foundation for the Scheduler, not a completed leased job queue:
automatic discovery of abandoned sweeps, scheduler ownership/leases, independent
Compose worker secret provisioning and the Compose Worker service are still
pending. In particular, an abrupt first invocation that never returns its sweep
ID requires subsequent durable discovery, which this entrypoint does not claim.

API readiness now performs a restricted canonical session read on every probe.
An available browser session service removes the stale `browser_session` missing
capability; absent or failed service retains it. Product readiness remains 503
until real Host Run dispatch/recovery is implemented.

Validation command:

```sh
uv run pytest -q tests/test_domain_worker.py tests/test_http_api.py tests/test_browser_test_profile.py tests/test_final_artifact_orphans.py
```

First run: 23 passed in 19.89s; no failed case or rerun. Four new real-PG/process
cases cover actual cleanup, existing-plan repeat recovery, API-role denial and
credential revocation denial. Existing actual SIGKILL orphan recovery cases also
passed. One existing Starlette TestClient deprecation warning remains.

`versions.lock.json`, dependency locks and all 32 migration SQL files unchanged.
No production system, existing database, Git index, commit or public push changed.

## Explicit invocation contract

For an independently provisioned restricted maintenance identity and private
files (the existing API credential is deliberately insufficient):

```sh
uv run python -m nexloop_eios.domain_worker \
  --database-url-file /private/worker/maintenance_dsn \
  --signing-key-file /private/worker/artifact_key \
  --service-credential-file /private/worker/service_credential \
  --artifact-root /var/lib/nexloop/artifacts \
  --signing-key-id compose-test-v1 --world test \
  --minimum-age-seconds 3600 --limit 25
```

These paths describe the entrypoint contract, not deployed files or a completed
Compose worker. Resume adds `--resume <persisted_sweep_id>`; normal paging adds
`--after <next_after>`. The age must be at least60 seconds and page size1–50.
Maintenance authority must be confined to `eios:artifact:orphans_test` for the test
world. Production grants or credentials must not be copied into this profile.

Final browser-dependency failure test:
`uv run pytest -q tests/test_browser_test_profile.py` passed4 in3.57s. After
closing the actual identity pool, the next readiness probe marks the browser
unavailable again. This final assertion was added while the broad CI was running;
its dedicated final run is the authoritative check of that assertion.

Full local CI: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`
exited0:897 Python tests in226.13s,24 actual Pi SQLite tests,four frozen Pi
package builds and frontend/Host builds;zero critical skips. No first failure or
retry. This does not claim actual Pi Run kill/reopen or new Compose Worker proof.

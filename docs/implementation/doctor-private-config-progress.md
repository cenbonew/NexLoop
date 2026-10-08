# Read-only doctor with private DSN files

NX-006 startup evidence and preparation for NX-009. Community Compose/clean installation and complete S1/S2 gates remain incomplete.

## Actual behavior

nexloop-doctor now accepts --database-url-file and the stage-compatible DATABASE_URL_FILE environment path. Precedence is explicit flag, then nonempty file environment setting, then legacy NEXLOOP_DATABASE_URL only when no file is selected. A selected missing/unreadable/public/nonregular/invalid file fails configuration and leaves database_url empty; it never falls back to another environment DSN or the platform default database. The report exposes only the source category and boolean configuration check, never the private path/value/exception text.

The shared private_configuration.py contains the already tested bounded, owner-only O_NOFOLLOW/O_NONBLOCK file reader, previously embedded in artifact_smoke.py. Smoke reexports/imports that function for compatibility and keeps the same behavior. The file loader does not alter modes, generate files or mutate configuration. .env.example adds only a blank DATABASE_URL_FILE entry; the real ignored .env is untouched.

--mode test makes doctor use only explicit test model configuration, bypassing model credential/provider values in the inherited environment. Doctor performs no model request. Without the flag, existing configured-profile diagnosis remains available. Doctor still opens a read-only PG transaction, validates the restricted role/exact catalog and opens only an already-existing private Artifact directory. It never creates/chmods the root, bootstraps/migrates the DB or claims product_ready.

## Current operator command

```sh
uv run --frozen nexloop-doctor --mode test \
  --database-url-file "${PRIVATE_CONFIG_DIR:?}/test_api_dsn" \
  --artifact-root "${PRIVATE_DATA_ROOT:?}/artifacts"
```

This requires an already provisioned isolated NexLoop DB and service-owned private files/directory. It does not replace NX-009 authority provisioning/Compose. The earlier artifact-smoke checkpoint described the then-environment-only doctor; this file records its updated file interface. The environment-only interface remains supported when no file is selected.

## Evidence and commands

A real subprocess test configures a private nexloop_api DSN file, an administrative legacy environment DSN and a wrong file environment path simultaneously. The explicit file wins, restricted real PG checks pass, the Artifact directory stays empty and the report omits DSN and synthetic model key. An invalid configured-file case with a nonempty legacy DSN proves zero psycopg.connect calls, failed configuration/PG checks and a sanitized exit-1 report. The existing missing-DSN test was strengthened with the same call-count assertion: an exception thrown by a spy could otherwise be swallowed by diagnose's safe failure boundary.

`uv run --frozen pytest -xq tests/test_doctor.py tests/test_artifact_smoke.py --tb=short`: 14 passed in 5.19 seconds. After strengthening the spies, rerun of the same command: 14 passed in 5.14 seconds. No failure or skip occurred in this step.

Full CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Final input/JUnit hashes and results are recorded in doctor-private-config-ci.json.

Changed: private_configuration.py (shared existing file reader), artifact_smoke.py (shared import), doctor.py (file precedence/test mode/report), tests/test_doctor.py, .env.example (blank path), README usage, this report/CI evidence, planning evidence and public change inventory. versions.lock.json, dependency locks, frozen upstreams and all SQL migrations remain unchanged at 0014. Image digests remain unresolved.

Complete AT-051/clean community Compose, identity/key provisioning, runtime-owner Artifact lifecycle, full orphan worker, HTTP/Host, actual Pi Run kill/reopen, effects/outbox and complete S2 acceptance remain unfinished. No acceptance scenario is promoted based on these module tests. No real credential/model call, production/original-checkout mutation, persistent migration, Git staging/commit/push occurred.

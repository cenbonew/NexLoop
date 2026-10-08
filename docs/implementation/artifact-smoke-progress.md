# Explicit test-world Artifact smoke CLI

NX-006/NX-007 independent-startup evidence and preparation for NX-009; full community Compose/clean installation remain incomplete.

## Implemented runnable entry points

The installed `nexloop-artifact-smoke` CLI requires `--mode test` and file paths for an existing restricted DSN, service credential and raw 32-byte signing key. It opens the composed backend, verifies the exact PG catalog/restricted role/active signer, authenticates specifically in the test world, creates a small fixed diagnostic Artifact through the actual signed EIOS/PG metadata and filesystem fsync path, and reads it back through actual authorization. It returns a bounded public JSON report and exit 0 only when those actual checks succeed. Failures return exit 1 with a generic message and explicit partial checks, never DSN, service token, raw key or exception text.

The diagnostic intent is a fresh opaque ID per invocation, not a business-intent retry API. Artifacts have a five-minute retention deadline and may be collected by the authorized cleanup service after expiry; the smoke does not silently delete or migrate anything. It is an explicit small write, unlike the read-only doctor. No model call/config/key or embedding is read; model_validation is not_run and product_ready is always false.

Private text configuration must be a bounded nonempty UTF-8 regular file, owned by the service and inaccessible to group/other. O_NOFOLLOW/O_NONBLOCK reject symlink/FIFO inputs without a blocking read. Secret values are not CLI arguments or environment substitutions. The existing signing-file checks remain mandatory. Console entry points are declared in the core package: nexloop-artifact-smoke and nexloop-doctor. Both --help commands actually executed successfully after the editable package rebuild.

## Example with already provisioned isolated test configuration

```sh
uv run --frozen nexloop-doctor --artifact-root "${PRIVATE_DATA_ROOT:?}/artifacts"
uv run --frozen nexloop-artifact-smoke --mode test \
  --database-url-file "${PRIVATE_CONFIG_DIR:?}/test_api_dsn" \
  --service-credential-file "$PRIVATE_CONFIG_DIR/test_service_credential" \
  --signing-key-file "$PRIVATE_CONFIG_DIR/artifact_signing_key" \
  --signing-key-id configured-key-id \
  --artifact-root "$PRIVATE_DATA_ROOT/artifacts"
```

Doctor also requires NEXLOOP_DATABASE_URL through its existing trusted environment interface; the smoke uses its explicit file flags instead. These commands do not bootstrap/configure a blank DB, invent service authority or replace backup requirements. Product-grade authority provisioning and community Compose remain to be implemented. The example is configuration-dependent, not a claimed executed deployment.

## Real commands, first failure and tests

First `uv run --frozen pytest -xq tests/test_artifact_smoke.py --tb=short` did not reach tests: it raced with a second uv command rebuilding/installing the same editable core package for console help, and uv exited 2 because an already-removed dist-info directory was missing. Both sessions were observed terminal. Corrected execution order with `uv sync --frozen`, then the same targeted test command: 7 passed in 1.18 seconds. No test-specific environment deletion/reset was used.

Added actual real-world-only credential rejection. Targeted rerun of the same command: 8 passed in 2.14 seconds. The subprocess tests use fresh isolated real PG with a restricted nexloop_api DSN, synthetic service credentials and an actually provisioned synthetic signer. Positive test-world smoke persists exactly one available test-world Artifact; real-only credentials fail authentication before any Artifact metadata write. stdout/stderr are checked for absence of token, DSN and signing bytes. Remaining cases cover absent configuration and public/symlink/FIFO/oversized/empty secret files. Admin access provisions synthetic authority/configuration and reads evidence; actual smoke writes use the restricted backend.

Console commands actually run: `uv run --frozen nexloop-artifact-smoke --help` and `uv run --frozen nexloop-doctor --help`.

Full CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`; final result/input/JUnit hashes are in artifact-smoke-ci.json.

## Files, locks and incomplete gates

Changed: packages/eios-core/src/nexloop_eios/artifact_smoke.py, packages/eios-core/pyproject.toml console entries, tests/test_artifact_smoke.py, this report/CI evidence, planning evidence and public change inventory. versions.lock.json and all migrations remain unchanged at 0014; uv dependency lock has no new dependencies. Frozen upstream/package mappings and unresolved image digests remain unchanged.

This does not pass complete AT-051 independent clean install or NX-009 Compose. Existing persisted identity/key/schema provisioning is a prerequisite. Runtime-owner Artifact lifecycle/full orphan worker, HTTP/Host, actual Pi Run kill/reopen, effects/outbox and complete S2 acceptance remain incomplete. No model validation or actual deployment is claimed. No production DB/original checkout modified, real credential read/called, persistent migration, Git staging/commit/push occurred.

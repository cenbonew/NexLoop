# Executable owned core test sandbox

NX-006/NX-007/NX-008 independent-startup evidence; full community Compose and S1/S2 gates remain incomplete.

## Actual one-command flow

`nexloop-core-sandbox --mode test --pg-bin <installed PostgreSQL bin directory>` now creates a fresh private mkdtemp PGDATA/socket directory, runs initdb, starts its own PostgreSQL listening only on that private Unix socket, bootstraps the actual NexLoop migration catalog, provisions a test-world service identity and private signer, then runs actual doctor and Artifact write/read smoke. It stops only the PostgreSQL belonging to that exact new data directory. Authoritative pg_ctl status must confirm the server is stopped before guarded removal of the generated directory. An unconfirmed shutdown/cleanup forces failure; it does not claim a completed sandbox. Initialization failures retain uncertain private state rather than deleting it blindly.

No existing DSN/data directory is accepted. The only configuration input is an explicit PG binary directory and the required test mode. No production environment/model key/channel credential/TOS/GitHub/other server is read or called. SQL parameter logging is disabled on the owned server. Bootstrap/configuration use the fresh test-cluster nexloop_bootstrap role; actual Artifact mutations use restricted nexloop_api through the real EIOS fact resolver, signed permit and protected functions. The returned JSON has no raw DSN/key/token or private configuration path. product_ready is false and model_validation is not_run.

Sandbox profile provisioning persists genuine typed service/API-key authentication, app ceilings, actor/membership/grants, READ/CREATE/DELETE scope/control/policy and revision facts for eios:artifact:local_test only. This configuration recipe is adapted from the project's synthetic authority test setup into a standalone product test-profile module; it imports no tests and does not replace runtime decisions with Memory allow results. Only the literal test world is published. The publisher requires the exact bootstrap session/current role and empty identity/business state, refuses to overwrite existing identities, and has no real-world/user/agent provisioning endpoint. No human identity is manufactured. Runtime resolves the newly persisted records from PostgreSQL normally.

Private DSN/credential/key files are generated atomically with mode 0600 under the newly private root and are removed only after owned-server shutdown. No persistent migration or backup exception is introduced. This is an ephemeral component diagnostic, not a long-running system, workflow Harness or alternate Pi implementation.

## Executed commands and evidence

`uv sync --frozen` then `uv run --frozen pytest -xq tests/test_core_sandbox.py --tb=short`: 3 passed in 1.97 seconds. Cases exercise the actual fresh-PG subprocess flow, physical Artifact smoke and confirmed shutdown/cleanup; profile overwrite refusal with unchanged credential/fact counts; and missing binaries with zero directory creation calls. No test failure or skip occurred in this step.

The non-editable wheel test now verifies three installed console --help commands and actually runs the new sandbox binary from its separate venv/non-workspace cwd, with PYTHONPATH/PYTHONHOME removed. That installed command creates another fresh private PG instance and completes/retire its actual test loop. The previous installed-wheel bootstrap and restricted Artifact tests remain active. Current wheel/runtime results are in core-sandbox-ci.json and ignored .ci-results/wheel-install.json.

Full local CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Final result/input/JUnit hashes and the tested/final rebuilt wheel hash are recorded in core-sandbox-ci.json.

## Usage on the verified local PG installation

```sh
uv run --frozen nexloop-core-sandbox --mode test \
  --pg-bin /opt/homebrew/opt/postgresql@18/bin
```

The actual automated subprocess used that installed bin directory (or NEXLOOP_TEST_PG_BIN on another test machine). Missing binaries fail rather than selecting an existing system database or skipping. The explicit directory is configurable; it is not a server/credential default.

Changed: sandbox_profile.py, core_sandbox.py, core pyproject console entry, tests/test_core_sandbox.py, expanded non-editable install test, README usage, this report/CI evidence, planning evidence and public change inventory. versions.lock.json, dependency locks and all prior SQL migrations remain unchanged at 0014; upstream/package mappings and unresolved image digests are unchanged.

Full community Compose/service lifetime, real identity provisioning, runtime-owner Artifact/full orphan lifecycle, HTTP/Host, actual Pi Run kill/reopen, queues/outbox, real effects and complete S2 acceptance remain incomplete. AT-051 is not promoted from this ephemeral component scope to full community installation acceptance. No deployment or real model validation is claimed. No production DB/original checkout was modified or Git staging/commit/push performed.

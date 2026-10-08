# Standalone Artifact retention checkpoint

Scope: NX-007/NX-006 implementation progress, not completed S1/S2 acceptance. The retained frozen service authorization algorithm is unchanged. No Pi Run terminal-owner GC is claimed.

## Actual implementation

New independent migration `0006_local_artifact_retention.sql` provides narrow DELETE-permit-based claim, lock and finish functions. `postgres_artifacts.py` adds `claim_deletion`, `deletion_transaction` and `LocalArtifactService.delete_expired`. New claims consume a freshly signed actual EIOS DELETE decision; the physical-deletion transaction revalidates current credentials, tenant revision and all authority facts, and keeps those locks through hash verification, unlink, directory fsync and the PostgreSQL tombstone.

Only completed standalone uploads (`available`) past their database-stored retention deadline may enter `deleting`; pending uploads are protected. Runtime invocation references and any job JSON containing the opaque Artifact ID conservatively block collection, including terminal jobs. This intentionally does not interpret owner termination or silently remove bindings. Short SHARE table locks on invocation/job tables prevent new references during the physical deletion transaction. This conservative locking is a temporary throughput limitation until explicit lifecycle bindings and row-level coordination are integrated. Metadata and authority tables remain unavailable for raw application writes.

A committed deletion claim hides the Artifact from normal reads. Physical removal and PostgreSQL commit are not atomic: failure after unlink leaves a durable `deleting` row. After lease expiry a new worker advances the fence, verifies missing-file idempotency and commits `deleted`. The old fence is rejected. Deleted identities remain tombstones and cannot be reused as uploads. No final file without metadata is automatically inferred to be an orphan.

The trusted file coordinator holds the database transaction only across bounded local file verification and fsync (16 MiB upload bound). Unknown filesystem errors fail rather than pretending deletion succeeded. App/model callers never receive signing-key material.

## Tests and evidence

Actual disposable PostgreSQL and restricted application roles exercise expired deletion/tombstone/idempotency, future retention refusal, revoke-after-claim denial before unlink, fsync/unlink followed by transaction failure and new fenced-owner recovery, unfinished-upload protection, and a real second connection's Runtime writer lock refusal. The writer probe takes a table lock only, and creates no business data directly.

Initial baseline command `uv run --frozen pytest -xq tests/test_bootstrap.py tests/test_db_boundary.py tests/test_postgres_artifacts.py --tb=short`: 18 passed in 13.33s. First retention suite: 16 passed in 14.59s. Review then added reference-table locks and the two additional cases; current-input validation follows below. There were no failing tests in those two initial runs; their evidence predates the last locking change.

Changed files: new migration 0006 and catalog hash; postgres_artifacts.py; tests/test_postgres_artifacts.py; bootstrap/role tests expecting head 0006; versions.lock.json; this report and planning evidence. Earlier published-input migrations 0001–0005 remain unchanged in this checkpoint. versions.lock.json head 0005 → 0006, upstream commits unchanged, six image digests still empty/unresolved.

## Remaining work

Runtime-bound terminal-owner proofs, reference attachment/detachment, orphan reconciliation, lifecycle audit/outbox and complete bootstrap assembly remain required before NX-007 is done. This checkpoint supplies standalone upload retention only. Compose/doctor/API/Host, actual Pi Run recovery, governed business Actions and S2 failure scenarios remain incomplete. No production migration, staging, commit, push or deployment occurred. No real model/channel call is claimed.

Current-input targeted rerun: 18 Artifact tests passed in 16.12s. Full `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check` exited 0: 252 Python cases in 31.85s, 24 SQLite FULL cases, four Pi source builds; zero failures/errors/skips. Tested inputs and JUnit hashes recorded in artifact-retention-ci.json. Verified migrations 0001–0005 unchanged against previous CI input hashes. No test failure occurred in this checkpoint.

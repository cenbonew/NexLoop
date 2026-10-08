# Governed temporary Artifact cleanup

NX-007 progress. This implements the maintenance path for abandoned temporary files, not complete final-blob orphan reconciliation or runtime-owner retention. NX-007 remains in_progress and no AT is promoted.

## Runtime behavior

Authenticated Backend.collect_temporary_artifact_files / LocalArtifactService.collect_temporary_files accept an aware past cutoff (at least 60 seconds old), a page limit of 1..100, and an opaque .tmp-hex cursor. The service derives tenant/world from the authenticated session. Actual EIOS DELETE for the local Artifact namespace issues a signed short-lived permit. New protected SQL 0017 grants its guard exclusively to nexloop_domain_worker; API roles cannot execute maintenance even if their service holds DELETE. Read-only identities deny through the actual frozen fact resolver.

The PostgreSQL authority/permit transaction remains open across the directory lock, physical unlink and directory fsync. The guard rechecks credential, world/tenant, full fact epoch, payload binding and PG expiry after acquiring the directory flock and before each candidate. Rechecking the same consumed permit is permitted only for the exact bound cleanup payload and still asserts live authority. It cannot change cursor, cutoff or namespace. The first use is recorded in the existing permit ledger; rollback on failure does not pretend filesystem changes roll back.

Physical discovery uses a bounded-memory heap page of matching .tmp-[32 hex] names. Final blob IDs and unrelated names never become candidates, irrespective of age or metadata state. Recent files, symlinks and non-regular/non-owner files stay intact. Exclusive directory flock serializes this cleanup with active physical uploads. File paths are directory-FD relative and nofollow metadata checks prevent namespace escape. If a later guard/error stops a partially processed page, a finally block fsyncs any preceding unlink; retrying a page safely skips absent files. The result includes removed/examined counts, next_after and exhausted. Reset the cursor for subsequent sweeps so earlier names created later can be considered. No scheduler is installed in this checkpoint.

The low-level filesystem primitive is trusted infrastructure, just like existing put/remove primitives. Its unit callback is not an authorization replacement for the runtime service. Product entry uses only actual PG/EIOS proof; direct filesystem methods are not exposed as a runtime tool. Namespace cleanup is maintenance authority and does not infer ownership or liveness of unregistered final blobs. Those require separate metadata/owner reconciliation; this implementation never deletes them.

## Commands and evidence

`uv run --frozen pytest -xq tests/test_temporary_artifact_cleanup.py --tb=short` initially stopped at a read-only negative case (2 passed/1 failed): actual missing DELETE fact resolution returned AuthorizationUnavailable, while the test expected ArtifactAccessDenied. An immediate broader retry observed the same incorrect exception expectation (2 passed/1 failed). Updated the assertion to accept both authoritative fail-closed paths; no runtime permission was widened.

`uv run --frozen pytest -xq tests/test_temporary_artifact_cleanup.py tests/test_local_artifacts.py tests/test_bootstrap.py --tb=short`: 40 passed in 6.35s, then 41 passed in 7.01s after adding the partial-failure/fsync/retry case. Real PG cases cover worker-only maintenance, bounded pages, repeat sweeps, final/recent/symlink preservation, READ-only denial, signed-permit-to-SQL revocation race, namespace separation and invalid input with zero permit/file effects. The partial failure test explicitly exercises the physical primitive with a synthetic callback, not real PG permit expiry or runtime-owner lifecycle acceptance.

Strict local CI: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Final result/source/JUnit hashes and installed/rebuilt wheel evidence are in temporary-artifact-cleanup-ci.json.

Changed: local_artifacts.py bounded maintenance page/guard/finally fsync; postgres_artifacts.py PG guard coordinator; backend.py authenticated maintenance facade; new 0017 SQL/catalog; seven cleanup cases and bootstrap/doctor/sandbox/wheel revision expectations; versions.lock.json bootstrap 0016 -> 0017; reports and live planning evidence. Earlier SQL bytes, dependency locks and upstream/Pi mappings are unchanged; image digests remain unresolved.

No original production system/DB/source was changed, no persistent migration/deployment or real model/channel call was made, and no Git staging/commit/push occurred. Full final-orphan/owner retention, Compose/HTTP Host and S2 durable Runtime/effect gates remain incomplete. These are independent implementation work, not external input blockers.

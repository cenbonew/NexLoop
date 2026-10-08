# Expired pending Artifact cleanup and upload fencing

NX-007/NX-008 remain in_progress. Owner-bound retention, unregistered-file reconciliation and automatic scanning are not complete.

## Actual implementation

Migration 0013_expired_pending_artifacts.sql extends the existing protected deletion claim to accept pending uploads only when BOTH their recorded retention deadline and actual upload lease have expired. Current live EIOS DELETE authority, server tenant/world/principal ownership, fenced deletion lease and conservative runtime invocation/job reference locks remain required. Available/deleting/deleted behavior is preserved. A live upload returns serialization failure; a future retention deadline remains permission-denied. This is cleanup of a specifically identified expired record, not an inference that every unfinished upload is disposable.

A related race was found during implementation: an old upload could otherwise write its final blob after a GC tombstone. The new narrow nexloop_lock_artifact_upload verifies the persisted upload token/fence/lease, current credential and signed CREATE permit, then locks live authority and the metadata row before the filesystem operation. It rechecks the row after waiting. Authority-before-metadata ordering matches deletion claim to avoid introducing a metadata/authority lock inversion.

PostgresArtifactRepository.upload_transaction holds those locks while LocalArtifactService.put performs immutable file write and file/directory fsync. Finalization runs on the same database connection/transaction. The success reference is returned only after commit. A retired/stale upload cannot enter the file-write section through this service. If the process dies after physical fsync, the metadata transaction rolls back to its independently reserved pending row; existing lease takeover recovers the same immutable file and ID. Low-level LocalBlobStore remains an infrastructure primitive requiring trusted coordination, not an ungoverned runtime write API.

The existing actual child-process exit-after-fsync test now injects process exit from the real store.put after fsync, inside the upload transaction, instead of replacing repository.finalize. Its recovery still uses restricted PG, the same intent and the real file.

## Real PG cases and first failures

New cases reserve an already retention-expired upload through the actual EIOS-authorized restricted repository, with and without a physical final file. They prove deletion is refused while lease is live; after a real bounded 1.05-second lease wait, the service removes any file and writes a tombstone. The old finalize and upload transaction are fenced; deletion is replayable. A separate expired-lease case with future retention remains protected. No admin timestamp or business mutation creates expiry evidence.

First targeted command: `uv run --frozen pytest -xq tests/test_postgres_artifacts.py tests/test_bootstrap.py tests/test_doctor.py tests/test_db_boundary.py --tb=short`: 19 passed and one old active-upload test failed because it expected InsufficientPrivilege rather than the now-specific SerializationFailure for a live upload lease. Updated that exact expectation; the file remains protected.

The next run exposed a SyntaxError in the new helper SQL: the initial copy retained part of the old available-shortcut/update block. Removed that fragment so the helper only locks/validates pending state and never finalizes before filesystem fsync. The existing published migration was not modified. Targeted rerun then passed 31 cases in 26.65 seconds.

After correcting the explicit authority-before-metadata lock order, `uv run --frozen pytest -xq tests/test_postgres_artifacts.py --tb=short` passed all 21 cases in 21.74 seconds. Complete CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`; final results/input/JUnit hashes are in pending-artifact-recovery-ci.json.

## Changed files and incomplete acceptance

Changed: new migration 0013 and catalog checksum; postgres_artifacts.py upload transaction and service assembly; test_postgres_artifacts.py cleanup/fencing cases and actual crash boundary; exact catalog expectations in test_bootstrap.py/test_doctor.py/test_db_boundary.py; versions.lock.json head 0012→0013; this report/CI report; planning evidence and public file inventory. SQL migrations 0001–0012 remain byte-for-byte unchanged against the prior successful CI hashes. Upstream commits, Pi package mapping and unresolved image digests are unchanged.

This does not implement scanning arbitrary unregistered final files, runtime-owner terminal deletion propagation or a complete orphan reconciliation worker. Conservatively protected runtime-bound files still require lifecycle integration. HTTP Host, authoritative definition publication, workflow/outbox, actual Pi Run kill/reopen, real effects/reconciliation, deployment and full S2 acceptance remain unfinished. No S2 scenario is promoted to passed from these focused tests. No production environment/original checkout was modified, real credentials read/called, or Git staging/commit/push performed.

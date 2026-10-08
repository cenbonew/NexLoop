# Authorized bounded Artifact cleanup discovery

NX-007/NX-008 remain in_progress. This exposes bounded authenticated cleanup batches; no scheduler or full orphan/owner lifecycle is accepted.

## Actual implementation

New migration 0014_artifact_cleanup_candidates.sql provides a narrow signed READ-permit query for cleanup candidates. It consumes the normal actual-EIOS Artifact permit and returns only opaque IDs belonging to the authenticated tenant/world/principal. A keyset cursor uses explicit C ordering, a 1–100 limit and a 32-hex ID (or empty initial cursor). Returned IDs are only expired-retention available rows or expired-retention pending/deleting rows whose lease has ended. Deleted tombstones, future deadlines and active leases are excluded. Runtime invocation/job references are conservatively excluded from discovery; the deletion claim still independently locks/rechecks those references to cover concurrent insertion. A partial metadata cursor index supports the query. No raw application table SELECT grant is added.

PostgresArtifactRepository.cleanup_candidates signs its exact limit/cursor parameters and validates the returned page's type, bounded size, sorted uniqueness and cursor advancement. LocalArtifactService.collect_expired processes each ID through the existing DELETE permit and fenced transaction/unlink/fsync/tombstone path. READ discovery cannot itself authorize deletion. The authenticated Backend facade exposes collect_expired_artifacts with the same bounds.

A batch returns deleted IDs, next_after and exhausted only after all its requested deletions succeed. Batches are not atomic: if a later record's live DELETE authority/lease/reference check fails, the exception propagates and earlier committed tombstones remain durable. Retry from the earlier cursor safely rechecks current state; deleted rows are no longer candidates. New/changed candidates sorting before a saved cursor are found by the next sweep starting at the empty cursor. A full-size last page needs one further empty page to prove exhaustion. Automatic scheduling/checkpoint persistence remains Host work.

## Real PostgreSQL tests and commands

A three-expired-record case proves stable two-page discovery and batch cleanup, while a future-retention completed record and a live-lease pending upload remain intact. Separate actual service principals in one tenant and another tenant each discover only their own records. Four invalid limit/cursor cases fail before permit creation. An independent typed-grant change keeps READ but removes DELETE: discovery still succeeds, batch deletion is denied, and file/available metadata remain unchanged. All operations use real restricted PG and actual EIOS typed facts. Admin access seeds authority/configuration and reads evidence; no admin business writer performs cleanup.

`uv run --frozen pytest -xq tests/test_postgres_artifacts.py tests/test_bootstrap.py tests/test_doctor.py tests/test_db_boundary.py --tb=short`: 37 passed in 31.14 seconds before the final READ-without-DELETE case was added.

The first final-case run of `uv run --frozen pytest -xq tests/test_postgres_artifacts.py --tb=short` failed after 17 passes: replace_fact expects JSON-compatible fixture values, but the test supplied an EffectiveGrantFact object. Converted that synthetic value with model_dump(mode='json'); no runtime permission check was changed. Rerun and full CI results are recorded in artifact-cleanup-pages-ci.json.

Full CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`.

## Files, versions and limits

Changed files: new 0014 SQL and catalog, postgres_artifacts.py candidate/batch methods, backend.py authenticated batch facade, test_postgres_artifacts.py, exact-head/count tests in test_bootstrap.py/test_doctor.py/test_db_boundary.py, versions.lock.json head 0013→0014, this report/CI report, planning evidence and public file inventory. Existing SQL 0001–0013 are byte-for-byte unchanged against the prior successful CI hashes. versions.lock.json changes only bootstrap_revision; upstream commits, Pi package mapping and unresolved image digests are unchanged.

Unregistered final files are still not inferred/deleted from filename scans. Runtime-owner terminal cleanup, complete orphan reconciliation worker, scheduling, HTTP Host, definition publication, workflow/outbox, actual Pi Run kill/reopen, real effects and full S2 acceptance remain incomplete. No S2 acceptance is promoted to passed from these partial cases. No original checkout/production DB was modified, persistent environment migrated, real credential read/called, or Git staging/commit/push performed.

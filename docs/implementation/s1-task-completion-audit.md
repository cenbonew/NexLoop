# S1 task-level completion audit

This audit separates backlog deliverables from complete S1/product acceptance. The prior 0014/558-test audit is superseded by the current 0032 implementation and community-core CI evidence.

| Task | Actual requirement evidence | Decision |
| --- | --- | --- |
| NX-006 | Selective frozen vendoring, independent packaged lineage, noneditable installation, clean PG bootstrap; authenticated Consumer create/read, governed preference edit and Relation link, revocation/tenant/property/idempotency/fail-closed tests | Task deliverable complete; current full CI passed. No complete product extraction claim; additional capabilities append this core. |
| NX-008 | 32 immutable checksummed migrations; exact catalog/reopen/drift rejection; restricted roles, FORCE RLS, direct business writes denied, owner/superuser/disguised role and PUBLIC function execution denied; missing PG/Memory rejected | Task deliverable complete. |
| NX-010 | Frozen dependency locks, mandatory real disposable PG, source integrity, four frozen Pi builds, actual SQLite FULL tests and mandatory zero-skip JUnit gates | Minimal local CI task complete; future Host/runtime/workflow tests must join this gate. |
| NX-007 | Authorized atomic physical writes/read, hash/size checks, retention/reference protection, pending recovery, bounded temporary cleanup, dedicated-authority durable final orphan ledger, actual SIGKILL/reopen reconciliation | Minimal local Artifact adapter task complete; current CI passed. Positive runtime-owner lifecycle integration remains a later runtime requirement; existing references conservatively protect content. |
| NX-009 | Core-test Dockerfile, secret-isolated Compose graph, clean-only initializer, restricted test profile, doctor/Artifact check job, public-only prepared wheel context | in_progress. Client rendering and native-PG entrypoint pass; Docker daemon unavailable, no container build/run or AT-051 acceptance. No full API/Host stack. |

Evidence: final-artifact-orphan-ci.json, governed-object-edit-ci.json and governed-relation-link-ci.json and community-core-ci.json. Installed console checks include the new compose bootstrap entrypoint. No S2 AT case is promoted by these component tests. RuntimeAdapter/Pi Run recovery, actual governed external effects, runtime-owner Artifact lifecycle, HTTP Host and full community Compose acceptance remain incomplete.

versions.lock.json remains at bootstrap revision 0032 and frozen upstream commits. PostgreSQL and Python build-base OCI index digests are now resolved; their status explicitly excludes runtime verification. Application image digests remain blank. No production migration, server mutation, staging, commit, push or deployment occurred.

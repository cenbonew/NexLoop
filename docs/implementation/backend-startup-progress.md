# Backend signing-key startup attestation

NX-006/NX-007/NX-008 remain in_progress; complete S1/S2 gates remain unaccepted.

## Implemented behavior

`open_backend` now verifies that its private file key matches an active PostgreSQL signing key before opening or creating Artifact storage. A fresh 32-byte random challenge is signed with domain-separated HMAC-SHA256 (`nexloop-backend-signer-v1:`). Only key ID, random challenge and signature reach the restricted database connection. Raw key material never becomes a SQL argument. The new migration 0012_backend_signer_attestation.sql exposes a narrow boolean read-only SECURITY DEFINER function; it returns false for absent/inactive keys and malformed challenges/signatures, and returns no key bytes, computed MAC or business data. PUBLIC execute is revoked and only restricted backend roles receive execute.

The backend validates its restricted role and runs the verification in a read-only transaction. A missing/inactive/mismatched key raises stable StorageUnavailable before the Artifact directory is created. No key provisioning, schema migration or Memory fallback is performed during startup. Existing protected dispatch continues to check key status, live credentials and live authority; the startup proof cannot authorize a business request by itself.

Actual PostgreSQL tests separately cover mismatched, inactive and missing key configuration, asserting that the Artifact directory remains absent and no metadata is written. Another test injects a caller exception inside the opened backend context and verifies actual pool/descriptor closure plus closed-handle rejection. Existing positive composed Artifact and governed object operations run through the new startup verifier. Signing file and DB configuration are synthetic only.

versions.lock.json bootstrap_revision changes 0011→0012. Existing SQL migrations 0001–0011 are byte-for-byte unchanged against the prior successful CI input hashes. Frozen upstream commits, Pi package mapping and unresolved image digest fields are unchanged.

## Commands and first failures

Targeted command: `uv run --frozen pytest -xq tests/test_backend.py tests/test_bootstrap.py tests/test_doctor.py tests/test_db_boundary.py --tb=short`.

First run: 10 passed, then a source-catalog test still expected 11 migrations after the new head was added. Corrected its remaining exact count to 12; no checksum verification was weakened. Rerun: 19 passed in 10.44 seconds.

A full CI run was inadvertently started before the targeted failure was inspected. It loaded the old exact-count test and failed that same assertion; its failure is retained in the CI evidence, and a complete rerun after correction is required. Full command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Final results and input/JUnit hashes are in backend-startup-ci.json.

Changed files: new 0012 SQL, migration catalog, backend.py, tests/test_backend.py, exact-head expectations in test_bootstrap.py/test_doctor.py/test_db_boundary.py, versions.lock.json, this report/CI report, planning evidence and public change inventory.

This is a startup identity/key configuration check, not an HTTP Host or release-readiness result. Definition publication, owner-bound Artifact GC, workflow/events/outbox, actual Pi Run persistence/recovery, effects/reconciliation, deployment and the full S2 acceptance matrix remain incomplete. No production database/original checkout was modified, real model/channel credentials read or called, or Git staging/commit/push performed.

# S1 implementation progress — 2026-10-07

This is an implementation checkpoint, not completed S1/S2 acceptance.

## Actual changes

- `packages/eios-core/src/nexloop_eios/assembly.py`: validate both current_user and session_user, all elevated role flags including replication, and any membership path to owner/elevated roles. PostgreSQL NOINHERIT alone does not remove SET ROLE authority.
- `packages/eios-core/src/eios/migrations/0003_default_authority_hardening.sql`: append-only independent lineage; owner/bootstrap new functions no longer implicitly grant PUBLIC EXECUTE. Original 0001/0002 bytes/checksums remain unchanged. Catalog and versions.lock.json now pin new head 0003.
- `packages/eios-core/src/nexloop_eios/local_artifacts.py`: service-owned 0700 filesystem backend, opaque tenant/world names, separate opaque Artifact identity/content digest (identical bytes do not share retention/deletion authority), bounded bytes, file and directory fsync, atomic create-if-absent, symlink rejection through O_NOFOLLOW and directory descriptors, multi-process directory-inode flock, integrity-verified range reads, abandoned-temp GC, and terminal/expired retention proof checks. Metadata explicitly reports encryption=none; no encryption claim is invented.
- Artifact access uses the actual EIOS AuthorizationFactQuery and AuthorizationDecisionService. It rejects wrong tenant/world/resource/operation, non-authoritative/expired/denied decisions and unsupported obligations. Missing resolver fails closed. No public filesystem URL or Memory metadata index is exposed.
- `tests/test_assembly.py`, `tests/test_db_boundary.py`: actual disposable PostgreSQL negative checks for administrative SET ROLE disguise, owner membership, and owner function default EXECUTE ACL.
- `tests/test_local_artifacts.py`: real filesystem/reopen/multi-process/crash/symlink/corruption/fsync/retention/range checks. DecisionProbe is an explicitly synthetic unit double; these are **not** real PostgreSQL Artifact authorization evidence.
- `tests/test_contracts.py`: checks the live packages/contracts copy, including world/mode isolation, unsupported authority fields, revision/digest requirements and finite runtime budget. Frozen handoff validation alone did not cover this live copy.
- `scripts/check_test_report.py`, `tests/test_ci_gate.py`, `scripts/ci/check`: require nonempty JUnit reports with zero critical skips/failures/errors; always require Pi source build/SQLite tests rather than silently skip on a missing file.

## Commands actually executed and failures

`uv run --frozen pytest -q tests/test_assembly.py tests/test_db_boundary.py`: 4 passed before adding the new migration ACL check.

`uv run --frozen pytest -q tests/test_local_artifacts.py --tb=short`: initial 14 passed/2 failed because the synthetic credential fixture used guessed names instead of the actual frozen EIOS model fields. Read _AuthenticationBinding and corrected exact fields, retaining SubjectKind.SERVICE (no fake human/session).

Retry: 19 passed/1 failed when concurrent macOS O_CREAT/O_NOFOLLOW lock-file creation returned ENOENT. A disposable directory-flock probe confirmed supported OS behavior. Replaced lock-file creation with flock on the stable private directory inode. Re-ran: 19 passed. Added retention tests: 25 cases passed, with the 3 assembly checks giving 28 passed.

`uv run --frozen pytest -q --tb=short`: 196 passed after Artifact/role/default-ACL changes, before live-contract/CI-gate tests were added.

`uv run --frozen pytest -q tests/test_contracts.py tests/test_ci_gate.py --tb=short`: 17 passed.

Final local CI evidence is recorded in `s1-foundation-ci.json` after the actual run. A test result is not assigned to a product AT scenario unless its complete scope is exercised.

Additional bootstrap hardening: original upstream 0001 has conditional grants to nex_eios_app/nex_eios_runtime. NexLoop bootstrap now rejects their presence before schema creation; a disposable-cluster test confirms no schema/grants are created on rejection. This prevents accidental authority inheritance from an existing EIOS cluster without altering the frozen upstream migration checksum.

## Remaining implementation, not external blockers

NX-006 is still in_progress: whole retained core assembly needs Artifact metadata/governance integration. NX-008 has restricted roles/FORCE RLS and exact catalog but will gain narrow explicitly granted functions for authenticated infrastructure/Action paths. NX-007 has tested physical backend and authorization entry semantics; its PostgreSQL metadata reservations, live resolver, upload/GC fencing and final-blob orphan reconciliation still need implementation. NX-010 has a functioning foundation gate; application/API/Host tests join it as they are implemented.

The next required work is live PostgreSQL metadata and identity/authorization assembly, then single-machine Compose/doctor and the S2 governed-write/recovery slice. AT-051 is not passed from these partial checks. AT-013/030/031/033/034/039/052 remain not_run. Runtime business reconciliation and complete Run owner recovery are not proven by storage conformance alone.

No original production data/source was modified. No persistent database migration, server mutation, staging, commit or push occurred. Model credential presence was detected previously without exposing its value; real Provider validation is outstanding implementation work. Embedding provider choice and image digests are still unresolved. Missing external channels affect only their later controlled validations.


Final run completed: `source "$HOME/.nvm/nvm.sh" && nvm use && scripts/ci/check` exited 0. **223 Python tests passed**, including 30 Artifact tests; **24 actual SQLite FULL conformance tests passed**; all four frozen Pi packages built; both JUnit reports have zero skipped/failing/error cases. Publication scan during final CI: 574 paths, zero violations. Handoff validator: 69 checks passed. Final Artifact hardening separated physical identity from content digest, verified independent deletion of identical payloads, rejected same-ID/different-payload overwrite and header/identity injection. This evidence does not substitute for PostgreSQL metadata/governance or product fault acceptance.

versions.lock.json change in this checkpoint: NexLoop EIOS bootstrap head 0002 → 0003. Three upstream commits and frozen Pi artifact selection remain unchanged; all six image digest fields remain empty.


Publication guard correction: `--staged` previously inspected cached path names but read worktree bytes, which could miss an unsanitized index blob after the worktree was cleaned. It now reads exact ACMR index blobs via `git show :path`; the normal scan also checks both sources. Four isolated Git-repository tests prove that sanitized worktree content cannot hide staged credentials, private ignored paths forced into the index are rejected, and unstaged content is not substituted for the index. The actual NexLoop index has **zero staged blobs**. Synthetic index fixtures exist only under disposable pytest directories; no NexLoop files were staged.

Final strict CI after the index-scanner correction: 223 Python cases passed in 8.00s, 24 Pi cases passed; exit 0. s1-foundation-ci.json includes tested-input and JUnit hashes. No task or product acceptance is marked complete from this partial slice.

## Subsequent PostgreSQL Artifact checkpoint

Live service facts and signed PostgreSQL Artifact reserve/publish/read are now implemented and tested; the earlier missing-resolver/reservation items have advanced. See [Artifact governance progress](artifact-governance-progress.md). PostgreSQL retention/GC/orphan/lifecycle integration remains incomplete. Task statuses remain in_progress and product ATs remain not_run.

Standalone expired-upload retention now uses PostgreSQL DELETE permits, leased/fenced tombstones and live revalidation across filesystem deletion. See artifact-retention-progress.md and artifact-retention-ci.json (252 Python + 24 Pi passed). Runtime-bound terminal-owner GC and final orphan reconciliation remain incomplete. Bootstrap head is 0006.

Frozen ActionGovernor, claim/permit/precondition/approval contracts and definition/version-resolution dependencies now extracted (85 provenance records). See eios-action-core-progress.md and eios-action-core-ci.json; 471 Python + 24 Pi cases passed. Action unit contracts are explicitly separate from live PG Action execution, still required.

A real PostgreSQL ActionClaimPort now implements restricted signed live-authority reserve/retryable/finalize, stable intent binding and fenced recovery. See postgres-action-claims-progress.md and postgres-action-claims-ci.json; strict CI 480 Python + 24 Pi passed. This is a durable claim ledger, not a completed ActionExecutionPermit/business instance/effect slice. Bootstrap head 0007; task/AT completion remains unproven.

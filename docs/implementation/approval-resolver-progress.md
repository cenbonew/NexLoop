# Core review: explicit Approval evidence resolver extraction

NX-006 remains in_progress; this closes a source-contract evidence gap, not production Approval execution.

## Review and implemented change

The NX-006/NX-008 review used current planning deliverables, docs/handoff/docs/07_EIOS_INTEGRATION.md extraction gates and docs/handoff/docs/14_TEST_ACCEPTANCE.md. Existing clean-PG bootstrap/role/Artifact/governed-create evidence does not prove the complete independent-install/API/full-core acceptance. Tasks are not promoted by aggregate test counts. The concrete gap found was two excluded Approval snapshot contract cases: the upstream resolver had not been copied.

Copied the generic src/eios/composition/action_approval.py from frozen EIOS 1e363db982daeca74058453ad12fc4a6cac333da. Preserved immutable Approval subject, requester/Action/capability/schema binding, exact snapshot, freshness and evidence-window checks. NexLoop requires an explicit callable authority_verifier, removing the original implicit InMemoryApprovalHumanAuthorityVerifier default. Missing/None/unusable verifiers fail at construction. This resolver is not wired into the production Governor; the production factory still uses unavailable approval ports/authority and denies unsupported approval-required Actions.

The original composition.__init__ eagerly imports StorageBundle/build_storage, pulling a broad production storage closure. NexLoop's package initializer is explicitly adapted to avoid that import; no original storage assembly or domain source is copied. PROVENANCE.json records original/adapted hashes and both adaptations for two added records (87 total source records). Third-party licenses remain preserved. The source reuse inventory now has 25 entries; the new entry records 234 original initializer-inclusive dependencies and 29 adapted dependencies separately. scripts/audit_eios.py includes the new capability and adapted-closure field; its syntax was checked after CI, without wholesale regeneration of existing audits.

Restored the original test_action_governance.py including both formerly excluded snapshot cases. Only source AST paths and the explicit synthetic in-memory verifier argument are adapted. UPSTREAM_ACTION_TESTS.json records hashes and those changes. The synthetic verifier is an upstream unit-test oracle, not a production fallback or real PostgreSQL Approval acceptance.

## Actual commands and results

`uv run --frozen pytest -xq tests/test_vendor_imports.py tests/upstream/test_eios_action_governance.py --tb=short`: 163 passed in 0.47 seconds.

Added three boundary cases for missing/unusable verifiers. `uv run --frozen pytest -xq tests/test_approval_resolver_boundary.py tests/test_vendor_imports.py tests/upstream/test_eios_action_governance.py --tb=short`: 166 passed in 0.36 seconds. No test failures or skipped cases occurred in this step.

`source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`: exit 0, 543 Python cases in 74.71 seconds, 24 actual Pi SQLite FULL cases and four frozen-source builds, zero skips/errors/failures. Current tested-input and JUnit hashes are recorded in approval-resolver-ci.json. The audit generator was additionally parsed with ast.parse; it was not claimed as executed end-to-end.

## Files, version lock and incomplete scope

Changed: two generic composition source files, PROVENANCE.json, restored upstream governance test and its manifest, tests/test_approval_resolver_boundary.py, source-reuse-inventory.json/.md, audit generator, this report/CI evidence, planning task evidence and public change inventory. versions.lock.json is unchanged at bootstrap 0014; all existing SQL migration bytes are unchanged. Frozen upstream commits/package mapping and unresolved image digests are unchanged.

Real PG Approval persistence/human authority, authoritative definition publication and complete core contracts/negative gates still need integration. NX-007 still lacks runtime-owner lifecycle and complete orphan worker; NX-009 lacks clean community Compose; NX-011 lacks HTTP/Host/frontend; actual Pi Run recovery, real effects, queues/outbox and S2 acceptance remain incomplete. AT-001/002/003/051 are not promoted to passed from module-level evidence, and the restored two unit cases do not pass any S2 Approval gate. No production environment/original checkout was modified, real credential read or called, persistent environment migrated, or Git staging/commit/push performed.

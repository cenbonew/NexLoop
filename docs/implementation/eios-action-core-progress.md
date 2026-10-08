# Frozen EIOS Action core extraction

NX-006 progress. The extracted core previously had ontology schemas/store and authorization decisions but lacked the actual ActionGovernor/claim contracts. That cannot support the requested governed instance-write slice. This checkpoint brings in the frozen generic implementation rather than substituting an Artifact permit for an Action permit.

## Actual files

Copied byte-identical code from frozen EIOS commit `1e363db982daeca74058453ad12fc4a6cac333da`: `eios/actions/{__init__,models,ports,preconditions,governance,write_attempts,in_memory}.py`, `eios/approvals/{__init__,models,ports}.py`, and `eios/ontology/{version_resolution,definition_registry}.py`. PROVENANCE.json now has 85 records, each with upstream and NexLoop SHA256 and preserved MIT authorization/third-party notices. No tennis/domain implementation, production configuration, database contents or upstream Git history was copied.

The InMemoryActionClaimStore and approval verifier are the upstream unit-test oracle and generic source dependencies, not a configured NexLoop persistence fallback. open_core still requires real PostgreSQL, restricted application roles and the independent catalog. ActionGovernor's upstream default verifier must not be used in future production assembly: an explicit authoritative verifier/ApprovalPort is required, and missing human authority must fail closed. Production Action claim/approval assembly is not yet enabled.

`tests/upstream/test_eios_action_claim_contracts.py` preserves the entire upstream contract suite. `test_eios_action_governance.py` preserves governor tests with source-AST paths adapted to the vendored layout. One two-case integration test requiring the unextracted ApprovalEvidenceResolver composition adapter is excluded explicitly, not marked skipped or treated as accepted. Governor approval-required/snapshot/binding tests remain. Full source/test hashes and the exclusion are recorded in UPSTREAM_ACTION_TESTS.json. tests/test_vendor_imports.py verifies all extracted imports/source hashes and the test manifest.

## Executed commands, failures and evidence

Initial `uv run --frozen pytest -xq tests/test_vendor_imports.py tests/upstream/test_eios_action_claim_contracts.py --tb=short` failed during import: missing eios.ontology.version_resolution. Added its exact frozen source; retry revealed its missing definition_registry dependency. Added that exact frozen source; targeted retry passed 60 cases (59 Action claim contracts + source integrity/import check) in 0.40s.

`uv run --frozen pytest -xq tests/upstream/test_eios_action_governance.py --tb=short`: 159 passed in 0.26s. These test immutable request digest, tenant/action/capability binding, required scopes, approval/policy evidence, gate order, lease/fence/ownership, replay/conflict, malformed clock/results and secret-safe failure messages using explicit synthetic spies/oracles. They are unit contract evidence, not a live PostgreSQL Action or external-effect claim.

## Remaining scope

Next required code is a PostgreSQL ActionClaimPort with actual EIOS authorization/evidence binding, current-authority checks, durable idempotency and fenced lifecycle; then governed ontology instance capabilities. Existing PostgresOntologyStore raw DML and a fabricated ActionWriteContext must not be exposed as a shortcut. Prerequisite API/Host and lifecycle integration remain incomplete; NX-012 and S2 ATs remain unaccepted.

versions.lock.json is unchanged in this checkpoint (bootstrap 0006, three upstream commits pinned, image digests unresolved). No production writes/migration, credential access, staging, commit, push or deployment occurred. Missing embedding/channel inputs remain limited to their future validation; they do not block this work.

Final strict CI exited 0: 471 Python tests in 32.25s, 24 Pi SQLite FULL tests and four Pi package builds, zero errors/failures/skips. Evidence/input/JUnit hashes: eios-action-core-ci.json. The designated upstream worktree HEAD was reverified and git status was clean. NexLoop index remains empty.

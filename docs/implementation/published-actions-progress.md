# Published Action contract reader and Governor integration

NX-006/NX-008 progress; whole governed instance/effect slice remains incomplete.

## Changed files and actual behavior

New `0008_action_definitions.sql` stores immutable published ActionDefinition/capability snapshots in an owner-only, FORCE-RLS tenant/world configuration directory. Definition content/key/capability cannot change within a published version. Retirement uses active=false; configuration mutation advances the existing tenant authority epoch. App roles get only narrow function execution, no raw metadata SELECT/DML.

`action_definitions.py` evaluates an actual frozen EIOS EXECUTE query for the canonical Action target, signs a distinct short-lived definition-read protocol, and reads through the protected function. SQL rechecks current credential/tenant epoch and all 12 authority facts. The function locks live authority, reads configuration with MVCC and rechecks; no config row lock is taken after tenant locks, avoiding the publisher row/epoch lock inversion. Reader uses strict frozen ActionDefinition and CapabilityContractSnapshot JSON validation, exact tenant/name/version/published status, and actual validate_capability_binding. Wrong signer, missing publication and incompatible capability fail closed.

`action_governor.py` adds govern_published_action: loads published contracts, derives scopes from authenticated server binding, constructs exact evidence for a declared empty Action-specific policy set, and uses the actual frozen Governor with PG claim/time. Caller cannot pass a replacement definition/risk/capability or granted scopes through this entry point. Nonempty Action-specific policy references remain unavailable until authoritative policy evidence storage is assembled. Separate live EIOS policy/control/ceiling/grant intersections still apply. Required approvals remain fail closed, never default Memory authority.

Tests prove actual restricted PG published contract read, no raw application metadata access, retirement/epoch invalidation, absent publication, wrong signer, capability mismatch, actual persisted-definition→Governor→claim permit, forged contract digest rejection before any reservation, and published content immutability. Initial configuration fixtures are synthetic admin-owned publication/authority setup; all read/claim execution uses restricted application roles. There is no Consumer object insertion or external-effect success claim.

Other changed files: migration catalog, versions.lock.json bootstrap head 0007→0008, bootstrap/role/doctor assertions, tests/test_action_definitions.py. Prior migrations 0001–0007 and upstream pins remain unchanged; image digests unresolved.

## Executed commands and results

`uv run --frozen pytest -xq tests/test_action_definitions.py tests/test_bootstrap.py tests/test_db_boundary.py tests/test_doctor.py --tb=short`: 14 passed in 8.03s at initial four-case reader checkpoint.

Reader checks expanded to actual missing-publication SQL path and capability mismatch: 5 passed in 3.76s. Added published Governor and digest forgery cases: 7 passed in 5.34s. Final immutability/retirement current-input suite: 8 passed in 5.95s. No failing test run in this checkpoint. During pre-test API review corrected validate_capability_binding to take the actual ActionDefinition rather than its inner binding; the frozen function signature was inspected.

Full local CI current-input result follows below. No real credentials were read, model/network calls made, production schema/data changed, or Git files staged/committed/pushed.

## Remaining scope

Publishing/editing APIs and version-chain registration semantics, actual referenced ontology schema storage/revision checks and dispatch-time binding must still be integrated. This directory is an adapted independent persisted publication snapshot, not a claim that the full upstream Definition Registry is assembled. Current-authority and epoch checks invalidate this snapshot's sessions after mutation; the protected business-write dispatcher is still absent.

Next required: governed instance storage functions, full capability/schema publication adapter, lifecycle/audit/outbox/Action-specific policy evidence, community API/Host/Compose, actual Pi Run kill/reopen and S2 acceptance. No product AT is marked passed. Missing channel/embedding inputs affect later external validation only.

Final strict local CI exit 0: 508 Python tests in 50.49s, 24 Pi SQLite FULL tests, four frozen Pi builds, zero skips/errors/failures. Source/test/JUnit hashes recorded in published-actions-ci.json. Migrations 0001–0007 verified unchanged against preceding tested-input hashes.

# Governed object property edits

NX-006 missing preference mutation is now implemented through a published EIOS Action; NX-006 remains incomplete pending Relation behavior and the complete retained-core contract. This is not S2 or full product acceptance.

## Behavior and authority

GovernedObjectEditor.edit and authenticated Backend.edit_object accept a typed partial property patch, stable intent ID and expected object revision. Schema validation uses the actual frozen ObjectTypeDefinition validator with partial=True, preserving other properties. The published Action must bind consumer.edit or ontology.object.edit, the exact schema and a low-risk/no-approval/no-policy PostgreSQL change scope. Other action policy/approval modes fail closed rather than inventing evidence.

The service must independently hold live EIOS Action EXECUTE, exact Object EDIT and each patched Property EDIT authority. Object/property proofs are resolved before reserving the durable Action claim, including terminal replay. No human identity or admin DSN is used. Signed protocol nexloop-object-edit-v1 binds the request digest, Action permit/definition/schema and all authority proofs. New protected SQL checks the live credential/directory epoch and all twelve fact records for each proof, keeps those locks until commit, verifies the active claim/fence/lease and exact schema, then locks the object and checks expected revision. Property JSON merge, revision increment and terminal claim outcome commit atomically. Revoked or missing authority cannot mutate formal objects.

0015 appends an object revision starting at 1, protected edit authority/Action functions, and the authorized metadata read's revision field. Older published SQL is unchanged. Revision mismatch fails with serialization failure; it does not overwrite a concurrent update. The same completed intent returns its original revision/receipt, without executing the mutation again; a different payload conflicts. No deletion/reset of properties is added by this patch API. Object identity is opaque, tenant/world/type constrained by server-derived session.

This does not implement Relation links, field masking, deletion propagation, general schema publication, agent-release/delegation or required Approval execution. Test identities are explicit synthetic configuration fixtures. Administrative test setup only publishes schema/Action/auth facts; Consumer creation and edits execute through restricted actual EIOS paths. Administrative business queries in tests are read-only evidence.

## Actual commands and failures

Target command: `uv run --frozen pytest -xq tests/test_object_edits.py --tb=short`. Initial combined-identity fixture failed fact resolution. Diagnosed the actual frozen resolver: all requested scopes must be known to each catalog record, shared role digests must match, and changed model snapshots must be rebuilt. A subsequent fixture rebuild used strict Python validation on JSON enum strings and failed; changed to model_validate_json. After fixes: 3 passed in 2.53s. A temporary test-only diagnostic context wrapper was removed before final validation; runtime provider retains its original fail-closed/epoch checks.

Expanded negative cases initially expected ActionAuthorizationDenied for an unknown object, but missing graph facts correctly returned AuthorizationUnavailable; accepted both fail-closed outcomes. Regression first failed a strict metadata-read assertion because it omitted the newly returned revision; updated the actual public result contract expectation. The SQL-race test injects property-grant revocation after all signed proof resolution and immediately before SQL dispatch.

Final targeted and full CI results/input hashes are recorded in governed-object-edit-ci.json. Strict CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. No AT status is promoted until its full scenario scope is exercised.

Changed: object_edits.py, object_reads.py typed operation argument (READ remains default), backend.py edit facade; 0015 SQL/catalog; versions.lock.json head 0014 -> 0015; edit tests and existing bootstrap/doctor/sandbox/wheel/read contract expectations; this report and final CI/planning evidence. No dependency/upstream/Pi mapping change or resolved image digest. No original DB migration, staging, commit, push, real model or external channel call.

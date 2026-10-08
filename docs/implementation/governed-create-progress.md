# First governed instance create checkpoint

NX-006/NX-008 core implementation progress. This proves one real restricted PostgreSQL instance-create path, not the full NX-012/S2 business slice.

## Actual changed files and chain

`object_actions.py` loads the persisted Action/capability/exact schema bundle, validates properties with the frozen EIOS validate_object_properties, builds a stable request/intent digest, obtains an actual ActionExecutionPermit via the published Governor/PG claim, re-evaluates live EIOS authority and signs a distinct protected instance command. No client-supplied permit, definition, risk, actor, tenant or granted-scope override is accepted.

`0010_governed_object_create.sql` adds explicit world to the original ontology.objects table without replacing its business-fact store or existing relation foreign keys. Opaque server-generated IDs bind tenant/world/type/intent, so worlds do not share an object identity. Restricted roles get function EXECUTE only; raw DML remains unavailable. The definer requires live signed service authority, the exact active published definition/schema, declared type/PostgreSQL change scope, allowed create capability, no Action-specific policy references, low risk and no approval requirement, plus the exact active stored claim/fence/payload digest/expiry. It locks/rechecks authority at dispatch, writes the original ontology.objects row and marks the Action terminal in the same transaction. The receipt exits Python only after commit. Changed payload/authority/claim cannot be accepted as a retry; the stored terminal outcome supports idempotent receipt replay.

Supported boundary is intentionally the already-declared no-approval/low-risk create adapter. Approval-required, Action-specific policy evidence and field-scoped change declarations remain fail closed. EIOS's separate actual credential/application/grant/scope/control/world policy intersection remains mandatory. The signing key exists only in trusted backend/test setup; Runtime has no DSN or signer. Business data is never inserted using an administrative DSN or a fabricated human/session.

`action_definitions.py` exposes the already-validated schema bundle to the private backend creator. Tests include actual Consumer creation, committed row readback, one-row replay, direct app DML refusal, property validation before claim and a real revoke-after-permit/before-business-SQL race. The test Consumer schema is deliberately minimal (no declared properties); this is not proof of preference/Relation/property-reading support. Admin fixture code configures identity/schema/Action and performs read-only evidence queries; the actual instance mutation uses restricted nexloop_api through the governed definer.

Other changed files: catalog, versions.lock.json head 0009→0010 and exact bootstrap/role/doctor tests. Migrations 0001–0009 remain unchanged; upstream pins and unresolved images remain unchanged.

## Executed commands and first failure

uv run --frozen pytest -xq tests/test_action_definitions.py --tb=short first failed the new create case on PL/pgSQL claim-variable/table-column ambiguity; the transaction rolled back without an object. Renamed the local v_claim variable, preserving the column name. Retry: 12 passed in 8.78s. Added property and revoke-race negatives: 14 passed in 10.32s. Full local CI current-input evidence follows below.

## Remaining requirements

Authenticated object/property reads, preference edit with expected-version fencing, Relation creation, schema/capability publish APIs, audit/domain event/outbox integration and complete authorization negative matrix remain required. Runtime-bound identity/release/delegation is not supplied by this service-only prototype. External effects and unknown-result reconciliation are not implemented by an internal object create. Community startup, API/Host, actual Pi Run kill/reopen and S2 ATs remain incomplete. No task/AT is marked done/passed from this partial scope.

No original production access/change, real model/channel call, credential logging, staging, commit or push occurred. All migrations were executed only on owned disposable PG test clusters. Missing external inputs affect their corresponding future validation only.

Final strict CI: exit 0, 514 Python cases in 54.75s, 24 Pi SQLite FULL cases, four Pi source builds, zero skips/errors/failures. Source/test/JUnit hashes in governed-create-ci.json; migrations 0001–0009 verified unchanged.

# Exact EIOS schema dependencies in the Action bundle

NX-006/NX-008 integration progress; formal business-write/effect acceptance is still incomplete.

## Changed files and behavior

New migration 0009_action_schema_bundle.sql keeps the existing upstream ontology.object_type_versions table as the source of formal object schema configuration. It adds immutable-version protection and authority-epoch publication triggers, then a protected bundle reader that first verifies the actual signed live Action authorization and published Action snapshot, reads every exact object-type reference from the existing EIOS table and fails if a tenant/type/version is missing. Metadata remains owner-only FORCE RLS with no direct application access. No new business-fact store, production migration or domain seed was introduced.

The schema rows are tenant-wide formal definitions; world is bound to the authenticated Action/configuration and cannot be replaced by a caller. Schema changes/deletion/publication advance the same tenant authority epoch, invalidating prior sessions/permits. A plain MVCC schema/config read under the locked epoch avoids publisher row/epoch lock inversion.

ActionDefinitionReader now validates the actual frozen ObjectTypeDefinition, exact name/version, complete referenced set, and upstream schema_contract_digest against each OntologySchemaReference.schema_digest. This is the upstream digest including version and pruning additive defaults; it is not an invented PostgreSQL JSON-text hash or the different registration idempotency digest. govern_published_action therefore cannot obtain a claim through the high-level entry point if an exact schema is absent or its digest mismatches.

Tests use an actual synthetic Consumer ObjectTypeDefinition (only_edit_via_actions=true) inserted as administrative schema/bootstrap configuration in the existing EIOS table, then run all bundle reads/Governor claims through restricted roles. No Consumer instance is inserted using administrative credentials. New cases demonstrate missing schema refusal, exact digest mismatch refusal and immutable stored schema version. Old published contract/capability/retirement/forgery cases still run.

Other changed files: action_definitions.py, tests/test_action_definitions.py, bootstrap/role/doctor exact-head assertions, catalog and versions.lock.json (0008→0009). Old migrations 0001–0008 and upstream commits/image fields are unchanged.

## Commands/results

uv run --frozen pytest -xq tests/test_action_definitions.py --tb=short: initial schema bundle suite 10 passed in 7.48s; after adding digest mismatch case, 11 passed in 8.23s. No initial failure in this checkpoint. Full local CI current-input evidence follows below.

## Still required

Governed schema publishing and linear version registration, exact relation/event/property dependencies, dispatch-time schema/reference checks, protected instance writes and actual effects remain incomplete. The low-level frozen Governor constructor is a trusted component; production dispatch must use the published bundle entry point and final protected dispatcher, not caller-provided definitions. No API or business writer is opened by this checkpoint.

Community startup, Pi Run kill/reopen and S2 ATs remain unaccepted. Embedding/channel inputs affect only their later external checks. No secrets were read/called/logged; no existing production database was touched; no files staged, committed or pushed.

Final strict local CI exit 0: 511 Python cases in 52.35s, 24 Pi SQLite FULL cases, four frozen-source builds, zero skips/errors/failures. Input/JUnit hashes in action-schema-ci.json. Old migrations 0001–0008 verified unchanged against prior CI hashes.

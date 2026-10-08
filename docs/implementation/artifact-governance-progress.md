# Artifact PostgreSQL governance checkpoint

This advances NX-006/NX-008/NX-007/NX-010. S1 and the S2 operational slice remain incomplete.

## Implemented code and boundaries

`0004_service_authority_facts.sql` adds an independent service credential directory and tenant-isolated typed EIOS authority records. App roles have only narrow function EXECUTE privileges; no raw SELECT/write on authority tables. The global authentication directory is deliberately owner-only rather than tenant-GUC-filtered: tenant is not known before authentication. The signing-key directory is also owner-only, contains no business facts and is not exposed by any read function. All tenant authority/permit/Artifact tables have FORCE RLS and no application DML. `0001`–`0003` checksums remain unchanged.

`authorization.py` keeps the actual frozen EIOS CredentialAuthenticationBinding, AuthorizationFactsResolver, opaque/sealed ResolvedAuthorizationContext, AuthorizationDecisionService and intersection evaluator. Its PostgreSQL adapter supplies repeatable-read/read-only snapshots, database time and checked repository witnesses. It never constructs an allow decision by replacing the resolver, never impersonates a human/session and never falls back to Memory. Tenant/principal/application/scopes come from the authenticated credential directory. Query world and binding must match the server-issued ServiceSession. Browser/delegation/agent-release adapters remain explicitly unavailable rather than silently interpreted as services.

`0005_local_artifact_governance.sql` and `postgres_artifacts.py` add short-lived Artifact permits signed by the trusted evaluator after a real EIOS allow decision. Claim text binds tenant/world/principal/credential/application directory, operation, parameter digest, expiry and all 12 loaded EIOS fact row hashes. An owner-only tenant registry rejects suspended tenants. Every credential/fact publication and tenant status change advances a monotonic tenant authority revision, bound into the authenticated session and permit: restoring identical grants cannot resurrect a prior permit. PostgreSQL validates the signature with an owner-only key, locks/rechecks live credentials and fact rows, bounds lifetime to 30 seconds, consumes permits once, and rejects payload/revision changes. Application SQL credentials alone cannot mint a signed allow result; Runtime/model/API clients never receive the signing key. The evaluator/signing-file boundary is trusted server code, not a user-supplied signature endpoint.

Artifacts authorize against the configured `eios:artifact:local_<world>` store resource, then metadata functions enforce exact tenant/world/creator ownership and opaque artifact identity. This supports private service uploads; cross-owner sharing is not implemented and must not be inferred from a broad store grant. File names/object_key are server-derived, encryption metadata remains honestly `none`.

Metadata reserve/finalize/read are function-only. Reserve persists pending metadata, immutable request digest and worker/token/fence/lease before filesystem writes. A receipt is returned only after physical file/directory fsync and authorized metadata publish. Reopen/retry uses the same request-derived identity. Stale upload fences and revoked authority are denied. File storage and DB are not described as atomic: a crash after fsync leaves a pending DB row; expiry/takeover validates/reuses the existing file before publishing. No Pi Run/consumer instance/effect failure acceptance is claimed by this Artifact test.

Actual files changed: `authorization.py`, `postgres_artifacts.py`, the two new SQL migrations and catalog, versions.lock.json, .env.example, `tests/authority_fixture.py`, `tests/test_postgres_authority.py`, `tests/test_postgres_artifacts.py`, and bootstrap/role tests adapted to exact new head 0005. AuthoritySigner.from_file requires a service-owned private regular 32-byte file and rejects symlinks; its key material is excluded from repr. The root .env/model key was not read or changed.

## Executed commands and initial failures

- `uv run --frozen pytest -q tests/test_bootstrap.py tests/test_db_boundary.py --tb=short`: 6 passed at revision 0004.
- `uv run --frozen pytest -q tests/test_postgres_authority.py --tb=short`: first failed 9 tests on a fixture missing Eq.node_id. Corrected the actual frozen policy model field. Retry: 9 passed with real PG and actual EIOS resolver/intersection.
- `uv run --frozen pytest -xq tests/test_bootstrap.py tests/test_postgres_authority.py --tb=short`: 13 passed after revision 0005.
- First metadata collection failed on a duplicate comma introduced while expanding synthetic authority scopes; fixed the syntax.
- First metadata test failed with `permission denied for function hmac`: prior default-ACL hardening correctly revoked implicit PUBLIC execution. Granted only hmac(bytea,bytea,text) and gen_random_bytes(integer) to the owner, leaving PUBLIC/app denied. Retry: 7 passed.
- Added cross-tenant metadata denial and an actual child process that exits after fsync but before DB publish. A fresh owner after lease expiry reopens the same physical file, advances the fence and completes publish. Metadata suite: 9 passed.
- `uv run --frozen pytest -q --tb=short`: 241 passed at that checkpoint.
- Added explicit database policy denial, private signing-file checks and administrative-repository-DSN rejection. `uv run --frozen pytest -q tests/test_postgres_authority.py tests/test_postgres_artifacts.py --tb=short`: 21 passed (10 authority + 11 Artifact).

Synthetic credential/signing material is generated only inside disposable test state and not printed, copied to public files or used as a model key. Administrative test setup writes only schema, identity/policy authority configuration and generated signing-key material. Artifact metadata/bytes are exercised through actual restricted API roles and narrow EIOS-governed functions. No existing PostgreSQL instance is touched.

## Remaining scope and blockers

Still required: PostgreSQL retention/GC claims and terminal-owner/binding proof, final-blob orphan reconciliation, lifecycle/audit/outbox integration and complete core assembly; then Compose/doctor/API/Host, governed Consumer/preference/Relation Actions, Inbox/Outbox, actual Pi Run single-owner kill/reopen and the specified S2 failure matrix. No AT scenario is marked passed from this partial scope.

Browser/human/consumer sessions and agent-release/run-bound credentials are not implemented by this service-only adapter. Adding them must preserve the frozen EIOS semantics, not reuse service credentials as human identity. Shared Artifact access needs explicit grants/delegation and tests.

Local CI will record the final current-input result in `artifact-governance-ci.json`. Image digests remain unresolved; Compose was not deployed. Real-model validation remains outstanding implementation; missing embedding/channel inputs affect only their later validations. No staging, commit, push, persistent-environment migration or original production mutation occurred.

## Tenant invalidation follow-up

The first epoch regression run failed with `permission denied for schema control` during the credential foreign-key check. Granted schema USAGE only to nexloop_owner; no application table access was added. Targeted rerun: `uv run --frozen pytest -xq tests/test_postgres_authority.py tests/test_postgres_artifacts.py --tb=short`, 23 passed in 18.43s, including suspended-tenant denial and revoke/restore rejection of old permits.

versions.lock.json advances the independent bootstrap revision from 0003 to 0005. All three upstream commits and Pi frozen-source selection remain unchanged; all six image digests remain unresolved and empty. The new migration catalog hashes match actual SQL; 0001 through 0003 remain unchanged.

Final strict CI command `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check` exited 0: 246 Python cases in 26.39s, 24 actual SQLite FULL cases and all four Pi source packages built; zero skipped/failing/error cases. Input and JUnit SHA256 evidence is in artifact-governance-ci.json. Handoff and publication guards passed. Product ATs remain not_run.

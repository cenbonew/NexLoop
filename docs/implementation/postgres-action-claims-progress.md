# PostgreSQL Action claim checkpoint

NX-006 core assembly progress; NX-012 remains not_started pending API/Host prerequisites. This adds the actual frozen ActionClaimPort's durable ledger, not a business-instance/external-effect endpoint.

## Actual changed files

`0007_action_claims.sql` and independent catalog: tenant/world/intent-scoped claim and consumed decision ledgers, owner-only FORCE RLS, function-only application access, canonical Action target binding, trusted evaluator HMAC, live service identity/current tenant authority revision plus 12 real authority fact row checks, single-use decision IDs and advisory/row locks. Authority and lease times are rechecked after owner lock waits. SQL never grants raw ontology/business-table writes.

`postgres_action_claims.py`: implements the frozen ActionClaimPort reserve/mark_retryable/finalize methods. Input/output use actual strict frozen EIOS models, resource IDs use the upstream canonical resource_id function, and policy decisions use the actual AuthorizationFactsResolver and AuthorizationDecisionService. The evaluator signs a distinct Action command protocol, with exact serialized command/verb/world/principal/resource and expiry. Artifact permits are never interpreted as Action permits. Application SQL credentials alone cannot sign allows. Signing material and service credential remain private server inputs.

Immutable action/capability/request binding and authenticated principal protect the stable intent. A live claim returns in_progress without ownership; altered payload returns conflict; takeover advances revision and changes an opaque fence; stale revisions/fences cannot finalize. Terminal replay returns only the recorded outcome, and changed terminal content conflicts. Mark_retryable is an explicit authorized transition, not an automatic interpretation of an unknown external result.

`tests/test_postgres_action_claims.py`: real disposable PostgreSQL tests via nexloop_api for four-connection unique ownership, terminal replay, payload conflict, lease takeover/reopen and fencing, explicit retryable, revoke-after-claim, wrong tenant, forged signer, changed outcome and a real revoke-after-evaluation/before-SQL race. Administrative setup contains only synthetic identity/policy/signing configuration. Claim data is written solely through restricted EIOS-governed functions; admin readback verifies rejected race made zero claims. No consumer instance or effect is written in this fixture.

`tests/authority_fixture.py` accepts a real ResourceType, selects matching scopes/restrictions and marks Action scope risk sensitive. Bootstrap/role tests and versions.lock.json expect new head 0007. Migrations 0001–0006 remain unchanged. Three upstream commits and unresolved image digests remain unchanged.

## Commands and first failures

- `uv run --frozen pytest -xq tests/test_bootstrap.py tests/test_db_boundary.py --tb=short`: 6 passed in 2.87s.
- First Action run failed fixture validation because an invented `@1` target separator violated the frozen canonical resource ID. Replaced it with actual resource_id(ResourceType.ACTION,stable_name,version) and matching SQL/authority records (`:1`).
- Retry failed output validation: strict enum contracts do not accept Python dict strings. Output now uses model_validate_json on canonical PostgreSQL JSON, matching frozen contracts.
- Next run: 4 passed then one test expected AuthorizationFactDenied but the intentionally stale service epoch correctly raised sanitized AuthorizationUnavailable. Updated the assertion to require that fail-closed outcome; no code relaxation.
- Current targeted suite before final lock-wait review: 9 passed in 7.85s. Final complete local CI verifies the subsequent expiry/lock-wait hardening.

## Limits and next integration

An authorized durable claim is not an ActionExecutionPermit. Production ActionGovernor still must resolve the exact published definition/capability/schema revisions, enforce business preconditions and approval evidence with authoritative ports, then obtain this claim. No default in-memory approval verifier is enabled. Governed instance write contexts and protected storage functions are still required.

ExternalWriteAttempt/unknown-result reconciliation is not yet integrated. Claim lease takeover must not be exposed as permission to repeat an external request with an unknown result. Runtime gets no DSN, HMAC or channel credentials. Actual Pi Run recovery, business mutation/effect scenarios, Runtime-bound Artifact GC, community startup and S2 ATs remain incomplete. No production access/change, credential logging, staging, commit, push or deployment occurred.

Final current-input CI exited 0: 480 Python cases in 40.14s, 24 Pi SQLite FULL cases, four Pi source builds, zero failures/errors/skips. Evidence/input/JUnit hashes in postgres-action-claims-ci.json. Prior migrations 0001–0006 verified unchanged against previous tested-input hashes. Bootstrap head advances 0006 → 0007 only.

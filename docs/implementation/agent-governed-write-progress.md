# NX-012 authenticated Agent governed writes

## Implemented behavior

The backend now derives immutable AgentInvocationBinding exclusively from the
current authenticated PostgreSQL credential. AGENT subject, agent ID, principal
and tenant must agree. SERVICE credentials cannot acquire an invocation or read
Agent release facts; Human sessions remain a different authority path.

Migration0033 adds the protected credential binding and Agent/AgentRelease/Agent
Application fact kinds. The frozen EIOS resolver checks live Agent status,
release activity/revision/digest, published application, scope, grant, policy,
controls and parent application ceilings. Signed dispatch requires complete,
unique SERVICE12/AGENT15 fact coverage, bound to the current credential directory.
All original binding, HMAC, payload, claim and short transaction checks remain.

Embedded parent frames are not independent authority. Resolution compares them
with current parent release, Agent and application records under the same tenant.
Dispatch locks those dependencies before the realm epoch lock and effect commit.
Missing/currently revoked/changed parent records fail closed even after a fresh
credential authentication. Frozen chain checks also reject expanded child
resources/operations and incomplete chains. There is no caller-supplied release
or fake Human identity, Memory fallback or raw business write.

Actual restricted PostgreSQL `nexloop_api` backend tests create a Consumer through
published `Consumer.create` and EIOS Action governance; identical intent reuses
its persisted receipt. Both root and valid parent-bound Agent writes pass. Direct
business-table reads remain denied. Synthetic privileged fixture setup publishes
authority/schema/Action configuration only; it never inserts business objects.

## Verification commands and first failures

```sh
uv run pytest -q tests/test_bootstrap.py tests/test_backend.py tests/test_worker_test_profile.py
uv run pytest -q tests/test_agent_governed_write.py tests/test_bootstrap.py
uv run pytest -q tests/test_agent_governed_write.py tests/test_bootstrap.py tests/test_backend.py tests/test_worker_test_profile.py tests/test_action_definitions.py
uv run pytest -q tests/test_agent_governed_write.py
uv run pytest -q tests/test_agent_governed_write.py tests/test_bootstrap.py tests/test_db_boundary.py
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

First bootstrap run:1 failed/16 passed because a source catalog count still
asserted32; corrected to33. First Agent run:1 failed/11 passed because an empty
scope set is invalid in the frozen model; changed to a valid but unauthorized
scope, preserving actual permission denial. Combined49 then passed in35.02s;
additional17 Agent cases passed in13.59s. Initial CI passed921 Python in247.23s,
24 actual Pi SQLite/four Pi builds and frontend/Host builds, but parent-chain
implementation/tests changed during that CI, so it is not final-current proof.

Parent-chain run initially failed1/passed22 because revoked application
configuration still set eligible=true. Corrected to a valid revoked/ineligible
configuration. Final29 Agent/bootstrap/DB-boundary cases passed in20.92s,
including successful live-parent write and actual parent revocation before SQL.
A fresh full CI follows all final changes, including Service fact-port denial
and same-principal/different-Agent credential rejection.

## Lock and environment changes

versions.lock.json: only extracted core bootstrap_revision0032→0033. Upstream
commits, npm/source correspondence, dependency locks and image digests unchanged.
Original32 SQL checksum records and actual files match the previous S1-tested
wheel byte hashes. Only0033 is new; checksum is recorded in catalog.json.
Integrity evidence: agent-migration-integrity-evidence.json.

No existing/persistent database is migrated; tests use newly owned PGDATA. The
launcher still rejects old catalog/configuration and existing project reuse.
No original EIOS/database, server, Git index/commit/push or public deployment is
changed. Prior S1 evidence remains scoped to its tested0032 image; a fresh0033
Compose recheck is required and recorded separately.

## Remaining scope

NX-012 stays in_progress: docs07§4 also requires short-lived server-issued
run-bound credentials and explicit Run audience binding. Those are not yet
implemented; a long-lived test Agent credential is not presented as a Runtime
credential. No RuntimeAdapter/Run admission, leases, external Action, model call
or overall S2 acceptance is claimed. Real-model/channel validation is unexecuted;
no new external input is needed for the remaining independent code work.

## Final-current results and clean container recheck

Final full CI exited0:929 Python tests in258.71s;25 Agent cases,24 actual Pi
SQLite tests,four frozen Pi builds and frontend/Host builds;zero critical skips.
Existing Starlette TestClient warning and Node SQLite notice remain. Native
Agent evidence: agent-governed-write-evidence.json. AT-052 is passed at the exact
controlled real-PG governed instance-write scope; autonomous model/Pi execution
and overall S2 are not implied. NX-012 remains in_progress for run-bound minting.

```sh
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-agent-foundation-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py run --output .ci-results/community-agent-foundation-20261008
uv run --frozen python .ci-results/collect_agent_foundation_evidence.py
uv run --frozen python .ci-results/stop_owned_agent_foundation.py
uv run --frozen python .ci-results/stop_owned_agent_foundation.py --execute
uv run --with jsonschema --with pyyaml --with rfc3339-validator python docs/handoff/tools/validate_handoff.py --planning planning
```

Fresh actual11-service project `nexloop-test-47631499e61680358966214b` passed0033
bootstrap/doctor,restricted Artifact,HTTPS login/CSRF/session,Host/cache and Worker
checks. External verified HTTPS also passed. This container check does not seed
or execute Agent writes; those are separately proved on native real PostgreSQL.
Product readiness remains503. Exact ownership dry-run preceded stopping the
four running services;all11 exit codes0,volumes/private inputs retained.
Container evidence: community-agent-foundation-evidence.json.

Changed files:

- packages/eios-core/src/nexloop_eios/authorization.py
- packages/eios-core/src/nexloop_eios/worker_test_profile.py
- packages/eios-core/src/eios/migrations/0033_authenticated_agent_invocation.sql
- packages/eios-core/src/eios/migrations/catalog.json
- tests/agent_authority_fixture.py
- tests/test_agent_governed_write.py
- tests/test_action_definitions.py
- tests/test_bootstrap.py
- tests/test_doctor.py
- tests/test_db_boundary.py
- tests/test_compose_bootstrap.py
- tests/test_wheel_install.py
- tests/test_core_sandbox.py
- versions.lock.json
- planning/tasks.json
- planning/acceptance-tests.json
- README.md
- docs/implementation/NX-012-agent-write-gap.md
- docs/implementation/agent-governed-write-progress.md
- docs/implementation/agent-governed-write-evidence.json
- docs/implementation/agent-migration-integrity-evidence.json
- docs/implementation/community-agent-foundation-evidence.json

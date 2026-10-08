# NX-012 server-issued Run credentials

Trusted `AuthenticatedServices.issue_run_credential` resolves each requested Action
through its active published EIOS contract and live authorization. It generates
Run UUID server-side and signs bounded issuance proofs. The restricted API role
calls a narrow PostgreSQL technical authority port; it creates neither formal
business objects nor new grants. Existing SOURCE authority is not expanded.

Only the opaque token hash is persisted. Its source credential/directory, exact
world, fixed audience `nexloop-agent-host`, allowlisted Action resources and
PostgreSQL-clock expiry are persisted atomically before returning the secret.
TTL is1–300 seconds and clipped to source credential expiry. The secret is absent
from DTO repr. No Run ID/tenant/actor/audience override is accepted by issuance.

`Backend.authenticate_run` requires expected Run ID and fixed audience. A Run
credential is rejected by ordinary root authentication, wrong Run/world/audience,
and nested issuance. Current source credential/authority change or expiry revokes
its usability; cached services recheck at every authorization and SQL dispatch.
Run and source rows are locked through the short effect transaction. Action target
restriction applies both to protected fact reads and SQL mutation permits. Run
ID/audience are server-derived query attributes for current policy evaluation.
Directory hashing normalizes UTC across connections. Human sessions remain
separate. Runtime receives no database/admin/signing/channel/model credential.

## Executed checks

```sh
uv run pytest -q tests/test_run_credentials.py tests/test_bootstrap.py
uv run pytest -q tests/test_run_credentials.py tests/test_agent_governed_write.py tests/test_backend.py tests/test_worker_test_profile.py
uv run pytest -q tests/test_run_credentials.py
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

First10 passed in7.67s. Expanded53 passed in41.52s. After enforcing the exact
published Action contract at issuance, final15 Run cases passed in13.77s. No
failure occurred in these focused runs. Real PG checks include actual governed
Consumer.create/receipt replay, context mismatch, expiry, source credential and
grant/release/Run revocation, revoke after signing before SQL, unauthorized target,
budget rejection and timezone invariance. Root and live-parent Agent tests pass.

New migration0034; previous33 SQL/checksum records match the actual previously
S1-tested0033 wheel. versions.lock.json changes only extracted core head0033→0034.
Dependency locks, upstream commits and base image digests unchanged. No persistent
or original EIOS database migration, deployment, Git staging/commit/push.

This is the trusted backend authority port, not an already integrated Host HTTP
Run/tool interface. Actual Pi Run admission/proxy/recovery, queues/fences and real
effect/unknown reconciliation remain under subsequent S2 tasks. Product ready503;
no model/channel call or complete autonomous operational loop is claimed.

Full CI first run:943 passed/1 failed (267.10s). Published-contract failure
changed while pytest had already loaded the old exception assertion; final
assertion accepts explicit ActionAuthorizationDenied. Final15 focused passed
then current-file full CI passed944 Python in271.10s,24 Pi SQLite, four Pi
packages and frontend/Host builds; zero critical skips. Existing Starlette
deprecation and Node SQLite experimental warnings remain.

Actual changed files: authorization.py, backend.py, new run_credentials.py,
new migration0034/catalog.json, versions.lock.json; new test_run_credentials.py
and head assertions in bootstrap/doctor/db-boundary/Compose/wheel/core-sandbox
tests; README and Run scope/progress/evidence documents. Original33 SQL
bytes and checksum records match the previous actual tested0033 wheel.

Fresh disposable Compose executed (never reused existing volumes):
```sh
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run python scripts/community_test.py prepare --output .ci-results/community-run-foundation-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run python scripts/community_test.py run --output .ci-results/community-run-foundation-20261008
uv run python .ci-results/collect_run_foundation_evidence.py
uv run python .ci-results/stop_owned_run_foundation.py
uv run python .ci-results/stop_owned_run_foundation.py --execute
```
Project nexloop-test-3abccb89028e4b3bf91f364e: actual11 services, clean0034
bootstrap34 migrations, restricted Artifact/Worker, Host TLS/owner, Valkey
TLS/ACL, browser login/session/CSRF and external CA-verified HTTPS passed.
Build had PyPI read-timeout retry, then completed successfully. Collector
verified no generated password in logs. Exact project/purpose ownership
dry-run preceded stop; all11 exited0, volumes retained. Agent/Run writes
are separately proved by native PG tests, not claimed inside this container.

NX-012 backend authorization deliverable and docs07 section4 audited complete;
AT-052 retains actual governed write proof. No other S2 acceptance promoted.
Next NX-013 is durable Inbox/Outbox/queue. No new input needed for that work.
Real DeepSeek/channel validation remains not_run; provider credentials are
needed only for their eventual live validation. Embedding choice remains S3.

# NX-013 durable Inbox/Outbox and queue

Technical EIOS ports extend the existing runtime.invocations/jobs/job_events;
there is no second job registry, Memory fallback, raw business writer or external
provider I/O. Migration0035 adds task scheduling fields and tenant-RLS Inbox and
Outbox. Source/event/world deduplication and canonical input digest persist with
invocation, job, event and Outbox in one short transaction before acceptance.
Same event with different payload/queue/budget conflicts without overwriting.

The trusted inbound adapter uses nexloop_api with current EIOS queue Action
permission. Domain Worker and scheduler claim with their own distinct credentials;
lease ownership is server-derived credential digest, never supplied actor. Queue,
tenant/world, source grants/application ceilings and policy revisions are checked
through the frozen resolver and a signed narrow SQL port at each operation.
Run credentials cannot acquire scheduler authority. Raw ledger access is denied.

PostgreSQL-clock due time, bounded attempts/leases, SKIP LOCKED, monotonic fence,
renewal, retry wait and expired-attempt deadletter are persisted. Task and Action
leases are separate. Old fence/other worker/expired lease cannot finish. Final
write delay cannot extend the old lease: claim/renew/finish/Outbox claim/ack verify
lease before returning and roll back on expiry. Lost completion replies can be
retried with the same fence/credential/canonical payload; conflicting result is
rejected. inspect returns the authoritative current task status/result.

Outbox notification claims/reclaim/ack have independent fence and bounded lease.
PG polling works without Valkey. Ack says wakeup notification delivered, not an
external business effect. No channel send or provider receipt is fabricated.
All Backend entry points keep lifecycle locking and current authorization.

## Actual checks and first failures

```sh
uv run pytest -q tests/test_bootstrap.py
uv run pytest -q tests/test_durable_queue.py
uv run pytest -q tests/test_queue_process_recovery.py
uv run pytest -q tests/test_queue_permit_expiry.py
uv run pytest -q tests/test_queue_commit_lease.py
uv run pytest -q tests/test_backend_queue.py
uv run pytest -q tests/test_durable_queue.py tests/test_queue_process_recovery.py tests/test_queue_permit_expiry.py tests/test_backend_queue.py
uv run pytest -q tests/test_community_container_contract.py -k 'root_docker_context or worker_is_separately'
```

Bootstrap first4 passed. Initial queue17 failed/1 passed: synthetic scheduler
provisioning advanced realm epoch after API authentication; test fixture now
refreshes sessions after all authority configuration without bypassing checks.
Next first-case rerun failed due extensions.gen_random_uuid execute privilege;
changed only new0035 to pg_catalog.gen_random_uuid. Process tests first3 failed
for the same UUID issue, then final3 passed7.75s. Queue19 passed21.61s before
adding receipt replay. Intermediate combined29 passed37.17s and36 passed43.23s.
Backend first10 passed9.04s; subsequently added inspect/closed handle and ack replay.
Permit tests first3 passed4.88s: valid signature with missing/future expiry and
actual advisory-lock wait beyond proof expiry reject with no ledger records.
Final-commit lease tests first5 passed14.54s: five real PG AFTER UPDATE delays2s
against lease1s roll back every original row/event. Docker boundary2 passed.

SIGKILL cases use spawned actual restricted processes on owned disposable PG:
committed event survives/replays, Outbox abandoned lease reclaims with new fence,
uncommitted acceptance has no ACK and no partial ledger after connection ends.
Five technical tables are inspected as evidence; privileged fixtures provision
only synthetic authority/schema/faults, never formal business objects.

## Changed files and remaining scope

New durable_queue.py and migration0035/catalog entry; Backend queue methods;
new queue/Backend/process/permit/final-lease tests and catalog-head assertions;
versions.lock.json extracted core head0034→0035. Previous34 SQL remain frozen.
Dependency/upstream/base-image locks are unchanged. Community Dockerfile splits
hash-locked requirements install before wheel COPY to retain dependency cache;
no change to hash/no-deps/UID/secret restrictions.

The current work provides real technical queue acceptance/recovery ports, not a
fully integrated inbound channel or Pi Run. AT-013's one message/necessary Run and
AT-034's HTTP409 require later integration. External unknown AT-033 remains
NX-015–017. Pi RuntimeAdapter source audit records real API and request digest/
SQLite FULL gaps; no Run recovery success is claimed. Product remains not ready.
No staging/commit/push/deployment/persistent migration or original EIOS mutation.

Final combined46 passed58.87s (42 new queue/Backend cases plus4 existing
bootstrap). Additional final old-fence-after-new-result check1 passed2.24s.
Current-file complete CI exit0:986 Python in332.03s;24 actual Pi SQLite FULL,
four Pi and frontend/Host builds; zero critical skips. Existing Starlette
deprecation/Node SQLite experimental warnings remain.

Fresh independent11-service Compose tested current0035/35-migration wheel:
```sh
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run python scripts/community_test.py prepare --output .ci-results/community-queue-foundation-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run python scripts/community_test.py run --output .ci-results/community-queue-foundation-20261008
uv run python .ci-results/collect_queue_foundation_evidence.py
uv run python .ci-results/stop_owned_queue_foundation.py
uv run python .ci-results/stop_owned_queue_foundation.py --execute
uv run python .ci-results/check_dependency_cache.py
```
Project nexloop-test-16db15379203d41a0e2a1bc4 passed clean bootstrap, doctor,
restricted Artifact/maintenance Worker, browser HTTPS login/session/CSRF, Host
TLS/owner and ValkeyTLS/ACL; external CA-verified HTTPS passed. All11 exact
owned containers stopped exit0 after ownership/purpose dry-run; volumes retained.
Queue acceptance/leases are proved separately on native PG, not falsely claimed
inside this container. Product readiness stays503. No model/channel call.

Actual cache probe changed wheel0035 to previous tested0034 only in a new
public-file-only private build context (no container started). Requirements
layer CACHED and wheel COPY not CACHED; complete second build1.36s. Actual new
requirements installation layer took161.3s on the first build. No image pushed.

NX-013 deliverable done; AT-030/031 passed for real PG task/outbox Worker scope.
AT-013/033/034 remain not_run for their full channel/Run/external/HTTP semantics.
NX-014 source audit is in_progress; implementation still required. Missing
external credentials only affect later real model/channel validation. No new
input is required to continue deterministic Pi integration.

# NX-013 durable queue source audit

Read actual extracted migration0001, runtime/jobs.py, kernel/ports/repositories.py
and docs06 sections2–5/8. The existing runtime.invocations, runtime.jobs and
runtime.job_events already define tenant/idempotency and invocation-job links;
Artifact retention/orphan SQL queries these tables. They are the durable ledger
to extend, rather than a separate second job registry. The extracted jobs table
lacks queue priority, attempts, due time and lease/fence fields. JobSnapshot
contains scheduling fields, but runtime/jobs.py supplies only InMemoryJobStore;
no extracted PostgreSQL JobRepository implementation was found. It cannot be
used as a runtime fallback. Existing ActionClaimPort fencing is a separate
Action lease, not the Run/task lease.

Inbox acceptance must persist event_id and normalized-payload digest, task and
outbox in one protected short transaction before ACK. Replay with same payload
returns the original task; different payload with same id must conflict.
Scheduler claims must be bound to authenticated tenant/world and use PG clock,
SKIP LOCKED, monotonic fence and bounded lease/reclaim. A stale fence cannot
commit result, even when the previous worker eventually returns. Outbox wakeup
can be retried; Valkey failure must leave PG polling viable. Provider HTTP
must not occur under a long database transaction.

No queue implementation or AT-013/030/031/033/034 success is claimed by this
audit. Next implementation extends existing EIOS ledger through narrow
restricted-role technical ports, with actual independent PG connections for
dedupe, atomic rollback, concurrent claim, lease expiry and old-fence rejection.

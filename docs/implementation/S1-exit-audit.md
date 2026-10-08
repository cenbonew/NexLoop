# S1 task-delivery audit against the frozen handoff

Authoritative scope: docs/handoff/docs/15_BACKLOG_AND_MILESTONES.md §2–3 and live
planning/tasks.json. S1 requires a clean PG/local Artifact foundation plus running
identity, contracts and local tests. NX-009 asks for explicit single-machine test
startup without TOS/GitHub/real model keys/server access. NX-011 asks for health,
same-origin login, internal authentication and contract generation. Queue leases,
Pi Run recovery and real effects are the S2 exit, not prerequisites for these S1
foundation tasks. No product-readiness or overall goal completion follows from
S1 task completion.

| Requirement | Actual evidence |
|---|---|
| Clean PG/bootstrap, local Artifact, no Memory fallback | Fresh pinned PostgreSQL18.4 Compose; independent0032 lineage; restricted EIOS Artifact write/read/digest and doctor in actual check container; community-worker-runtime-evidence.json and full real-PG CI |
| Independent explicit test environment | New random project and volumes; whitelisted public build contexts; no TOS/upstream production mounts; no server access or model key read;11 inspected services; existing project reuse refused |
| API/Worker foundation | Same actual Python image, separate nonroot entries and private DSNs; governed maintenance role/scopes; physical orphan removed and durable resume verified |
| Health checks | Actual HTTPS live200, foundation verified but product ready503; browser capability rechecked through restricted identity port |
| Same-origin login | Actual canonical PostgreSQL synthetic test realm, Argon2, secure cookie/session/CSRF/logout; external verified localhost HTTPS and earlier native desktop/mobile Chromium proof |
| Internal authentication | Actual Node Host TLS+private control key, wrong/anonymous/Origin denial, inherited kernel owner lock, previously verified SIGKILL/reopen Host lifecycle |
| Contracts | Six canonical target schemas generated into OpenAPI/TypeScript; structural drift gate and actual frontend/Host TypeScript builds |
| Local CI/no critical skip | scripts/ci/check; missing-PG actual negative gate exits1; failed/error report rejected; zero critical skips in full CI |

AT-051 scope is proved by the clean independent startup and restricted actual
Artifact/PG evidence. AT-047 is proved by the actual missing-PG failure/report
rejection and strict pipeline, not merely by a green test count.

No blanket acceptance promotion: AT-001/002/003 remain not_run until the final
product/API/property/retrieval scenarios have been executed at their full scope.
In particular there is no vector subsystem or permission-safe vector response
claim. No S2 acceptance is promoted. Pi SQLite FULL conformance and Host process
reopen are not actual Pi Run recovery. Real-model/channel validation and the
controlled operational loop remain unverified.

The new Worker is bounded Artifact maintenance, not a lease-based Action queue.
The real-effect queue/scheduler/discovery and RuntimeAdapter are still required
under NX-013–017. Business image registry digests remain unresolved; these local
built image IDs prove isolated tests only. No deployment/commit/public push.

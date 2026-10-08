# NexLoop — Engineering Contract

This repository is NexLoop, an open-source enterprise continuous-operations system. Product and engineering specifications are in `docs/`. Respond to the project owner in Chinese; identifiers and public API names remain English.

## Read first

Read `docs/handoff/00_START_HERE.md`, `docs/handoff/docs/01_PRD.md`, `02_ARCHITECTURE.md`, `07_EIOS_INTEGRATION.md`, `14_TEST_ACCEPTANCE.md`, then `planning/tasks.json`. The current stage is the lowest stage that still has tasks not in `done`. Local source paths and dev-machine facts are in the private annex `local-only/LOCAL_SOURCES.md` (kept outside git). Follow this contract and the ADRs in `docs/handoff/docs/16_ADR.md` plus any newer ADR under `adr/`, not guesses about earlier discussions.

Repository layout: the handoff package's public part lives frozen under `docs/handoff/`; `planning/` at the repo root is the live copy where task/acceptance status and evidence are updated; `packages/contracts/` is the only live copy of the JSON Schemas; `versions.lock.json` at the root is the single lock for upstream commits, package versions and image digests.

## Fixed decisions

- Real operations and action reliability precede consumer-twin simulation. No industry is required.
- Goals, ontology, relationships, memory and commitments persist; runs are short-lived and event-driven. Roles and consumers are N:M, not permanently resident agents.
- NEX-EIOS is an internal open-sourceable component, **vendored**: the generic core is copied into `packages/eios-core/` from the frozen commit with provenance recorded in `versions.lock.json`. NexLoop has no runtime, pip or git-submodule dependency on the NEX-EIOS repository, imports none of its git history, and copies no tennis/sports8/TOS domain code. Divergence from upstream is expected; fixes are cherry-picked by hand. The owner has confirmed permission to open-source its code. Preserve third-party licenses and remove production secrets/data.
- Pi Durable is the initial harness, behind a RuntimeAdapter. Do not introduce a second harness without an ADR.
- EvoOntology is selectively reused for semantic access, candidate evolution and evaluation. It must not become a second business-fact store or a way to bypass EIOS.
- PostgreSQL owns business state and durable queues. Valkey is disposable. Pi-local SQLite is an explicit temporary runtime-only exception, single owner, with tested durability and recovery.
- Local CI is authoritative. GitHub is a code synchronization/community remote, not a required online CI controller.

## Safety and correctness invariants

Only governed EIOS Actions mutate formal business objects or cause external effects. Never solve a blocked integration with direct SQL, superuser credentials, fake human identity, or an ungoverned endpoint.

Derive tenant/actor/world/scope from authenticated server-side context. Enforce current permissions and contact constraints at dispatch, not only at planning. Runtime has no DB/admin/channel secrets and no unrestricted production shell.

Persist before ACK. Stable business-intent IDs survive retries and restarts. An unknown external result is not a failure to blindly retry. Query/reconcile first. Do not claim generic exactly-once.

Facts, user statements, hypotheses, formal schema, semantic views and simulated outputs are different. Keep evidence, validity, revisions and correction/deletion propagation. Simulation/shadow cannot invoke real-effect credentials.

Do not require hidden chain-of-thought. Record observable inputs, tool calls, evidence and concise decision rationale.

## Implementation workflow

Start with S0 source/environment audit, then the smallest vertical slice. Every task gets code, tests, evidence and a status update. Do not mark placeholders, mocked backend buttons, skipped critical integration tests or unexecuted commands as complete.

Task status vocabulary: `not_started | in_progress | blocked | done`; acceptance status: `not_run | passed | failed | blocked | skipped`. `done`/`passed`/`failed` require at least one evidence entry `{type, ref, summary, recorded_at}` (`type` ∈ command | test_report | file | commit | note). Run `docs/handoff/tools/validate_handoff.py --planning planning` after each status change.

All interfaces in `contracts/` are NexLoop target contracts, not existing upstream APIs. Verify EIOS/Pi APIs at frozen commits before writing adapters. Inspect the actual Migration Catalog rather than trusting stale README revision numbers.

Do not import old EIOS production history/configuration wholesale. Build an independent clean bootstrap lineage for the extracted core; never apply it to the original production database.

Use parameterized SQL and short transactions. One migration owner per schema. Do not let an ORM auto-create/alter EIOS tables. Preserve published checksums.

Keep Python business modules and TypeScript runtime contract-driven. Keep credentials out of prompts, frontend builds, logs, fixtures and commits.

## Deployment

Read `local-only/LAN_PLAN.md` when present for the private two-host mapping. Public deployment examples must stay configurable. Obtain real environment facts through read-only inventory; do not guess SSH users, passwords or hardware.

The implementation repository's GitHub remote is PUBLIC. Before the first commit, create `.gitignore` from `deploy/examples/PUBLICATION_EXCLUSIONS.example` and additionally ignore the raw handoff drop directory; never `git add -A` before that. No migration without a verified backup. No destructive cleanup outside a designated disposable test environment. Do not stop unrelated services or change existing production DBs. Do not publicly push local-only material or old secrets.

Build and test in isolation. Unreviewed external PRs must not run with LAN or secret access. An image's deployed SHA/digest must match its tested release manifest.

## Completion report

Report changed files, task/acceptance IDs, exact commands actually executed, results including first failures/retries, observed limitations and remaining blockers. Separate test/demo/real evidence. Missing credentials block only the relevant external validation; continue useful independent work and never invent success.

Keep this file small. Put detailed specs, API contracts, runbooks and ADRs in their dedicated documents.

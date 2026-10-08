# S0 implementation report — 2026-10-07

## Delivered task evidence

NX-001: public .gitignore/.env.example/handoff/live planning/contracts baseline; private raw package and pre-existing server-baseline preserved and ignored. Git-ignored .env was prepared without a credential value; final presence-only check now detects a nonempty MODEL_API_KEY, with no value emitted. Publication normalization removed dangling private-annex links only in the public copy and regenerated its manifest.

NX-002: 24 capability inventory records (source paths, hashes, interfaces, initializer-aware Python import closures, SQL qualified dependencies, keep/adapt/exclude, upstream test candidates, licensing), write-path audit reading 0061/0313/0167, all 332 migration names/hashes and conservative exclusions. Executing upstream `expected_database_migrations()` with bytecode writing disabled confirmed count=332, revision=0332 and head checksum=018ce7505278db4d41090976b126809346da2755d3c2d7308d263c614cbb5576.

NX-003: Pi and Evo cloned and checked out at specified commits. npm Durable 1.0.4 is verified to **differ** from frozen source; its gitHead and both package tree hashes are in versions.lock.json. Pi's SQLite defaults NORMAL; public facade allows FULL override. Frozen source workspace and MIT notices are present; all four packages compiled and 24 SQLite FULL conformance tests passed. Evo's actual semantic/evolution/evaluation modules and license are audited, not installed as a second business store.

NX-004: both SSH aliases and sudo -n revalidated, public inventory omits IP/fingerprints. Rootful Docker, Wi-Fi/mirror requirements and router reservation evidence boundaries recorded. No server mutation occurred. Local tools verified, Python pinned 3.12, Node pinned 24.13.0 with independently verified archive SHA-256.

NX-005: target schemas copied, fact ownership maintained, MIT/NOTICE/source attribution plan established; third-party MIT texts preserved. Source lock has all three commits. Image digests remain empty/unresolved_not_deployable.

## Commands actually executed

- `git status --short`; `git check-ignore .env docs/tmp/.../local-only/LOCAL_SOURCES.md`; requested private-address scan; `uv run python scripts/check_publication.py` (later frozen runs).
- Python pathlib/shutil publication copy excluded local-only and merged documents, blanked env assignments, created planning/contracts live copies, preserved pre-existing files.
- `git -C <frozen-eios> rev-parse HEAD`; `uv run python scripts/audit_eios.py <frozen-eios>`; source reads of 0061/0313/0167, migrations.py, revision.py, source models/adapters and named test files.
- `git clone https://github.com/earendil-works/pi <upstream>/pi` and checkout adae8246453a2928a268ccecb9fb55125d96d0af; Evo clone and checkout f64413dae88d88645b1f2c069cf4e17308ad0f89.
- `npm view @earendil-works/pi-durable@1.0.4 version gitHead dist.integrity dist.tarball --json`; git diff/rev-parse comparing published and frozen package trees.
- Both aliases: `ssh -o BatchMode=yes -o ConnectTimeout=10 <alias>` running uname -m, os-release, nproc, free, df, sudo -n true, sudo ufw status, docker info/compose version, timedatectl and ss.
- `uv python pin 3.12`; `nvm install 24 && nvm use 24` (failed); `nvm install -b 24.13.0 && nvm use 24.13.0` (passed); independent official SHASUMS256 archive verification.
- `uv run --with jsonschema --with pyyaml --with rfc3339-validator python docs/handoff/tools/validate_handoff.py --planning planning`; initial publication repair added `--write-manifest`, then normal reruns passed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<frozen-eios>/src uv run --frozen python` invoking only expected_database_migrations and printing count/head/checksum; no original DB connected.
- `uv run python scripts/vendor_eios.py <frozen-eios>`; explicit initializer-closure repair; `uv sync`; `uv run pytest -q tests/test_bootstrap.py`; `uv run pytest -q tests/upstream`; `uv run pytest -q --tb=short`.

Full absolute private/local source paths are not needed to reproduce the public workflow; supply the source argument according to private LOCAL_SOURCES on the development machine.

## Initial S1 implementation evidence (not a completed S1)

73 selected EIOS source files plus new nexloop_eios assembly/bootstrap. Independent catalog owns byte-identical 0001 generic schema and new 0002 restricted roles/RLS; this is not an assertion of upstream revision 0332 equivalence. StorageSettings disables Memory fallback. Application assembly rejects administrative/BYPASSRLS/role-management DSNs. Unit tests run in disposable PGDATA/Unix sockets with per-test clusters; administrative connections are limited to bootstrap/catalog/system checks. Negative business writes use actual non-superuser app/worker/runtime roles and are denied.

Python final result: **168 passed**. 162 are upstream Policy/Application tests, 6 are NexLoop source/config/bootstrap/role/assembly checks. No governed Consumer write, real-effect Action, Pi Run kill/reopen, Inbox/Outbox failure matrix or S2 acceptance success is claimed. NX-006 remains in_progress until its retained storage/governance assembly is complete; later tasks remain not_started rather than done from this groundwork.

## Failures and retries

1. Source audit guessed two nonexistent paths; corrected using actual files. Initializer import closure initially omitted identity exports; fixed closure including namespace-package handling and expanded provenance to 73 files.
2. Initial uv sync failed because unsuccessful extraction had not created the workspace member; successfully extracted and locked on retry.
3. Public handoff validator initially failed private-annex links and manifest entries; publication-only normalization and manifest rebuild passed all 69 static checks (six schemas, eleven negative contract cases). This is handoff validation, not product acceptance.
4. Latest Node install fell back to source and failed without Xcode/CLT. Explicit 24.13.0 binary installed; nvm's empty-checksum warning was resolved with independent official SHA-256 verification.
5. Upstream test collection failed on Hypothesis, initializer-imported modules and Argon2; locked missing dependencies and re-ran: 162 passed. Unknown performance mark registered.
6. Import smoke initially imported a private circular helper before its public entry point. Smoke now imports public modules in supported entry order, still verifies hashes of every private source file. Final suite passed.
7. Adding assembly tests exposed a shared-PG test-order dependency; switched to per-test disposable clusters. Final Python suite passed 168 tests.
8. npm install repeatedly encountered ECONNRESET and slow fetches; interrupted only the verified task-owned pnpm PID, retained cache, validated mirror HTTP 200 and retried via project registry mirror with bounded timeouts. Retry installed locked dependencies and four source packages compiled; real SQLite FULL conformance passed 24 tests.

## Remaining inputs and limits

- Final presence-only check detects nonempty MODEL_API_KEY in ignored .env. No credential value was emitted, copied into public files or submitted. Real-model validation is not yet run because RuntimeAdapter/Provider integration remains outstanding; this is implementation work, not a missing-key blocker. Stage must use model_credentials secret precedence. Deterministic provider still needs RuntimeAdapter integration; no model call has been invented.
- Embedding provider is a future S3 input; use FTS/pg_trgm first. This does not block S0.
- Docker Desktop daemon was unavailable during local inventory; Compose and image digest resolution remain future work, not deployed artifacts.
- AT-013/030/031/033/034/052 remain not_run. S1/S2 implementation remains required. Missing external-channel credentials block only corresponding later controlled channel validation.
- No staging, commit, push, original production mutation or persistent-environment migration was performed.

## Pi build failures and final foundation CI

9. First source build failed because frozen Git excludes generated provider JSON and `@smithy/types` was only transitively available. Added explicit compatible Smithy version and immutable model-catalog hydration using original generator scripts; all source packages then compiled.
10. Running frozen install immediately after changing package metadata correctly failed with outdated-lockfile. Regenerated the lock explicitly; subsequent frozen install passed.
11. First Vitest startup failed on missing Rolldown arm64 native binding. Pinning Vite 8.0.16/Rolldown 1.0.3 from upstream frozen package-lock resolved it without deleting the lock or unrelated files. Final conformance: 24 passed, WAL and FULL verified per database.

Additional actual commands: `pnpm install --no-frozen-lockfile`; `pnpm install --frozen-lockfile`; `pnpm build:pi`; `pnpm exec vitest run vendor/pi/packages/durable/test/nexloop-sqlite-conformance.test.ts`; `uv run python scripts/check_pi_source.py`; `source "$HOME/.nvm/nvm.sh" && nvm use && scripts/ci/check`.

Lock changes: Pi selected artifact now records frozen-source build/conformance passed, pnpm dependency lock, source/generated provenance and immutable model catalog. Three upstream commit hashes are unchanged. Every image digest remains empty/unresolved. Local CI is a foundation gate, not a declaration that NX-010 or S1/S2 product acceptance is complete.

Final foundation CI completed with exit 0: publication 563 paths/0 violations; Pi integrity 303 source/43 generated files; handoff 69 checks; Python 168 passed in 4.51s; frozen install and four source builds passed; Pi conformance 24 passed. Machine-readable evidence: `foundation-ci.json`.

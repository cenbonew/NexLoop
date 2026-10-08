# Pi and EvoOntology frozen-source audit

## Pi package mapping

Source HEAD verified: `adae8246453a2928a268ccecb9fb55125d96d0af`. `packages/durable/package.json` reports @earendil-works/pi-durable 1.0.4 and Node >=22.19.0. `npm view @earendil-works/pi-durable@1.0.4 version gitHead dist.integrity dist.tarball --json` reports publication commit `7c10bd4337495ee613f2224843ecdf349b80d1df`, integrity `sha512-i22unyhavxrTF/KS/JcBOKprQRgepufOIztnqBKZXJCWbEHsXDzcINtjmmBSbFB/3S+4FWIl3eJ5FMbRe33Ybg==`.

`git rev-parse <commit>:packages/durable` produced published tree `3b5dadefd8d3a9b64401f63b6f74cc32bc2ddea3` versus frozen tree `b32bdd869cd4325e402d9a67156a0a22452667fe`. `git diff --name-only <published> <frozen> -- packages/durable/src` shows 17 changed files, including generation, harness, session transaction, SqliteStorage, conformance and types. Matching version text is insufficient. **Use a reproducible build from the frozen source, not registry Durable 1.0.4.** Frozen source is now vendored with 303 recorded source-file hashes and 43 generated-file hashes. All four workspace packages compiled with Node 24.13.0/TypeScript 6.0.2. The upstream storage conformance entry ran 24 tests against real SQLite with WAL and explicit FULL readback: all passed. This is storage evidence, not Harness Run kill/reopen acceptance.

## Actual APIs and durability

`src/index.ts` exports Harness, configure, createRegistry, Session, defineTask and typed task/conversation/submission APIs; there is no presumed generic createRun/resumeRun API. NexLoop Run must map to persisted conversation/task/submission identities with stable requestId. `harness/submissions.ts` looks up existing submissionByRequest; do not infer business-payload conflict checks from it.

`storage/sqlite/node.ts` exports `openNodeSqliteDatabase`, `NodeSqliteDatabase` and `openNodeSqliteStorage`. It opens node:sqlite DatabaseSync, sets WAL, then **synchronous=NORMAL**, busyTimeout 5000 ms, checkpoint 1000 pages. It serializes operations and transactions with BEGIN IMMEDIATE/COMMIT, rollback on failure, and checkpoint TRUNCATE at close. Since SqliteStorage.open accepts the public database facade, RuntimeAdapter can call openNodeSqliteDatabase, explicitly set PRAGMA synchronous=FULL and read it back (2), then initialize SqliteStorage. No upstream fork is needed for this override. The wrapper needs exclusive single-owner lifetime locking across processes, bounded runtime tools and kill/reopen tests; those are NX-014 work, not S0 completions.

Conformance entry: `src/testing/storage-conformance.ts`; exported via `./testing`. Candidate tests: `test/harness-generation-recovery.test.ts`, `test/harness-tool-recovery.test.ts`, SQLite node tests, session transaction tests. These are source-located tests; only executed commands in phase reports may claim passed results.

License: MIT, full notice preserved in licenses/Pi-MIT.txt. Current package required dependencies include chord/pi-ai/diff/typebox; pi-ai brings SDKs and telemetry. Frozen dependency locks must cover the actual build rather than floating ^1.0.4 resolving to another source revision.

## EvoOntology

Verified source HEAD `f64413dae88d88645b1f2c069cf4e17308ad0f89`, package version 1.1.0, Python >=3.10, no mandatory external dependencies, MIT (licenses/EvoOntology-MIT.txt). Actual modules: `runtime/runtime.py` SemanticLayer (browse/resolve/manifest), `ontology/store.py` SemanticStore (versioned semantic records), `evolution/session.py` EvolutionSession lifecycle, `evolution/adapter.py` adapter boundary, `evaluation/evaluation.py` EvaluationGate, `trajectory/trajectory.py` trajectory recording and `trigger/trigger.py` evolution triggers.

Selective reuse keeps semantic views and candidate/evaluation state apart from EIOS business objects. Do not install the BIRD example dependencies or use their direct database actions as a NexLoop write bypass. S3 read adapter and S6 candidate/evaluation behavior remain separate tasks; no Evo service has been deployed.

## Reproducible source build evidence

`pnpm-lock.yaml` covers workspace snapshots rather than the mismatched registry package. Frozen source omitted Git-ignored provider data. The frozen `nix/model-catalog.json` pins immutable revision `sha256-439c53478c84ed27a58f8b54e1c3bd45810e5a969515b4e3666a3898d51ee22f`. Downloaded that exact public catalog, verified its SHA-256, and used the original frozen hydrate/check/model-data scripts to generate provider JSON with deterministic epoch timestamp. `GENERATED.json` records every output; `scripts/check_pi_source.py` validates both catalog and source/output bytes offline.

The source build also required explicit `@smithy/types` 4.18.0 plus an override to avoid incompatible hoisting/version identity. Vite 8.0.16 and Rolldown 1.0.3 match upstream's frozen lock: the initially floating Rolldown 1.2.13 lacked an available arm64 native binding in the mirror. After locking the original versions, conformance startup and all 24 cases passed. No model credentials or real Provider calls were involved.

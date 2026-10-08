# Model configuration startup gate

Core/Host prerequisite progress; no model network call and no completed Host/RuntimeAdapter claim.

## Changed files and behavior

`packages/eios-core/src/nexloop_eios/model_profile.py` supplies an immutable secret-safe ModelProfile and explicit configuration loader. Trusted callers may select an ignored local .env file; the loader accepts only MODEL_ assignments, does not expand shell variables or execute syntax, and process environment overrides local-file values. A nonempty MODEL_CREDENTIALS_FILE wins over MODEL_API_KEY. An empty private file falls back to env only in development; stage refuses environment-only keys and development env-file loading. Stage may run explicit test mode without a key. Secret/env files must be private regular files owned by the service, not symlinks, and are size bounded. The stage Compose adapter must mount its secret with the matching UID and private mode; Compose is not yet delivered.

Credentials are raw UTF-8 single-line values with optional surrounding whitespace/final newline, not JSON objects. API key bytes never appear in repr/public_configuration or error messages. credential_for_provider is a trusted Host-only boundary, not an API/model/prompt field. No root .env/current real key was read, modified or sent by this checkpoint.

The supported real profile defaults to deepseek/deepseek-flash/https://api.deepseek.com and requires that exact maintainer allowlist endpoint; URL credentials, alternate hosts, ports, HTTP, paths and query strings are rejected. Missing keys yield provider=test/model_id=deterministic-test with validation_mode=test_only_real_validation_blocked. A configured key labels real_validation_pending, never success. This is configuration selection, not an implemented deterministic inference provider or a completed real-model validation. Pi remains the required Harness/provider integration path; no alternative Python model client was introduced. No embedding interface is assumed.

`tests/test_model_profile.py` contains 13 cases using runtime-generated synthetic tokens in private disposable files: priority, empty file fallback, stage restrictions, .env override, explicit test labeling, secret-safe repr/errors, approved model egress and symlink/permission/size refusal.

`postgres_action_claims.py` now sanitizes malformed-result decoding into action_claim_result_invalid without exposing native errors/fence/claim content. `tests/test_postgres_action_claims.py` adds a real SQL reserve followed by fault-injected parser failure; synthetic native detail is absent from the raised contract error. The durable reservation is not undone by a failed result decode; callers must use the existing stable intent/recovery semantics. No external effect retry is enabled.

## Commands and results

`uv run --frozen pytest -q tests/test_model_profile.py`: 13 passed in 0.07s.

`uv run --frozen pytest -q tests/test_model_profile.py tests/test_postgres_action_claims.py`: 23 passed in 8.58s. No initial test failure in this checkpoint. Full CI current-input evidence follows below.

versions.lock.json is unchanged: bootstrap head 0007, frozen upstream commits unchanged, images unresolved. No schema migration was added/applied outside isolated tests. No staging, commit, push, deployment, credential logging or model/provider network call occurred.

## Remaining work

Host must consume this profile and use actual frozen Pi APIs, including an explicit deterministic provider, credential isolation and authenticated model-call audit; real-model verification remains unperformed. Complete core ActionGovernor/definition/approval assembly, community Compose/doctor, governed instance/effect Actions, Pi Run kill/reopen, bound Artifact lifecycle and specified S2 acceptance remain incomplete. Embedding/channel inputs affect their later validations only; core code remains actionable.

Final current-input local CI exited 0: 494 Python cases in 40.40s, 24 Pi SQLite FULL tests and four frozen-source package builds, zero skips/errors/failures. Input/JUnit hashes in model-configuration-ci.json. No real model validation is inferred from these configuration tests.

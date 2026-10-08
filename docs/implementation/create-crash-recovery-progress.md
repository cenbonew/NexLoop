# Governed create process-death checkpoint

NX-006/NX-008 remain in_progress. This adds real restricted PostgreSQL process-death evidence to the extracted core, without marking the full S2 scenarios passed.

## Actual failure boundary and recovery

`tests/test_action_definitions.py::test_committed_create_survives_sigkill_before_receipt` starts a fresh Python backend with its own restricted `nexloop_api` pool. It resolves current service identity from the persisted synthetic credential digest and executes the actual published Consumer.create through EIOS governance, the PostgreSQL claim port and the protected business-write function. After `create` returns (and its transaction context has committed), the backend emits only a synchronization marker and waits without returning the business receipt. The parent sends SIGKILL and checks the actual process exit status. No business receipt reaches the parent.

The test observes the committed object through an admin **read-only evidence query**, then starts a second fresh backend/pool with the same stable intent. That backend resolves current identity again and runs the same governed adapter. The recorded terminal claim returns the original receipt. The database has exactly one object and one Action claim. No direct SQL business writer, fake human, Memory fallback, inherited connection pool or simulated exception is used.

The anonymous input pipe contains only synthetic fixture signing material and a synthetic credential digest, never production credentials. They are not placed in argv, environment, files, logs or prompts. Child diagnostics are suppressed to avoid dumping inputs on a failure. All subprocess waits are bounded and the first child is forcibly cleaned up if the test fails.

This proves only governed database create/replay across actual backend process death after commit and before delivery of a business receipt. It does **not** prove notification Outbox/relay recovery (AT-030), unknown external-effect reconciliation (AT-033), actual Pi Run kill/reopen, or full HTTP response-loss behavior. Those remain implementation requirements. AT-052 has partial real service-Action evidence; its complete integration gate remains unaccepted.

## Commands and failures

First command: `uv run --frozen pytest -xq tests/test_action_definitions.py --tb=short`: 12 passed, then the new test failed because its evidence query incorrectly selected `revision`, which is not a column of the actual frozen ontology.objects table. The process had already reached the post-commit boundary and been killed. Corrected the test query to the actual `schema_version` column without changing any migration or runtime code.

Rerun of the same command: 19 passed in 15.03 seconds, including actual SIGKILL and fresh-process replay.

Full CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Final results and input/JUnit hashes are recorded in create-crash-recovery-ci.json after completion.

## Changed files and remaining work

Changed: tests/test_action_definitions.py, this report, its CI evidence JSON, planning/tasks.json evidence, docs/implementation/change-inventory.json. versions.lock.json is unchanged; bootstrap stays 0011 and all eleven published migration checksums match the prior successful CI. Frozen upstream commits and unresolved image digests are unchanged.

API/Host/Compose, definition publication, preferences/relations, owner-bound Artifact lifecycle, actual Pi Run recovery, external effects and the S2 failure matrix remain incomplete. This step needs no model/channel credential or persistent-environment migration. No production database or original upstream checkout was modified; no Git staging, commit or push was performed.

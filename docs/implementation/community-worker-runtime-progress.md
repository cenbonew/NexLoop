# S1 independent Compose Worker verification

The disposable community graph now has eleven services: clean PostgreSQL,
core/cache/Host/browser/Worker bootstrap jobs, restricted API and Worker,
React HTTPS frontend, Node Host, Valkey and actual check job. Python API and
Worker use the same frozen core image with distinct entrypoints and credentials.

`worker_test_profile.py` creates an independent SERVICE principal, restricted
application/scopes and DELETE grant only for `eios:artifact:orphans_test`. Its
SCRAM `nexloop_domain_worker` DSN, service credential and signing key live in a
separate private worker volume. Initialization verifies owner, endpoint, exact
catalog and active core signer; existing/foreign configuration is refused. No
ontology/business row is seeded, no production role or identity is copied.

The actual UID10001 `worker-test` verifies live EIOS authority, creates one
explicit synthetic filesystem-only orphan and invokes the governed cleanup
port. It verifies removal and idempotent resume of the persisted plan. It has
only the internal data network and its own private credential/artifact volumes;
API, Host and check containers do not mount Worker credentials. Bootstrap alone
has the initialization DSN. No real model, channel, original EIOS or TOS access.

## Commands and first failures

```sh
uv run pytest -q tests/test_worker_test_profile.py tests/test_domain_worker.py tests/test_community_container_contract.py tests/test_community_launcher.py
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run pytest -q tests/test_worker_test_profile.py tests/test_domain_worker.py tests/test_community_container_contract.py tests/test_community_launcher.py
uv run pytest -q tests/test_worker_test_profile.py
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-worker-runtime-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py run --output .ci-results/community-worker-runtime-20261008
uv run --frozen python .ci-results/collect_worker_runtime_evidence.py
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

First focused run:14 passed/22 setup errors because Node20 was used. The locked
Host builder correctly refused it. After `nvm use`,36 passed in41.01s. Additional
world/read permission assertions initially had1 failure/3 passed because the
expected exception type was wrong: absent Artifact READ authority facts fail
closed as AuthorizationUnavailable. The assertion was corrected to the actual
specific error; final4 passed in3.76s. Implementation was not widened to pass.

Fresh prepare/run exited0, actual11-service project
`nexloop-test-630e3772a8c0fb5903613d2c`. Actual restricted Worker cleanup/resume,
PG/Artifact/doctor, API HTTPS, cache TLS/ACL, Host lock/control and browser
login/session/CSRF/logout all passed. External verified HTTPS on the host
loopback also passed; current readiness reports browser available and503 with
host_dispatch missing. Container image IDs/mount isolation and exact worker
output are in community-worker-runtime-evidence.json. No new DOM proof is
claimed; earlier actual browser evidence remains separate.

Versions/dependency locks and32 migration SQL files unchanged. No commit, push,
public release, server change, persistent-environment migration or production
change. S2 still needs governed agent release ceilings, inbox/outbox/leases,
Pi RuntimeAdapter Run recovery and real-effect reliability. This Worker is a
bounded maintenance job, not the unfinished S2 scheduler/Action Worker queue.

Full CI exited0:904 Python tests in234.63s,24 actual Pi SQLite tests,four
frozen Pi builds and frontend/Host builds,zero critical skips. Existing Starlette
warning/Node SQLite experimental notice remain. Exact ownership dry-run preceded
stopping four running services; all11 container exit codes0,volumes retained.

S1 task audit: S1-exit-audit.md. NX-009/NX-011 delivery complete under the actual
handoff foundation scope; AT-047/051 passed only at their stated scope. Other
full product acceptance remains unpromoted. NX-012 starts with the actual agent
release gap in NX-012-agent-write-gap.md. Overall goal remains incomplete.

Changed implementation files this turn:

- packages/eios-core/src/nexloop_eios/worker_test_profile.py
- packages/eios-core/src/nexloop_eios/container_entrypoint.py
- deploy/community/compose.test.yaml
- scripts/community_test.py
- tests/test_worker_test_profile.py
- tests/test_community_container_contract.py
- tests/test_community_launcher.py
- README.md
- planning/tasks.json
- planning/acceptance-tests.json
- docs/implementation/community-worker-runtime-progress.md
- docs/implementation/community-worker-runtime-evidence.json
- docs/implementation/missing-pg-gate-evidence.json
- docs/implementation/S1-exit-audit.md
- docs/implementation/NX-012-agent-write-gap.md

Additional actual commands:

```sh
uv run --frozen python .ci-results/check_missing_pg_gate.py
uv run --frozen python .ci-results/stop_owned_worker_runtime.py
uv run --frozen python .ci-results/stop_owned_worker_runtime.py --execute
uv run --with jsonschema --with pyyaml --with rfc3339-validator python docs/handoff/tools/validate_handoff.py --planning planning
uv run --frozen python scripts/check_publication.py
git diff --cached --stat
```

Final mandatory validator:69 checks passed;43 tasks/60 scenarios/6 schemas/11
negative schema cases. Publication:852 paths,0 staged blobs,0 violations.

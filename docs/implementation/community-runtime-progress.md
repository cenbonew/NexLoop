# NX-009 isolated Compose runtime verification

Docker daemon became available (`docker info --format '{{.ServerVersion}}'`: 28.1.1). No Docker reset, user-resource cleanup or production-system change was performed.

## Changes

- `scripts/community_test.py`: parse the complete container JSON report instead of its final line.
- `tests/test_community_launcher.py`: regression for the actual multiline report shape.
- `deploy/community/README.md`: replace obsolete unavailable-daemon statement with scoped runtime evidence.
- `versions.lock.json`: PostgreSQL status now reflects actual isolated core Compose runtime; Python base status reflects a successful digest-pinned core container build. Digests, upstream commits and dependency versions are unchanged. Locally built backend image IDs are in runtime evidence; no registry digest or deployability is claimed.
- `community-runtime-evidence.json`: actual container states/image IDs, bootstrap/catalog, governed Artifact smoke and doctor reports, internal network/no published ports, controlled shutdown and retained data.

## Commands and first failure

```sh
docker info --format '{{.ServerVersion}}'
uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-runtime-20261008
uv run --frozen python scripts/community_test.py verify --output .ci-results/community-runtime-20261008
uv run --frozen python scripts/community_test.py run --output .ci-results/community-runtime-20261008
uv run --frozen pytest -q tests/test_community_launcher.py --tb=short
uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-runtime-20261008-retry
uv run --frozen python scripts/community_test.py run --output .ci-results/community-runtime-20261008-retry
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

First launcher run exited 1 even though bootstrap/check containers both exited 0. The container entrypoint prints indented JSON; the launcher incorrectly parsed only `}`. The first project's complete reports were read directly through its exact Compose ownership labels. After the parser fix, nine focused tests passed, and a second fresh project ran through the launcher with exit 0. Neither database was reused or migrated in place.

Fresh official PostgreSQL 18.4 reached the independent `nexloop-eios-v1` revision 0032 (32 migrations). UID 10001 check job received no bootstrap DSN; the actual restricted `nexloop_api` role authenticated the service and performed EIOS-authorized local Artifact write/read. Doctor catalog/PG/artifact-directory/test-model-configuration checks passed. No Memory fallback, model key, TOS, original EIOS DB or server access was used.

Read-only ownership checks preceded stopping exactly the two running test PostgreSQL containers. Both stopped with exit 0; all images, volumes and ignored private inputs remain available. No volume or unrelated container was deleted/stopped.

## Remaining scope

NX-009 remains `in_progress`; AT-051 is not promoted. This is the core-only Compose slice, not the full API/frontend/Host product. Product readiness remains false. Pi Run recovery, governed instance writes/real effects and real-model validation remain unaccepted. The Docker-unavailable blocker is resolved; further independent implementation can proceed.

Full CI exited 0: 875 Python tests in 193.23s (one existing Starlette deprecation warning), 24 actual Pi SQLite checks, four Pi package builds and frontend/Host builds passed; zero critical skips. Publication scan: 824 paths, zero staged blobs, zero violations. Planning validator: 69 static checks passed. No commit/push/deployment.

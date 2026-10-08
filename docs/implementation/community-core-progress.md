# NX-009 community core slice

Implemented compose_bootstrap.py and container_entrypoint.py in the actual EIOS core package, a console entrypoint, community Dockerfile/Compose test graph, fail-closed root .dockerignore and a public-only wheel build-context preparer. The initializer refuses foreign/nonempty databases, existing mismatched profiles and arbitrary endpoint substitutions; creates only a test-world service and restricted API credentials; uses SCRAM and a private signing key; never reads repository model credentials. Existing profiles are verified read-only, not migrated or overwritten.

Actual commands:

- `uv run --frozen pytest -xq tests/test_compose_bootstrap.py tests/test_community_container_contract.py tests/test_wheel_install.py --tb=short`: first run 7 passed/1 failed (test import could not resolve scripts namespace). Fixed with explicit importlib file loading; rerun 11 passed in 10.45s.
- `uv run --frozen python scripts/prepare_community_build.py --output .ci-results/community-build-review`: exit 0, four-file ignored context, no container built.
- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`: authoritative current full result is community-core-ci.json.
- `docker buildx imagetools inspect postgres:18.4-bookworm` and Python base: index digests resolved. PostgreSQL raw manifest inspection succeeded; Python raw retry failed with registry EOF after successful readable inspection.
- Docker info/pull attempts failed because desktop daemon was unavailable; opening Desktop started backend processes but did not provide daemon connectivity. No build or Compose execution succeeded.

Real native disposable PostgreSQL evidence includes clean bootstrap/reopen, private-file tamper denial, foreign database refusal, actual restricted EIOS-authorized Artifact write/read and deterministic provider doctor. Compose client rendering verifies the graph only; it is not container acceptance. Installed wheel console execution is mandatory in CI.

versions.lock.json: bootstrap revision stays 0018, upstream/dependency pins unchanged; PostgreSQL/Python OCI index metadata added as resolved_manifest_only_not_runtime_verified. Backend/gateway/Host/web image digests remain blank.

Still incomplete: Docker container build/run, full community API/Host stack and AT-051; controlled test-profile credential renewal after its one-hour expiry; actual RuntimeAdapter/Pi Run crash recovery, runtime-owner lifecycle and governed external effects/S2; real model validation. Need an operational Docker daemon for container evidence. Model/channel/embedding credentials affect only corresponding later validation. No private key, server write, persistent migration, git staging, commit, push or deployment.

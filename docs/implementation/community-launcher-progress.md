# NX-009 private test launcher

Added scripts/community_test.py and tests/test_community_launcher.py. Preparation generates a fresh random Compose project, private bootstrap input files and an independently whitelisted wheel context, without reading repository .env. Verification refuses private-file mode/ownership/symlink/endpoint changes, image-lock/Compose/build drift and extra build files. Run filters MODEL_/COMPOSE_ overrides, checks Docker availability, refuses any existing project containers or volumes, and uses explicit --env-file. It performs no down/delete/migration on existing environments. Successful future runs retain disposable resources for inspection; this is not deployment or product readiness.

Actual commands:

- `uv run --frozen pytest -xq tests/test_community_launcher.py --tb=short`: initial 8 cases passed in 1.86s, no failure. An environment-filter assertion was then strengthened; final version is included in full CI.
- `uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-launch-review`: private files and public-only wheel context generated.
- `uv run --frozen python scripts/community_test.py verify --output .ci-results/community-launch-review`: succeeded, output matches preparation manifest.
- `uv run --frozen python scripts/community_test.py run --output .ci-results/community-launch-review`: exit 1, sanitized failure; current docker info independently confirms unavailable daemon. No image build or project startup was reached.
- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`: actual final result in community-launcher-ci.json.

NX-009 remains in_progress. Docker daemon connectivity is required to collect real container evidence; full Host/API/community acceptance and S2/Pi Run/external-effect tasks remain incomplete. versions.lock.json is unchanged from the prior manifest-only digest checkpoint, bootstrap remains 0018. No credentials are printed, committed or copied into build contexts. No git staging/commit/push, server mutation, persistent migration or deployment.

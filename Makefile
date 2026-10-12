# NX-033 §7 command contract (local part). Everything host-specific comes from the environment of the caller; nothing here
# names a host, address, user or path. N: pytest workers on a development machine (at most 3 locally).
PYTHON ?= python3
N ?= 3
CI := $(PYTHON) scripts/ci/nexloop_ci.py
RELEASE := $(PYTHON) scripts/release/release.py
ARCHIVE := $(PYTHON) scripts/release/archive.py
PYTEST := uv run --frozen pytest -q -p no:cacheprovider -n $(N)

.PHONY: ci status test test-integration test-recovery test-e2e test-real-browser lint build-release verify-release prune-releases sync-github

# Enqueue SHA (from REF, as IDENTITY) in the local queue and wait for its result.json (printed on stdout).
ci:
	@test -n "$(SHA)" -a -n "$(REF)" -a -n "$(IDENTITY)" -a -n "$(NEXLOOP_CI_REPO)" -a -n "$(NEXLOOP_CI_QUEUE)" -a -n "$(NEXLOOP_CI_IDENTITIES)" \
	  || { echo 'SHA, REF, IDENTITY and NEXLOOP_CI_REPO/QUEUE/IDENTITIES are required' >&2; exit 2; }
	@$(CI) enqueue --repo "$(NEXLOOP_CI_REPO)" --queue "$(NEXLOOP_CI_QUEUE)" --identities "$(NEXLOOP_CI_IDENTITIES)" --ref "$(REF)" --sha "$(SHA)" --identity "$(IDENTITY)" >/dev/null
	@$(CI) result --queue "$(NEXLOOP_CI_QUEUE)" --sha "$(SHA)" --wait $${NEXLOOP_CI_WAIT:-7200}

status:
	@$(CI) status --queue "$(NEXLOOP_CI_QUEUE)"

# Single stages on a development machine, without the queue.
test:
	$(PYTEST) tests --ignore=tests/test_build_provider_environment.py --ignore=tests/test_community_container_contract.py \
	  --ignore=tests/test_community_launcher.py --ignore=tests/test_wheel_install.py

test-integration:
	$(PYTEST) $(wildcard tests/test_*_pg.py)

test-recovery:
	$(PYTEST) tests/test_backup_pg.py tests/test_restore_fence_pg.py tests/test_restore_drill_pg.py tests/test_restore_incentives_pg.py tests/test_deletion_replay_pg.py

test-e2e:
	uv run --frozen pytest -q -p no:cacheprovider tests/test_sse_tcp_reconnect.py tests/test_sse_dom_tcp_resume.py

# AT-015 in headless Chromium (opt-in lane, serial; Node 24 and the Playwright Chromium headless shell, see
# docs/implementation/AT-015-real-browser.md).
test-real-browser:
	LC_ALL=C.UTF-8 uv run --frozen pytest -q -p no:cacheprovider -m real_browser tests/test_sse_real_browser.py

lint:
	uv run --frozen pytest -q -p no:cacheprovider tests/test_publication.py tests/test_ci_queue.py
	LC_ALL=C.UTF-8 uv run --frozen python scripts/check_definer_search_path.py
	uv run --frozen python scripts/ci/test-prepush-scan.py

# NX-034 §2, §6 (part 2). Run as the release account on a clean checkout of SHA; host facts come from the environment:
# NEXLOOP_RELEASE_ROOT, NEXLOOP_RELEASE_SIGNING_KEY, NEXLOOP_RELEASE_ALLOWED_SIGNERS, NEXLOOP_CI_QUEUE (CI results).
# Build, scan, stage preflight, sign and seal; any failing step stops the chain (the evidence written so far is kept).
build-release:
	@test -n "$(SHA)" -a -n "$(NEXLOOP_RELEASE_ROOT)" -a -n "$(NEXLOOP_RELEASE_SIGNING_KEY)" -a -n "$(NEXLOOP_RELEASE_ALLOWED_SIGNERS)" -a -n "$(NEXLOOP_CI_QUEUE)" \
	  || { echo 'SHA and NEXLOOP_RELEASE_ROOT/SIGNING_KEY/ALLOWED_SIGNERS and NEXLOOP_CI_QUEUE are required' >&2; exit 2; }
	@set -e; result=$$($(CI) result --queue "$(NEXLOOP_CI_QUEUE)" --sha "$(SHA)" | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["status"])'); \
	  test "$$result" = passed || { echo "CI result of $(SHA) is not passed" >&2; exit 1; }; \
	  ci_result=$$(ls -d "$(NEXLOOP_CI_QUEUE)/results/$(SHA)"/attempt-* | sort -t- -k2 -n | tail -1)/result.json; \
	  out=$$($(RELEASE) build --sha "$(SHA)" --ci-result "$$ci_result" --release-root "$(NEXLOOP_RELEASE_ROOT)" $(if $(SEQUENCE),--sequence $(SEQUENCE),)); echo "$$out"; \
	  dir=$$(echo "$$out" | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["release_dir"])'); \
	  id=$$(basename "$$dir"); \
	  $(RELEASE) sbom --release-dir "$$dir"; \
	  $(RELEASE) preflight --release-dir "$$dir" --category stage; \
	  $(RELEASE) sign --release-dir "$$dir" --key "$(NEXLOOP_RELEASE_SIGNING_KEY)"; \
	  $(ARCHIVE) seal --root "$(NEXLOOP_RELEASE_ROOT)" --release-id "$$id" --allowed-signers "$(NEXLOOP_RELEASE_ALLOWED_SIGNERS)"

# Read-only: signature, completeness and seal of RELEASE (the deploy-side digest checks are NX-032's dry-run).
verify-release:
	@test -n "$(RELEASE_ID)" -a -n "$(NEXLOOP_RELEASE_ROOT)" -a -n "$(NEXLOOP_RELEASE_ALLOWED_SIGNERS)" || { echo 'RELEASE_ID, NEXLOOP_RELEASE_ROOT and NEXLOOP_RELEASE_ALLOWED_SIGNERS are required' >&2; exit 2; }
	@$(ARCHIVE) check --root "$(NEXLOOP_RELEASE_ROOT)" --release-id "$(RELEASE_ID)" --allowed-signers "$(NEXLOOP_RELEASE_ALLOWED_SIGNERS)"

# D3 retention: dry run unless APPLY=1.
prune-releases:
	@test -n "$(NEXLOOP_RELEASE_ROOT)" || { echo 'NEXLOOP_RELEASE_ROOT is required' >&2; exit 2; }
	@$(ARCHIVE) prune --root "$(NEXLOOP_RELEASE_ROOT)" $(if $(filter 1,$(APPLY)),--apply,)

# NX-034 §3: gated push of main (and of owner-signed tags named in TAGS) to the sync remote; pending when unreachable.
sync-github:
	@test -n "$(NEXLOOP_CI_REPO)" -a -n "$(NEXLOOP_CI_QUEUE)" -a -n "$(NEXLOOP_SYNC_REMOTE)" -a -n "$(NEXLOOP_RESTRICTED_IDENTIFIERS_FILE)" \
	  || { echo 'NEXLOOP_CI_REPO/QUEUE, NEXLOOP_SYNC_REMOTE and NEXLOOP_RESTRICTED_IDENTIFIERS_FILE are required' >&2; exit 2; }
	@$(PYTHON) scripts/release/sync_github.py --repo "$(NEXLOOP_CI_REPO)" --queue "$(NEXLOOP_CI_QUEUE)" --remote "$(NEXLOOP_SYNC_REMOTE)" \
	  --restricted-identifiers "$(NEXLOOP_RESTRICTED_IDENTIFIERS_FILE)" $(foreach t,$(TAGS),--tag $(t)) \
	  $(if $(NEXLOOP_TAG_ALLOWED_SIGNERS),--tag-signers "$(NEXLOOP_TAG_ALLOWED_SIGNERS)",) $(if $(BASELINE),--baseline $(BASELINE),)

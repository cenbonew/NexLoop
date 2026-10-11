# NX-033 §7 command contract (local part). Everything host-specific comes from the environment of the caller; nothing here
# names a host, address, user or path. N: pytest workers on a development machine (at most 3 locally).
PYTHON ?= python3
N ?= 3
CI := $(PYTHON) scripts/ci/nexloop_ci.py
PYTEST := uv run --frozen pytest -q -p no:cacheprovider -n $(N)

.PHONY: ci status test test-integration test-recovery test-e2e lint

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

lint:
	uv run --frozen pytest -q -p no:cacheprovider tests/test_publication.py tests/test_ci_queue.py
	LC_ALL=C.UTF-8 uv run --frozen python scripts/check_definer_search_path.py
	uv run --frozen python scripts/ci/test-prepush-scan.py

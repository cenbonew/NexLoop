#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Phase 2 (parallel by test): everything else; each test owns its PGDATA, port and temp dirs.
# NEXLOOP_CI_PYTEST_WORKERS=0 runs serially for diagnosis.
mkdir -p .ci-results
ignore_args=(); for t in "${WORKSPACE_BUILD_TESTS[@]}"; do ignore_args+=(--ignore="$t"); done
uv run --frozen pytest -q -n "${NEXLOOP_CI_PYTEST_WORKERS:-6}" --dist load "${ignore_args[@]}" --junitxml=.ci-results/python.xml
uv run --frozen python scripts/check_test_report.py .ci-results/python.xml

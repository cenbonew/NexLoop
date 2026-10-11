#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Phase 2 (parallel by test; stages v3): everything except the workspace build tests, the deadline gate and the low-parallelism
# stage, which run in their own stages. NEXLOOP_CI_PYTEST_WORKERS=0 runs serially for diagnosis.
mkdir -p .ci-results
args=(); for t in "${WORKSPACE_BUILD_TESTS[@]}"; do args+=(--ignore="$t"); done
for t in "${DEADLINE_GATE_TESTS[@]}" "${HEAVY_TESTS[@]}"; do args+=(--deselect="$t"); done
uv run --frozen pytest -q -n "${NEXLOOP_CI_PYTEST_WORKERS:-6}" --dist load "${args[@]}" --junitxml=.ci-results/python.xml
uv run --frozen python scripts/check_test_report.py .ci-results/python.xml

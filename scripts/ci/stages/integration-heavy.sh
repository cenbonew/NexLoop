#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Phase 2b (stages v3, D-j): HEAVY_TESTS (lib.sh) at low parallelism; still required (python-heavy), a failure stops the run.
mkdir -p .ci-results
uv run --frozen pytest -q -n "${NEXLOOP_CI_HEAVY_WORKERS:-2}" --dist load "${HEAVY_TESTS[@]}" --junitxml=.ci-results/python-heavy.xml
uv run --frozen python scripts/check_test_report.py .ci-results/python-heavy.xml

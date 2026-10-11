#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Phase 1 (serial): WORKSPACE_BUILD_TESTS (lib.sh); nothing may run beside them.
mkdir -p .ci-results
uv run --frozen pytest -q -p no:xdist "${WORKSPACE_BUILD_TESTS[@]}" --junitxml=.ci-results/python-build.xml
uv run --frozen python scripts/check_test_report.py .ci-results/python-build.xml

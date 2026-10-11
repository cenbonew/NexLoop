#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Phase 1b (serial, NX-049 deadline gate, ADR-022 §4): DEADLINE_GATE_TESTS (lib.sh) run alone, with the deployment guard
# topology (4 guard worker processes); the 2 s tool deadline is not relaxed.
# The exit status is the gate result; scripts/ci/check records it and still runs the remaining phases for diagnosis.
mkdir -p .ci-results
deadline_gate_rc=0
NEXLOOP_TEST_GUARD_WORKERS=4 uv run --frozen pytest -q -p no:xdist "${DEADLINE_GATE_TESTS[@]}" --junitxml=.ci-results/python-deadline-gate.xml || deadline_gate_rc=$?
uv run --frozen python scripts/check_test_report.py .ci-results/python-deadline-gate.xml || deadline_gate_rc=1
exit "$deadline_gate_rc"

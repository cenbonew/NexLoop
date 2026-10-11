#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Phase 1b (serial, NX-049 deadline gate, ADR-022 §4): the real-Pi cases that must finish under the
# 2 s tool deadline run alone, with the deployment guard topology (4 guard worker processes).
DEADLINE_GATE_TESTS=('tests/test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]'
  'tests/test_role_pi_effect_checkpoint.py::test_actual_two_pi_role_runs_one_shared_intent_one_real_loopback_effect')
# The exit status is the gate result; scripts/ci/check records it and still runs the remaining phases for diagnosis.
mkdir -p .ci-results
deadline_gate_rc=0
NEXLOOP_TEST_GUARD_WORKERS=4 uv run --frozen pytest -q -p no:xdist "${DEADLINE_GATE_TESTS[@]}" --junitxml=.ci-results/python-deadline-gate.xml || deadline_gate_rc=$?
uv run --frozen python scripts/check_test_report.py .ci-results/python-deadline-gate.xml || deadline_gate_rc=1
exit "$deadline_gate_rc"

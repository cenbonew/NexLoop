# Shared by scripts/ci/check and every stage script (NX-033 §4.1): strict mode, the repository root, and the lists more than
# one stage needs. Sourced, never executed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
# Tests that rebuild apps/web/dist, apps/agent-host/dist, vendor/pi dist or install the core wheel. They rewrite shared
# workspace files, so nothing may run beside them (serial-build runs them; integration ignores them).
WORKSPACE_BUILD_TESTS=(tests/test_build_provider_environment.py tests/test_community_container_contract.py
  tests/test_community_launcher.py tests/test_wheel_install.py)
# NX-049 serial deadline gate (ADR-022 §4; owner decision D-j 2026-10-11, gate at most 5 minutes on the CI host): the real-Pi
# cases that must finish under the 2 s tool deadline, and the Agent Host run limit at the deployment concurrency, run alone
# with the deployment guard topology (4 guard worker processes). They are not run again in the parallel stage.
DEADLINE_GATE_TESTS=('tests/test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]'
  'tests/test_role_pi_effect_checkpoint.py::test_actual_two_pi_role_runs_one_shared_intent_one_real_loopback_effect'
  'tests/test_agent_host_run_limit.py::test_full_host_refuses_a_further_run_with_retryable_error_before_any_guard_request'
  'tests/test_agent_host_run_limit.py::test_full_host_queues_a_further_run_until_the_active_one_settles')
# Low-parallelism stage (D-j): real Host/Pi and process-kill cases that pass serially but time out under the full parallel load
# (ADR-020 §4 baseline, classes B and C). Node id prefixes: a parametrized test is selected with all its parameters.
HEAVY_TESTS=(
  'tests/test_closure_plan_run_pg.py::test_no_reasonable_contact_is_a_normal_no_action'
  'tests/test_closure_plan_run_pg.py::test_action_intent_then_external_result_launches_the_next_reevaluation'
  'tests/test_closure_refusal_versions_pg.py::test_explicit_refusal_stops_contact_even_if_the_run_tries'
  'tests/test_runtime_dispatch.py::test_worker_reclaim_sigkilled_inflight_pi_with_reopened_backend'
  'tests/test_role_tail.py::test_actual_artifact_fact_lock_crosses_previous_deadline'
  'tests/test_role_tail.py::test_context_bind_lock_expiry_rolls_back_binding_and_job'
  'tests/test_role_post_accept.py::test_active_role_query_keeps_original_governed_finalize'
  'tests/test_model_response_commit_window.py::test_actual_returned_second_model_response_before_sqlite_commit_sigkill_same_pg_intent'
  'tests/test_outbound_messages_pg.py::test_scope_denied_reply_is_rejected_by_governance_and_never_persisted'
  'tests/test_outbound_messages_pg.py::test_unknown_reconciled_by_provider_query_and_recorder_sigkill_recovers'
  'tests/test_context_engine_pg.py::test_open_work_projection_is_consumer_read_gated_and_pins_unconfirmed'
  'tests/test_message_runtime_effect_e2e.py::test_same_message_pi_effect_worker_and_current_human_receipt'
  'tests/test_reply_fallback_pg.py::test_restricted_consumer_unanswered_message_still_gets_its_bound_fallback_reply'
  'tests/test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[tail_statement_wait]'
  'tests/test_context_v6_pg.py::test_actual_pi_run_records_every_model_call_of_a_v6_run'
  'tests/test_role_context_v6_pg.py::test_actual_pi_role_run_on_v6_records_every_model_call'
  'tests/test_message_driven_delivery.py::test_two_user_messages_export_their_exact_body_and_replay_once'
  'tests/test_native_web_delivery.py::test_native_provider_event_exact_id_different_transport_runs_delivers_once'
  'tests/test_offering_runtime_pg.py::test_actual_https_scope_denial_is_bounded_current_read_no_receipt'
  'tests/test_commercial_j01_pg.py::test_j01_verified_payment_wakes_the_plan_and_the_reevaluation_concludes_no_contact'
  # Throughput or latency thresholds (N within a window, an elapsed/deadline upper bound): unchanged, judged at low parallelism.
  'tests/test_nx049_memo_stress_pg.py::test_action_memo_never_serves_a_revoked_or_expired_allow'
  'tests/test_nx049_memo_stress_pg.py::test_derived_message_memo_never_serves_a_revoked_deleted_or_moved_allow'
  'tests/test_read_assert_memo.py::test_concurrent_revocation_never_serves_a_stale_allow'
  'tests/test_runtime_effect_recovery.py::test_same_pi_run_survives_host_and_effect_worker_kill_query_first'
  'tests/test_backend_lifecycle_capacity.py::test_eight_concurrent_requests_complete_on_pool_of_four'
  'tests/test_effect_origin_expired_pg.py::test_a_dispatched_unknown_intent_stops_requery_and_unknown_backs_off'
  'tests/test_effect_provider_transport.py::test_numeric_https_silent_tls_handshake_obeys_absolute_deadline'
  'tests/test_effect_provider_transport.py::test_tls_trickle_close_response_cannot_extend_absolute_deadline'
  'tests/test_valkey_wakeup_product.py::test_trickled_tls_reply_has_global_deadline_after_pg_commit'
  'tests/test_relay_multi_consumer_pg.py::test_enrollment_throughput_in_process'
  'tests/test_nx037_endpoint_conformance.py::test_conforming_endpoint_passes_every_check'
  'tests/upstream/test_eios_application_restrictions.py::test_large_restriction_sets_have_bounded_lookup_cost_without_truncation'
)

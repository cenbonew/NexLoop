#!/usr/bin/env bash
# Node ids for the O1/O4 before/after comparison (CI baseline B class + selected end-to-end tests).
NODES=(
 "tests/test_context_artifacts.py::test_actual_three_process_replay_model_tool_do_not_invert_locks"
 "tests/test_context_host_export.py::test_actual_bound_pack_goes_through_pg_guard_and_exports_original_statement"
 "tests/test_context_host_recovery.py::test_sigkill_context_host_and_worker_reopen_same_bound_pack[None]"
 "tests/test_context_host_recovery.py::test_sigkill_context_host_and_worker_reopen_same_bound_pack[artifact_deleted]"
 "tests/test_role_pi_effect_checkpoint.py::test_actual_two_pi_role_runs_one_shared_intent_one_real_loopback_effect"
 "tests/test_runtime_effect_recovery.py::test_same_pi_run_survives_host_and_effect_worker_kill_query_first"
 "tests/test_runtime_effect_tools.py::test_actual_pi_rebuilt_tool_call_hits_original_business_receipt"
 "tests/test_local_message_delivery_assembly.py::test_explicit_configuration_to_same_human_real_json_delivery"
 "tests/test_message_driven_delivery.py::test_two_user_messages_export_their_exact_body_and_replay_once"
 "tests/test_message_relay.py::test_cli_same_message_actual_pi_effect_and_human_receipt"
 "tests/test_native_web_delivery.py::test_native_provider_event_exact_id_different_transport_runs_delivers_once"
 "tests/test_scope_denials.py::test_real_pi_distinct_tool_calls_persist_denial_and_human_http"
 "tests/test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]"
)
exec "$(dirname "$0")/run_profile.sh" "$1" "${NODES[@]}"

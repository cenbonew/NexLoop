-- NX-052 batch 4: tenant foreign keys on the Run, Context, effect and plan families (after message acceptance, not on the
-- ACK-before path). Requires 0162 (configuration and initial identity take FOR NO KEY UPDATE, so these checks never wait on them).
-- Direct: tables without a tenant-carrying foreign key to a table that already has a tenant path (their run_id/intent_id/
-- context_id references do not carry tenant_id). Chain (no own key, covered by the 3c rule in tests/test_tenant_fk_pg.py):
-- context_sources, model_requests, model_request_prompts, context_artifact_bindings, fallback_context_bindings, effect_attempts,
-- effect_catalog_bindings, effect_provider_bindings, effect_events, effect_observations, effect_query_admissions,
-- local_delivery_recoveries, plan_events, plan_outcomes, plan_runs, plan_steps.
-- Not here: vendored EIOS core (0001: runtime.jobs, invocations, job_events, audit_events, idempotency_records,
-- artifact_references) and runtime.nexloop_inbox/nexloop_outbox, whose tenant-carrying key points at runtime.jobs.
-- NOT VALID first, then VALIDATE. Rollback (as a new migration): drop constraint if exists <table>_tenant_fk.
alter table authz.nexloop_action_decisions add constraint nexloop_action_decisions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_artifact_permits add constraint nexloop_artifact_permits_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_message_fallback_issuances add constraint nexloop_message_fallback_issuances_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_message_run_issuances add constraint nexloop_message_run_issuances_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_role_policy_bindings add constraint nexloop_role_policy_bindings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_role_run_bindings add constraint nexloop_role_run_bindings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_runtime_activations add constraint nexloop_runtime_activations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_runtime_execution_markers add constraint nexloop_runtime_execution_markers_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_runtime_run_bindings add constraint nexloop_runtime_run_bindings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_local_artifacts add constraint nexloop_local_artifacts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_context_packs add constraint nexloop_context_packs_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_effect_intents add constraint nexloop_effect_intents_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_effect_control_reservations add constraint nexloop_effect_control_reservations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_effect_dispatch_controls add constraint nexloop_effect_dispatch_controls_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_effect_outbox add constraint nexloop_effect_outbox_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_effect_recovery_audits add constraint nexloop_effect_recovery_audits_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_plans add constraint nexloop_plans_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_role_trigger_events add constraint nexloop_role_trigger_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_role_context_artifacts add constraint nexloop_role_context_artifacts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_run_tool_timings add constraint nexloop_run_tool_timings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_run_purge_items add constraint nexloop_run_purge_items_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_task_dispatch_controls add constraint nexloop_task_dispatch_controls_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_scope_denials add constraint nexloop_scope_denials_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_cost_entries add constraint nexloop_cost_entries_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_budget_settlements add constraint nexloop_budget_settlements_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_dispatch_refusals add constraint nexloop_dispatch_refusals_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_strategies add constraint nexloop_strategies_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_human_requests add constraint nexloop_human_requests_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_relay_attempts add constraint nexloop_relay_attempts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_relay_handoffs add constraint nexloop_relay_handoffs_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_work_feed add constraint nexloop_work_feed_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_action_claims add constraint nexloop_action_claims_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_action_decisions validate constraint nexloop_action_decisions_tenant_fk;
alter table authz.nexloop_artifact_permits validate constraint nexloop_artifact_permits_tenant_fk;
alter table authz.nexloop_message_fallback_issuances validate constraint nexloop_message_fallback_issuances_tenant_fk;
alter table authz.nexloop_message_run_issuances validate constraint nexloop_message_run_issuances_tenant_fk;
alter table authz.nexloop_role_policy_bindings validate constraint nexloop_role_policy_bindings_tenant_fk;
alter table authz.nexloop_role_run_bindings validate constraint nexloop_role_run_bindings_tenant_fk;
alter table authz.nexloop_runtime_activations validate constraint nexloop_runtime_activations_tenant_fk;
alter table authz.nexloop_runtime_execution_markers validate constraint nexloop_runtime_execution_markers_tenant_fk;
alter table authz.nexloop_runtime_run_bindings validate constraint nexloop_runtime_run_bindings_tenant_fk;
alter table runtime.nexloop_local_artifacts validate constraint nexloop_local_artifacts_tenant_fk;
alter table runtime.nexloop_context_packs validate constraint nexloop_context_packs_tenant_fk;
alter table runtime.nexloop_effect_intents validate constraint nexloop_effect_intents_tenant_fk;
alter table runtime.nexloop_effect_control_reservations validate constraint nexloop_effect_control_reservations_tenant_fk;
alter table runtime.nexloop_effect_dispatch_controls validate constraint nexloop_effect_dispatch_controls_tenant_fk;
alter table runtime.nexloop_effect_outbox validate constraint nexloop_effect_outbox_tenant_fk;
alter table runtime.nexloop_effect_recovery_audits validate constraint nexloop_effect_recovery_audits_tenant_fk;
alter table runtime.nexloop_plans validate constraint nexloop_plans_tenant_fk;
alter table runtime.nexloop_role_trigger_events validate constraint nexloop_role_trigger_events_tenant_fk;
alter table runtime.nexloop_role_context_artifacts validate constraint nexloop_role_context_artifacts_tenant_fk;
alter table runtime.nexloop_run_tool_timings validate constraint nexloop_run_tool_timings_tenant_fk;
alter table runtime.nexloop_run_purge_items validate constraint nexloop_run_purge_items_tenant_fk;
alter table runtime.nexloop_task_dispatch_controls validate constraint nexloop_task_dispatch_controls_tenant_fk;
alter table runtime.nexloop_scope_denials validate constraint nexloop_scope_denials_tenant_fk;
alter table runtime.nexloop_cost_entries validate constraint nexloop_cost_entries_tenant_fk;
alter table runtime.nexloop_budget_settlements validate constraint nexloop_budget_settlements_tenant_fk;
alter table runtime.nexloop_dispatch_refusals validate constraint nexloop_dispatch_refusals_tenant_fk;
alter table runtime.nexloop_strategies validate constraint nexloop_strategies_tenant_fk;
alter table runtime.nexloop_human_requests validate constraint nexloop_human_requests_tenant_fk;
alter table runtime.nexloop_relay_attempts validate constraint nexloop_relay_attempts_tenant_fk;
alter table runtime.nexloop_relay_handoffs validate constraint nexloop_relay_handoffs_tenant_fk;
alter table runtime.nexloop_work_feed validate constraint nexloop_work_feed_tenant_fk;
alter table runtime.nexloop_action_claims validate constraint nexloop_action_claims_tenant_fk;

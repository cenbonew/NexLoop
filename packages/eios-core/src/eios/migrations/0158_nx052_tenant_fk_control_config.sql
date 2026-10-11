-- NX-052 batch 2: tenant foreign keys on the cold control-plane configuration tables (written by trusted configuration,
-- governed setup or owner actions, not per message or per Run). Hot tables (ontology.objects, runtime.jobs,
-- authz.nexloop_authority_facts) and the message/Run/runtime families are later batches.
-- NOT VALID first (brief SHARE ROW EXCLUSIVE lock, no scan; new rows are checked at once), then VALIDATE (SHARE UPDATE
-- EXCLUSIVE, reads and writes continue). An orphan row fails VALIDATE and the whole migration rolls back; orphans are an
-- owner decision, never deleted here. Rollback (as a new migration): alter table ... drop constraint <table>_tenant_fk.
alter table control.nexloop_browser_applications add constraint nexloop_browser_applications_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_business_applications add constraint nexloop_browser_business_applications_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_accounts add constraint nexloop_browser_accounts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_memberships add constraint nexloop_browser_memberships_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_rate_policies add constraint nexloop_browser_rate_policies_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_alert_rules add constraint nexloop_alert_rules_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_alert_silences add constraint nexloop_alert_silences_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_workbench_configurations add constraint nexloop_workbench_configurations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_budget_limits add constraint nexloop_budget_limits_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_context_strategies add constraint nexloop_context_strategies_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_metric_definitions add constraint nexloop_metric_definitions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_metric_sources add constraint nexloop_metric_sources_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_goals add constraint nexloop_goals_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_goal_versions add constraint nexloop_goal_versions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_key_results add constraint nexloop_key_results_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_restore_channels add constraint nexloop_restore_channels_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_restore_scopes add constraint nexloop_restore_scopes_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_retention_holds add constraint nexloop_retention_holds_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_relation_action_bindings add constraint nexloop_relation_action_bindings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_action_effect_categories add constraint nexloop_action_effect_categories_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_erasure_salts add constraint nexloop_erasure_salts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_consumer_owners add constraint nexloop_consumer_owners_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_relay_enrollments add constraint nexloop_relay_enrollments_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_control_heads add constraint nexloop_control_heads_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_control_scopes add constraint nexloop_control_scopes_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_commercial_connectors add constraint nexloop_commercial_connectors_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_applications validate constraint nexloop_browser_applications_tenant_fk;
alter table control.nexloop_browser_business_applications validate constraint nexloop_browser_business_applications_tenant_fk;
alter table control.nexloop_browser_accounts validate constraint nexloop_browser_accounts_tenant_fk;
alter table control.nexloop_browser_memberships validate constraint nexloop_browser_memberships_tenant_fk;
alter table control.nexloop_browser_rate_policies validate constraint nexloop_browser_rate_policies_tenant_fk;
alter table control.nexloop_alert_rules validate constraint nexloop_alert_rules_tenant_fk;
alter table control.nexloop_alert_silences validate constraint nexloop_alert_silences_tenant_fk;
alter table control.nexloop_workbench_configurations validate constraint nexloop_workbench_configurations_tenant_fk;
alter table control.nexloop_budget_limits validate constraint nexloop_budget_limits_tenant_fk;
alter table control.nexloop_context_strategies validate constraint nexloop_context_strategies_tenant_fk;
alter table control.nexloop_metric_definitions validate constraint nexloop_metric_definitions_tenant_fk;
alter table control.nexloop_metric_sources validate constraint nexloop_metric_sources_tenant_fk;
alter table control.nexloop_goals validate constraint nexloop_goals_tenant_fk;
alter table control.nexloop_goal_versions validate constraint nexloop_goal_versions_tenant_fk;
alter table control.nexloop_key_results validate constraint nexloop_key_results_tenant_fk;
alter table control.nexloop_restore_channels validate constraint nexloop_restore_channels_tenant_fk;
alter table control.nexloop_restore_scopes validate constraint nexloop_restore_scopes_tenant_fk;
alter table control.nexloop_retention_holds validate constraint nexloop_retention_holds_tenant_fk;
alter table control.nexloop_relation_action_bindings validate constraint nexloop_relation_action_bindings_tenant_fk;
alter table control.nexloop_action_effect_categories validate constraint nexloop_action_effect_categories_tenant_fk;
alter table control.nexloop_erasure_salts validate constraint nexloop_erasure_salts_tenant_fk;
alter table control.nexloop_consumer_owners validate constraint nexloop_consumer_owners_tenant_fk;
alter table control.nexloop_relay_enrollments validate constraint nexloop_relay_enrollments_tenant_fk;
alter table control.nexloop_control_heads validate constraint nexloop_control_heads_tenant_fk;
alter table control.nexloop_control_scopes validate constraint nexloop_control_scopes_tenant_fk;
alter table control.nexloop_commercial_connectors validate constraint nexloop_commercial_connectors_tenant_fk;

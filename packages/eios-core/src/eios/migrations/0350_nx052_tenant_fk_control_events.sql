-- NX-052 batch 4b-1: control-plane events, sessions and logins, rate counters, ledgers, takeovers and sweeps (low write rate).
-- NOT VALID first, then VALIDATE. Rollback (as a new migration): drop constraint if exists <table>_tenant_fk.
-- The key points from a child row to its tenant; deleting a child row (erasure, retention, restore) is never constrained by it,
-- and no code deletes a tenant row, so no ON DELETE action is needed.
alter table control.nexloop_action_effect_externality add constraint nexloop_action_effect_externality_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_alert_events add constraint nexloop_alert_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_alert_state add constraint nexloop_alert_state_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_evidence add constraint nexloop_browser_evidence_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_login_events add constraint nexloop_browser_login_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_rate_counters add constraint nexloop_browser_rate_counters_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_browser_sessions add constraint nexloop_browser_sessions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_consumer_identity_link_receipts add constraint nexloop_consumer_identity_link_receipts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_contact_refusal_hits add constraint nexloop_contact_refusal_hits_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_contact_restrictions add constraint nexloop_contact_restrictions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_control_events add constraint nexloop_control_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_effect_control_ledger add constraint nexloop_effect_control_ledger_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_effect_plan_bindings add constraint nexloop_effect_plan_bindings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_real_dispatch_switches add constraint nexloop_real_dispatch_switches_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_relationship_context_recipes add constraint nexloop_relationship_context_recipes_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_reply_escalations add constraint nexloop_reply_escalations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_reply_reconciles add constraint nexloop_reply_reconciles_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_takeovers add constraint nexloop_takeovers_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_takeover_escalations add constraint nexloop_takeover_escalations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_takeover_settlements add constraint nexloop_takeover_settlements_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_orphan_sweeps add constraint nexloop_orphan_sweeps_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_retention_sweeps add constraint nexloop_retention_sweeps_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_action_effect_externality validate constraint nexloop_action_effect_externality_tenant_fk;
alter table control.nexloop_alert_events validate constraint nexloop_alert_events_tenant_fk;
alter table control.nexloop_alert_state validate constraint nexloop_alert_state_tenant_fk;
alter table control.nexloop_browser_evidence validate constraint nexloop_browser_evidence_tenant_fk;
alter table control.nexloop_browser_login_events validate constraint nexloop_browser_login_events_tenant_fk;
alter table control.nexloop_browser_rate_counters validate constraint nexloop_browser_rate_counters_tenant_fk;
alter table control.nexloop_browser_sessions validate constraint nexloop_browser_sessions_tenant_fk;
alter table control.nexloop_consumer_identity_link_receipts validate constraint nexloop_consumer_identity_link_receipts_tenant_fk;
alter table control.nexloop_contact_refusal_hits validate constraint nexloop_contact_refusal_hits_tenant_fk;
alter table control.nexloop_contact_restrictions validate constraint nexloop_contact_restrictions_tenant_fk;
alter table control.nexloop_control_events validate constraint nexloop_control_events_tenant_fk;
alter table control.nexloop_effect_control_ledger validate constraint nexloop_effect_control_ledger_tenant_fk;
alter table control.nexloop_effect_plan_bindings validate constraint nexloop_effect_plan_bindings_tenant_fk;
alter table control.nexloop_real_dispatch_switches validate constraint nexloop_real_dispatch_switches_tenant_fk;
alter table control.nexloop_relationship_context_recipes validate constraint nexloop_relationship_context_recipes_tenant_fk;
alter table control.nexloop_reply_escalations validate constraint nexloop_reply_escalations_tenant_fk;
alter table control.nexloop_reply_reconciles validate constraint nexloop_reply_reconciles_tenant_fk;
alter table control.nexloop_takeovers validate constraint nexloop_takeovers_tenant_fk;
alter table control.nexloop_takeover_escalations validate constraint nexloop_takeover_escalations_tenant_fk;
alter table control.nexloop_takeover_settlements validate constraint nexloop_takeover_settlements_tenant_fk;
alter table runtime.nexloop_orphan_sweeps validate constraint nexloop_orphan_sweeps_tenant_fk;
alter table runtime.nexloop_retention_sweeps validate constraint nexloop_retention_sweeps_tenant_fk;

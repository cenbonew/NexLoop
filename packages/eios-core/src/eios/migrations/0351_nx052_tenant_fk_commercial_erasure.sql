-- NX-052 batch 4b-2: commercial, commitment, incentive, erasure and audit tables. Append-only and erasure triggers are unaffected:
-- they guard updates and deletes of these rows, the key only checks that the row's tenant exists on insert.
-- NOT VALID first, then VALIDATE. Rollback (as a new migration): drop constraint if exists <table>_tenant_fk.
-- The key points from a child row to its tenant; deleting a child row (erasure, retention, restore) is never constrained by it,
-- and no code deletes a tenant row, so no ON DELETE action is needed.
alter table control.nexloop_commercial_customer_links add constraint nexloop_commercial_customer_links_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commercial_commitment_bindings add constraint nexloop_commercial_commitment_bindings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commercial_events add constraint nexloop_commercial_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commercial_exceptions add constraint nexloop_commercial_exceptions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commercial_receipts add constraint nexloop_commercial_receipts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commercial_records add constraint nexloop_commercial_records_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commercial_wakes add constraint nexloop_commercial_wakes_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commitment_events add constraint nexloop_commitment_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commitment_evidence add constraint nexloop_commitment_evidence_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commitment_exceptions add constraint nexloop_commitment_exceptions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_commitments add constraint nexloop_commitments_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_incentive_issues add constraint nexloop_incentive_issues_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_consumer_erasures add constraint nexloop_consumer_erasures_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_data_exports add constraint nexloop_data_exports_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_deletion_ledger add constraint nexloop_deletion_ledger_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_erased_contact_keys add constraint nexloop_erased_contact_keys_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_erasure_artifacts add constraint nexloop_erasure_artifacts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_erasure_tombstones add constraint nexloop_erasure_tombstones_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_read_audit_daily add constraint nexloop_read_audit_daily_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_workbench_read_audit add constraint nexloop_workbench_read_audit_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table control.nexloop_commercial_customer_links validate constraint nexloop_commercial_customer_links_tenant_fk;
alter table runtime.nexloop_commercial_commitment_bindings validate constraint nexloop_commercial_commitment_bindings_tenant_fk;
alter table runtime.nexloop_commercial_events validate constraint nexloop_commercial_events_tenant_fk;
alter table runtime.nexloop_commercial_exceptions validate constraint nexloop_commercial_exceptions_tenant_fk;
alter table runtime.nexloop_commercial_receipts validate constraint nexloop_commercial_receipts_tenant_fk;
alter table runtime.nexloop_commercial_records validate constraint nexloop_commercial_records_tenant_fk;
alter table runtime.nexloop_commercial_wakes validate constraint nexloop_commercial_wakes_tenant_fk;
alter table runtime.nexloop_commitment_events validate constraint nexloop_commitment_events_tenant_fk;
alter table runtime.nexloop_commitment_evidence validate constraint nexloop_commitment_evidence_tenant_fk;
alter table runtime.nexloop_commitment_exceptions validate constraint nexloop_commitment_exceptions_tenant_fk;
alter table runtime.nexloop_commitments validate constraint nexloop_commitments_tenant_fk;
alter table runtime.nexloop_incentive_issues validate constraint nexloop_incentive_issues_tenant_fk;
alter table control.nexloop_consumer_erasures validate constraint nexloop_consumer_erasures_tenant_fk;
alter table control.nexloop_data_exports validate constraint nexloop_data_exports_tenant_fk;
alter table control.nexloop_deletion_ledger validate constraint nexloop_deletion_ledger_tenant_fk;
alter table control.nexloop_erased_contact_keys validate constraint nexloop_erased_contact_keys_tenant_fk;
alter table runtime.nexloop_erasure_artifacts validate constraint nexloop_erasure_artifacts_tenant_fk;
alter table runtime.nexloop_erasure_tombstones validate constraint nexloop_erasure_tombstones_tenant_fk;
alter table runtime.nexloop_read_audit_daily validate constraint nexloop_read_audit_daily_tenant_fk;
alter table runtime.nexloop_workbench_read_audit validate constraint nexloop_workbench_read_audit_tenant_fk;

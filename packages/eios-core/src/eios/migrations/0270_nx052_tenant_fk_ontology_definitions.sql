-- NX-052 batch 3a: cold ontology definition, review and configuration tables (review or governed actions, not per message).
-- NOT VALID first (brief lock, no scan; new rows are checked at once), then VALIDATE (reads and writes continue). An orphan row fails
-- VALIDATE and the migration rolls back; orphans are an owner decision. Rollback (as a new migration): drop constraint if exists <table>_tenant_fk.
alter table ontology.nexloop_merge_configurations add constraint nexloop_merge_configurations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_recall_settings add constraint nexloop_recall_settings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_embedding_profiles add constraint nexloop_embedding_profiles_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_definition_aliases add constraint nexloop_definition_aliases_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_candidate_definitions add constraint nexloop_candidate_definitions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_candidate_events add constraint nexloop_candidate_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_candidate_rejections add constraint nexloop_candidate_rejections_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_review_decisions add constraint nexloop_review_decisions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_instance_approvals add constraint nexloop_instance_approvals_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_assessment_revisions add constraint nexloop_assessment_revisions_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_merge_configurations validate constraint nexloop_merge_configurations_tenant_fk;
alter table ontology.nexloop_recall_settings validate constraint nexloop_recall_settings_tenant_fk;
alter table ontology.nexloop_embedding_profiles validate constraint nexloop_embedding_profiles_tenant_fk;
alter table ontology.nexloop_definition_aliases validate constraint nexloop_definition_aliases_tenant_fk;
alter table ontology.nexloop_candidate_definitions validate constraint nexloop_candidate_definitions_tenant_fk;
alter table ontology.nexloop_candidate_events validate constraint nexloop_candidate_events_tenant_fk;
alter table ontology.nexloop_candidate_rejections validate constraint nexloop_candidate_rejections_tenant_fk;
alter table ontology.nexloop_review_decisions validate constraint nexloop_review_decisions_tenant_fk;
alter table ontology.nexloop_instance_approvals validate constraint nexloop_instance_approvals_tenant_fk;
alter table ontology.nexloop_assessment_revisions validate constraint nexloop_assessment_revisions_tenant_fk;

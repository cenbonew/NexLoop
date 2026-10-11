-- NX-052 batch 3b: extraction and Claim tables (a few to tens of rows per extracted message) and the NexLoop recall index. The vendored
-- EIOS core tables (0001: ontology.events, relations, *_type_versions) get no NexLoop foreign key: the core must not depend on the
-- NexLoop control plane, and they keep relying on the identity snapshot.
-- NOT VALID first (brief lock, no scan; new rows are checked at once), then VALIDATE (reads and writes continue). An orphan row fails
-- VALIDATE and the migration rolls back; orphans are an owner decision. Rollback (as a new migration): drop constraint if exists <table>_tenant_fk.
alter table ontology.nexloop_claims add constraint nexloop_claims_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_claim_matches add constraint nexloop_claim_matches_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_claim_rematch add constraint nexloop_claim_rematch_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_conversation_topics add constraint nexloop_conversation_topics_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_extraction_runs add constraint nexloop_extraction_runs_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_extraction_run_claims add constraint nexloop_extraction_run_claims_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_mutation_proposals add constraint nexloop_mutation_proposals_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_claim_extraction_feed add constraint nexloop_claim_extraction_feed_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_recall_entries add constraint nexloop_recall_entries_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_recall_embeddings add constraint nexloop_recall_embeddings_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table ontology.nexloop_claims validate constraint nexloop_claims_tenant_fk;
alter table ontology.nexloop_claim_matches validate constraint nexloop_claim_matches_tenant_fk;
alter table ontology.nexloop_claim_rematch validate constraint nexloop_claim_rematch_tenant_fk;
alter table ontology.nexloop_conversation_topics validate constraint nexloop_conversation_topics_tenant_fk;
alter table ontology.nexloop_extraction_runs validate constraint nexloop_extraction_runs_tenant_fk;
alter table ontology.nexloop_extraction_run_claims validate constraint nexloop_extraction_run_claims_tenant_fk;
alter table ontology.nexloop_mutation_proposals validate constraint nexloop_mutation_proposals_tenant_fk;
alter table runtime.nexloop_claim_extraction_feed validate constraint nexloop_claim_extraction_feed_tenant_fk;
alter table ontology.nexloop_recall_entries validate constraint nexloop_recall_entries_tenant_fk;
alter table ontology.nexloop_recall_embeddings validate constraint nexloop_recall_embeddings_tenant_fk;

-- NX-052 batch 3c: tenant foreign keys on the conversation and message tables (message acceptance, relay and reply paths).
-- Requires 0162 (configuration and initial identity take FOR NO KEY UPDATE on the tenant row, so these checks never wait on them).
-- Chain rule (dispatcher ruling, option A): a table whose composite foreign key starts from tenant_id and references a table that
-- has a tenant path gets no foreign key of its own; runtime.nexloop_conversation_messages and runtime.nexloop_outbound_messages
-- (-> nexloop_conversations) and runtime.nexloop_native_web_transports (-> nexloop_native_web_events) are covered by the chain.
-- tests/test_tenant_fk_pg.py asserts the chain for every child of these roots, including future ones.
-- NOT VALID first, then VALIDATE. Rollback (as a new migration): drop constraint if exists nexloop_<table>_tenant_fk.
alter table runtime.nexloop_conversations add constraint nexloop_conversations_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_native_web_events add constraint nexloop_native_web_events_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_message_inbox add constraint nexloop_message_inbox_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_message_outbox add constraint nexloop_message_outbox_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_message_routes add constraint nexloop_message_routes_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_message_provider_facts add constraint nexloop_message_provider_facts_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_message_reply_links add constraint nexloop_message_reply_links_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_message_relay_leases add constraint nexloop_message_relay_leases_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_relay_claims add constraint nexloop_relay_claims_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_relay_consumer_cursors add constraint nexloop_relay_consumer_cursors_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_staff_replies add constraint nexloop_staff_replies_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_conversations validate constraint nexloop_conversations_tenant_fk;
alter table runtime.nexloop_native_web_events validate constraint nexloop_native_web_events_tenant_fk;
alter table runtime.nexloop_message_inbox validate constraint nexloop_message_inbox_tenant_fk;
alter table runtime.nexloop_message_outbox validate constraint nexloop_message_outbox_tenant_fk;
alter table runtime.nexloop_message_routes validate constraint nexloop_message_routes_tenant_fk;
alter table runtime.nexloop_message_provider_facts validate constraint nexloop_message_provider_facts_tenant_fk;
alter table runtime.nexloop_message_reply_links validate constraint nexloop_message_reply_links_tenant_fk;
alter table runtime.nexloop_message_relay_leases validate constraint nexloop_message_relay_leases_tenant_fk;
alter table runtime.nexloop_relay_claims validate constraint nexloop_relay_claims_tenant_fk;
alter table runtime.nexloop_relay_consumer_cursors validate constraint nexloop_relay_consumer_cursors_tenant_fk;
alter table runtime.nexloop_staff_replies validate constraint nexloop_staff_replies_tenant_fk;

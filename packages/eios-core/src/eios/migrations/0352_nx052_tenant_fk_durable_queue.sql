-- NX-052 batch 5: the NexLoop durable queue tables runtime.nexloop_inbox and runtime.nexloop_outbox (one row per job event,
-- as frequent as runtime.jobs). Their tenant-carrying key points at the vendored runtime.jobs, which by ruling carries no NexLoop
-- key, so they get their own. The vendored EIOS core tables (0001: ontology.objects, ontology.events, runtime.jobs, ...) stay
-- without one: their tenant consistency is the 0002 tenant_boundary row-level security policy (tenant_id must equal the
-- eios.tenant_id setting, which only authenticated definer functions set after the identity snapshot or signed-authority check).
-- NOT VALID first, then VALIDATE. Rollback (as a new migration): drop constraint if exists <table>_tenant_fk.
-- The key points from a child row to its tenant; deleting a child row (erasure, retention, restore) is never constrained by it,
-- and no code deletes a tenant row, so no ON DELETE action is needed.
alter table runtime.nexloop_inbox add constraint nexloop_inbox_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_outbox add constraint nexloop_outbox_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table runtime.nexloop_inbox validate constraint nexloop_inbox_tenant_fk;
alter table runtime.nexloop_outbox validate constraint nexloop_outbox_tenant_fk;

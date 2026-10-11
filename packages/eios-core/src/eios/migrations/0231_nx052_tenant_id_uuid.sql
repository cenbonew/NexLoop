-- NX-052 batch 1b: tenant ids are canonical lowercase uuid text (the contracts' format: uuid). The column stays text.
-- NOT VALID first so new tenants are checked at once without scanning; VALIDATE then checks existing rows. A deployment
-- holding a non-uuid tenant fails here and the whole migration rolls back: renaming such a tenant is an owner decision.
-- Rollback: alter table control.nexloop_tenants drop constraint nexloop_tenants_tenant_id_uuid (as a new migration).
alter table control.nexloop_tenants add constraint nexloop_tenants_tenant_id_uuid
 check (tenant_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') not valid;
alter table control.nexloop_tenants validate constraint nexloop_tenants_tenant_id_uuid;

-- NX-052 closure: authz.nexloop_browser_token_realms is the last NexLoop table with tenant_id and no tenant path. Its rows are
-- written by the browser_token_realm_register trigger on control.nexloop_browser_sessions (which has a tenant foreign key since
-- batch 4b), keyed by token_digest, so no tenant-carrying key leads to a parent: it gets its own.
-- NOT VALID first, then VALIDATE. Rollback (as a new migration): drop constraint if exists nexloop_browser_token_realms_tenant_fk.
alter table authz.nexloop_browser_token_realms add constraint nexloop_browser_token_realms_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid;
alter table authz.nexloop_browser_token_realms validate constraint nexloop_browser_token_realms_tenant_fk;

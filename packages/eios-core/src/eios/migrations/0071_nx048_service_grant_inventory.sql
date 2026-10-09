-- NX-048 (temporary number): read-only service-authority inventory for the
-- technical configurator, so the versioned service-grant manifest can be diffed
-- (doctor) and re-applied idempotently through 0050 control.nexloop_configure_manifest.
-- No write path, no token digests, no Human directory rows; published 0001..0070 unchanged.
create function control.nexloop_service_grant_inventory(p_tenant text) returns jsonb
language plpgsql security definer stable set search_path=pg_catalog set row_security=on as $$
declare v_tenant control.nexloop_tenants%rowtype;v_human jsonb;
begin
 if session_user<>'nexloop_configurator' or p_tenant is null or p_tenant !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$' then
  raise exception 'inventory rejected' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Identifiers only (no profile data): a service manifest must never reuse a Human principal/subject.
 select coalesce(jsonb_agg(distinct x),'[]'::jsonb) into v_human from (
  select bm.principal_id x from control.nexloop_browser_memberships bm where bm.tenant_id=p_tenant
  union select bm.subject_id from control.nexloop_browser_memberships bm where bm.tenant_id=p_tenant) h;
 select * into v_tenant from control.nexloop_tenants where tenant_id=p_tenant;
 if not found then
  return jsonb_build_object('tenant_present',false,'authority_revision',0,'tenant_status',null,'facts','[]'::jsonb,'credentials','[]'::jsonb,'human_identifiers',v_human);
 end if;
 return jsonb_build_object('tenant_present',true,'authority_revision',v_tenant.authority_revision,'tenant_status',v_tenant.status,
  -- Human-bound facts are outside the service-grant scope and never exported here.
  'facts',(select coalesce(jsonb_agg(jsonb_build_object('kind',af.fact_kind,'key',to_jsonb(af.entity_key),'payload',af.payload) order by af.fact_kind,af.entity_key),'[]'::jsonb)
    from authz.nexloop_authority_facts af where af.tenant_id=p_tenant
     and coalesce(af.payload->>'kind','')<>'human' and coalesce(af.payload->>'subject_kind','')<>'human'
     and not (af.fact_kind in ('membership','actor','subject_authority','grants','scope','controls','policies')
      and exists(select 1 from control.nexloop_browser_memberships bm where bm.tenant_id=p_tenant and bm.principal_id=any(af.entity_key)))),
  'credentials',(select coalesce(jsonb_agg(jsonb_build_object('credential_id',sc.credential_id,'binding',sc.binding,'worlds',to_jsonb(sc.worlds),
     'status',sc.status,'expires_at',sc.expires_at) order by sc.credential_id),'[]'::jsonb)
    from authz.nexloop_service_credentials sc where sc.tenant_id=p_tenant),
  'human_identifiers',v_human);
end $$;
alter function control.nexloop_service_grant_inventory(text) owner to nexloop_owner;
revoke all on function control.nexloop_service_grant_inventory(text) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;
grant execute on function control.nexloop_service_grant_inventory(text) to nexloop_configurator;

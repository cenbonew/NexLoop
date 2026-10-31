-- S77 / ADR-042 (dispatcher 2026-10-31, option A2; temporary number 0390): trusted configuration accepts one more kind of object
-- type version, a group-only increment. Generic, no type name here. control.nexloop_configure_manifest (latest body 0381) is NOT
-- changed: every object type row it inserts advances the tenant's monotonic authority revision (0009 epoch trigger, 0004 guard),
-- so a wrapper inserting before the wrapped body would always fail its expected-revision check (ADR-042 §2, implementation note).
-- Instead the configurator calls control.nexloop_configure_group_increment(tenant, expected revision, types) as its own step,
-- before the manifest that names the new version: under the same advisory lock and expected-revision rule, for each item of
-- version N >= 2: N present and equal is a no-op, N present and different a conflict; otherwise version N-1 must exist and the
-- item must differ from it only by property_groups (control.nexloop_group_only_increment: every N-1 group kept exactly, every new
-- group with a new name and non-empty, unique members that are existing properties ungrouped in N-1), else 'schema increment not
-- supported'. All or nothing; the step is recorded as a configuration publication (manifest kind group_increment). The following
-- manifest then finds version N present and equal (its no-op path); any other increment is still refused there as before.
create function control.nexloop_group_only_increment(p_previous jsonb,p_item jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(p_item)='object' and jsonb_typeof(p_previous)='object'
  and (p_item-'version'-'property_groups')=(p_previous-'version'-'property_groups')
  and jsonb_typeof(coalesce(p_item->'property_groups','[]'::jsonb))='array'
  -- every previous group kept exactly
  and not exists(select 1 from jsonb_array_elements(coalesce(p_previous->'property_groups','[]'::jsonb)) g
   where not coalesce(p_item->'property_groups','[]'::jsonb) @> jsonb_build_array(g))
  -- at least one new group, each with a new name and non-empty unique members: existing properties ungrouped before
  and exists(select 1 from jsonb_array_elements(p_item->'property_groups') g where not coalesce(p_previous->'property_groups','[]'::jsonb) @> jsonb_build_array(g))
  and not exists(select 1 from jsonb_array_elements(p_item->'property_groups') g
   where not coalesce(p_previous->'property_groups','[]'::jsonb) @> jsonb_build_array(g) and (
    jsonb_typeof(g) is distinct from 'object' or jsonb_typeof(g->'property_names') is distinct from 'array' or jsonb_array_length(g->'property_names')=0
    or exists(select 1 from jsonb_array_elements(coalesce(p_previous->'property_groups','[]'::jsonb)) o where o->>'group_name'=g->>'group_name')
    or (select count(*) from jsonb_array_elements_text(g->'property_names'))<>(select count(distinct n) from jsonb_array_elements_text(g->'property_names') n)
    or exists(select 1 from jsonb_array_elements_text(g->'property_names') n where
      not exists(select 1 from jsonb_array_elements(p_item->'properties') e where e->>'property_name'=n)
      or exists(select 1 from jsonb_array_elements(coalesce(p_previous->'property_groups','[]'::jsonb)) o where o->'property_names' ? n))))
  -- no property in two new groups
  and (select count(*) from jsonb_array_elements(p_item->'property_groups') g,jsonb_array_elements_text(g->'property_names') n
        where not coalesce(p_previous->'property_groups','[]'::jsonb) @> jsonb_build_array(g))
    =(select count(distinct n) from jsonb_array_elements(p_item->'property_groups') g,jsonb_array_elements_text(g->'property_names') n
        where not coalesce(p_previous->'property_groups','[]'::jsonb) @> jsonb_build_array(g))
$$;
alter function control.nexloop_group_only_increment(jsonb,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_group_only_increment(jsonb,jsonb) from public;

create function control.nexloop_configure_group_increment(p_tenant text,p_expected bigint,p_types jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare item jsonb;previous jsonb;existing jsonb;v integer;v_revision bigint;n integer:=0;v_text text;v_digest text;
begin
 if session_user<>'nexloop_configurator' or p_tenant is null or p_tenant!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$' or p_expected is null
  or jsonb_typeof(p_types) is distinct from 'array' or jsonb_array_length(p_types) not between 1 and 64 then
  raise exception 'configuration rejected' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 perform set_config('eios.tenant_id',p_tenant,true);
 select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant for no key update;
 if not found or v_revision<>p_expected then raise exception 'configuration conflict' using errcode='40001';end if;
 for item in select value from jsonb_array_elements(p_types) loop
  if jsonb_typeof(item) is distinct from 'object' or coalesce(item->>'type_name','')='' or coalesce(item->>'version','')!~'^[0-9]{1,6}$'
   or (item->>'version')::integer<2 then raise exception 'configuration rejected' using errcode='22023';end if;
  v:=(item->>'version')::integer;
  select definition into existing from ontology.object_type_versions where tenant_id=p_tenant and type_name=item->>'type_name' and version=v;
  if found then
   if existing is distinct from item then raise exception 'configuration conflict' using errcode='40001';end if;
   continue;
  end if;
  select definition into previous from ontology.object_type_versions where tenant_id=p_tenant and type_name=item->>'type_name' and version=v-1;
  if not found or previous->>'version' is distinct from (v-1)::text or not control.nexloop_group_only_increment(previous,item) then
   raise exception 'schema increment not supported' using errcode='42501';end if;
  insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(p_tenant,item->>'type_name',v,item);
  n:=n+1;
 end loop;
 if n=0 then return jsonb_build_object('configured',true,'inserted',0,'authority_revision',v_revision);end if;
 select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant;
 v_text:=jsonb_build_object('kind','group_increment','object_types',p_types)::text;v_digest:=encode(sha256(convert_to(v_text,'UTF8')),'hex');
 insert into control.nexloop_configuration_publications values(p_tenant,('00000000-0000-5000-8000-'||substr(v_digest,1,12))::uuid,v_digest,session_user,
  clock_timestamp(),v_revision,v_text::jsonb,v_digest);
 return jsonb_build_object('configured',true,'inserted',n,'authority_revision',v_revision);
end $$;
alter function control.nexloop_configure_group_increment(text,bigint,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_group_increment(text,bigint,jsonb) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;
grant execute on function control.nexloop_configure_group_increment(text,bigint,jsonb) to nexloop_configurator;

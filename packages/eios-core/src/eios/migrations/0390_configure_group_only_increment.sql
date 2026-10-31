-- S77 / ADR-042 (dispatcher 2026-10-31, option A2; temporary number 0390): trusted configuration accepts one more kind of object
-- type version, a group-only increment. Generic, no type name here. control.nexloop_configure_manifest (latest body 0381) is NOT
-- changed: every object type row it inserts advances the tenant's monotonic authority revision (0009 epoch trigger, 0004 guard),
-- so a wrapper inserting before the wrapped body would always fail its expected-revision check (ADR-042 §2, implementation note).
-- Instead the configurator calls control.nexloop_configure_group_increment(tenant, text, digest, key id, key) as its own step,
-- before the manifest that names the new version - the same trust layer as configure_manifest (configurator session, sha256
-- digest of the exact text, tenant in the text, active signing key, advisory lock, expected revision, replay by manifest id), with
-- a narrower body (kind group_increment: object_types only). For each item of
-- version N >= 2: N present and equal is a no-op, N present and different a conflict; otherwise version N-1 must exist and the
-- item must differ from it only by property_groups (control.nexloop_group_only_increment: every N-1 group kept exactly, every new
-- group with a new name and non-empty, unique members that are existing properties ungrouped in N-1), else 'schema increment not
-- supported'. All or nothing; the step is recorded as a configuration publication under its manifest id. The following
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

create function control.nexloop_configure_group_increment(p_tenant text,p_text text,p_digest text,p_key_id text,p_key bytea) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare m jsonb;item jsonb;previous jsonb;existing jsonb;v integer;v_revision bigint;v_expected bigint;v_manifest uuid;n integer:=0;
 prior control.nexloop_configuration_publications;v_fingerprint text;
begin
 -- The same trust layer as control.nexloop_configure_manifest: configurator session only, the exact text bound by its sha256
 -- digest, the tenant named in the text, an active signing key of the same material, one advisory lock, the expected revision,
 -- replay by manifest id with the same digest. A narrower body: only object_types of version >= 2.
 begin m:=p_text::jsonb;exception when others then m:=null;end;
 if session_user<>'nexloop_configurator' or p_tenant is null or p_tenant!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or p_text is null or octet_length(p_text)>4194304 or jsonb_typeof(m) is distinct from 'object'
  or p_digest is distinct from encode(sha256(convert_to(p_text,'UTF8')),'hex')
  or m->>'schema_version' is distinct from '1.0' or m->>'kind' is distinct from 'group_increment' or m->>'tenant_id' is distinct from p_tenant
  or (select count(*) from jsonb_object_keys(m))<>6
  or exists(select 1 from jsonb_object_keys(m) k where k not in ('schema_version','kind','manifest_id','tenant_id','expected_revision','object_types'))
  or jsonb_typeof(m->'expected_revision') is distinct from 'number' or m->>'expected_revision'!~'^[0-9]+$'
  or jsonb_typeof(m->'object_types') is distinct from 'array' or jsonb_array_length(m->'object_types') not between 1 and 64
  or p_key_id is null or p_key_id!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$' or p_key is null or octet_length(p_key)<>32 then
  raise exception 'configuration rejected' using errcode='42501';end if;
 v_manifest:=(m->>'manifest_id')::uuid;v_expected:=(m->>'expected_revision')::bigint;v_fingerprint:=encode(sha256(p_key),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into prior from control.nexloop_configuration_publications where tenant_id=p_tenant and manifest_id=v_manifest;
 if found then
  if prior.manifest_digest is distinct from p_digest or prior.secret_fingerprint is distinct from v_fingerprint then
   raise exception 'configuration conflict' using errcode='40001';end if;
  return jsonb_build_object('configured',true,'manifest_digest',p_digest,'authority_revision',prior.authority_revision,'inserted',0);
 end if;
 if not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=p_key_id and key_material=p_key and active) then
  raise exception 'configuration conflict' using errcode='40001';end if;
 select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant for no key update;
 if not found or v_revision<>v_expected then raise exception 'configuration conflict' using errcode='40001';end if;
 for item in select value from jsonb_array_elements(m->'object_types') loop
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
 -- As configure_manifest: the publication advances the revision once more and is recorded under its manifest id.
 update control.nexloop_tenants set authority_revision=authority_revision+1 where tenant_id=p_tenant returning authority_revision into v_revision;
 insert into control.nexloop_configuration_publications values(p_tenant,v_manifest,p_digest,session_user,clock_timestamp(),v_revision,m,v_fingerprint);
 return jsonb_build_object('configured',true,'manifest_digest',p_digest,'authority_revision',v_revision,'inserted',n);
end $$;
alter function control.nexloop_configure_group_increment(text,text,text,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_configure_group_increment(text,text,text,text,bytea) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;
grant execute on function control.nexloop_configure_group_increment(text,text,text,text,bytea) to nexloop_configurator;

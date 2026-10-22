-- World model visibility without refused reads (stage 2026-10-22: opening the world model logged 40 'workbench member read denied'
-- ERRORs). The workbench now decides an object's visibility through `object_fields` (no value, no audit row, no exception) before its
-- audited read. object_fields already applied the member derivation (authz.nexloop_workbench_member_may_read) but not the NX-029 guard
-- of the read itself (runtime.nexloop_assert_not_erasing, inside authz.nexloop_assert_read_authority): a Consumer being erased, or a
-- Message / Conversation of it, answered readable=true and its read was then refused. object_fields now answers readable=false for it,
-- the same Consumer the read guard derives (runtime.nexloop_read_consumer). Nothing else changes. Temporary number; the dispatcher
-- renumbers at merge.
create or replace function authz.nexloop_workbench_object_fields(p_tenant text,p_world text,p_role text,p_type text,p_object text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare o ontology.objects;def jsonb;restriction jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=p_type and object_id=p_object;
 if not found then return null;end if;
 if not authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,'eios:object:'||p_type||'/'||p_object)
  -- World model visibility noise: the object read refuses an object whose Consumer is being erased (NX-029, assert_not_erasing).
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,runtime.nexloop_read_consumer(p_world,jsonb_build_object('tenant_id',p_tenant,'type_name',p_type,'object_id',p_object,'target_resource','eios:object:'||p_type||'/'||p_object))) then
  return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',false,'fields','[]'::jsonb);end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=o.schema_version;
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[p_type];
 return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',true,'schema_version',o.schema_version,'revision',o.nexloop_revision,
  'fields',coalesce((select jsonb_agg(jsonb_build_object('name',x.name,'present',x.present,'restricted',x.restricted,'erasing',x.erasing,
      'readable',not x.restricted and not x.erasing and authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,
        'eios:property:'||p_type||'/'||p_object||'/'||x.name)) order by x.ord)
    from (select p.value->>'property_name' as name,p.ordinality ord,
       (o.properties ? (p.value->>'property_name') and o.properties->(p.value->>'property_name')<>'null'::jsonb) present,
       coalesce(restriction->'restricted_groups' ? ontology.nexloop_property_group_of(def,p.value->>'property_name'),false) restricted,
       -- NX-029: a value that names a Consumer being erased (an id, or an array of ids) is never shown.
       exists(select 1 from jsonb_array_elements(case jsonb_typeof(o.properties->(p.value->>'property_name')) when 'array' then o.properties->(p.value->>'property_name')
          when 'string' then jsonb_build_array(o.properties->(p.value->>'property_name')) else '[]'::jsonb end) e
         where jsonb_typeof(e)='string' and e#>>'{}'~'^[a-f0-9]{64}$' and runtime.nexloop_consumer_erasing(p_tenant,p_world,e#>>'{}')) erasing
      from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) with ordinality p
      where coalesce(p.value->>'property_name','')~'^[A-Za-z][A-Za-z0-9_]{0,63}$') x),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_object_fields(text,text,text,text,text) owner to nexloop_owner;

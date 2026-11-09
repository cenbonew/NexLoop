-- The workbench Action for a human capability picks a version only on a live execute grant (L6 B1 on 0432, 2026-11-09). 0432 chose
-- the highest active version of one Action whose grants fact merely had a non-empty grant list; a caller holding only read on :2
-- (or an expired grant on :2) and execute on :1 was pointed at :2 and then refused by the governed entry - never a widening, but the
-- write failed. Now a version counts only when one of its grants carries the operation 'execute' and is valid now (valid_from not
-- in the future, valid_until absent or in the future, for the grant and for the grants fact). Same signature, owner and grants;
-- every other branch is 0432's. Temporary number; the dispatcher renumbers.
create or replace function authz.nexloop_workbench_action_for(p_digest text,p_world text,p_capability text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';
 v_principal text:=ident->'binding'->>'subject_principal_id';n integer;v_names integer;v_name text;v_version integer;v_granted boolean;
 v_now timestamptz:=clock_timestamp();
begin
 select count(*),count(distinct d.definition->>'stable_name'),min(d.definition->>'stable_name') into n,v_names,v_name
  from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
   and d.definition->'capability_binding'->>'capability_name'=p_capability;
 -- One definition, none, several Actions, or a non-human session: the wrapped body answers or refuses exactly as before.
 if n<=1 or v_names>1 or ident->'binding'->>'subject_kind' is distinct from 'human' then
  return authz.nexloop_workbench_action_for_single_version(p_digest,p_world,p_capability);
 end if;
 if not exists(select 1 from control.nexloop_governed_capabilities g where g.capability=p_capability and g.subject_rule='human') then
  raise exception 'workbench capability unknown' using errcode='22023';end if;
 -- Versions of one Action: the highest version the caller holds a live execute grant on; else the highest version, not granted.
 select (d.definition->>'version')::integer,true into v_version,v_granted
  from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
   and d.definition->'capability_binding'->>'capability_name'=p_capability
   and exists(select 1 from authz.nexloop_authority_facts f cross join lateral jsonb_array_elements(coalesce(f.payload->'grants','[]'::jsonb)) g
    where f.tenant_id=v_tenant and f.fact_kind='grants' and f.entity_key=array[v_principal,d.resource_id]
     and (f.payload->>'valid_until' is null or (f.payload->>'valid_until')::timestamptz>v_now)
     and coalesce(g->'operations','[]'::jsonb) ? 'execute'
     and (g->>'valid_from' is null or (g->>'valid_from')::timestamptz<=v_now)
     and (g->>'valid_until' is null or (g->>'valid_until')::timestamptz>v_now))
  order by (d.definition->>'version')::integer desc limit 1;
 if v_version is null then
  select max((d.definition->>'version')::integer),false into v_version,v_granted
   from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
    and d.definition->'capability_binding'->>'capability_name'=p_capability;
 end if;
 return jsonb_build_object('stable_name',v_name,'version',v_version,'granted',v_granted);
end $$;

-- NX-028 slice 2 (temporary number 0151): which published Action carries a registered human capability in the
-- caller's tenant (the workbench write entry names operations, the tenant names its Actions), and whether the caller
-- holds a configured grant on it at all (so a missing grant is reported as forbidden, not as unavailable). Grants
-- nothing: the governed entry (0150) still checks the caller's EXECUTE, the human subject and the registry on every write.
create function authz.nexloop_workbench_action_for(p_digest text,p_world text,p_capability text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';n integer;v_result jsonb;
begin
 if ident->'binding'->>'subject_kind' is distinct from 'human' then raise exception 'workbench requires a human session' using errcode='42501';end if;
 if not exists(select 1 from control.nexloop_governed_capabilities g where g.capability=p_capability and g.subject_rule='human') then
  raise exception 'workbench capability unknown' using errcode='22023';end if;
 select count(*),min(jsonb_build_object('stable_name',d.definition->>'stable_name','version',(d.definition->>'version')::integer)::text)::jsonb into n,v_result
  from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
   and d.definition->'capability_binding'->>'capability_name'=p_capability;
 if n=0 then return null;end if;
 if n>1 then raise exception 'workbench capability published more than once' using errcode='22023';end if;
 return v_result||jsonb_build_object('granted',exists(select 1 from authz.nexloop_authority_facts f where f.tenant_id=v_tenant and f.fact_kind='grants'
  and f.entity_key=array[ident->'binding'->>'subject_principal_id','eios:action:'||(v_result->>'stable_name')||':'||(v_result->>'version')]
  and jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0));
end $$;
alter function authz.nexloop_workbench_action_for(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_action_for(text,text,text) from public;
grant execute on function authz.nexloop_workbench_action_for(text,text,text) to nexloop_api;

-- NX-043 X1 follow-up (dispatcher 2026-10-21): a workbench role may hold up to 128 Actions, as nexloop_eios.workbench_roles does; the
-- owner role holds 65 with the experiments scope. control.nexloop_configure_workbench is re-created from its latest body (0116) with
-- only that bound changed (registered in tests/test_function_copy_guard.py). The migration number lives only in the file name and the
-- catalog.
create or replace function control.nexloop_configure_workbench(p_tenant text,p_roles jsonb,p_members jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);cur control.nexloop_workbench_configurations;r record;m jsonb;v_members jsonb:='{}'::jsonb;
 v_actions text[];f record;v_role text;v_version integer;v_app text;
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench members are trusted configuration only' using errcode='42501';end if;
 if p_roles->>'schema_version' is distinct from 'nexloop-workbench-roles/1' or jsonb_typeof(p_roles->'manifest_version') is distinct from 'number'
  or jsonb_typeof(p_roles->'roles') is distinct from 'object' or p_members->>'schema_version' is distinct from 'nexloop-workbench-members/1'
  or p_members->>'tenant_id' is distinct from p_tenant or jsonb_typeof(p_members->'members') is distinct from 'array'
  or jsonb_array_length(p_members->'members')>1000 or coalesce(p_members->>'application_id','')='' then
  raise exception 'workbench manifest invalid' using errcode='22023';end if;
 v_version:=(p_roles->>'manifest_version')::integer;v_app:=p_members->>'application_id';
 perform set_config('eios.tenant_id',p_tenant,true);
 -- D2: exactly the three roles; each a non-empty list of Action resources.
 if (select array_agg(k order by k) from jsonb_object_keys(p_roles->'roles') k) is distinct from array['operator','owner','reviewer'] then
  raise exception 'workbench roles invalid' using errcode='22023';end if;
 for r in select key,value from jsonb_each(p_roles->'roles') loop
  if jsonb_typeof(r.value) is distinct from 'array' or jsonb_array_length(r.value) not between 1 and 128
   or exists(select 1 from jsonb_array_elements(r.value) x where jsonb_typeof(x) is distinct from 'string' or x#>>'{}'!~'^eios:action:[A-Za-z][A-Za-z0-9._-]{0,159}:[1-9][0-9]{0,5}$') then
   raise exception 'workbench role % invalid',r.key using errcode='22023';end if;
 end loop;
 if not exists(select 1 from control.nexloop_browser_business_applications b where b.tenant_id=p_tenant and b.application_id=v_app) then
  raise exception 'workbench application must be a published browser business application' using errcode='22023';end if;
 select * into cur from control.nexloop_workbench_configurations c where c.tenant_id=p_tenant order by c.manifest_version desc limit 1;
 if cur.tenant_id is not null and v_version<cur.roles_version then raise exception 'workbench roles version cannot go back' using errcode='22023';end if;
 for m in select value from jsonb_array_elements(p_members->'members') loop
  if jsonb_typeof(m) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(m) k) is distinct from array['principal_id','role']
   or not (p_roles->'roles' ? (m->>'role')) or coalesce(m->>'principal_id','')='' or v_members ? (m->>'principal_id') then
   raise exception 'workbench member invalid' using errcode='22023';end if;
  -- An existing, active browser Human of this tenant (the identity operator created it; configuration cannot invent one).
  if not exists(select 1 from control.nexloop_browser_memberships bm join control.nexloop_browser_subjects s on s.subject_id=bm.subject_id
    where bm.tenant_id=p_tenant and bm.principal_id=m->>'principal_id' and bm.payload->>'status'='active' and s.payload->>'kind'='human') then
   raise exception 'workbench member must be an existing active Human' using errcode='42501';end if;
  if control.nexloop_is_customer_principal(p_tenant,m->>'principal_id') then
   raise exception 'a customer principal cannot be a workbench member' using errcode='42501';end if;
  v_members:=v_members||jsonb_build_object(m->>'principal_id',m->>'role');
 end loop;
 insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
  values(p_tenant,coalesce(cur.manifest_version,0)+1,v_version,v_app,p_roles->'roles',v_members,encode(sha256(convert_to((p_roles->'roles')::text,'UTF8')),'hex'),session_user);
 -- Every current Human grant of a workbench Action must still be inside its holder's role (revoke grants first, then roles).
 v_actions:=control.nexloop_workbench_actions(p_tenant);
 for f in select af.payload from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='grants' and af.payload->>'subject_kind'='human' loop
  v_role:=v_members->>(f.payload->>'principal_id');
  if exists(select 1 from unnest(control.nexloop_workbench_granted(f.payload,v_actions)) g
    where v_role is null or not (p_roles->'roles'->v_role ? g)) then
   raise exception 'a current Human grant is outside the workbench role of %',f.payload->>'principal_id' using errcode='42501';end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('configured',true,'manifest_version',coalesce(cur.manifest_version,0)+1,'roles_version',v_version,'members',(select count(*) from jsonb_object_keys(v_members)));
end $$;
alter function control.nexloop_configure_workbench(text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_workbench(text,jsonb,jsonb) from public;
grant execute on function control.nexloop_configure_workbench(text,jsonb,jsonb) to nexloop_configurator;

-- NX-028 slice 1 (design §2, D1/D2): enterprise member identity for the owner workbench.
--
-- * The workbench is its own browser business application (a separate row of control.nexloop_browser_business_applications;
--   a separate login realm and cookie in the API). Its members are existing browser Humans written by trusted configuration
--   only (nexloop_configurator); there is no self-registration path.
-- * A role is a set of Action grants (owner / operator / reviewer, deploy/authorization/workbench-roles.v1.json). The role
--   ceiling is kept here; the grants themselves are ordinary EIOS authority facts written by trusted configuration (0050).
-- * A customer principal (one that owns a Consumer, control.nexloop_consumer_owners) never holds a workbench grant and never
--   becomes a member; a member never becomes a customer owner. Enforced on every write of the three tables involved.
-- Append-only: each apply is the next configuration revision (manifest_version) of the tenant, the latest is current;
-- roles_version is the public role manifest's version and never goes back.

create table control.nexloop_workbench_configurations (
 tenant_id text not null,manifest_version integer not null check(manifest_version between 1 and 1000000),
 roles_version integer not null check(roles_version between 1 and 1000000),
 application_id text not null check(length(application_id) between 1 and 320),
 roles jsonb not null check(jsonb_typeof(roles)='object'),members jsonb not null check(jsonb_typeof(members)='object'),
 roles_digest text not null check(roles_digest~'^[a-f0-9]{64}$'),recorded_by text not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,manifest_version)
);
alter table control.nexloop_workbench_configurations owner to nexloop_owner;
alter table control.nexloop_workbench_configurations enable row level security;
alter table control.nexloop_workbench_configurations force row level security;
create policy tenant_boundary on control.nexloop_workbench_configurations to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger nx028_append_only before update or delete on control.nexloop_workbench_configurations for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_workbench_configurations from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

-- Current configuration of a tenant (null when none). Owner-only helpers; callers set no GUC.
create function control.nexloop_workbench_current(p_tenant text) returns control.nexloop_workbench_configurations
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r control.nexloop_workbench_configurations;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_workbench_configurations c where c.tenant_id=p_tenant order by c.manifest_version desc limit 1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return r;
end $$;
-- Every Action any workbench role carries, plus the workbench read Action itself (always workbench-only).
create function control.nexloop_workbench_actions(p_tenant text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select array(select distinct x from (
   select 'eios:action:nexloop.workbench.read:1' x
   union all select jsonb_array_elements_text(r.value) from jsonb_each((control.nexloop_workbench_current(p_tenant)).roles) r) s order by x) $$;
-- The member's current role, or null.
create function control.nexloop_workbench_role(p_tenant text,p_principal text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select (control.nexloop_workbench_current(p_tenant)).members->>p_principal $$;
-- A customer principal: owns a Consumer in any world of the tenant.
create function control.nexloop_is_customer_principal(p_tenant text,p_principal text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v:=exists(select 1 from control.nexloop_consumer_owners o where o.tenant_id=p_tenant and o.principal_id=p_principal);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
-- Workbench Actions a Human grant fact actually grants (non-empty operations).
create function control.nexloop_workbench_granted(p_payload jsonb,p_actions text[]) returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array(select distinct g->'resource'->>'resource_id' from jsonb_array_elements(coalesce(p_payload->'grants','[]'::jsonb)) g
  where g->'resource'->>'resource_id'=any(p_actions) and jsonb_array_length(coalesce(g->'operations','[]'::jsonb))>0 order by 1) $$;
alter function control.nexloop_workbench_current(text) owner to nexloop_owner;
alter function control.nexloop_workbench_actions(text) owner to nexloop_owner;
alter function control.nexloop_workbench_role(text,text) owner to nexloop_owner;
alter function control.nexloop_is_customer_principal(text,text) owner to nexloop_owner;
alter function control.nexloop_workbench_granted(jsonb,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_workbench_current(text),control.nexloop_workbench_actions(text),control.nexloop_workbench_role(text,text),
 control.nexloop_is_customer_principal(text,text),control.nexloop_workbench_granted(jsonb,text[]) from public;

-- Trusted configuration: roles (public manifest) + members (private file) + which browser application is the workbench.
create function control.nexloop_configure_workbench(p_tenant text,p_roles jsonb,p_members jsonb) returns jsonb
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
  if jsonb_typeof(r.value) is distinct from 'array' or jsonb_array_length(r.value) not between 1 and 64
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

-- A Human grant of a workbench Action: only for a current member, only inside the member's role, never a customer.
create function authz.nexloop_workbench_grant_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_granted text[];v_role text;cfg control.nexloop_workbench_configurations;
begin
 if new.fact_kind<>'grants' or new.payload->>'subject_kind' is distinct from 'human' then return new;end if;
 v_granted:=control.nexloop_workbench_granted(new.payload,control.nexloop_workbench_actions(new.tenant_id));
 if cardinality(v_granted)=0 then return new;end if;
 if control.nexloop_is_customer_principal(new.tenant_id,new.payload->>'principal_id') then
  raise exception 'a customer principal cannot hold a workbench grant' using errcode='42501';end if;
 cfg:=control.nexloop_workbench_current(new.tenant_id);v_role:=cfg.members->>(new.payload->>'principal_id');
 if v_role is null or exists(select 1 from unnest(v_granted) g where not (cfg.roles->v_role ? g)) then
  raise exception 'workbench grant outside the member role' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_workbench_grant_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_grant_guard() from public;
create trigger nx028_workbench_grant_guard before insert or update on authz.nexloop_authority_facts
 for each row execute function authz.nexloop_workbench_grant_guard();

-- A workbench member never becomes a customer owner.
create function control.nexloop_customer_owner_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if control.nexloop_workbench_role(new.tenant_id,new.principal_id) is not null then
  raise exception 'a workbench member cannot own a Consumer' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_customer_owner_guard() owner to nexloop_owner;
revoke all on function control.nexloop_customer_owner_guard() from public;
create trigger nx028_customer_owner_guard before insert or update on control.nexloop_consumer_owners
 for each row execute function control.nexloop_customer_owner_guard();

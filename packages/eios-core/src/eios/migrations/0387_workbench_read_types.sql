-- I35 (owner 2026-10-31, option A; docs/design/i35-owner-property-read.md): a workbench role reads the object types its roles manifest
-- lists. Temporary number (0386 is batch 72's), renumbered at merge; depends on 0123/0224 (the member derivation and its audit),
-- 0226 (the audit constraints it widens), 0357 (runtime.nexloop_object_consumers_read) and 0383 (a pseudonymized Consumer is erasing).
-- * control.nexloop_workbench_read_types: per roles manifest version and role, the explicitly listed type names (no wildcard),
--   append-only; written only by the technical configurator with the roles (control.nexloop_configure_workbench_read_types, called by
--   workbench_roles --apply right after control.nexloop_configure_workbench).
-- * authz.nexloop_workbench_member_may_read: the 0224 derivation is renamed and asked first, unchanged. When it refuses, a role whose
--   current list (the roles version of the tenant's current workbench configuration) names the type reads the object and every
--   declared property, except:
--     - a property in a group the owner restricted (property_group_restriction) - checked last, no list overrides it;
--     - an object any of whose Consumers is erasing or erased: the shared resolution runtime.nexloop_object_consumers_read (0315/0357:
--       the Consumer by its own id, Conversation through runtime.nexloop_conversations, Message through its Conversation,
--       Commitment.made_to, CommercialRecord.consumer_ref, any other type's consumer_id, and relations), plus
--       RoleExecutionCeiling.consumer_ids (an array); each id asked of runtime.nexloop_consumer_erasing (I35-a, per-type table in
--       docs/design/i35-owner-property-read.md);
--       this is checked first for every derivation, the existing ones included (they already refused it at the object read).
--   No grant, no fact; operator and reviewer lists are empty in v52, so their reads are unchanged.
-- * authz.nexloop_assert_workbench_member_read (the audited read) audits an object of any type: kind 'object' for the newly readable
--   types, the six existing kinds unchanged; the audit constraints admit them.
create table control.nexloop_workbench_read_types (
 tenant_id text not null references control.nexloop_tenants(tenant_id),roles_version integer not null check(roles_version between 1 and 1000000),
 role text not null check(role in ('owner','operator','reviewer')),
 type_names text[] not null check(cardinality(type_names)<=64 and array_position(type_names,null) is null),
 recorded_by text not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,roles_version,role)
);
alter table control.nexloop_workbench_read_types owner to nexloop_owner;
alter table control.nexloop_workbench_read_types enable row level security;
alter table control.nexloop_workbench_read_types force row level security;
create policy tenant_boundary on control.nexloop_workbench_read_types to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger workbench_read_types_append_only before update or delete on control.nexloop_workbench_read_types for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_workbench_read_types from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

-- The configurator's write: {manifest_version, read_types: {operator: [...], owner: [...], reviewer: [...]}}, each list sorted and unique.
-- The version must be the roles version of the current workbench configuration; a replay of the same lists is a no-op.
create function control.nexloop_configure_workbench_read_types(p_tenant text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);cur control.nexloop_workbench_configurations;r record;v_names text[];v_old text[];n integer:=0;
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench read types are trusted configuration only' using errcode='42501';end if;
 if jsonb_typeof(p_body) is distinct from 'object' or jsonb_typeof(p_body->'manifest_version') is distinct from 'number'
  or jsonb_typeof(p_body->'read_types') is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(p_body->'read_types') k) is distinct from array['operator','owner','reviewer'] then
  raise exception 'workbench read types invalid' using errcode='22023';end if;
 cur:=control.nexloop_workbench_current(p_tenant);
 if cur.tenant_id is null or cur.roles_version<>(p_body->>'manifest_version')::integer then
  raise exception 'workbench read types must follow the current roles version' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 for r in select key,value from jsonb_each(p_body->'read_types') order by key loop
  if jsonb_typeof(r.value) is distinct from 'array' or jsonb_array_length(r.value)>64
   or exists(select 1 from jsonb_array_elements(r.value) x where jsonb_typeof(x) is distinct from 'string' or x#>>'{}'!~'^[A-Za-z][A-Za-z0-9_]{0,63}$') then
   raise exception 'workbench read types of % invalid',r.key using errcode='22023';end if;
  v_names:=array(select jsonb_array_elements_text(r.value));
  if v_names is distinct from array(select distinct x from unnest(v_names) x order by 1) then
   raise exception 'workbench read types of % must be sorted and unique',r.key using errcode='22023';end if;
  select type_names into v_old from control.nexloop_workbench_read_types where tenant_id=p_tenant and roles_version=cur.roles_version and role=r.key;
  if found then
   if v_old is distinct from v_names then raise exception 'workbench read types of % differ for roles version %',r.key,cur.roles_version using errcode='22023';end if;
  else
   insert into control.nexloop_workbench_read_types(tenant_id,roles_version,role,type_names,recorded_by) values(p_tenant,cur.roles_version,r.key,v_names,session_user);
   n:=n+1;
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('roles_version',cur.roles_version,'recorded',n);
end $$;
alter function control.nexloop_configure_workbench_read_types(text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_workbench_read_types(text,jsonb) from public;
grant execute on function control.nexloop_configure_workbench_read_types(text,jsonb) to nexloop_configurator;

-- The types a role reads by its current list (empty without one).
create function control.nexloop_workbench_role_read_types(p_tenant text,p_role text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);cur control.nexloop_workbench_configurations;v text[];
begin
 cur:=control.nexloop_workbench_current(p_tenant);
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select type_names into v from control.nexloop_workbench_read_types where tenant_id=p_tenant and roles_version=cur.roles_version and role=p_role;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return coalesce(v,'{}');
end $$;
alter function control.nexloop_workbench_role_read_types(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_workbench_role_read_types(text,text) from public;

alter function authz.nexloop_workbench_member_may_read(text,text,text,text) rename to nexloop_workbench_member_may_read_before_read_types;
revoke all on function authz.nexloop_workbench_member_may_read_before_read_types(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_workbench_member_may_read(p_tenant text,p_world text,p_role text,p_target text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m text[];o ontology.objects;def jsonb;grp text;restriction jsonb;v_ok boolean;
begin
 m:=regexp_match(p_target,'^eios:(object|property):([A-Za-z][A-Za-z0-9_]{0,63})/([a-f0-9]{64})(?:/([A-Za-z][A-Za-z0-9_]{0,63}))?$');
 -- I35-c: only a workbench role derives anything (0123 let a null role through to a Consumer object; the member read never passes one).
 if p_role is null or p_role not in ('owner','operator','reviewer') then return false;end if;
 perform set_config('eios.tenant_id',p_tenant,true);  -- runtime.nexloop_object_consumers reads under the tenant
 -- NX-029, for every derivation: nothing of an object whose Consumer is erasing or erased (a Consumer object by its own id).
 if m is not null and ((m[2]='Consumer' and runtime.nexloop_consumer_erasing(p_tenant,p_world,m[3]))
   or exists(select 1 from unnest(runtime.nexloop_object_consumers_read(p_tenant,p_world,m[2],m[3])) c where runtime.nexloop_consumer_erasing(p_tenant,p_world,c))
   -- RoleExecutionCeiling names its Consumers in the array consumer_ids, which the shared resolution does not follow.
   or (m[2]='RoleExecutionCeiling' and exists(select 1 from ontology.objects x,jsonb_array_elements_text(case jsonb_typeof(x.properties->'consumer_ids')
      when 'array' then x.properties->'consumer_ids' else '[]'::jsonb end) c
     where x.tenant_id=p_tenant and x.world=p_world and x.type_name=m[2] and x.object_id=m[3] and runtime.nexloop_consumer_erasing(p_tenant,p_world,c)))) then
  return false;end if;
 if authz.nexloop_workbench_member_may_read_before_read_types(p_tenant,p_world,p_role,p_target) then v_ok:=true;
 else
  if m is null or (m[1]='object')<>(m[4] is null) or p_role is null or not (m[2]=any(control.nexloop_workbench_role_read_types(p_tenant,p_role))) then
   return false;end if;
  perform set_config('eios.tenant_id',p_tenant,true);
  select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=m[2] and object_id=m[3];
  if not found then return false;end if;
  -- Only an object of a published type version: its declared properties are what the list opens.
  select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=m[2] and version=o.schema_version;
  if def is null then return false;end if;
  if m[4] is null then return true;end if;
  if not exists(select 1 from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) p where p->>'property_name'=m[4]) then return false;end if;
  v_ok:=true;
 end if;
 -- Last, whatever derived it: a property in an owner-restricted group is never read (no list overrides it).
 m:=regexp_match(p_target,'^eios:property:([A-Za-z][A-Za-z0-9_]{0,63})/([a-f0-9]{64})/([A-Za-z][A-Za-z0-9_]{0,63})$');
 if m is null then return v_ok;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select v.definition into def from ontology.objects x join ontology.object_type_versions v on v.tenant_id=x.tenant_id and v.type_name=x.type_name and v.version=x.schema_version
  where x.tenant_id=p_tenant and x.world=p_world and x.type_name=m[1] and x.object_id=m[2];
 grp:=ontology.nexloop_property_group_of(def,m[3]);
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[m[1]];
 return v_ok and not (grp is not null and coalesce(restriction->'restricted_groups' ? grp,false));
end $$;
alter function authz.nexloop_workbench_member_may_read(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_member_may_read(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

alter table runtime.nexloop_workbench_read_audit drop constraint nexloop_workbench_read_audit_object_kind_check;
alter table runtime.nexloop_workbench_read_audit add constraint nexloop_workbench_read_audit_object_kind_check
 check(object_kind in ('message','consumer','goal','plan_step','commitment','service_offering','artifact','object'));
alter table runtime.nexloop_workbench_read_audit drop constraint nexloop_workbench_read_audit_target_resource_check;
alter table runtime.nexloop_workbench_read_audit add constraint nexloop_workbench_read_audit_target_resource_check
 check(target_resource~'^eios:object:[A-Za-z][A-Za-z0-9_]{0,63}/[a-f0-9]{64}$' or target_resource~'^eios:artifact:[a-f0-9]{32}$');

create or replace function authz.nexloop_assert_workbench_member_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w jsonb;ident jsonb;v_target text:=p_claims->>'target_resource';v_object text;
begin
 w:=authz.nexloop_workbench_member(p_digest,p_world);ident:=w->'identity';
 if p_claims->>'derivation' is distinct from 'workbench-member-v1' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'resource_id' is distinct from v_target or p_claims->>'world' is distinct from p_world
  or p_claims->>'tenant_id' is distinct from w->>'tenant_id' or p_claims->>'principal_id' is distinct from w->>'principal_id'
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or coalesce(p_claims->>'read_purpose','')!~'^[a-z][a-z_]{0,63}$'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench member read claim invalid' using errcode='42501';end if;
 if not authz.nexloop_workbench_member_may_read(w->>'tenant_id',p_world,w->>'role',v_target) then
  raise exception 'workbench member read denied' using errcode='42501';end if;
 v_object:=substring(v_target from '^eios:(?:object|property):([A-Za-z][A-Za-z0-9_]{0,63}/[a-f0-9]{64})');
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 -- One audit row per object and transaction; a failed write fails the read (no exception handler here).
 insert into runtime.nexloop_workbench_read_audit(tenant_id,world,principal_id,role,session_id,object_kind,target_resource,read_purpose,read_txid)
  values(w->>'tenant_id',p_world,w->>'principal_id',w->>'role',ident->'binding'->>'session_id',
   case split_part(v_object,'/',1) when 'Message' then 'message' when 'Consumer' then 'consumer' when 'Goal' then 'goal' when 'PlanStep' then 'plan_step'
    when 'Commitment' then 'commitment' when 'ServiceOffering' then 'service_offering' else 'object' end,'eios:object:'||v_object,p_claims->>'read_purpose',txid_current())
  on conflict(tenant_id,read_txid,principal_id,target_resource) do nothing;
 return ident->'binding';
end $$;

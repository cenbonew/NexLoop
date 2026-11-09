-- NX-062 M4a (docs/implementation/NX-062-M4-projections.md; ADR-041 amendment A1, owner 2026-11-09; L6 M4-1 to M4-5): machine client
-- grants and the first two machine read verbs, object.get and object.list, read in SQL on current facts. The migration number is
-- temporary; it lives only in the file name and the catalog.
--
-- * authz.nexloop_machine_client_grants (A1 §1, M4-1): the one authority of what a machine client may use - per (client, capability)
--   append-only revisions, the latest in force: active with a non-empty object type scope (no wildcard), or revoked. Written only by
--   the owner's governed Actions nexloop.machine_client.grant / .revoke_grant (subject rule human; the handler also requires an active
--   owner, world real); a granted capability is current in the allowlist, a registered read verb and outside the never-grant list; the
--   types are the verb's registered types. No application role nor the configurator holds any privilege; no row is ever deleted.
-- * The read verbs object.get and object.list are registered with their projection (metadata fields and, per type, the properties
--   returned; Commitment and CommercialRecord only - every other type answers not found) and allowlisted.
-- * authz.nexloop_machine_read(prefix, digest, verb, params) (M4-2): identity (the machine snapshot, same function and error), the verb
--   in the key's scope, current in the allowlist, the client's latest grant active, the object type in its scope, the key's world as the
--   data's world; every time is clock_timestamp(), nothing cached. A projection that resolves to nothing refuses (fail closed). An object
--   that does not exist, of another tenant or world, whose Consumer is being erased or erased (runtime.nexloop_object_consumers_read
--   and runtime.nexloop_consumer_erasing, the NX-029/NX-054 functions the workbench derivation calls), or a value naming such a
--   Consumer: the same not found / omitted value. A property in an owner-restricted group is never returned. A legal hold never hides
--   an object (it only keeps it from deletion) and an expired object is deleted by the retention keeper (0129), exactly as the workbench
--   sees them; object.list returns no total and no hidden count (L6 N2). Each read counts per day,
--   key prefix, verb and type (T-12); admission (M3: rate policy, windows) runs before it in the API.
-- * authz.nexloop_machine_capability_list (0437): the grant term is now the client's latest active grant (M4-3) - the list is scope ∩
--   allowlist ∩ grants.
set local timezone = 'UTC';

-- Grants ---------------------------------------------------------------------------------------------------------------------
create table authz.nexloop_machine_client_grants (
 tenant_id text not null,
 client_id text not null,
 capability text not null check(capability~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 revision integer not null check(revision>=1),
 state text not null check(state in ('active','revoked')),
 object_types text[] not null,
 issued_by_principal text not null check(length(issued_by_principal) between 1 and 200),
 issue_request text not null check(issue_request~'^[A-Za-z0-9._:-]{1,200}$'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,client_id,capability,revision),
 foreign key(tenant_id,client_id) references authz.nexloop_machine_clients(tenant_id,client_id),
 check((state='active')=(cardinality(object_types) between 1 and 32)),
 check(state='active' or cardinality(object_types)=0)
);

create table runtime.nexloop_machine_read_counts (
 day date not null,
 tenant_id text not null references control.nexloop_tenants(tenant_id),  -- NX-052 tenant path
 key_prefix text not null check(key_prefix~'^[a-z2-7]{12}$'),
 verb text not null,
 object_type text not null,
 calls bigint not null check(calls>=0),
 primary key(day,tenant_id,key_prefix,verb,object_type)
);

-- The types a read verb returns: the type_name of each {type_name, properties} entry of its projection.
create function control.nexloop_machine_verb_types(p_verb text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(array_agg(e->>'type_name' order by e->>'type_name'),'{}') from control.nexloop_machine_read_verbs v,jsonb_array_elements(v.projection) e
  where v.verb=p_verb and jsonb_typeof(e)='object'
$$;

create function authz.nexloop_machine_grant_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare latest integer;t text;
begin
 select max(revision) into latest from authz.nexloop_machine_client_grants where tenant_id=new.tenant_id and client_id=new.client_id and capability=new.capability;
 if new.revision is distinct from coalesce(latest,0)+1 then raise exception 'machine grant revision must follow the latest' using errcode='22023';end if;
 if new.state='active' then
  if control.nexloop_machine_capability_forbidden(new.capability) or not control.nexloop_machine_capability_current(new.capability)
   or not exists(select 1 from control.nexloop_machine_read_verbs v where v.verb=new.capability) then
   raise exception 'machine grant capability not allowed' using errcode='42501';end if;
  if cardinality(new.object_types)<>(select count(distinct x) from unnest(new.object_types) x) then raise exception 'machine grant types duplicated' using errcode='22023';end if;
  foreach t in array new.object_types loop
   if t is null or t like '%*%' or not (t=any(control.nexloop_machine_verb_types(new.capability))) then
    raise exception 'machine grant type not allowed' using errcode='42501';end if;
  end loop;
 end if;
 return new;
end $$;
create trigger nx062_guard before insert on authz.nexloop_machine_client_grants for each row execute function authz.nexloop_machine_grant_guard();
create trigger nx062_append_only before update or delete on authz.nexloop_machine_client_grants for each row execute function control.nexloop_nx022_append_only();
create trigger nx062_no_truncate before truncate on authz.nexloop_machine_client_grants for each statement execute function control.nexloop_nx022_append_only();
do $$declare t text;begin
 foreach t in array array['authz.nexloop_machine_client_grants','runtime.nexloop_machine_read_counts'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy nx062_owner on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $$;

-- The client's grant in force: its latest revision, when active (null otherwise).
create function authz.nexloop_machine_grant_types(p_tenant text,p_client text,p_capability text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select case when g.state='active' then g.object_types end from authz.nexloop_machine_client_grants g
  where g.tenant_id=p_tenant and g.client_id=p_client and g.capability=p_capability order by g.revision desc limit 1
$$;

-- Governed Actions (owner only) ---------------------------------------------------------------------------------------------
create function control.nexloop_governed_grant_machine_capability(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c authz.nexloop_machine_clients;v_types text[];v_revision integer;v_grant boolean:=body->>'operation'='grant_machine_capability';v_current text[];
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,
  case when v_grant then array['client_id','capability','object_types'] else array['client_id','capability','reason'] end);
 if body->>'operation' not in ('grant_machine_capability','revoke_machine_grant') or coalesce(body->>'client_id','')!~'^[a-z][a-z0-9-]{2,62}$'
  or coalesce(body->>'capability','')!~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$' then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 if v_grant then
  if jsonb_typeof(body->'object_types') is distinct from 'array' or jsonb_array_length(body->'object_types') not between 1 and 32
   or exists(select 1 from jsonb_array_elements(body->'object_types') e where jsonb_typeof(e)<>'string' or e#>>'{}'!~'^[A-Z][A-Za-z0-9_]{0,63}$') then
   perform control.nexloop_machine_access_refuse('invalid');end if;
  select array_agg(x order by x) into v_types from jsonb_array_elements_text(body->'object_types') x;
  if cardinality(v_types)<>(select count(distinct x) from unnest(v_types) x) then perform control.nexloop_machine_access_refuse('invalid');end if;
 elsif jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 select * into c from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=body->>'client_id' for update;
 if not found then perform control.nexloop_machine_access_refuse('client_not_found');end if;
 v_current:=authz.nexloop_machine_grant_types(p_tenant,c.client_id,body->>'capability');
 if v_grant then
  if c.status<>'active' then perform control.nexloop_machine_access_refuse('client_inactive');end if;
  if control.nexloop_machine_capability_forbidden(body->>'capability') or not control.nexloop_machine_capability_current(body->>'capability')
   or not exists(select 1 from control.nexloop_machine_read_verbs v where v.verb=body->>'capability') then
   perform control.nexloop_machine_access_refuse('capability_not_allowed');end if;
  if not (v_types<@control.nexloop_machine_verb_types(body->>'capability')) then perform control.nexloop_machine_access_refuse('type_not_allowed');end if;
  if v_current is not distinct from v_types then return jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','changed',false);end if;
 elsif v_current is null then
  return jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','changed',false);
 end if;
 select coalesce(max(revision),0)+1 into v_revision from authz.nexloop_machine_client_grants where tenant_id=p_tenant and client_id=c.client_id and capability=body->>'capability';
 insert into authz.nexloop_machine_client_grants(tenant_id,client_id,capability,revision,state,object_types,issued_by_principal,issue_request)
  values(p_tenant,c.client_id,body->>'capability',v_revision,case when v_grant then 'active' else 'revoked' end,case when v_grant then v_types else '{}' end,p_principal,p_intent);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,case when v_grant then 'nexloop.machine_client.grant' else 'nexloop.machine_client.revoke_grant' end,
  'machine-grant:'||p_tenant||':'||c.client_id||':'||(body->>'capability')||':'||v_revision,
  jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','revision',v_revision,'object_types',to_jsonb(coalesce(v_types,'{}'))));
 return jsonb_build_object('client_id',c.client_id,'capability',body->>'capability','revision',v_revision,'state',case when v_grant then 'active' else 'revoked' end,
  'object_types',to_jsonb(coalesce(v_types,'{}')),'changed',true);
end $$;

-- Read verbs and their projection ----------------------------------------------------------------------------------------------
insert into control.nexloop_machine_read_verbs(verb,projection,description,source_task) values
 ('object.get','["type_name","object_id","schema_version","revision","updated_at","properties",
   {"type_name":"Commitment","properties":["due_at","due_precision","fulfillment_basis","late","made_to","promised_at","related_goal_ref","status","supersedes"]},
   {"type_name":"CommercialRecord","properties":["amount_minor","connector_id","currency","data_mode","event_count","kind","late_event_count","link_state","occurred_at","refunded_minor","status","status_at","verification_state"]}]',
  'Read one object of the world model by type and id: metadata and the registered properties of its type only (no text, contact or reference to a message).','NX-062'),
 ('object.list','["type_name","items","next_after",
   {"type_name":"Commitment","properties":["due_at","due_precision","fulfillment_basis","late","made_to","promised_at","related_goal_ref","status","supersedes"]},
   {"type_name":"CommercialRecord","properties":["amount_minor","connector_id","currency","data_mode","event_count","kind","late_event_count","link_state","occurred_at","refunded_minor","status","status_at","verification_state"]}]',
  'List the objects of one type, at most 50 a page, each with the registered properties of its type only.','NX-062');
insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values
 ('object.get',1,'active','read','Read one object (registered properties only).','migration:0439_nx062_machine_reads.sql','NX-062'),
 ('object.list',1,'active','read','List the objects of one type (registered properties only).','migration:0439_nx062_machine_reads.sql','NX-062');

-- The properties of one object the projection returns (value and value type), erasing values and restricted groups left out.
create function authz.nexloop_machine_object_properties(p_tenant text,p_world text,o ontology.objects,p_props text[]) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare def jsonb;restriction jsonb;v jsonb:='{}'::jsonb;p text;val jsonb;
begin
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=o.type_name and version=o.schema_version;
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[o.type_name];
 foreach p in array p_props loop
  val:=o.properties->p;
  if val is null or val='null'::jsonb then continue;end if;
  if def is null or not exists(select 1 from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) d where d->>'property_name'=p) then continue;end if;
  if coalesce(restriction->'restricted_groups' ? ontology.nexloop_property_group_of(def,p),false) then continue;end if;
  if exists(select 1 from jsonb_array_elements(case jsonb_typeof(val) when 'array' then val when 'string' then jsonb_build_array(val) else '[]'::jsonb end) e
    where jsonb_typeof(e)='string' and e#>>'{}'~'^[a-f0-9]{64}$' and runtime.nexloop_consumer_erasing(p_tenant,p_world,e#>>'{}')) then continue;end if;
  v:=v||jsonb_build_object(p,jsonb_build_object('value',val,'value_type',jsonb_typeof(val)));
 end loop;
 return v;
end $$;

-- An object the machine may see: it exists in the key's world, and nothing of it belongs to a Consumer being erased or erased.
create function authz.nexloop_machine_object_visible(p_tenant text,p_world text,p_type text,p_object text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return exists(select 1 from ontology.objects x where x.tenant_id=p_tenant and x.world=p_world and x.type_name=p_type and x.object_id=p_object)
  and not exists(select 1 from unnest(runtime.nexloop_object_consumers_read(p_tenant,p_world,p_type,p_object)) c where runtime.nexloop_consumer_erasing(p_tenant,p_world,c));
end $$;

create function authz.nexloop_machine_read(p_prefix text,p_digest text,p_verb text,p_params jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);v_tenant text;v_world text;v_client text;v_types text[];v_type text;
 v_props text[];v_fields text[];o ontology.objects;v_result jsonb;v_after text;v_limit integer:=50;v_items jsonb:='[]'::jsonb;v_scanned integer:=0;
begin
 v_tenant:=ident->'binding'->>'tenant_id';v_world:=ident->>'world';v_client:=ident->>'client_id';
 if p_verb is null or not (ident->'scope' ? p_verb) then raise exception 'machine read: not_permitted' using errcode='42501';end if;
 if not control.nexloop_machine_capability_current(p_verb) then raise exception 'machine read: not_permitted' using errcode='42501';end if;
 v_types:=authz.nexloop_machine_grant_types(v_tenant,v_client,p_verb);
 if v_types is null then raise exception 'machine read: not_permitted' using errcode='42501';end if;
 if jsonb_typeof(p_params) is distinct from 'object' then raise exception 'machine read: invalid' using errcode='22023';end if;
 v_type:=p_params->>'type_name';
 if coalesce(v_type,'')!~'^[A-Z][A-Za-z0-9_]{0,63}$' then raise exception 'machine read: invalid' using errcode='22023';end if;
 -- Outside the grant's types, or a type the verb does not project: not found, as for an absent object (T-11).
 if not (v_type=any(v_types)) or not (v_type=any(control.nexloop_machine_verb_types(p_verb))) then raise exception 'machine read: not_found' using errcode='P0002';end if;
 select array_agg(x) into v_props from control.nexloop_machine_read_verbs v,jsonb_array_elements(v.projection) e,jsonb_array_elements_text(e->'properties') x
  where v.verb=p_verb and jsonb_typeof(e)='object' and e->>'type_name'=v_type;
 select array_agg(e#>>'{}') into v_fields from control.nexloop_machine_read_verbs v,jsonb_array_elements(v.projection) e where v.verb=p_verb and jsonb_typeof(e)='string';
 if coalesce(cardinality(v_props),0)=0 or coalesce(cardinality(v_fields),0)=0 then raise exception 'machine read: projection_unavailable' using errcode='55000';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if p_verb='object.get' then
  if (select array_agg(k order by k) from jsonb_object_keys(p_params) k) is distinct from array['object_id','type_name']
   or coalesce(p_params->>'object_id','')!~'^[a-f0-9]{64}$' then raise exception 'machine read: invalid' using errcode='22023';end if;
  if not authz.nexloop_machine_object_visible(v_tenant,v_world,v_type,p_params->>'object_id') then raise exception 'machine read: not_found' using errcode='P0002';end if;
  select * into o from ontology.objects where tenant_id=v_tenant and world=v_world and type_name=v_type and object_id=p_params->>'object_id';
  v_result:=jsonb_build_object('type_name',o.type_name,'object_id',o.object_id,'schema_version',o.schema_version,'revision',o.nexloop_revision,
   'updated_at',to_jsonb(o.updated_at),'properties',authz.nexloop_machine_object_properties(v_tenant,v_world,o,v_props));
 elsif p_verb='object.list' then
  if exists(select 1 from jsonb_object_keys(p_params) k where k not in ('type_name','after')) or (p_params ? 'after' and coalesce(p_params->>'after','')!~'^[a-f0-9]{64}$') then
   raise exception 'machine read: invalid' using errcode='22023';end if;
  v_after:=coalesce(p_params->>'after','');
  for o in select * from ontology.objects where tenant_id=v_tenant and world=v_world and type_name=v_type and object_id>v_after order by object_id limit v_limit loop
   if authz.nexloop_machine_object_visible(v_tenant,v_world,v_type,o.object_id) then
    v_items:=v_items||jsonb_build_array(jsonb_build_object('object_id',o.object_id,'revision',o.nexloop_revision,'updated_at',to_jsonb(o.updated_at),
     'properties',authz.nexloop_machine_object_properties(v_tenant,v_world,o,v_props)));
   end if;
   v_scanned:=v_scanned+1;v_after:=o.object_id;
  end loop;
  -- L6 N2: no total and no count of hidden objects (either would tell how many objects the key may not see); a page may be short.
  v_result:=jsonb_build_object('type_name',v_type,'items',v_items,'next_after',case when v_scanned=v_limit then to_jsonb(v_after) else 'null'::jsonb end);
 else
  raise exception 'machine read: not_permitted' using errcode='42501';
 end if;
 insert into runtime.nexloop_machine_read_counts(day,tenant_id,key_prefix,verb,object_type,calls)
  values((clock_timestamp() at time zone 'UTC')::date,v_tenant,ident->>'key_prefix',p_verb,v_type,1)
  on conflict(day,tenant_id,key_prefix,verb,object_type) do update set calls=runtime.nexloop_machine_read_counts.calls+1;
 -- The projection is the whole answer: no other top-level field.
 if exists(select 1 from jsonb_object_keys(v_result) k where not (k=any(v_fields))) then raise exception 'machine read: projection_unavailable' using errcode='55000';end if;
 return v_result;
end $$;

-- 0437's capability list with its grant term (M4-3): scope ∩ current allowlist ∩ the client's latest active grant.
create or replace function authz.nexloop_machine_capability_list(p_prefix text,p_digest text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);v jsonb;
begin
 select coalesce(jsonb_agg(jsonb_build_object('capability',c.capability,'kind',c.kind,'description',c.description) order by c.capability),'[]'::jsonb) into v
  from (select distinct on (capability) capability,kind,description,state from control.nexloop_machine_capabilities order by capability,revision desc) c
  where c.state='active' and c.capability in (select jsonb_array_elements_text(ident->'scope'))
   and control.nexloop_machine_capability_current(c.capability)
   and authz.nexloop_machine_grant_types(ident->'binding'->>'tenant_id',ident->>'client_id',c.capability) is not null;
 return v;
end $$;

do $$declare f text;begin
 foreach f in array array['control.nexloop_machine_verb_types(text)','authz.nexloop_machine_grant_guard()','authz.nexloop_machine_grant_types(text,text,text)',
  'control.nexloop_governed_grant_machine_capability(text,text,text,text,text,jsonb)','authz.nexloop_machine_object_properties(text,text,ontology.objects,text[])',
  'authz.nexloop_machine_object_visible(text,text,text,text)','authz.nexloop_machine_read(text,text,text,jsonb)','authz.nexloop_machine_capability_list(text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function authz.nexloop_machine_read(text,text,text,jsonb),authz.nexloop_machine_capability_list(text,text) to nexloop_api;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('machine_client.grant','grant_machine_capability','human','control.nexloop_governed_grant_machine_capability(text,text,text,text,text,jsonb)','NX-062'),
 ('machine_client.revoke_grant','revoke_machine_grant','human','control.nexloop_governed_grant_machine_capability(text,text,text,text,text,jsonb)','NX-062');

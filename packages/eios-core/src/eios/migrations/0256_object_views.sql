-- Object View, read only (capability gap P2, owner 2026-10-20 as recommended; same pattern as Object Set, ADR-036). The migration
-- number lives only in the file name and the catalog (renumbered at integration); nothing below repeats it.
-- 1. control.nexloop_object_view_definitions: published versions of the tenant's Object Views, stored in the field shape of the
--    vendored contract eios/ontology/definitions.py ObjectViewDefinition: source (one object type), fields (property references in
--    display order), relations (relation type references), no functions or actions, display_hints {title_property?,
--    property_groups: the sections}. Versions are immutable and never deleted.
-- 2. Governed owner Action object_view.publish / publish_object_view: the next version of one named view. Every field must be a
--    property of the source type and appear in exactly one section; every relation must touch the source type. A View is layout
--    only: it never grants a READ and holds no value (restricted groups are withheld at read time, as without a View).
-- 3. Workbench observe verb object_view (every workbench member, layout only): the View the detail of an object of one type uses -
--    the most recently published version of any view on that type - or null, then the client keeps its default layout.
-- Nothing here writes a business object or derives a READ.

-- 1. Definitions ---------------------------------------------------------------------------------------------------------------
create table control.nexloop_object_view_definitions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 stable_name text not null check(stable_name~'^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$'),
 version integer not null check(version>=1),
 object_type text not null check(object_type~'^[A-Z][A-Za-z0-9_]{0,63}$'),
 definition jsonb not null check(jsonb_typeof(definition)='object' and octet_length(definition::text)<=32768),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),
 reason text not null check(char_length(reason) between 1 and 500),
 published_by text not null,request_intent text not null,published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,stable_name,version),
 check(definition->>'definition_type'='object_view' and definition->>'stable_name'=stable_name and (definition->>'version')::integer=version
  and definition->>'tenant_id'=tenant_id and definition->>'status'='published' and definition->'source'->'object_type'->>'stable_name'=object_type
  and definition->'functions'='[]'::jsonb and definition->'actions'='[]'::jsonb));
create index nexloop_object_view_definitions_type on control.nexloop_object_view_definitions(tenant_id,world,object_type,published_at desc);
alter table control.nexloop_object_view_definitions owner to nexloop_owner;
alter table control.nexloop_object_view_definitions enable row level security;
alter table control.nexloop_object_view_definitions force row level security;
create policy object_view_definition_tenant on control.nexloop_object_view_definitions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_object_view_definitions from public,nexloop_api,nexloop_identity,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,
 nexloop_runtime,nexloop_configurator,nexloop_metrics;

create function control.nexloop_object_view_definitions_immutable() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'published Object View versions are immutable' using errcode='42501';end $$;
alter function control.nexloop_object_view_definitions_immutable() owner to nexloop_owner;
revoke all on function control.nexloop_object_view_definitions_immutable() from public;
create trigger object_view_definitions_immutable before update or delete on control.nexloop_object_view_definitions
 for each row execute function control.nexloop_object_view_definitions_immutable();
create trigger object_view_definitions_no_truncate before truncate on control.nexloop_object_view_definitions
 for each statement execute function control.nexloop_object_view_definitions_immutable();

-- 2. Governed publication ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_publish_object_view(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_name text:=body->>'stable_name';v_type text:=body->>'object_type';tv integer;tdef jsonb;v_props jsonb;sec jsonb;f jsonb;r jsonb;v_ref jsonb;
 v_fields jsonb:='[]'::jsonb;v_seen text[]:='{}';v_groups jsonb:='[]'::jsonb;v_relations jsonb:='[]'::jsonb;v_group_names text[]:='{}';v_hints jsonb;
 rel record;v_version integer;v_def jsonb;latest control.nexloop_object_view_definitions;v_compare jsonb;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','stable_name','object_type','sections','title_property','relations','reason'))
  or coalesce(v_name,'')!~'^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$' or coalesce(v_type,'')!~'^[A-Z][A-Za-z0-9_]{0,63}$'
  or jsonb_typeof(body->'sections') is distinct from 'array' or jsonb_array_length(body->'sections') not between 1 and 8
  or jsonb_typeof(coalesce(body->'relations','[]'::jsonb)) is distinct from 'array' or jsonb_array_length(coalesce(body->'relations','[]'::jsonb))>8
  or (body ? 'title_property' and body->'title_property'<>'null'::jsonb and jsonb_typeof(body->'title_property') is distinct from 'string')
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'object views: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select version,definition into tv,tdef from ontology.object_type_versions where tenant_id=p_tenant and type_name=v_type order by version desc limit 1;
 if not found then raise exception 'object views: unknown_object_type' using errcode='22023';end if;
 v_props:=coalesce((select jsonb_object_agg(e->>'property_name',true) from jsonb_array_elements(tdef->'properties') e),'{}'::jsonb);
 -- The vendored contract's references: object type by stable name, version and a digest of the stored type definition.
 v_ref:=jsonb_build_object('tenant_id',p_tenant,'schema_type','object_type','stable_name',v_type,'version',tv,
  'schema_digest',encode(sha256(convert_to(tdef::text,'UTF8')),'hex'));
 for sec in select value from jsonb_array_elements(body->'sections') loop
  if jsonb_typeof(sec) is distinct from 'object' or exists(select 1 from jsonb_object_keys(sec) k where k not in ('name','title','fields'))
   or coalesce(sec->>'name','')!~'^[a-z][a-z0-9_]{0,31}$' or sec->>'name'=any(v_group_names)
   or jsonb_typeof(sec->'title') is distinct from 'string' or char_length(btrim(sec->>'title')) not between 1 and 40
   or jsonb_typeof(sec->'fields') is distinct from 'array' or jsonb_array_length(sec->'fields') not between 1 and 24 then
   raise exception 'object views: invalid_section' using errcode='22023';end if;
  v_group_names:=v_group_names||(sec->>'name');
  for f in select value from jsonb_array_elements(sec->'fields') loop
   if jsonb_typeof(f) is distinct from 'string' or f#>>'{}'!~'^[A-Za-z][A-Za-z0-9_]{0,63}$' or not v_props ? (f#>>'{}') then
    raise exception 'object views: unknown_property' using errcode='22023';end if;
   if (f#>>'{}')=any(v_seen) then raise exception 'object views: duplicate' using errcode='22023';end if;
   v_seen:=v_seen||(f#>>'{}');
   v_fields:=v_fields||jsonb_build_array(jsonb_build_object('object_type',v_ref,'property_name',f#>>'{}'));
  end loop;
  v_groups:=v_groups||jsonb_build_array(jsonb_build_object('group_name',sec->>'name','display_name',btrim(sec->>'title'),'property_names',sec->'fields'));
 end loop;
 if cardinality(v_seen)>48 then raise exception 'object views: invalid' using errcode='22023';end if;
 if body->>'title_property' is not null and not (body->>'title_property')=any(v_seen) then raise exception 'object views: unknown_property' using errcode='22023';end if;
 for r in select value from jsonb_array_elements(coalesce(body->'relations','[]'::jsonb)) loop
  if jsonb_typeof(r) is distinct from 'string' or r#>>'{}'!~'^[a-z][a-z0-9_]{0,63}$' then raise exception 'object views: unknown_relation' using errcode='22023';end if;
  select relation_name,version,definition into rel from ontology.relation_type_versions where tenant_id=p_tenant and relation_name=r#>>'{}' order by version desc limit 1;
  if not found or v_type not in (coalesce(rel.definition->>'source_type',''),coalesce(rel.definition->>'target_type','')) then
   raise exception 'object views: unknown_relation' using errcode='22023';end if;
  v_relations:=v_relations||jsonb_build_array(jsonb_build_object('tenant_id',p_tenant,'schema_type','relation_type','stable_name',rel.relation_name,'version',rel.version,
   'schema_digest',encode(sha256(convert_to(rel.definition::text,'UTF8')),'hex')));
 end loop;
 if (select count(distinct value) from jsonb_array_elements(v_relations))<>jsonb_array_length(v_relations) then raise exception 'object views: duplicate' using errcode='22023';end if;
 v_hints:=jsonb_build_object('property_groups',v_groups)||case when body->>'title_property' is not null then jsonb_build_object('title_property',body->>'title_property') else '{}'::jsonb end;
 select * into latest from control.nexloop_object_view_definitions where tenant_id=p_tenant and world=p_world and stable_name=v_name order by version desc limit 1 for update;
 if found and latest.object_type<>v_type then raise exception 'object views: object_type_changed' using errcode='22023';end if;
 v_compare:=jsonb_build_object('source',jsonb_build_object('object_type',v_ref,'object_set',null),'fields',v_fields,'relations',v_relations,'display_hints',v_hints);
 if found and jsonb_build_object('source',latest.definition->'source','fields',latest.definition->'fields','relations',latest.definition->'relations',
   'display_hints',latest.definition->'display_hints')=v_compare then
  return jsonb_build_object('stable_name',v_name,'version',latest.version,'changed',false);end if;
 v_version:=coalesce(latest.version,0)+1;
 v_def:=jsonb_build_object('tenant_id',p_tenant,'definition_type','object_view','stable_name',v_name,'version',v_version,'status','published',
  'required_scopes','[]'::jsonb,'required_markings','[]'::jsonb,'created_by',p_principal,
  'created_at',to_char(clock_timestamp() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'source_lineage','[]'::jsonb)||v_compare
  ||jsonb_build_object('functions','[]'::jsonb,'actions','[]'::jsonb);
 insert into control.nexloop_object_view_definitions(tenant_id,world,stable_name,version,object_type,definition,definition_digest,reason,published_by,request_intent)
  values(p_tenant,p_world,v_name,v_version,v_type,v_def,encode(sha256(convert_to(v_def::text,'UTF8')),'hex'),body->>'reason',p_principal,p_intent);
 return jsonb_build_object('stable_name',v_name,'version',v_version,'changed',true);
end $$;

-- 3. The View an object detail uses (layout only) ---------------------------------------------------------------------------------
create function runtime.nexloop_object_view_for_type(p_tenant text,p_world text,p_type text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d control.nexloop_object_view_definitions;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into d from control.nexloop_object_view_definitions where tenant_id=p_tenant and world=p_world and object_type=p_type
  order by published_at desc,stable_name,version desc limit 1;
 if not found then return null;end if;
 return jsonb_build_object('stable_name',d.stable_name,'version',d.version,'object_type',d.object_type,
  'object_type_version',(d.definition->'source'->'object_type'->>'version')::integer,
  'title_property',d.definition->'display_hints'->'title_property',
  'sections',(select coalesce(jsonb_agg(jsonb_build_object('name',g->>'group_name','title',g->>'display_name','fields',g->'property_names') order by ord),'[]'::jsonb)
    from jsonb_array_elements(d.definition->'display_hints'->'property_groups') with ordinality x(g,ord)),
  'relations',(select coalesce(jsonb_agg(r->>'stable_name' order by ord),'[]'::jsonb) from jsonb_array_elements(d.definition->'relations') with ordinality x(r,ord)),
  'definition_digest',d.definition_digest,'published_at',d.published_at);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_governed_publish_object_view(text,text,text,text,text,jsonb)','runtime.nexloop_object_view_for_type(text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('object_view.publish','publish_object_view','human','control.nexloop_governed_publish_object_view(text,text,text,text,text,jsonb)','NX-028');

-- 4. Workbench read (observe; every other verb unchanged) ------------------------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_object_views;
revoke all on function authz.nexloop_workbench_observe_read_before_object_views(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'object_view' then
  return authz.nexloop_workbench_observe_read_before_object_views(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as the earlier verbs).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 -- Every workbench member holding the read Action: a View is layout only; the values keep their own READ (object_fields, read_object).
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select array_agg(k2 order by k2) from jsonb_object_keys(c) k2) is distinct from array['type_name','verb'] or coalesce(c->>'type_name','')!~'^[A-Z][A-Za-z0-9_]{0,63}$' then
  raise exception 'workbench observe invalid' using errcode='22023';end if;
 return jsonb_build_object('view',runtime.nexloop_object_view_for_type(v_tenant,p_world,c->>'type_name'));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;

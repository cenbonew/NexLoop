-- Object Set, read only (ADR-036; capability gap D-G2, owner 2026-10-20). The migration number lives only in the file name and the
-- catalog (renumbered at integration); nothing below repeats it.
-- 1. control.nexloop_object_set_definitions: published versions of the tenant's Object Sets, stored in the field shape of the
--    vendored contract eios/ontology/definitions.py ObjectSetDefinition (object type, constant filters, sort, projection; no
--    parameters, link traversals, aggregations or semantic search; snapshots_allowed false). Versions are immutable and never
--    deleted.
-- 2. Governed owner Action object_set.publish / publish_object_set: the next version of one named set. Filter and sort properties
--    must belong to a declared property group the owner has not restricted (no inference of a restricted value from a count).
-- 3. runtime.nexloop_object_set_candidates: evaluated in SQL at read time over ontology.objects, no member snapshot; returns the
--    candidate ids of one page and the candidate total; a set whose filter or sort property became restricted returns withheld.
-- 4. Workbench observe verbs object_sets and object_set_members for every workbench member; the caller checks each object's READ
--    (ADR-025/ADR-035, audited) and each projected property's READ, and shows the rest as invisible / withheld.
-- Nothing here writes a business object or derives a READ.

-- 1. Definitions ---------------------------------------------------------------------------------------------------------------
create table control.nexloop_object_set_definitions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 stable_name text not null check(stable_name~'^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$'),
 version integer not null check(version>=1),
 definition jsonb not null check(jsonb_typeof(definition)='object' and octet_length(definition::text)<=32768),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),
 reason text not null check(char_length(reason) between 1 and 500),
 published_by text not null,request_intent text not null,published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,stable_name,version),
 check(definition->>'definition_type'='object_set' and definition->>'stable_name'=stable_name and (definition->>'version')::integer=version
  and definition->>'tenant_id'=tenant_id and definition->>'status'='published' and definition->'snapshots_allowed'='false'::jsonb));
alter table control.nexloop_object_set_definitions owner to nexloop_owner;
alter table control.nexloop_object_set_definitions enable row level security;
alter table control.nexloop_object_set_definitions force row level security;
create policy object_set_definition_tenant on control.nexloop_object_set_definitions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_object_set_definitions from public,nexloop_api,nexloop_identity,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,
 nexloop_runtime,nexloop_configurator,nexloop_metrics;

create function control.nexloop_object_set_definitions_immutable() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'published Object Set versions are immutable' using errcode='42501';end $$;
alter function control.nexloop_object_set_definitions_immutable() owner to nexloop_owner;
revoke all on function control.nexloop_object_set_definitions_immutable() from public;
create trigger object_set_definitions_immutable before update or delete on control.nexloop_object_set_definitions
 for each row execute function control.nexloop_object_set_definitions_immutable();
create trigger object_set_definitions_no_truncate before truncate on control.nexloop_object_set_definitions
 for each statement execute function control.nexloop_object_set_definitions_immutable();

-- A property may filter or sort only when it belongs to a declared property group the owner has not restricted (ADR-036 §2.5).
create function runtime.nexloop_object_set_property_open(p_tenant text,p_type_definition jsonb,p_property text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare grp text:=ontology.nexloop_property_group_of(p_type_definition,p_property);
begin
 if grp is null then return false;end if;
 return not exists(select 1 from authz.nexloop_authority_facts af where af.tenant_id=p_tenant and af.fact_kind='property_group_restriction'
  and af.entity_key=array[p_type_definition->>'type_name'] and af.payload->'restricted_groups' ? grp);
end $$;

-- 2. Governed publication ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_publish_object_set(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_name text:=body->>'stable_name';v_type text:=body->>'object_type';tv integer;tdef jsonb;f jsonb;s jsonb;p jsonb;v_props jsonb;vt text;
 v_version integer;v_ref jsonb;v_def jsonb;v_filters jsonb:='[]'::jsonb;v_sort jsonb:='[]'::jsonb;v_projection jsonb:='[]'::jsonb;latest control.nexloop_object_set_definitions;
 v_compare jsonb;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','stable_name','object_type','filters','sort','projection','reason'))
  or coalesce(v_name,'')!~'^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$' or coalesce(v_type,'')!~'^[A-Z][A-Za-z0-9_]{0,63}$'
  or jsonb_typeof(body->'filters') is distinct from 'array' or jsonb_array_length(body->'filters')>8
  or jsonb_typeof(coalesce(body->'sort','[]'::jsonb)) is distinct from 'array' or jsonb_array_length(coalesce(body->'sort','[]'::jsonb))>3
  or jsonb_typeof(body->'projection') is distinct from 'array' or jsonb_array_length(body->'projection') not between 1 and 12
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'object sets: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select version,definition into tv,tdef from ontology.object_type_versions where tenant_id=p_tenant and type_name=v_type order by version desc limit 1;
 if not found then raise exception 'object sets: unknown_object_type' using errcode='22023';end if;
 v_props:=coalesce((select jsonb_object_agg(e->>'property_name',e->>'value_type') from jsonb_array_elements(tdef->'properties') e),'{}'::jsonb);
 -- The vendored contract's references: object type by stable name, version and a digest of the stored type definition.
 v_ref:=jsonb_build_object('tenant_id',p_tenant,'schema_type','object_type','stable_name',v_type,'version',tv,
  'schema_digest',encode(sha256(convert_to(tdef::text,'UTF8')),'hex'));
 for f in select value from jsonb_array_elements(body->'filters') loop
  if jsonb_typeof(f) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(f) k) is distinct from array['operator','property','value']
   or coalesce(f->>'property','')!~'^[a-z][a-z0-9_]{0,63}$' or not v_props ? (f->>'property')
   or f->>'operator' not in ('eq','ne','gt','gte','lt','lte','in','not_in') or f->>'operator' is null then
   raise exception 'object sets: invalid_filter' using errcode='22023';end if;
  vt:=v_props->>(f->>'property');
  if f->>'operator' in ('in','not_in') then
   if jsonb_typeof(f->'value') is distinct from 'array' or jsonb_array_length(f->'value') not between 1 and 50
    or exists(select 1 from jsonb_array_elements(f->'value') e where jsonb_typeof(e) not in ('string','number','boolean') or (jsonb_typeof(e)='string' and char_length(e#>>'{}')>200)) then
    raise exception 'object sets: invalid_filter' using errcode='22023';end if;
  elsif jsonb_typeof(f->'value') not in ('string','number','boolean') or (jsonb_typeof(f->'value')='string' and char_length(f->>'value')>200) then
   raise exception 'object sets: invalid_filter' using errcode='22023';
  elsif f->>'operator' in ('gt','gte','lt','lte') and not ((vt in ('integer','number') and jsonb_typeof(f->'value')='number')
    or (vt in ('string','datetime') and jsonb_typeof(f->'value')='string')) then
   raise exception 'object sets: invalid_filter' using errcode='22023';end if;
  if not runtime.nexloop_object_set_property_open(p_tenant,tdef,f->>'property') then raise exception 'object sets: restricted_property' using errcode='22023';end if;
  v_filters:=v_filters||jsonb_build_array(jsonb_build_object('property',jsonb_build_object('object_type',v_ref,'property_name',f->>'property'),
   'operator',f->>'operator','value',f->'value'));
 end loop;
 for s in select value from jsonb_array_elements(coalesce(body->'sort','[]'::jsonb)) loop
  if jsonb_typeof(s) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(s) k) is distinct from array['direction','property']
   or coalesce(s->>'property','')!~'^[a-z][a-z0-9_]{0,63}$' or not v_props ? (s->>'property') or s->>'direction' not in ('asc','desc') or s->>'direction' is null then
   raise exception 'object sets: invalid_sort' using errcode='22023';end if;
  if not runtime.nexloop_object_set_property_open(p_tenant,tdef,s->>'property') then raise exception 'object sets: restricted_property' using errcode='22023';end if;
  v_sort:=v_sort||jsonb_build_array(jsonb_build_object('property',jsonb_build_object('object_type',v_ref,'property_name',s->>'property'),'direction',s->>'direction'));
 end loop;
 for p in select value from jsonb_array_elements(body->'projection') loop
  if jsonb_typeof(p) is distinct from 'string' or p#>>'{}'!~'^[a-z][a-z0-9_]{0,63}$' or not v_props ? (p#>>'{}') then
   raise exception 'object sets: invalid_projection' using errcode='22023';end if;
  v_projection:=v_projection||jsonb_build_array(jsonb_build_object('object_type',v_ref,'property_name',p#>>'{}'));
 end loop;
 if (select count(distinct value) from jsonb_array_elements(v_projection))<>jsonb_array_length(v_projection)
  or (select count(distinct value->'property') from jsonb_array_elements(v_sort))<>jsonb_array_length(v_sort)
  or (select count(distinct value) from jsonb_array_elements(v_filters))<>jsonb_array_length(v_filters) then
  raise exception 'object sets: duplicate' using errcode='22023';end if;
 select * into latest from control.nexloop_object_set_definitions where tenant_id=p_tenant and world=p_world and stable_name=v_name order by version desc limit 1 for update;
 v_compare:=jsonb_build_object('object_type',v_ref,'filters',v_filters,'sort',v_sort,'projection',v_projection);
 if found and jsonb_build_object('object_type',latest.definition->'object_type','filters',latest.definition->'filters','sort',latest.definition->'sort',
   'projection',latest.definition->'projection')=v_compare then
  return jsonb_build_object('stable_name',v_name,'version',latest.version,'changed',false);end if;
 v_version:=coalesce(latest.version,0)+1;
 v_def:=jsonb_build_object('tenant_id',p_tenant,'definition_type','object_set','stable_name',v_name,'version',v_version,'status','published',
  'required_scopes','[]'::jsonb,'required_markings','[]'::jsonb,'created_by',p_principal,
  'created_at',to_char(clock_timestamp() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'source_lineage','[]'::jsonb,
  'object_type',v_ref,'parameters','[]'::jsonb,'filters',v_filters,'link_traversals','[]'::jsonb,'sort',v_sort,'projection',v_projection,
  'aggregations','[]'::jsonb,'snapshots_allowed',false);
 insert into control.nexloop_object_set_definitions(tenant_id,world,stable_name,version,definition,definition_digest,reason,published_by,request_intent)
  values(p_tenant,p_world,v_name,v_version,v_def,encode(sha256(convert_to(v_def::text,'UTF8')),'hex'),body->>'reason',p_principal,p_intent);
 return jsonb_build_object('stable_name',v_name,'version',v_version,'changed',true);
end $$;

-- 3. Evaluation at read time ------------------------------------------------------------------------------------------------------
-- Candidates only: one page of ids (in the declared sort, then object id) and the candidate total. Property names come from a
-- published definition (validated at publication) and are quoted; values are bound parameters.
create function runtime.nexloop_object_set_candidates(p_tenant text,p_world text,p_name text,p_version integer,p_offset integer,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d control.nexloop_object_set_definitions;def jsonb;tdef jsonb;f jsonb;i integer:=0;v_where text:='';v_order text:='';v_prop text;vt text;
 v_type text;v_props jsonb;v_values jsonb:='[]'::jsonb;v_items jsonb;v_total bigint;v_cmp text;
begin
 if p_offset is null or p_offset not between 0 and 1000 or p_limit is null or p_limit not between 1 and 50 then
  raise exception 'object set read invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into d from control.nexloop_object_set_definitions where tenant_id=p_tenant and world=p_world and stable_name=p_name
  and (p_version is null or version=p_version) order by version desc limit 1;
 if not found then return null;end if;
 def:=d.definition;v_type:=def->'object_type'->>'stable_name';
 select definition into tdef from ontology.object_type_versions where tenant_id=p_tenant and type_name=v_type order by version desc limit 1;
 v_props:=coalesce((select jsonb_object_agg(e->>'property_name',e->>'value_type') from jsonb_array_elements(tdef->'properties') e),'{}'::jsonb);
 -- A restriction added after publication withholds the whole set (ADR-036 §2.5).
 if exists(select 1 from (select value->'property'->>'property_name' prop from jsonb_array_elements(def->'filters')
    union all select value->'property'->>'property_name' from jsonb_array_elements(def->'sort')) x
   where not v_props ? x.prop or not runtime.nexloop_object_set_property_open(p_tenant,tdef,x.prop)) then
  return jsonb_build_object('stable_name',d.stable_name,'version',d.version,'object_type',v_type,'status','withheld','items','[]'::jsonb,'total',null);
 end if;
 for f in select value from jsonb_array_elements(def->'filters') loop
  v_prop:=f->'property'->>'property_name';vt:=v_props->>v_prop;v_values:=v_values||jsonb_build_array(f->'value');
  v_cmp:=case f->>'operator' when 'gt' then '>' when 'gte' then '>=' when 'lt' then '<' when 'lte' then '<=' end;
  v_where:=v_where||case f->>'operator'
   when 'eq' then format(' and o.properties->%L=($1->%s)',v_prop,i)
   when 'ne' then format(' and (o.properties->%L) is distinct from ($1->%s)',v_prop,i)
   when 'in' then format(' and o.properties ? %L and ($1->%s) @> jsonb_build_array(o.properties->%L)',v_prop,i,v_prop)
   when 'not_in' then format(' and not (o.properties ? %L and ($1->%s) @> jsonb_build_array(o.properties->%L))',v_prop,i,v_prop)
   else case when vt in ('integer','number') then
     format(' and jsonb_typeof(o.properties->%L)=''number'' and (o.properties->>%L)::numeric %s (($1->%s)#>>''{}'')::numeric',v_prop,v_prop,v_cmp,i)
    when vt='datetime' then
     format(' and jsonb_typeof(o.properties->%L)=''string'' and (o.properties->>%L)~''^\d{4}-\d{2}-\d{2}'' and (o.properties->>%L)::timestamptz %s (($1->%s)#>>''{}'')::timestamptz',
      v_prop,v_prop,v_prop,v_cmp,i)
    else format(' and jsonb_typeof(o.properties->%L)=''string'' and (o.properties->>%L) collate "C" %s (($1->%s)#>>''{}'') collate "C"',v_prop,v_prop,v_cmp,i) end end;
  i:=i+1;
 end loop;
 select string_agg(format('(o.properties->%L) %s nulls last,',value->'property'->>'property_name',case value->>'direction' when 'desc' then 'desc' else 'asc' end),'' order by ord)
  into v_order from jsonb_array_elements(def->'sort') with ordinality x(value,ord);
 execute format('select coalesce((select jsonb_agg(x.object_id order by x.n) from (select o.object_id,row_number() over (order by %s o.object_id) n
     from ontology.objects o where o.tenant_id=$2 and o.world=$3 and o.type_name=$4 %s order by %s o.object_id offset $5 limit $6) x),''[]''::jsonb),
    (select count(*) from ontology.objects o where o.tenant_id=$2 and o.world=$3 and o.type_name=$4 %s)',
   coalesce(v_order,''),v_where,coalesce(v_order,''),v_where)
  into v_items,v_total using v_values,p_tenant,p_world,v_type,p_offset,p_limit;
 return jsonb_build_object('stable_name',d.stable_name,'version',d.version,'object_type',v_type,'status','ok','items',v_items,'total',v_total,
  'projection',(select coalesce(jsonb_agg(value->>'property_name' order by ord),'[]'::jsonb) from jsonb_array_elements(def->'projection') with ordinality x(value,ord)),
  'next_offset',case when p_offset+jsonb_array_length(v_items)<v_total and jsonb_array_length(v_items)=p_limit then p_offset+p_limit end);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_object_set_property_open(text,jsonb,text)','control.nexloop_governed_publish_object_set(text,text,text,text,text,jsonb)',
   'runtime.nexloop_object_set_candidates(text,text,text,integer,integer,integer)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('object_set.publish','publish_object_set','human','control.nexloop_governed_publish_object_set(text,text,text,text,text,jsonb)','NX-028');

-- 4. Workbench reads (observe; every other verb unchanged) ------------------------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_object_sets;
revoke all on function authz.nexloop_workbench_observe_read_before_object_sets(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is null or c->>'verb' not in ('object_sets','object_set_members') then
  return authz.nexloop_workbench_observe_read_before_object_sets(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for these verbs (as the earlier verbs).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 -- Every workbench member holding the read Action; each object is then READ-checked by the caller (ADR-036 §2.5).
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if c->>'verb'='object_sets' then
  if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
  return jsonb_build_object('object_sets',coalesce((select jsonb_agg(jsonb_build_object('stable_name',d.stable_name,'version',d.version,'versions',
     (select count(*) from control.nexloop_object_set_definitions x where x.tenant_id=d.tenant_id and x.world=d.world and x.stable_name=d.stable_name),
     'object_type',d.definition->'object_type'->>'stable_name','object_type_version',(d.definition->'object_type'->>'version')::integer,
     'filters',(select coalesce(jsonb_agg(jsonb_build_object('property',f->'property'->>'property_name','operator',f->>'operator','value',f->'value') order by ord),'[]'::jsonb)
       from jsonb_array_elements(d.definition->'filters') with ordinality x(f,ord)),
     'sort',(select coalesce(jsonb_agg(jsonb_build_object('property',s->'property'->>'property_name','direction',s->>'direction') order by ord),'[]'::jsonb)
       from jsonb_array_elements(d.definition->'sort') with ordinality x(s,ord)),
     'projection',(select coalesce(jsonb_agg(p->>'property_name' order by ord),'[]'::jsonb) from jsonb_array_elements(d.definition->'projection') with ordinality x(p,ord)),
     'definition_digest',d.definition_digest,'published_by',d.published_by,'published_at',d.published_at,'reason',d.reason) order by d.stable_name)
    from (select distinct on (stable_name) * from control.nexloop_object_set_definitions where tenant_id=v_tenant and world=p_world
      order by stable_name,version desc) d),'[]'::jsonb));
 end if;
 if exists(select 1 from jsonb_object_keys(c) key_name where key_name not in ('verb','stable_name','version','offset','limit'))
  or coalesce(c->>'stable_name','')!~'^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$'
  or (c ? 'version' and jsonb_typeof(c->'version') is distinct from 'number') or jsonb_typeof(c->'offset') is distinct from 'number'
  or jsonb_typeof(c->'limit') is distinct from 'number' then raise exception 'workbench observe invalid' using errcode='22023';end if;
 return runtime.nexloop_object_set_candidates(v_tenant,p_world,c->>'stable_name',(c->>'version')::integer,(c->>'offset')::integer,(c->>'limit')::integer);
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;

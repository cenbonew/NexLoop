-- NX-041 S3 (design D8, §2.5, §11 S3): synthetic data and structural generation inside a registered simulation world. The migration
-- number lives only in the file name and the catalog; nothing below repeats it.
-- 1. Two kinds (D8). synthetic: Consumers, objects and session events from parameters and distributions alone. structural: object
--    types and properties from the tenant's published Object Type definitions (ontology.object_type_versions) and per-type counts
--    of real objects, scaled; never a real value, identifier, message or contact datum (only count(*) touches real rows). A
--    desensitized snapshot is not part of this batch (NX-042 D2).
-- 2. Output lives in runtime.nexloop_sim_objects and runtime.nexloop_sim_session_events (world must be a simulation world, origin is
--    always synthetic) and in control.nexloop_consumer_personas (persona synthetic, ADR-034); nothing is written to ontology.objects,
--    conversations, messages or Claims, so no extraction, work feed or fact path ever sees it. Real and test are refused.
-- 3. Reproducible: every value is a function of (seed, kind, parameters, published definitions, real counts); the frozen input
--    record control.nexloop_sim_generations keeps generator version, seed, parameters, parameter digest, definitions digest, counts
--    and an output digest (the same input gives the same digest in any world).
-- 4. Every table has a world column, so the S2 whole deletion removes generated rows with the world.
-- 5. Governed owner Action sim_world.generate / generate_sim_world_data (human, owner only, called from the owner's real world).

-- 1. Tables ---------------------------------------------------------------------------------------------------------------------
create table control.nexloop_sim_generations (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(runtime.nexloop_world_kind(world)='simulation'),
 generation_id text not null check(generation_id~'^[0-9a-f]{64}$'),
 request_id text not null check(char_length(request_id) between 1 and 200),
 kind text not null check(kind in ('synthetic','structural')),
 generator_version text not null,
 seed text not null,
 params jsonb not null check(jsonb_typeof(params)='object'),
 params_digest text not null check(params_digest~'^[0-9a-f]{64}$'),
 definitions_digest text check(definitions_digest~'^[0-9a-f]{64}$'),
 counts jsonb not null check(jsonb_typeof(counts)='object'),
 output_digest text not null check(output_digest~'^[0-9a-f]{64}$'),
 created_by text not null,
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,generation_id),
 unique(tenant_id,world,request_id),
 unique(tenant_id,world,kind,seed));
create table runtime.nexloop_sim_objects (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(runtime.nexloop_world_kind(world)='simulation'),
 generation_id text not null,
 type_name text not null,
 object_id text not null check(object_id~'^[0-9a-f]{64}$'),
 properties jsonb not null check(jsonb_typeof(properties)='object'),
 origin text not null default 'synthetic' check(origin='synthetic'),
 primary key(tenant_id,world,type_name,object_id),
 foreign key(tenant_id,world,generation_id) references control.nexloop_sim_generations(tenant_id,world,generation_id) on delete cascade);
create table runtime.nexloop_sim_session_events (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(runtime.nexloop_world_kind(world)='simulation'),
 generation_id text not null,
 consumer_id text not null check(consumer_id~'^[0-9a-f]{64}$'),
 sequence integer not null check(sequence>0),
 event_kind text not null check(event_kind in ('inbound_message','outbound_message','visit')),
 offset_seconds integer not null check(offset_seconds>=0),
 attributes jsonb not null check(jsonb_typeof(attributes)='object'),
 origin text not null default 'synthetic' check(origin='synthetic'),
 primary key(tenant_id,world,consumer_id,sequence),
 foreign key(tenant_id,world,generation_id) references control.nexloop_sim_generations(tenant_id,world,generation_id) on delete cascade);
create index nexloop_sim_objects_generation on runtime.nexloop_sim_objects(tenant_id,world,generation_id);
create index nexloop_sim_session_events_generation on runtime.nexloop_sim_session_events(tenant_id,world,generation_id);
do $$declare t text;begin
 foreach t in array array['control.nexloop_sim_generations','runtime.nexloop_sim_objects','runtime.nexloop_sim_session_events'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics',t);
 end loop;
end $$;

-- 2. Deterministic draws: a uniform value in [0,1) from sha256(seed | label). -----------------------------------------------------
create function runtime.nexloop_sim_draw(p_seed text,p_label text) returns double precision
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select (('x'||substr(encode(sha256(convert_to(p_seed||'|'||p_label,'UTF8')),'hex'),1,13))::bit(52)::bigint)::double precision/4503599627370496.0
$$;
create function runtime.nexloop_sim_id(p_seed text,p_label text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to('nexloop:sim:'||p_seed||'|'||p_label,'UTF8')),'hex')
$$;
alter function runtime.nexloop_sim_draw(text,text) owner to nexloop_owner;
alter function runtime.nexloop_sim_id(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_draw(text,text) from public;
revoke all on function runtime.nexloop_sim_id(text,text) from public;

-- A property value drawn from its published value type only (never from a real value).
create function runtime.nexloop_sim_value(p_seed text,p_label text,p_type text) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case p_type
  when 'string' then to_jsonb('syn-'||substr(runtime.nexloop_sim_id(p_seed,p_label),1,12))
  when 'integer' then to_jsonb(floor(runtime.nexloop_sim_draw(p_seed,p_label)*1000)::integer)
  when 'number' then to_jsonb(round((runtime.nexloop_sim_draw(p_seed,p_label)*1000)::numeric,2))
  when 'boolean' then to_jsonb(runtime.nexloop_sim_draw(p_seed,p_label)<0.5)
  when 'json' then '{}'::jsonb
  else 'null'::jsonb end
$$;
alter function runtime.nexloop_sim_value(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_value(text,text,text) from public;

-- 3. The generator ------------------------------------------------------------------------------------------------------------
-- Consumers (persona synthetic) and their session events; the same code serves both kinds.
create function runtime.nexloop_sim_generate_consumers(p_tenant text,p_world text,p_gen text,p_seed text,p_n integer,p_min integer,p_max integer,
 p_segments jsonb,p_by text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare i integer;j integer;c text;v_seg text;v_events integer;v_total integer:=0;v_weight numeric;v_sum numeric;v_u double precision;v_acc numeric;
 v_kinds text[]:=array['inbound_message','outbound_message','visit'];
begin
 select sum(value::numeric) into v_sum from jsonb_each_text(p_segments);
 for i in 1..p_n loop
  c:=runtime.nexloop_sim_id(p_seed,'consumer:'||i);
  v_u:=runtime.nexloop_sim_draw(p_seed,'segment:'||i);v_acc:=0;v_seg:=null;
  for v_seg,v_weight in select key,value::numeric from jsonb_each_text(p_segments) order by key loop
   v_acc:=v_acc+v_weight/v_sum;exit when v_u<v_acc;
  end loop;
  insert into runtime.nexloop_sim_objects(tenant_id,world,generation_id,type_name,object_id,properties)
   values(p_tenant,p_world,p_gen,'Consumer',c,jsonb_build_object('segment',v_seg,'persona','synthetic',
    'joined_days_ago',floor(runtime.nexloop_sim_draw(p_seed,'joined:'||i)*365)::integer));
  insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by)
   values(p_tenant,p_world,c,'synthetic',1,p_by);
  v_events:=p_min+floor(runtime.nexloop_sim_draw(p_seed,'events:'||i)*(p_max-p_min+1))::integer;
  for j in 1..v_events loop
   insert into runtime.nexloop_sim_session_events(tenant_id,world,generation_id,consumer_id,sequence,event_kind,offset_seconds,attributes)
    values(p_tenant,p_world,p_gen,c,j,v_kinds[1+floor(runtime.nexloop_sim_draw(p_seed,'kind:'||i||':'||j)*3)::integer],
     floor(runtime.nexloop_sim_draw(p_seed,'at:'||i||':'||j)*2592000)::integer,
     jsonb_build_object('length',1+floor(runtime.nexloop_sim_draw(p_seed,'len:'||i||':'||j)*400)::integer));
  end loop;
  v_total:=v_total+v_events;
 end loop;
 return v_total;
end $$;
alter function runtime.nexloop_sim_generate_consumers(text,text,text,text,integer,integer,integer,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_generate_consumers(text,text,text,text,integer,integer,integer,jsonb,text) from public;

-- Digest of everything a generation produced, independent of the world it was written to.
create function runtime.nexloop_sim_output_digest(p_tenant text,p_world text,p_gen text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select encode(sha256(convert_to(coalesce((select string_agg(h,',' order by h) from (
   select md5(o.type_name||'|'||o.object_id||'|'||o.properties::text) h from runtime.nexloop_sim_objects o
    where o.tenant_id=p_tenant and o.world=p_world and o.generation_id=p_gen
   union all
   select md5(e.consumer_id||'|'||e.sequence||'|'||e.event_kind||'|'||e.offset_seconds||'|'||e.attributes::text) from runtime.nexloop_sim_session_events e
    where e.tenant_id=p_tenant and e.world=p_world and e.generation_id=p_gen) z),''),'UTF8')),'hex')
$$;
alter function runtime.nexloop_sim_output_digest(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_output_digest(text,text,text) from public;

create function control.nexloop_governed_generate_sim_world_data(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_target text:=body->>'world';v_kind text:=body->>'kind';v_seed text:=body->>'seed';w control.nexloop_sim_worlds;g control.nexloop_sim_generations;
 v_params jsonb;v_pdigest text;v_ddigest text;v_gen text;v_n integer;v_min integer;v_max integer;v_segments jsonb;v_max_objects integer;
 v_events integer;v_counts jsonb:='{}';v_real bigint;v_total bigint;v_scale numeric;r record;p record;i integer;v_props jsonb;v_k integer;v_objects integer:=0;
 v_version constant text:='nexloop-sim-generator/1';
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in
   ('operation','request_id','world','kind','seed','consumers','events_per_consumer','segments','max_objects'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or jsonb_typeof(body->'world') is distinct from 'string' or v_kind is null or v_kind not in ('synthetic','structural')
  or jsonb_typeof(body->'seed') is distinct from 'string' or v_seed!~'^[A-Za-z0-9_.-]{1,64}$'
  or jsonb_typeof(body->'consumers') is distinct from 'number' or (body->>'consumers')!~'^[0-9]{1,4}$' or (body->>'consumers')::integer not between 1 and 2000
  or jsonb_typeof(body->'events_per_consumer') is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body->'events_per_consumer') k where k not in ('min','max'))
  or jsonb_typeof(body->'events_per_consumer'->'min') is distinct from 'number' or jsonb_typeof(body->'events_per_consumer'->'max') is distinct from 'number'
  or (body->'events_per_consumer'->>'min')!~'^[0-9]{1,2}$' or (body->'events_per_consumer'->>'max')!~'^[0-9]{1,2}$'
  or (body->'events_per_consumer'->>'min')::integer>(body->'events_per_consumer'->>'max')::integer
  or (body->'events_per_consumer'->>'max')::integer>50 then
  raise exception 'sim generation: invalid' using errcode='22023';end if;
 if v_kind='synthetic' then
  if body ? 'max_objects' or jsonb_typeof(body->'segments') is distinct from 'object'
   or (select count(*) from jsonb_object_keys(body->'segments')) not between 1 and 10
   or exists(select 1 from jsonb_each(body->'segments') s where s.key!~'^[a-z][a-z0-9_]{0,31}$' or jsonb_typeof(s.value)<>'number' or (s.value::text)::numeric<=0) then
   raise exception 'sim generation: invalid' using errcode='22023';end if;
 else
  if body ? 'segments' or jsonb_typeof(body->'max_objects') is distinct from 'number' or (body->>'max_objects')!~'^[0-9]{1,5}$'
   or (body->>'max_objects')::integer>10000 then
   raise exception 'sim generation: invalid' using errcode='22023';end if;
 end if;
 -- D8: only a simulation world receives generated data; real, test and shadow never do.
 if runtime.nexloop_world_kind(v_target) is distinct from 'simulation' then raise exception 'sim generation: not_a_simulation_world' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'sim generation: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into w from control.nexloop_sim_worlds where world=v_target and tenant_id=p_tenant for share;
 if not found then raise exception 'sim generation: not_registered' using errcode='22023';end if;
 v_params:=body-'operation'-'request_id'-'world';
 v_pdigest:=encode(sha256(convert_to(v_params::text,'UTF8')),'hex');
 select * into g from control.nexloop_sim_generations where tenant_id=p_tenant and world=v_target and request_id=body->>'request_id';
 if found then
  if g.params_digest<>v_pdigest then raise exception 'sim generation: request_reused' using errcode='22023';end if;
  return jsonb_build_object('world',g.world,'generation_id',g.generation_id,'generator_version',g.generator_version,'params_digest',g.params_digest,
   'definitions_digest',g.definitions_digest,'counts',g.counts,'output_digest',g.output_digest,'created',false);
 end if;
 if w.status<>'active' then raise exception 'sim generation: world_not_active' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_sim_generations where tenant_id=p_tenant and world=v_target and kind=v_kind and seed=v_seed) then
  raise exception 'sim generation: seed_used' using errcode='22023';end if;
 v_gen:=runtime.nexloop_sim_id(v_target,'generation:'||(body->>'request_id'));
 v_n:=(body->>'consumers')::integer;v_min:=(body->'events_per_consumer'->>'min')::integer;v_max:=(body->'events_per_consumer'->>'max')::integer;
 -- The frozen input row first (objects and events reference it); digests and counts are completed below.
 insert into control.nexloop_sim_generations(tenant_id,world,generation_id,request_id,kind,generator_version,seed,params,params_digest,counts,output_digest,created_by)
  values(p_tenant,v_target,v_gen,body->>'request_id',v_kind,v_version,v_seed,v_params,v_pdigest,'{}',repeat('0',64),p_principal);
 if v_kind='synthetic' then
  v_segments:=body->'segments';
 else
  v_max_objects:=(body->>'max_objects')::integer;
  -- Structure only: the latest published definition of every type of this tenant, and count(*) of real objects per type.
  select encode(sha256(convert_to(coalesce(string_agg(x.type_name||'@'||x.version||':'||x.definition::text,',' order by x.type_name),''),'UTF8')),'hex')
   into v_ddigest from (select distinct on (type_name) type_name,version,definition from ontology.object_type_versions
    where tenant_id=p_tenant order by type_name,version desc) x;
  select count(*) into v_total from ontology.objects where tenant_id=p_tenant and world='real';
  v_scale:=case when v_total=0 then 0 else least(1,v_max_objects::numeric/v_total) end;
  for r in select distinct on (type_name) type_name,version,definition from ontology.object_type_versions
    where tenant_id=p_tenant and type_name<>'Consumer' order by type_name,version desc loop
   select count(*) into v_real from ontology.objects where tenant_id=p_tenant and world='real' and type_name=r.type_name;
   v_k:=round(v_real*v_scale)::integer;
   continue when v_k=0;
   for i in 1..v_k loop
    v_props:='{}';
    for p in select e->>'property_name' property_name,e->>'value_type' value_type from jsonb_array_elements(coalesce(r.definition->'properties','[]')) e loop
     v_props:=v_props||jsonb_build_object(p.property_name,runtime.nexloop_sim_value(v_seed,r.type_name||':'||i||':'||p.property_name,p.value_type));
    end loop;
    insert into runtime.nexloop_sim_objects(tenant_id,world,generation_id,type_name,object_id,properties)
     values(p_tenant,v_target,v_gen,r.type_name,runtime.nexloop_sim_id(v_seed,r.type_name||':'||i),v_props);
   end loop;
   v_objects:=v_objects+v_k;v_counts:=v_counts||jsonb_build_object(r.type_name,v_k);
  end loop;
  -- Consumers follow the real Consumer count (scaled, at most the requested number); segments are not structure and stay one.
  select count(*) into v_real from ontology.objects where tenant_id=p_tenant and world='real' and type_name='Consumer';
  v_n:=least(v_n,round(v_real*v_scale)::integer);
  v_segments:='{"structural":1}';
 end if;
 v_events:=case when v_n>0 then runtime.nexloop_sim_generate_consumers(p_tenant,v_target,v_gen,v_seed,v_n,v_min,v_max,v_segments,p_principal) else 0 end;
 v_counts:=v_counts||jsonb_build_object('Consumer',v_n,'session_events',v_events);
 update control.nexloop_sim_generations set definitions_digest=v_ddigest,counts=v_counts,output_digest=runtime.nexloop_sim_output_digest(p_tenant,v_target,v_gen)
  where tenant_id=p_tenant and world=v_target and generation_id=v_gen returning * into g;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('sim-generation:'||v_gen,'nexloop.sim_world.generated',p_tenant,p_principal,'sim_world',p_intent,
   jsonb_build_object('world',v_target,'generation_id',v_gen,'kind',v_kind,'generator_version',v_version,'params_digest',v_pdigest,'counts',v_counts),clock_timestamp());
 return jsonb_build_object('world',v_target,'generation_id',v_gen,'generator_version',v_version,'params_digest',v_pdigest,
  'definitions_digest',v_ddigest,'counts',v_counts,'output_digest',g.output_digest,'created',true);
end $$;
alter function control.nexloop_governed_generate_sim_world_data(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_generate_sim_world_data(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('sim_world.generate','generate_sim_world_data','human','control.nexloop_governed_generate_sim_world_data(text,text,text,text,text,jsonb)','NX-041');

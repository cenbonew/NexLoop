-- NX-054 stage publication (docs/implementation/NX-054-stage-publish.md; L6 review of 82a11dd2 R-1 to R-3): read-only functions
-- for the precheck and the baseline timings, executed by the existing read-only metrics login (nexloop_metrics: it executes
-- functions only, holds no table privilege and cannot write - the read-only identity is the server's, not a client setting), and
-- the catalog head the publication checks before a rollback or resume. Counts, durations and versions only: no id, property value
-- or business content leaves these functions. Temporary number; the dispatcher renumbers.
--
-- authz.nexloop_relation_precheck(tenant, specs)   per relation spec {name, source_type, property, target_type} and world: the
--                                                   shape of the property the derivation follows; supersedes, assessment name
--                                                   collisions, the published catalog
-- authz.nexloop_relation_erasure_timing(tenant, consumer)
--                                                   the objects a Consumer's erasure purges (counted now) and, once it ran, its
--                                                   state, durations and the time span of its tombstones per item class (R-3:
--                                                   run_file is asynchronous - reported apart; exact request_ref 'erasure:<id>')
-- authz.nexloop_relation_relay_p95(since, until)   the relay steps' p95 over the window (existing process samples)
-- control.nexloop_relation_catalog_head(tenant)    the latest published catalog version (R-1), for the configurator and metrics
-- Published migrations are not edited.

create function authz.nexloop_relation_precheck(p_tenant text,p_specs jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);s jsonb;fields jsonb:='[]'::jsonb;r record;names text[]:='{}';out jsonb;
begin
 if jsonb_typeof(p_specs) is distinct from 'array' or jsonb_array_length(p_specs) not between 1 and 200 then
  raise exception 'relation precheck invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 for s in select value from jsonb_array_elements(p_specs) loop
  if coalesce(s->>'name','')!~'^[a-z][a-z0-9_]{0,63}$' or coalesce(s->>'property','')!~'^[A-Za-z][A-Za-z0-9_]{0,63}$'
   or coalesce(s->>'source_type','')!~'^[A-Z][A-Za-z0-9_]{0,63}$' or coalesce(s->>'target_type','')!~'^[A-Z][A-Za-z0-9_]{0,63}$' then
   raise exception 'relation precheck invalid' using errcode='22023';end if;
  names:=names||(s->>'name');
  for r in select o.world,count(*) n,
     count(*) filter (where coalesce(o.properties->>(s->>'property'),'')<>'') with_value,
     count(*) filter (where o.properties->>(s->>'property')~'^[a-f0-9]{64}$') hex64,
     count(*) filter (where o.properties->>(s->>'property') like 'pseudonym:%') pseudonyms,
     count(*) filter (where exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=s->>'target_type'
       and t.object_id=o.properties->>(s->>'property'))) same_world,
     count(*) filter (where exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world<>o.world and t.type_name=s->>'target_type'
       and t.object_id=o.properties->>(s->>'property'))) other_world,
     count(distinct o.properties->>(s->>'property')) distinct_targets,
     count(o.properties->>(s->>'property'))-count(distinct o.properties->>(s->>'property')) shared
    from ontology.objects o where o.tenant_id=p_tenant and o.type_name=s->>'source_type' group by o.world loop
   fields:=fields||jsonb_build_object('relation',s->>'name','source_type',s->>'source_type','property',s->>'property','target_type',s->>'target_type',
     'world',r.world,'objects',r.n,'with_value',r.with_value,'hex64',r.hex64,'pseudonyms',r.pseudonyms,'target_same_world',r.same_world,
     'target_other_world',r.other_world,'dangling',r.hex64-r.same_world-r.other_world,'distinct_targets',r.distinct_targets,'sharing_a_target',r.shared);
  end loop;
 end loop;
 out:=jsonb_build_object('fields',fields,
  'supersedes',jsonb_build_object('superseded_more_than_once',(select count(*) from (select 1 from runtime.nexloop_commitments c where c.tenant_id=p_tenant
     and c.supersedes is not null group by c.world,c.supersedes having count(*)>1) x)),
  'assessments',jsonb_build_object(
     'first_batch_name_collisions',coalesce((select jsonb_agg(distinct o.properties->>'relation_name' order by o.properties->>'relation_name') from ontology.objects o
        where o.tenant_id=p_tenant and o.type_name='RelationshipAssessment' and o.properties->>'relation_name'=any(names)),'[]'::jsonb),
     'relation_names',(select count(distinct o.properties->>'relation_name') from ontology.objects o where o.tenant_id=p_tenant and o.type_name='RelationshipAssessment'),
     'version_zero',(select count(*) from ontology.objects o where o.tenant_id=p_tenant and o.type_name='RelationshipAssessment' and o.properties->>'relation_version'='0')),
  'catalog',jsonb_build_object(
     'versions',coalesce((select jsonb_agg(jsonb_build_object('version',c.version,'longest_ownership_chain',c.longest_ownership_chain,
        'relation_types',cardinality(c.relation_names)) order by c.version) from control.nexloop_relation_catalogs c where c.tenant_id=p_tenant),'[]'::jsonb),
     'latest_type_versions',coalesce((select jsonb_object_agg(x.relation_name,x.v) from (select relation_name,max(version) v from ontology.relation_type_versions
        where tenant_id=p_tenant group by 1) x),'{}'::jsonb),
     'derivation_map_rows',(select count(*) from control.nexloop_relation_derivations d where d.tenant_id=p_tenant),
     'deriving',(select count(*) from control.nexloop_relation_derivations d where d.tenant_id=p_tenant and d.source_property is not null),
     'relations',coalesce((select jsonb_object_agg(x.world,x.n) from (select world,count(*) n from ontology.relations where tenant_id=p_tenant group by 1) x),'{}'::jsonb),
     'derived_relations',(select count(*) from ontology.relations where tenant_id=p_tenant and metadata @> '{"derived":true}')));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return out;
end $$;

create function authz.nexloop_relation_erasure_timing(p_tenant text,p_consumer text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e control.nexloop_consumer_erasures;objects jsonb;spans jsonb;sync record;out jsonb;
begin
 if coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then raise exception 'relation erasure timing invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select coalesce(jsonb_object_agg(x.type_name,x.n),'{}'::jsonb) into objects from (
  select o.type_name,count(*) n from ontology.objects o where o.tenant_id=p_tenant and o.world='real' and p_consumer=any(runtime.nexloop_object_consumers(p_tenant,'real',o.type_name,o.object_id))
   and o.type_name<>'Consumer' group by 1) x;
 select * into e from control.nexloop_consumer_erasures where tenant_id=p_tenant and world='real' and consumer_id=p_consumer;
 if not found then
  out:=jsonb_build_object('objects_now',objects,'erasure',null);
 else
  -- R-3: per item class (exact request_ref, the index's columns); run_file comes later from the keeper's file deletion - apart.
  select coalesce(jsonb_object_agg(x.item_class,jsonb_build_object('tombstones',x.n,'seconds',x.seconds)),'{}'::jsonb) into spans from (
   select t.item_class,count(*) n,round(extract(epoch from max(t.executed_at)-min(t.executed_at))::numeric,3) seconds
    from runtime.nexloop_erasure_tombstones t where t.tenant_id=p_tenant and t.world='real' and t.reason='consumer_erasure' and t.request_ref='erasure:'||e.request_id
    group by 1) x;
  select min(t.executed_at) first_at,max(t.executed_at) last_at,count(*) n into sync from runtime.nexloop_erasure_tombstones t
   where t.tenant_id=p_tenant and t.world='real' and t.reason='consumer_erasure' and t.request_ref='erasure:'||e.request_id and t.item_class<>'run_file';
  out:=jsonb_build_object('objects_now',objects,'erasure',jsonb_build_object('state',e.state,
    'request_to_completion_seconds',case when e.completed_at is null then null else round(extract(epoch from e.completed_at-e.created_at)::numeric,3) end,
    'sync_purge_seconds',case when sync.first_at is null then null else round(extract(epoch from sync.last_at-sync.first_at)::numeric,3) end,
    'sync_tombstones',sync.n,'by_item_class',spans,'counts',e.counts));
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return out;
end $$;

create function authz.nexloop_relation_relay_p95(p_since timestamptz,p_until timestamptz) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_object_agg(x.service,jsonb_build_object('samples',x.samples,'calls',x.calls,'p95_ms_median',x.p50,'p95_ms_max',x.pmax)),'{}'::jsonb) from (
  select s.service,count(*) samples,sum((s.sample->>'calls')::numeric)::bigint calls,
   percentile_cont(0.5) within group (order by (s.sample->>'p95_ms')::numeric) filter (where (s.sample->>'calls')::numeric>0) p50,
   max((s.sample->>'p95_ms')::numeric) filter (where (s.sample->>'calls')::numeric>0) pmax
  from runtime.nexloop_process_samples s where s.kind='relay_step' and s.recorded_at>=p_since and s.recorded_at<p_until and s.sample ? 'p95_ms' group by 1) x
$$;

create function control.nexloop_relation_catalog_head(p_tenant text) returns integer
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v integer;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select max(version) into v from control.nexloop_relation_catalogs where tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;

do $$declare f text;begin
 foreach f in array array['authz.nexloop_relation_precheck(text,jsonb)','authz.nexloop_relation_erasure_timing(text,text)',
   'authz.nexloop_relation_relay_p95(timestamptz,timestamptz)','control.nexloop_relation_catalog_head(text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
  execute 'grant execute on function '||f||' to nexloop_metrics';
 end loop;
end $$;
grant execute on function control.nexloop_relation_catalog_head(text) to nexloop_configurator;

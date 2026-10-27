-- NX-054 §8 step 2 (stage precheck 2026-10-27): 44 Commitment.made_to values "erasure:workbench-erase-synthetic-<64 hex>" blocked
-- publication as "neither 64 hex nor an erasure pseudonym". They are the erasure's own marker: the commitments phase of
-- runtime.nexloop_erasure_step writes made_to = 'erasure:' || request_id (0138 on, still in 0358), while the other erased references
-- carry 'pseudonym:<32 hex>' (0138, 0305-0307). One judgement for both, runtime.nexloop_relation_value_erased(value): an erasure
-- pseudonym ('pseudonym:...') or an erasure request marker ('erasure:' and an erasure request id, the 0132 request_id shape). The
-- derivation (ontology.nexloop_relation_derive_one, 0344's body) and the precheck (authz.nexloop_relation_precheck, 0336's body) use
-- it - one change each, registered in the copy guard: such a value derives no relation and is never counted dangling, and the
-- precheck counts it among the pseudonyms. The backfill (0349) already skips any value that is not 64 hex. Temporary number; the
-- dispatcher renumbers.

create function runtime.nexloop_relation_value_erased(p_value text) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(p_value like 'pseudonym:%' or p_value~'^erasure:[A-Za-z0-9._:-]{1,190}$',false)
$$;
alter function runtime.nexloop_relation_value_erased(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_relation_value_erased(text) from public;

create or replace function ontology.nexloop_relation_derive_one(o ontology.objects,p_relation text,p_marker jsonb,p_drop_stale boolean)
 returns text language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p record;v text;v_id text;v_ok boolean;v_result text;
begin
 if not coalesce(p_marker @> '{"derived":true}'::jsonb,false) then
  raise exception 'relation derivation marker' using errcode='22023';end if;
 select m.relation_name,m.version,m.source_property,m.target_type,m.cardinality into p from control.nexloop_relation_derivations m
  where m.tenant_id=o.tenant_id and m.relation_name=p_relation and m.source_type=o.type_name;
 if not found then return 'unknown';end if;
 v:=case when p.source_property is null then null else o.properties->>p.source_property end;
 v_ok:=v~'^[a-f0-9]{64}$' and exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=p.target_type and t.object_id=v);
 if v_ok then
  v_id:=encode(sha256(convert_to(jsonb_build_array(o.tenant_id,o.world,p.relation_name,o.type_name,o.object_id,p.target_type,v)::text,'UTF8')),'hex');
  if p.cardinality in ('one_to_one','one_to_many') then
   perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(o.tenant_id,o.world,p.relation_name)::text,0));
   if exists(select 1 from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.relation_name=p.relation_name
      and r.target_type=p.target_type and r.target_object_id=v and r.source_object_id<>o.object_id) then
    raise exception 'relation cardinality conflict' using errcode='42501',detail=p.relation_name;end if;
  end if;
  insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,metadata,created_at)
   values(v_id,o.tenant_id,o.world,p.relation_name,p.version,o.type_name,o.object_id,p.target_type,v,p_marker,clock_timestamp())
   on conflict do nothing;
  v_result:=case when found then 'created' else 'existing' end;
 elsif v is not null and v<>'' and not runtime.nexloop_relation_value_erased(v) then
  insert into runtime.nexloop_relation_derivation_dangling(tenant_id,world,relation_name,dangling,last_source_type)
   values(o.tenant_id,o.world,p.relation_name,1,o.type_name)
   on conflict(tenant_id,world,relation_name) do update set dangling=runtime.nexloop_relation_derivation_dangling.dangling+1,
    last_source_type=excluded.last_source_type,last_seen_at=clock_timestamp();
  v_result:='dangling';
 else
  v_result:='none';
 end if;
 -- P-2: an inserted object has no earlier relation. On update, the derived relations of this name that no longer match go;
 -- a relation written through the governed link entry (not marked derived) stays (I-2).
 if p_drop_stale then
  delete from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world and r.source_type=o.type_name and r.source_object_id=o.object_id
   and r.relation_name=p.relation_name and r.metadata @> '{"derived":true}' and (not coalesce(v_ok,false) or r.target_object_id is distinct from v);
 end if;
 return v_result;
end $$;

create or replace function authz.nexloop_relation_precheck(p_tenant text,p_specs jsonb) returns jsonb
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
     count(*) filter (where runtime.nexloop_relation_value_erased(o.properties->>(s->>'property'))) pseudonyms,
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

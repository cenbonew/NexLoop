-- NX-054 P4a (docs/design/relation-read-p4.md v2; L6 reviews of 2845724b..ad9812e2): the erasing read guard and the workbench's
-- object_fields resolve an object's Consumers as relations united with the property rule (runtime.nexloop_object_consumers_read):
-- never fewer than before (the property rule, 0315), more where only a relation links the object (a link Action, a chain the
-- property rule does not know). Both readers are 0315's bodies with that one call changed (registered in the copy guard). The
-- backfill (0349) and the purge keep their own resolution (purge: P4c, 0361). The fallback is measured offline, never counted
-- in the read path: authz.nexloop_relation_read_gaps(tenant), STABLE, the nexloop_metrics login only (session_user checked; the
-- caller sets its transaction read only), counts per world and type of the objects whose property-rule Consumers the relations
-- miss (gaps; gaps_erasing apart, as P3 verify leaves erasing Consumers out) and the reverse (relation_only). Counts only.
-- Ships with P4c on real (design §3 R4). Temporary number; the dispatcher renumbers.

create function runtime.nexloop_object_consumers_read(p_tenant text,p_world text,p_type text,p_object text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce(array_agg(distinct x order by x),'{}') from unnest(runtime.nexloop_object_consumers(p_tenant,p_world,p_type,p_object)
  ||runtime.nexloop_relation_object_consumers(p_tenant,p_world,p_type,p_object)) x
$$;
alter function runtime.nexloop_object_consumers_read(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_object_consumers_read(text,text,text,text) from public;

create or replace function runtime.nexloop_assert_not_erasing(p_world text,p_claims jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare prior text:=current_setting('eios.tenant_id',true);t text:=p_claims->>'tenant_id';
 r text:=coalesce(p_claims->>'resource_id',p_claims->>'target_resource','');v text;c text;hit boolean:=false;
begin
 if coalesce(t,'')='' then return;end if;
 if prior is distinct from t then perform set_config('eios.tenant_id',t,true);end if;
 -- (a) No erasing Consumer in this tenant and world: nothing to refuse.
 if exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.state<>'pending_confirmation') then
  -- Erasing indirect read: the one resolution shared with object_fields (runtime.nexloop_object_consumers).
  v:=substring(r from '^eios:(?:object|property):([A-Z][A-Za-z0-9_]{0,63})/');
  if v is null and p_claims->>'type_name' is not null then v:=p_claims->>'type_name';end if;
  c:=coalesce(substring(r from '^eios:(?:object|property):[A-Z][A-Za-z0-9_]{0,63}/([A-Za-z0-9][A-Za-z0-9._:-]{0,159})'),p_claims->>'object_id');
  if v is not null and c is not null then
   hit:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.state<>'pending_confirmation'
    and e.consumer_id=any(runtime.nexloop_object_consumers_read(t,p_world,v,c)));
  end if;
 end if;
 if prior is distinct from t then perform set_config('eios.tenant_id',coalesce(prior,''),true);end if;
 if hit then raise exception 'consumer is being erased' using errcode='42501';end if;
end $$;

create or replace function authz.nexloop_workbench_object_fields(p_tenant text,p_world text,p_role text,p_type text,p_object text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare o ontology.objects;def jsonb;restriction jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=p_type and object_id=p_object;
 if not found then return null;end if;
 if not authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,'eios:object:'||p_type||'/'||p_object)
  -- Erasing indirect read: the Consumers the object belongs to, the resolution the read guard uses (runtime.nexloop_object_consumers).
  or exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=p_world and e.state<>'pending_confirmation'
   and e.consumer_id=any(runtime.nexloop_object_consumers_read(p_tenant,p_world,p_type,p_object))) then
  return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',false,'fields','[]'::jsonb);end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=o.schema_version;
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[p_type];
 return jsonb_build_object('type_name',p_type,'object_id',p_object,'readable',true,'schema_version',o.schema_version,'revision',o.nexloop_revision,
  'fields',coalesce((select jsonb_agg(jsonb_build_object('name',x.name,'present',x.present,'restricted',x.restricted,'erasing',x.erasing,
      'readable',not x.restricted and not x.erasing and authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,
        'eios:property:'||p_type||'/'||p_object||'/'||x.name)) order by x.ord)
    from (select p.value->>'property_name' as name,p.ordinality ord,
       (o.properties ? (p.value->>'property_name') and o.properties->(p.value->>'property_name')<>'null'::jsonb) present,
       coalesce(restriction->'restricted_groups' ? ontology.nexloop_property_group_of(def,p.value->>'property_name'),false) restricted,
       -- NX-029: a value that names a Consumer being erased (an id, or an array of ids) is never shown.
       exists(select 1 from jsonb_array_elements(case jsonb_typeof(o.properties->(p.value->>'property_name')) when 'array' then o.properties->(p.value->>'property_name')
          when 'string' then jsonb_build_array(o.properties->(p.value->>'property_name')) else '[]'::jsonb end) e
         where jsonb_typeof(e)='string' and e#>>'{}'~'^[a-f0-9]{64}$' and runtime.nexloop_consumer_erasing(p_tenant,p_world,e#>>'{}')) erasing
      from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) with ordinality p
      where coalesce(p.value->>'property_name','')~'^[A-Za-z][A-Za-z0-9_]{0,63}$') x),'[]'::jsonb));
end $$;

-- The property-rule Consumers the gap check compares: 0315's rule, but for a RelationshipAssessment only its evidence Message's
-- Consumer (source_id / target_id have no relation type in the first batch, ADR-038 §3.6: they stay on the property rule after P5).
create function runtime.nexloop_relation_gap_property_consumers(p_tenant text,p_world text,p_type text,p_object text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v text;
begin
 if p_type<>'RelationshipAssessment' then return runtime.nexloop_object_consumers(p_tenant,p_world,p_type,p_object);end if;
 select o.properties->>'evidence_message_id' into v from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name=p_type and o.object_id=p_object;
 return coalesce((select array[cv.consumer_id] from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
   on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
   where m.tenant_id=p_tenant and m.world=p_world and m.message_id=v and cv.consumer_id~'^[a-f0-9]{64}$' limit 1),'{}');
end $$;
alter function runtime.nexloop_relation_gap_property_consumers(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_relation_gap_property_consumers(text,text,text,text) from public;

create function authz.nexloop_relation_read_gaps(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);out jsonb;
begin
 if session_user<>'nexloop_metrics' then raise exception 'relation read gaps: the metrics login only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 with types as (
   select distinct m.source_type from control.nexloop_relation_derivations m join control.nexloop_relation_type_profiles f
    on f.tenant_id=m.tenant_id and f.relation_name=m.relation_name and f.version=m.version
   where m.tenant_id=p_tenant and f.consumer_ownership is not null),
  objs as (
   select o.world,o.type_name,runtime.nexloop_relation_gap_property_consumers(o.tenant_id,o.world,o.type_name,o.object_id) a,
    runtime.nexloop_relation_object_consumers(o.tenant_id,o.world,o.type_name,o.object_id) r
   from ontology.objects o where o.tenant_id=p_tenant and o.type_name in (select source_type from types)),
  marked as (
   select x.*,exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=x.world
     and e.state<>'pending_confirmation' and e.consumer_id=any(x.a||x.r)) erasing,
    exists(select 1 from unnest(x.a) c where not c=any(x.r)) gap,exists(select 1 from unnest(x.r) c where not c=any(x.a)) extra
   from objs x)
 select coalesce(jsonb_agg(jsonb_build_object('world',world,'type_name',type_name,'objects',n,'gaps',g,'gaps_erasing',ge,'relation_only',ro)
   order by world,type_name),'[]'::jsonb) into out
 from (select world,type_name,count(*) n,count(*) filter (where gap and not erasing) g,count(*) filter (where gap and erasing) ge,
   count(*) filter (where extra) ro from marked group by world,type_name) s;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('tenant_id',p_tenant,'rows',out,'gaps',(select coalesce(sum((v->>'gaps')::bigint),0) from jsonb_array_elements(out) v),
  'gaps_erasing',(select coalesce(sum((v->>'gaps_erasing')::bigint),0) from jsonb_array_elements(out) v),
  'relation_only',(select coalesce(sum((v->>'relation_only')::bigint),0) from jsonb_array_elements(out) v));
end $$;
alter function authz.nexloop_relation_read_gaps(text) owner to nexloop_owner;
revoke all on function authz.nexloop_relation_read_gaps(text) from public;
grant execute on function authz.nexloop_relation_read_gaps(text) to nexloop_metrics;

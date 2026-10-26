-- NX-054 P4a follow-up (stage drill 2026-10-26): the gap check reported {"gaps":0,"rows":[]} for a tenant whose relation catalog is
-- not published - it covers only the source types of published ownership relation types, so with none it saw nothing and said
-- nothing. authz.nexloop_relation_read_gaps (0357's body, one addition, registered in the copy guard) now also returns
-- ownership_types, the number of source types it covers; scripts/acceptance/relation_read_gaps.py fails (exit 2) when it is 0.
-- Temporary number; the dispatcher renumbers.

create or replace function authz.nexloop_relation_read_gaps(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);out jsonb;v_types integer;
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
   from ontology.objects o where o.tenant_id=p_tenant and o.type_name in (select source_type from types)
    -- A simulation world being purged is left out, as P3 verify leaves it (L6 I-1).
    and not exists(select 1 from control.nexloop_sim_worlds w where w.tenant_id=p_tenant and w.world=o.world and w.status in ('deleting','deleted'))),
  marked as (
   select x.*,exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=x.world
     and e.state<>'pending_confirmation' and e.consumer_id=any(x.a||x.r)) erasing,
    -- A property naming no Consumer object of the world (deleted, or never there) is dangling, not a gap: no relation can exist.
    exists(select 1 from unnest(x.a) c where not c=any(x.r) and exists(select 1 from ontology.objects k where k.tenant_id=p_tenant and k.world=x.world
      and k.type_name='Consumer' and k.object_id=c)) gap,
    exists(select 1 from unnest(x.a) c where not c=any(x.r) and not exists(select 1 from ontology.objects k where k.tenant_id=p_tenant and k.world=x.world
      and k.type_name='Consumer' and k.object_id=c)) dangling,
    exists(select 1 from unnest(x.r) c where not c=any(x.a)) extra
   from objs x)
 select coalesce(jsonb_agg(jsonb_build_object('world',world,'type_name',type_name,'objects',n,'gaps',g,'gaps_erasing',ge,'dangling',d,'relation_only',ro)
   order by world,type_name),'[]'::jsonb) into out
 from (select world,type_name,count(*) n,count(*) filter (where gap and not erasing) g,count(*) filter (where gap and erasing) ge,
   count(*) filter (where dangling and not gap) d,count(*) filter (where extra) ro from marked group by world,type_name) s;
 select count(distinct m.source_type) into v_types from control.nexloop_relation_derivations m join control.nexloop_relation_type_profiles f
  on f.tenant_id=m.tenant_id and f.relation_name=m.relation_name and f.version=m.version where m.tenant_id=p_tenant and f.consumer_ownership is not null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('tenant_id',p_tenant,'rows',out,
  'ownership_types',v_types,'gaps',(select coalesce(sum((v->>'gaps')::bigint),0) from jsonb_array_elements(out) v),
  'gaps_erasing',(select coalesce(sum((v->>'gaps_erasing')::bigint),0) from jsonb_array_elements(out) v),
  'dangling',(select coalesce(sum((v->>'dangling')::bigint),0) from jsonb_array_elements(out) v),
  'relation_only',(select coalesce(sum((v->>'relation_only')::bigint),0) from jsonb_array_elements(out) v));
end $$;

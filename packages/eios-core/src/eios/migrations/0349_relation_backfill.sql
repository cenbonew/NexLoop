-- NX-054 P3 slice 1 (docs/design/relation-backfill-p3.md, v4 with the V-1 to V-4 changes; L6 reviews of v1-v4): the backfill engine
-- of the relations of stock objects, as owner functions granted to no role. The signed Action entry nexloop.relation.backfill:1
-- (slice 2) calls them with the tenant of its authenticated context; nothing here takes a tenant from a client.
--
-- Tables (owner only, tenant policy): runs (one active run per tenant), cursors per (source type, world), counts per (relation,
-- world), the relations a run created, the objects a batch skipped (persisted in the batch's transaction, design C-2).
-- control.nexloop_relation_backfill_preflight(tenant)            blocking items (design §6) and a report; no write
-- control.nexloop_relation_backfill_start(tenant, run, digest, by) a run and its cursors; refused on any blocking item
-- control.nexloop_relation_backfill_batch(tenant, run, type, world, after, batch)
--                                                                 one batch of design §3.2: the caller's transaction runs it under
--                                                                 READ COMMITTED with statement_timeout 1s (set by the caller; a
--                                                                 function cannot change its own statement's timeout)
-- control.nexloop_relation_backfill_rescan(tenant, run, limit)    the skipped objects (design §4.1)
-- control.nexloop_relation_backfill_verify(tenant)                A, B, dangling, extra per (relation, world) (design §5); no write
-- control.nexloop_relation_backfill_revert_batch(tenant, run, limit), _abort(tenant, run, reason), _status(tenant, run)
-- L6 review of f2021ae9: batch, rescan and complete act on a running run only (reverting is revert_batch's alone, C-1); batch,
-- rescan and revert_batch refuse a statement_timeout outside (0, 1000 ms] (I-1: the signed entry sets it, this checks it);
-- aborted is final, a new run starts over and finds what exists (I-3, dispatcher); a skipped object's relations already counted
-- are not counted again by the rescan (S-1).
-- The backfill's run is the column backfill_run, not run_id: a run_id column without a world names an Agent Run, which the
-- simulation world guard (test_nx041_sim_worlds_pg) requires to be covered; a backfill run belongs to a tenant, not a world.
-- A created relation carries {"derived":true,"backfill":"<run>"}: the P2 triggers keep maintaining it (design §4.2). Derivation is
-- ontology.nexloop_relation_derive_one (P2.1) on the row as locked, with the tenant set (L6 D-2). Temporary number; the dispatcher
-- renumbers.

create table control.nexloop_relation_backfill_runs (
 tenant_id text not null references control.nexloop_tenants(tenant_id),backfill_run text not null check(backfill_run~'^[A-Za-z0-9._-]{1,80}$'),
 derivations_digest text not null check(derivations_digest~'^[0-9a-f]{64}$'),
 state text not null check(state in ('running','aborted','completed','reverting','reverted')),reason text,
 started_by text not null check(char_length(started_by) between 1 and 200),
 started_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,backfill_run));
create unique index nexloop_relation_backfill_one_active on control.nexloop_relation_backfill_runs(tenant_id) where state in ('running','reverting');
create table control.nexloop_relation_backfill_cursors (
 tenant_id text not null,backfill_run text not null,source_type text not null,world text not null,ordinal integer not null,
 last_object_id text not null default '',done boolean not null default false,batches integer not null default 0,
 last_batch_ms integer,last_batch_size integer,
 primary key(tenant_id,backfill_run,source_type,world),foreign key(tenant_id,backfill_run) references control.nexloop_relation_backfill_runs(tenant_id,backfill_run));
create table control.nexloop_relation_backfill_counts (
 tenant_id text not null,backfill_run text not null,relation_name text not null,world text not null,
 scanned bigint not null default 0,created bigint not null default 0,existing bigint not null default 0,dangling bigint not null default 0,
 skipped_erasure bigint not null default 0,skipped_vanished bigint not null default 0,skipped_world_purge bigint not null default 0,
 primary key(tenant_id,backfill_run,relation_name,world),foreign key(tenant_id,backfill_run) references control.nexloop_relation_backfill_runs(tenant_id,backfill_run));
create table control.nexloop_relation_backfill_created (
 tenant_id text not null,backfill_run text not null,relation_id text not null,
 primary key(tenant_id,backfill_run,relation_id),foreign key(tenant_id,backfill_run) references control.nexloop_relation_backfill_runs(tenant_id,backfill_run));
create table control.nexloop_relation_backfill_skipped (
 tenant_id text not null,backfill_run text not null,source_type text not null,world text not null,object_id text not null,
 reason text not null check(reason in ('source_locked','target_locked')),attempts integer not null default 0,
 counted text[] not null default '{}',
 first_seen_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,backfill_run,source_type,world,object_id),foreign key(tenant_id,backfill_run) references control.nexloop_relation_backfill_runs(tenant_id,backfill_run));
do $$declare t text;begin
 foreach t in array array['control.nexloop_relation_backfill_runs','control.nexloop_relation_backfill_cursors','control.nexloop_relation_backfill_counts',
   'control.nexloop_relation_backfill_created','control.nexloop_relation_backfill_skipped'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
 end loop;
end $$;

-- The derivation map a run starts from: a new published version changes it (design I-2).
create function control.nexloop_relation_backfill_digest(p_tenant text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select encode(sha256(convert_to(coalesce(string_agg(jsonb_build_array(m.relation_name,m.version,m.source_type,m.target_type,m.source_property,m.cardinality)::text,',' order by m.relation_name),''),'UTF8')),'hex')
  into v from control.nexloop_relation_derivations m where m.tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
-- The erasure states whose Consumers a batch skips (design §4.3, I-6): every state but pending_confirmation, named.
create function control.nexloop_relation_backfill_erasing_states() returns text[] language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array['blocking','settling','waiting_unknown_effects','purging','completed','completed_with_holds']
$$;

create function control.nexloop_relation_backfill_preflight(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);b jsonb:='[]'::jsonb;rep jsonb:='[]'::jsonb;m record;n bigint;known text[];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from control.nexloop_relation_derivations where tenant_id=p_tenant and source_property is not null) then
  b:=b||jsonb_build_object('item','catalog_unpublished');end if;
 if (select count(*) from pg_trigger t join pg_proc p on p.oid=t.tgfoid where t.tgrelid='ontology.objects'::regclass
     and t.tgname in ('nexloop_relation_derive','nexloop_relation_derive_update') and p.proname='nexloop_relation_derive_each' and t.tgenabled<>'D')<>2
  or to_regprocedure('ontology.nexloop_relation_derive_one(ontology.objects,text,jsonb,boolean)') is null then
  b:=b||jsonb_build_object('item','derivation_missing_or_disabled');end if;
 if exists(select 1 from control.nexloop_relation_backfill_runs where tenant_id=p_tenant and state in ('running','reverting')) then
  b:=b||jsonb_build_object('item','active_run');end if;
 select array_agg(x order by x) into known from (select (regexp_matches(pg_get_constraintdef(c.oid),'''([a-z_]+)''','g'))[1] x
   from pg_constraint c where c.conrelid='control.nexloop_consumer_erasures'::regclass and c.contype='c' and pg_get_constraintdef(c.oid) like '%state%pending_confirmation%') s;
 if known is distinct from (select array_agg(x order by x) from unnest(control.nexloop_relation_backfill_erasing_states()||'pending_confirmation'::text) x) then
  b:=b||jsonb_build_object('item','erasure_states_changed');end if;
 select count(*) into n from ontology.objects o where o.tenant_id=p_tenant and o.type_name='RelationshipAssessment'
  and o.properties->>'relation_name' in (select relation_name from control.nexloop_relation_derivations where tenant_id=p_tenant);
 if n>0 then b:=b||jsonb_build_object('item','assessment_name_collision','count',n);end if;
 for m in select * from control.nexloop_relation_derivations where tenant_id=p_tenant and source_property is not null order by relation_name loop
  for n in select count(*) from ontology.objects o join ontology.objects t on t.tenant_id=o.tenant_id and t.type_name=m.target_type
     and t.object_id=o.properties->>m.source_property and t.world<>o.world
     where o.tenant_id=p_tenant and o.type_name=m.source_type loop
   if n>0 then b:=b||jsonb_build_object('item','target_in_other_world','relation',m.relation_name,'count',n);end if;
  end loop;
  if m.cardinality in ('one_to_one','one_to_many') then
   select coalesce(sum(x.n),0) into n from (select count(*)-count(distinct o.properties->>m.source_property) n from ontology.objects o
    where o.tenant_id=p_tenant and o.type_name=m.source_type and o.properties->>m.source_property~'^[a-f0-9]{64}$' group by o.world) x;
   if n>0 then b:=b||jsonb_build_object('item','sharing_a_target','relation',m.relation_name,'count',n);end if;
  end if;
  rep:=rep||(select coalesce(jsonb_agg(jsonb_build_object('relation',m.relation_name,'world',x.world,'with_value',x.with_value,'hex',x.hex,
     'target_exists',x.target_exists) order by x.world),'[]'::jsonb) from (
   select o.world,count(*) filter (where coalesce(o.properties->>m.source_property,'')<>'') with_value,
    count(*) filter (where o.properties->>m.source_property~'^[a-f0-9]{64}$') hex,
    count(*) filter (where exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=m.target_type
      and t.object_id=o.properties->>m.source_property)) target_exists
   from ontology.objects o where o.tenant_id=p_tenant and o.type_name=m.source_type group by o.world) x);
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('digest',control.nexloop_relation_backfill_digest(p_tenant),'blocking',b,'report',rep);
end $$;

create function control.nexloop_relation_backfill_start(p_tenant text,p_run text,p_digest text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);pre jsonb;r control.nexloop_relation_backfill_runs;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_relation_backfill_runs where tenant_id=p_tenant and backfill_run=p_run;
 if found then  -- a replayed start: the same run
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('run_id',p_run,'state',r.state,'replayed',true);
 end if;
 pre:=control.nexloop_relation_backfill_preflight(p_tenant);
 if jsonb_array_length(pre->'blocking')>0 then raise exception 'relation backfill preflight blocking' using errcode='42501',detail=(pre->'blocking')::text;end if;
 if p_digest is distinct from pre->>'digest' then raise exception 'relation backfill catalog changed' using errcode='40001';end if;
 insert into control.nexloop_relation_backfill_runs(tenant_id,backfill_run,derivations_digest,state,started_by) values(p_tenant,p_run,p_digest,'running',p_by);
 -- Cursors (design §1): the source types with ownership relations first (direct, then transitive), then the others; real, test,
 -- shadow, simulation worlds.
 insert into control.nexloop_relation_backfill_cursors(tenant_id,backfill_run,source_type,world,ordinal)
 select p_tenant,p_run,s.source_type,w.world,row_number() over (order by s.rank,s.source_type,w.rank,w.world)
 from (select m.source_type,min(case f.consumer_ownership when 'target' then 0 when 'transitive' then 1 else 2 end) rank
   from control.nexloop_relation_derivations m left join control.nexloop_relation_type_profiles f
    on f.tenant_id=m.tenant_id and f.relation_name=m.relation_name and f.version=m.version
   where m.tenant_id=p_tenant and m.source_property is not null group by m.source_type) s
 join lateral (select distinct o.world,case runtime.nexloop_world_kind(o.world) when 'real' then 0 when 'test' then 1 when 'shadow' then 2 else 3 end rank
   from ontology.objects o where o.tenant_id=p_tenant and o.type_name=s.source_type) w on true;
 pre:=jsonb_build_object('run_id',p_run,'state','running','cursors',(select count(*) from control.nexloop_relation_backfill_cursors where tenant_id=p_tenant and backfill_run=p_run));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return pre;
end $$;

-- I-1: the R-1 bound is the caller's statement_timeout (a function cannot re-time its own statement); refuse anything else.
create function control.nexloop_relation_backfill_assert_timeout() returns void
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v integer:=(select setting::integer from pg_settings where name='statement_timeout');
begin
 if v<=0 or v>1000 then raise exception 'relation backfill needs statement_timeout in (0, 1000 ms]' using errcode='22023',detail=v::text;end if;
end $$;

-- Common checks of batch, rescan and revert (design §3.2 step 2, N-5): the run's state, the derivation map (not for revert), a
-- simulation world being purged. Returns null to go on, else the answer. Aborting on a changed map is written and returned (the
-- caller commits it).
create function control.nexloop_relation_backfill_gate(p_tenant text,p_run text,p_check_map boolean) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_relation_backfill_runs;
begin
 select * into r from control.nexloop_relation_backfill_runs where tenant_id=p_tenant and backfill_run=p_run for update;
 if not found then raise exception 'relation backfill run unknown' using errcode='42501';end if;
 if r.state<>'running' then return jsonb_build_object('stopped',r.state,'reason',r.reason);end if;  -- C-1: reverting is revert_batch's
 -- S-5: the map rows under FOR SHARE until this batch commits: a publication waits (at most the batch's second) instead of
 -- changing the map under the batch.
 perform 1 from control.nexloop_relation_derivations m where m.tenant_id=p_tenant for share;
 if p_check_map and control.nexloop_relation_backfill_digest(p_tenant) is distinct from r.derivations_digest then
  update control.nexloop_relation_backfill_runs set state='aborted',reason='catalog_changed',updated_at=clock_timestamp() where tenant_id=p_tenant and backfill_run=p_run;
  return jsonb_build_object('stopped','aborted','reason','catalog_changed');
 end if;
 return null;
end $$;

create function control.nexloop_relation_backfill_count(p_tenant text,p_run text,p_world text,p_counts jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare k text;c jsonb;
begin
 for k,c in select key,value from jsonb_each(p_counts) loop
  insert into control.nexloop_relation_backfill_counts as x(tenant_id,backfill_run,relation_name,world,scanned,created,existing,dangling,skipped_erasure,skipped_vanished,skipped_world_purge)
  values(p_tenant,p_run,k,p_world,coalesce((c->>'scanned')::bigint,0),coalesce((c->>'created')::bigint,0),coalesce((c->>'existing')::bigint,0),
   coalesce((c->>'dangling')::bigint,0),coalesce((c->>'skipped_erasure')::bigint,0),coalesce((c->>'skipped_vanished')::bigint,0),coalesce((c->>'skipped_world_purge')::bigint,0))
  on conflict(tenant_id,backfill_run,relation_name,world) do update set scanned=x.scanned+excluded.scanned,created=x.created+excluded.created,
   existing=x.existing+excluded.existing,dangling=x.dangling+excluded.dangling,skipped_erasure=x.skipped_erasure+excluded.skipped_erasure,
   skipped_vanished=x.skipped_vanished+excluded.skipped_vanished,skipped_world_purge=x.skipped_world_purge+excluded.skipped_world_purge;
 end loop;
end $$;

-- One locked source object (design §3.2 step 5): its relations, each target under KEY SHARE (design C-1). Returns the per-relation
-- outcomes and whether a target was busy (the object goes to the skipped table, design S-10).
create function control.nexloop_relation_backfill_object(o ontology.objects,p_run text,p_erasing text[]) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m record;v text;t ontology.objects;res text;outcome jsonb:='{}'::jsonb;busy boolean:=false;
 marker jsonb:=jsonb_build_object('derived',true,'backfill',p_run);
begin
 if cardinality(p_erasing)>0 and runtime.nexloop_object_consumers(o.tenant_id,o.world,o.type_name,o.object_id)&&p_erasing then
  return jsonb_build_object('outcome',(select jsonb_object_agg(m2.relation_name,'skipped_erasure') from control.nexloop_relation_derivations m2
    where m2.tenant_id=o.tenant_id and m2.source_type=o.type_name and m2.source_property is not null),'busy',false);
 end if;
 for m in select * from control.nexloop_relation_derivations where tenant_id=o.tenant_id and source_type=o.type_name and source_property is not null order by relation_name loop
  v:=o.properties->>m.source_property;
  if v is null or v!~'^[a-f0-9]{64}$' then continue;end if;
  select * into t from ontology.objects x where x.tenant_id=o.tenant_id and x.world=o.world and x.type_name=m.target_type and x.object_id=v
   and x.updated_at<clock_timestamp()-interval '10 seconds'  -- recently written: left to the rescan (design N-1 item 3)
   for key share skip locked;
  if found then
   res:=ontology.nexloop_relation_derive_one(o,m.relation_name,marker,false);
   if res='created' then
    insert into control.nexloop_relation_backfill_created(tenant_id,backfill_run,relation_id)
     values(o.tenant_id,p_run,encode(sha256(convert_to(jsonb_build_array(o.tenant_id,o.world,m.relation_name,o.type_name,o.object_id,m.target_type,v)::text,'UTF8')),'hex'))
     on conflict do nothing;
   end if;
   outcome:=outcome||jsonb_build_object(m.relation_name,res);
  elsif exists(select 1 from ontology.objects x where x.tenant_id=o.tenant_id and x.world=o.world and x.type_name=m.target_type and x.object_id=v) then
   busy:=true;  -- being deleted, locked FOR UPDATE, or written in the last seconds
  else
   outcome:=outcome||jsonb_build_object(m.relation_name,'dangling');
  end if;
 end loop;
 return jsonb_build_object('outcome',outcome,'busy',busy);
end $$;

create function control.nexloop_relation_backfill_tally(p_tally jsonb,p_outcome jsonb) returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(p_tally,'{}'::jsonb)||coalesce((select jsonb_object_agg(e.key,coalesce(p_tally->e.key,'{}'::jsonb)
   ||jsonb_build_object('scanned',coalesce((p_tally->e.key->>'scanned')::int,0)+1,e.value#>>'{}',coalesce((p_tally->e.key->>(e.value#>>'{}'))::int,0)+1))
  from jsonb_each(p_outcome) e),'{}'::jsonb)
$$;

create function control.nexloop_relation_backfill_erasing(p_tenant text,p_world text) returns text[]
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 -- SHARE: no erasure starts or moves while the batch runs (design §4.3); the 200 ms lock_timeout gives up instead of waiting.
 lock table control.nexloop_consumer_erasures in share mode;
 return coalesce((select array_agg(distinct consumer_id) from control.nexloop_consumer_erasures
  where tenant_id=p_tenant and world=p_world and state=any(control.nexloop_relation_backfill_erasing_states())),'{}');
end $$;

create function control.nexloop_relation_backfill_world_purging(p_tenant text,p_world text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select exists(select 1 from control.nexloop_sim_worlds w where w.tenant_id=p_tenant and w.world=p_world and w.status in ('deleting','deleted'))
$$;

create function control.nexloop_relation_backfill_batch(p_tenant text,p_run text,p_type text,p_world text,p_after text,p_batch integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);g jsonb;c control.nexloop_relation_backfill_cursors;props text[];ids text[];hi text;upto text;
 o ontology.objects;erasing text[];locked text[]:='{}';skipped text[]:='{}';res jsonb;tally jsonb:='{}'::jsonb;busy text[]:='{}';n integer:=0;
 early boolean:=false;waiters integer;counted_rows jsonb:='{}'::jsonb;
begin
 if p_batch not between 1 and 1000 then raise exception 'relation backfill batch size' using errcode='22023';end if;
 perform control.nexloop_relation_backfill_assert_timeout();
 perform set_config('eios.tenant_id',p_tenant,true);  -- L6 D-2: the tenant before any derivation
 perform set_config('lock_timeout','200ms',true);
 g:=control.nexloop_relation_backfill_gate(p_tenant,p_run,true);
 if g is not null then perform set_config('eios.tenant_id',coalesce(prior,''),true);return g;end if;
 select * into c from control.nexloop_relation_backfill_cursors where tenant_id=p_tenant and backfill_run=p_run and source_type=p_type and world=p_world for update;
 if not found then raise exception 'relation backfill cursor unknown' using errcode='42501';end if;
 -- N-6: the payload names its range start; a replay after the cursor moved on is refused, never a silent skip.
 if c.done or c.last_object_id is distinct from coalesce(p_after,'') then raise exception 'relation backfill cursor moved' using errcode='40001';end if;
 if control.nexloop_relation_backfill_world_purging(p_tenant,p_world) then  -- N-2
  update control.nexloop_relation_backfill_cursors set done=true where tenant_id=p_tenant and backfill_run=p_run and source_type=p_type and world=p_world;
  perform control.nexloop_relation_backfill_count(p_tenant,p_run,p_world,(select jsonb_object_agg(relation_name,jsonb_build_object('skipped_world_purge',1))
   from control.nexloop_relation_derivations where tenant_id=p_tenant and source_type=p_type and source_property is not null));
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('done',true,'skipped_world_purge',true);
 end if;
 select array_agg(source_property) into props from control.nexloop_relation_derivations where tenant_id=p_tenant and source_type=p_type and source_property is not null;
 -- N-3: the range first, unlocked: (after, hi].
 select array_agg(x.object_id order by x.object_id) into ids from (select ob.object_id from ontology.objects ob
   where ob.tenant_id=p_tenant and ob.type_name=p_type and ob.world=p_world and ob.object_id>coalesce(p_after,'')
    and exists(select 1 from unnest(props) p where ob.properties->>p~'^[a-f0-9]{64}$')
   order by ob.object_id limit p_batch) x;
 if ids is null then
  update control.nexloop_relation_backfill_cursors set done=true where tenant_id=p_tenant and backfill_run=p_run and source_type=p_type and world=p_world;
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('done',true);
 end if;
 hi:=ids[cardinality(ids)];
 erasing:=control.nexloop_relation_backfill_erasing(p_tenant,p_world);
 upto:=hi;
 for o in select * from ontology.objects ob where ob.tenant_id=p_tenant and ob.type_name=p_type and ob.world=p_world
   and ob.object_id>coalesce(p_after,'') and ob.object_id<=hi and exists(select 1 from unnest(props) p where ob.properties->>p~'^[a-f0-9]{64}$')
   order by ob.object_id for share skip locked loop
  -- R-1 / V-3: 750 ms from the transaction's start; at least one object per batch.
  if n>0 and clock_timestamp()-transaction_timestamp()>interval '750 milliseconds' then early:=true;exit;end if;
  locked:=locked||o.object_id;upto:=o.object_id;n:=n+1;
  res:=control.nexloop_relation_backfill_object(o,p_run,erasing);
  tally:=control.nexloop_relation_backfill_tally(tally,res->'outcome');
  if (res->>'busy')::boolean then
   busy:=busy||o.object_id;
   counted_rows:=counted_rows||jsonb_build_object(o.object_id,(select coalesce(jsonb_agg(k),'[]'::jsonb) from jsonb_object_keys(res->'outcome') k));
  end if;
 end loop;
 if not early then upto:=hi;end if;
 -- C-2 / N-3: the range up to where the batch stopped, less what it locked, is skipped (persisted with this batch).
 skipped:=array(select x from unnest(ids) x where x<=upto and not x=any(locked));
 insert into control.nexloop_relation_backfill_skipped(tenant_id,backfill_run,source_type,world,object_id,reason)
  select p_tenant,p_run,p_type,p_world,x,'source_locked' from unnest(skipped) x
  on conflict do nothing;
 insert into control.nexloop_relation_backfill_skipped(tenant_id,backfill_run,source_type,world,object_id,reason,counted)
  select p_tenant,p_run,p_type,p_world,x,'target_locked',array(select jsonb_array_elements_text(counted_rows->x)) from unnest(busy) x
  on conflict do nothing;
 perform control.nexloop_relation_backfill_count(p_tenant,p_run,p_world,tally);
 update control.nexloop_relation_backfill_cursors set last_object_id=upto,batches=batches+1,last_batch_size=p_batch,
  last_batch_ms=(extract(epoch from clock_timestamp()-transaction_timestamp())*1000)::integer
  where tenant_id=p_tenant and backfill_run=p_run and source_type=p_type and world=p_world;
 -- R-5 / V-2: the sessions waiting for this transaction now.
 select count(*) into waiters from pg_stat_activity a where pg_backend_pid()=any(pg_blocking_pids(a.pid));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('done',false,'after',upto,'locked',n,'skipped',cardinality(skipped),'target_locked',cardinality(busy),'early',early,
  'waiters',waiters,'elapsed_ms',(extract(epoch from clock_timestamp()-transaction_timestamp())*1000)::integer,'counts',tally);
end $$;

create function control.nexloop_relation_backfill_rescan(p_tenant text,p_run text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);g jsonb;s record;o ontology.objects;erasing text[];v_world text:=null;res jsonb;
 cleared integer:=0;kept integer:=0;tally jsonb;fresh jsonb;
begin
 perform control.nexloop_relation_backfill_assert_timeout();
 perform set_config('eios.tenant_id',p_tenant,true);
 perform set_config('lock_timeout','200ms',true);
 g:=control.nexloop_relation_backfill_gate(p_tenant,p_run,true);
 if g is not null then perform set_config('eios.tenant_id',coalesce(prior,''),true);return g;end if;
 for s in select * from control.nexloop_relation_backfill_skipped where tenant_id=p_tenant and backfill_run=p_run
   order by world,source_type,object_id limit greatest(1,least(p_limit,1000)) loop
  if clock_timestamp()-transaction_timestamp()>interval '750 milliseconds' and cleared+kept>0 then exit;end if;
  tally:='{}'::jsonb;
  if control.nexloop_relation_backfill_world_purging(p_tenant,s.world) then
   tally:=(select jsonb_object_agg(relation_name,jsonb_build_object('skipped_world_purge',1)) from control.nexloop_relation_derivations
    where tenant_id=p_tenant and source_type=s.source_type and source_property is not null);
  else
   if v_world is distinct from s.world then erasing:=control.nexloop_relation_backfill_erasing(p_tenant,s.world);v_world:=s.world;end if;
   select * into o from ontology.objects ob where ob.tenant_id=p_tenant and ob.world=s.world and ob.type_name=s.source_type and ob.object_id=s.object_id
    for share skip locked;
   if not found then
    if exists(select 1 from ontology.objects ob where ob.tenant_id=p_tenant and ob.world=s.world and ob.type_name=s.source_type and ob.object_id=s.object_id) then
     update control.nexloop_relation_backfill_skipped set attempts=attempts+1 where tenant_id=p_tenant and backfill_run=p_run and source_type=s.source_type and world=s.world and object_id=s.object_id;
     kept:=kept+1;continue;
    end if;
    tally:=(select jsonb_object_agg(relation_name,jsonb_build_object('skipped_vanished',1)) from control.nexloop_relation_derivations  -- S-9
     where tenant_id=p_tenant and source_type=s.source_type and source_property is not null);
   else
    res:=control.nexloop_relation_backfill_object(o,p_run,erasing);
    -- S-1: only the relations not counted at an earlier attempt.
    fresh:=coalesce((select jsonb_object_agg(e.key,e.value) from jsonb_each(res->'outcome') e where not e.key=any(s.counted)),'{}'::jsonb);
    if (res->>'busy')::boolean then
     update control.nexloop_relation_backfill_skipped set attempts=attempts+1,reason='target_locked',counted=counted||array(select jsonb_object_keys(fresh))
      where tenant_id=p_tenant and backfill_run=p_run and source_type=s.source_type and world=s.world and object_id=s.object_id;
     kept:=kept+1;
     perform control.nexloop_relation_backfill_count(p_tenant,p_run,s.world,control.nexloop_relation_backfill_tally('{}'::jsonb,fresh));
     continue;
    end if;
    tally:=control.nexloop_relation_backfill_tally('{}'::jsonb,fresh);
   end if;
  end if;
  perform control.nexloop_relation_backfill_count(p_tenant,p_run,s.world,coalesce(tally,'{}'::jsonb));
  delete from control.nexloop_relation_backfill_skipped where tenant_id=p_tenant and backfill_run=p_run and source_type=s.source_type and world=s.world and object_id=s.object_id;
  cleared:=cleared+1;
 end loop;
 res:=jsonb_build_object('cleared',cleared,'kept',kept,
  'remaining',(select count(*) from control.nexloop_relation_backfill_skipped where tenant_id=p_tenant and backfill_run=p_run),
  'max_attempts',(select coalesce(max(attempts),0) from control.nexloop_relation_backfill_skipped where tenant_id=p_tenant and backfill_run=p_run));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return res;
end $$;

-- Completed only with every cursor done and nothing skipped (design C-2).
create function control.nexloop_relation_backfill_complete(p_tenant text,p_run text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);g jsonb;v_open integer;v_skipped integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 g:=control.nexloop_relation_backfill_gate(p_tenant,p_run,true);
 if g is not null then perform set_config('eios.tenant_id',coalesce(prior,''),true);return g;end if;
 select count(*) filter (where not done) into v_open from control.nexloop_relation_backfill_cursors where tenant_id=p_tenant and backfill_run=p_run;
 select count(*) into v_skipped from control.nexloop_relation_backfill_skipped where tenant_id=p_tenant and backfill_run=p_run;
 if v_open=0 and v_skipped=0 then
  update control.nexloop_relation_backfill_runs set state='completed',updated_at=clock_timestamp() where tenant_id=p_tenant and backfill_run=p_run;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('completed',v_open=0 and v_skipped=0,'open_cursors',v_open,'skipped',v_skipped);
end $$;

create function control.nexloop_relation_backfill_abort(p_tenant text,p_run text,p_reason text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_relation_backfill_runs set state='aborted',reason=left(coalesce(p_reason,'operator'),200),updated_at=clock_timestamp()
  where tenant_id=p_tenant and backfill_run=p_run and state='running' returning state into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('run_id',p_run,'aborted',v is not null);
end $$;

-- Revert (design §7): only relations this run created and still marked with it; the derived or linked ones stay.
create function control.nexloop_relation_backfill_revert_batch(p_tenant text,p_run text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r control.nexloop_relation_backfill_runs;ids text[];v_deleted integer;v_state text;
begin
 perform control.nexloop_relation_backfill_assert_timeout();
 perform set_config('eios.tenant_id',p_tenant,true);
 perform set_config('lock_timeout','200ms',true);
 select * into r from control.nexloop_relation_backfill_runs where tenant_id=p_tenant and backfill_run=p_run for update;
 if not found then raise exception 'relation backfill run unknown' using errcode='42501';end if;
 if r.state='running' then raise exception 'relation backfill run active: abort it first' using errcode='42501';end if;
 if r.state='reverted' then perform set_config('eios.tenant_id',coalesce(prior,''),true);return jsonb_build_object('state','reverted','deleted',0);end if;
 if r.state<>'reverting' then
  update control.nexloop_relation_backfill_runs set state='reverting',updated_at=clock_timestamp() where tenant_id=p_tenant and backfill_run=p_run;
 end if;
 select array_agg(relation_id) into ids from (select relation_id from control.nexloop_relation_backfill_created where tenant_id=p_tenant and backfill_run=p_run
  order by relation_id limit greatest(1,least(p_limit,1000))) x;
 delete from ontology.relations where tenant_id=p_tenant and relation_id=any(coalesce(ids,'{}')) and metadata @> jsonb_build_object('backfill',p_run);
 get diagnostics v_deleted=row_count;
 delete from control.nexloop_relation_backfill_created where tenant_id=p_tenant and backfill_run=p_run and relation_id=any(coalesce(ids,'{}'));
 if not exists(select 1 from control.nexloop_relation_backfill_created where tenant_id=p_tenant and backfill_run=p_run) then
  update control.nexloop_relation_backfill_runs set state='reverted',updated_at=clock_timestamp() where tenant_id=p_tenant and backfill_run=p_run;
 end if;
 select state into v_state from control.nexloop_relation_backfill_runs where tenant_id=p_tenant and backfill_run=p_run;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('state',v_state,'deleted',v_deleted,
  'catalog_changed',control.nexloop_relation_backfill_digest(p_tenant) is distinct from r.derivations_digest);  -- S-3: noted, never stops a revert
end $$;

create function control.nexloop_relation_backfill_status(p_tenant text,p_run text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);out jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('run_id',r.backfill_run,'state',r.state,'reason',r.reason,
   'cursors',(select jsonb_agg(jsonb_build_object('source_type',c.source_type,'world',c.world,'after',c.last_object_id,'done',c.done,'batches',c.batches) order by c.ordinal)
     from control.nexloop_relation_backfill_cursors c where c.tenant_id=r.tenant_id and c.backfill_run=r.backfill_run),
   'counts',(select jsonb_agg(to_jsonb(x)-'tenant_id'-'backfill_run' order by x.relation_name,x.world) from control.nexloop_relation_backfill_counts x where x.tenant_id=r.tenant_id and x.backfill_run=r.backfill_run),
   'created',(select count(*) from control.nexloop_relation_backfill_created x where x.tenant_id=r.tenant_id and x.backfill_run=r.backfill_run),
   'skipped',(select coalesce(jsonb_agg(jsonb_build_object('source_type',s.source_type,'world',s.world,'object_id',s.object_id,'reason',s.reason,'attempts',s.attempts)
     order by s.world,s.source_type,s.object_id),'[]'::jsonb) from control.nexloop_relation_backfill_skipped s where s.tenant_id=r.tenant_id and s.backfill_run=r.backfill_run))
 into out from control.nexloop_relation_backfill_runs r where r.tenant_id=p_tenant and r.backfill_run=p_run;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return out;
end $$;

-- verify (design §5): per (relation, world) A (a 64-hex value naming an object of the target type in the world, the source not
-- belonging to an erasing Consumer), B (of these, with the relation), dangling, extra derived relations that no property gives.
create function control.nexloop_relation_backfill_verify(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);m record;out jsonb:='[]'::jsonb;v_skipped bigint;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for m in select * from control.nexloop_relation_derivations where tenant_id=p_tenant and source_property is not null order by relation_name loop
  out:=out||coalesce((select jsonb_agg(jsonb_build_object('relation',m.relation_name,'world',x.world,'a',x.a,'b',x.b,'dangling',x.dangling,'erasing',x.erasing,
     'extra',(select count(*) from ontology.relations r where r.tenant_id=p_tenant and r.world=x.world and r.relation_name=m.relation_name and r.metadata @> '{"derived":true}'
       and not exists(select 1 from ontology.objects s where s.tenant_id=r.tenant_id and s.world=r.world and s.type_name=r.source_type and s.object_id=r.source_object_id
        and s.properties->>m.source_property=r.target_object_id))) order by x.world) from (
   select o.world,
    count(*) filter (where t.object_id is not null and not e.erasing) a,
    count(*) filter (where t.object_id is not null and not e.erasing and exists(select 1 from ontology.relations r where r.tenant_id=o.tenant_id and r.world=o.world
      and r.relation_name=m.relation_name and r.source_type=o.type_name and r.source_object_id=o.object_id and r.target_object_id=t.object_id)) b,
    count(*) filter (where t.object_id is null and o.properties->>m.source_property~'^[a-f0-9]{64}$') dangling,
    count(*) filter (where t.object_id is not null and e.erasing) erasing
   from ontology.objects o
   left join ontology.objects t on t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=m.target_type and t.object_id=o.properties->>m.source_property
   cross join lateral (select exists(select 1 from control.nexloop_consumer_erasures ce where ce.tenant_id=o.tenant_id and ce.world=o.world
      and ce.state=any(control.nexloop_relation_backfill_erasing_states())
      and ce.consumer_id=any(runtime.nexloop_object_consumers(o.tenant_id,o.world,o.type_name,o.object_id))) erasing) e
   where o.tenant_id=p_tenant and o.type_name=m.source_type and o.properties->>m.source_property~'^[a-f0-9]{64}$'
    and not control.nexloop_relation_backfill_world_purging(p_tenant,o.world)  -- N-2: a world being purged is not counted
   group by o.world) x),'[]'::jsonb);
 end loop;
 -- I-2: what the runs still have to look at again (acceptance needs 0).
 select count(*) into v_skipped from control.nexloop_relation_backfill_skipped s join control.nexloop_relation_backfill_runs r
  on r.tenant_id=s.tenant_id and r.backfill_run=s.backfill_run where s.tenant_id=p_tenant and r.state in ('running','completed');
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('relations',out,'skipped_remaining',v_skipped);
end $$;

do $$declare f text;begin
 foreach f in array array['control.nexloop_relation_backfill_digest(text)','control.nexloop_relation_backfill_erasing_states()',
   'control.nexloop_relation_backfill_preflight(text)','control.nexloop_relation_backfill_start(text,text,text,text)',
   'control.nexloop_relation_backfill_gate(text,text,boolean)','control.nexloop_relation_backfill_assert_timeout()','control.nexloop_relation_backfill_count(text,text,text,jsonb)',
   'control.nexloop_relation_backfill_object(ontology.objects,text,text[])','control.nexloop_relation_backfill_tally(jsonb,jsonb)',
   'control.nexloop_relation_backfill_erasing(text,text)','control.nexloop_relation_backfill_world_purging(text,text)',
   'control.nexloop_relation_backfill_batch(text,text,text,text,text,integer)','control.nexloop_relation_backfill_rescan(text,text,integer)',
   'control.nexloop_relation_backfill_complete(text,text)','control.nexloop_relation_backfill_abort(text,text,text)',
   'control.nexloop_relation_backfill_revert_batch(text,text,integer)','control.nexloop_relation_backfill_status(text,text)',
   'control.nexloop_relation_backfill_verify(text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;

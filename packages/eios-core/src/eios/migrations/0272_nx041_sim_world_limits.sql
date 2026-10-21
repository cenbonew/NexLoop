-- NX-041 sim world limits (owner decision 2026-10-21, as recommended): how many simulation worlds a tenant may hold and when they
-- leave. The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. At most 20 active simulation or shadow worlds per tenant: sim_world.create refuses the 21st (limit_reached). Worlds deleting or
--    deleted do not count. The count is taken under a per-tenant transaction lock, so concurrent creations cannot pass it together.
-- 2. Expiry: expires_in_days is optional (default 30) and at most 30; a longer period is refused (expiry_over_30_days). A replayed
--    request still answers with its world, also at the limit.
-- 3. The retention keeper's sim_world_summary pass first starts the whole deletion of every expired active world of the tenant
--    through the owner's delete path (S2: summary first, credentials revoked, deletion ledger, a bounded pass; the 30-day summary
--    retention unchanged), then continues the worlds still deleting, least recently continued first. A world whose deletion fails
--    or stays blocked is counted and skipped; it never holds up the others. No new retention class.

create or replace function control.nexloop_governed_create_sim_world(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_kind text:=body->>'kind';v_days integer;v_world text;v_hex text;w control.nexloop_sim_worlds;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','kind','purpose','expires_in_days'))
  or v_kind is null or v_kind not in ('simulation','shadow') or jsonb_typeof(body->'purpose') is distinct from 'string'
  or char_length(btrim(body->>'purpose')) not between 1 and 500 or jsonb_typeof(coalesce(body->'expires_in_days','30'::jsonb)) is distinct from 'number'
  or coalesce(body->>'expires_in_days','30')!~'^[0-9]{1,3}$' or coalesce(body->>'expires_in_days','30')::integer not between 1 and 365
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  raise exception 'sim world: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'sim world: owner_world_only' using errcode='22023';end if;
 v_days:=coalesce(body->>'expires_in_days','30')::integer;
 if v_days>30 then raise exception 'sim world: expiry_over_30_days' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Idempotent per request: the same request gives the same world.
 select * into w from control.nexloop_sim_worlds where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if runtime.nexloop_world_kind(w.world)<>v_kind or w.purpose<>btrim(body->>'purpose') then raise exception 'sim world: request_reused' using errcode='22023';end if;
  return jsonb_build_object('world',w.world,'status',w.status,'expires_at',runtime.nexloop_commercial_ts(w.expires_at),'created',false);
 end if;
 -- At most 20 active worlds per tenant, counted under the tenant's lock (held to the end of the transaction).
 perform pg_advisory_xact_lock(hashtextextended('nexloop:sim-world-limit:'||p_tenant,0));
 if (select count(*) from control.nexloop_sim_worlds where tenant_id=p_tenant and status='active')>=20 then
  raise exception 'sim world: limit_reached' using errcode='22023';end if;
 v_hex:=md5('nexloop:sim-world:'||p_tenant||':'||(body->>'request_id'));
 v_world:=case v_kind when 'simulation' then 'sim:' else 'shadow:' end||substr(v_hex,1,8)||'-'||substr(v_hex,9,4)||'-4'||substr(v_hex,14,3)||'-8'||substr(v_hex,18,3)||'-'||substr(v_hex,21,12);
 insert into control.nexloop_sim_worlds(world,tenant_id,created_by,purpose,status,expires_at,request_id)
  values(v_world,p_tenant,p_principal,btrim(body->>'purpose'),'active',clock_timestamp()+make_interval(days=>v_days),body->>'request_id') returning * into w;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('sim-world:create:'||v_world,'nexloop.sim_world.created',p_tenant,p_principal,'sim_world',p_intent,
   jsonb_build_object('world',v_world,'kind',v_kind,'expires_at',runtime.nexloop_commercial_ts(w.expires_at)),clock_timestamp());
 return jsonb_build_object('world',v_world,'status','active','expires_at',runtime.nexloop_commercial_ts(w.expires_at),'created',true);
end $$;

create or replace function runtime.nexloop_retention_sweep_sim_worlds(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz;x record;n_resumed integer:=0;n_summaries integer:=0;n_expired integer:=0;n_failed integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v_cut:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));
 if p_class<>'sim_world_summary' or v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 -- Expired active worlds (at most 20 per tenant) start their whole deletion through the owner's delete path, each on its own.
 for x in select world from control.nexloop_sim_worlds where tenant_id=p_tenant and status='active' and expires_at<=clock_timestamp() order by expires_at,world loop
  begin
   perform control.nexloop_governed_delete_sim_world(p_tenant,'real',p_by,null,'sim-world-expiry:'||x.world,
    jsonb_build_object('operation','delete_sim_world','request_id','sim-world-expiry:'||x.world,'world',x.world,'reason','expired'));
   n_expired:=n_expired+1;
  exception when others then n_failed:=n_failed+1;
  end;
  perform set_config('eios.tenant_id',p_tenant,true);
 end loop;
 -- Worlds still deleting, least recently continued first, so a blocked world never holds up the others.
 for x in select w.world from control.nexloop_sim_worlds w left join control.nexloop_sim_world_summaries s on s.world=w.world
   where w.tenant_id=p_tenant and w.status='deleting' order by s.progress->>'at' nulls first,w.created_at limit 10 loop
  begin
   perform runtime.nexloop_sim_world_purge(p_tenant,x.world,p_batch);n_resumed:=n_resumed+1;
  exception when others then n_failed:=n_failed+1;
  end;
  perform set_config('eios.tenant_id',p_tenant,true);
 end loop;
 perform set_config('nexloop.sim_summary_retention','on',true);
 -- The batch limit applies to the expired summaries themselves (review 4.1), never to registry rows, which stay forever.
 delete from control.nexloop_sim_world_summaries s where s.ctid in (select s2.ctid from control.nexloop_sim_world_summaries s2
   join control.nexloop_sim_worlds w on w.world=s2.world and w.tenant_id=s2.tenant_id and w.status='deleted'
   where s2.tenant_id=p_tenant and s2.completed_at<v_cut order by s2.completed_at limit p_batch);
 get diagnostics n_summaries=row_count;
 perform set_config('nexloop.sim_summary_retention','',true);
 if n_summaries>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'deleted',
   jsonb_build_object('summaries',n_summaries),p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n_summaries,'more',n_summaries>=p_batch,
  'counts',jsonb_build_object('summaries',n_summaries,'worlds_resumed',n_resumed,'worlds_expired',n_expired,'worlds_failed',n_failed),'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;

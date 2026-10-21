-- NX-041 S6 review fixes (L6 nx041-s6-review 1.2, 2.3, 3.1-3.4, 5.1, 5.2). The migration number lives only in the file name and the
-- catalog; nothing below repeats it.
-- 1. A dedicated login role nexloop_sim_files for the simulation group's file purger (never nexloop_scheduler): it executes the
--    two file purge entries and nothing else.
-- 2. Each simulation Host is registered with its own purge key (authz.nexloop_sim_hosts; trusted configuration only). The database
--    issues a per-world purge permit (HMAC, minutes) only for a world being deleted; the Host deletes only with a valid permit
--    (review 2.3) and answers with a signed deletion proof: the listing digest and entry count before deletion, the state after
--    (absent), and whether that Host ever held the world. The confirmation entry accepts only a proof that verifies with the
--    Host's key (3.2): a caller without the Host key cannot confirm.
-- 3. Confirmation is per Host (3.3): a world's files count as gone only when every active registered Host has confirmed it, so an
--    absent from a Host that never held the world completes that Host's part only.
-- 4. The world's summary (S2) completes only when its files are confirmed (3.4): until then completed_at stays empty, the
--    summary is never removed by retention, and runtime.nexloop_sim_world_files_overdue lists deleted worlds still waiting.
-- 5. The stand-in intent's submitted_at is the wall-clock audit time again (5.1): the S6 trigger that set it to the world time goes;
--    runtime.nexloop_sim_world_time no longer catches errors (5.2): it calls runtime.nexloop_world_now when present (S5), else the
--    wall clock, and any error inside world_now propagates.

-- 1. The file purger's role ---------------------------------------------------------------------------------------------------
do $$begin
 if not exists(select 1 from pg_roles where rolname='nexloop_sim_files') then
  create role nexloop_sim_files login nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls;
 end if;
end $$;
grant usage on schema authz to nexloop_sim_files;

-- 2. Hosts and their keys ----------------------------------------------------------------------------------------------------
create table authz.nexloop_sim_hosts (
 host_id text primary key check(host_id~'^[a-z][a-z0-9-]{2,62}$'),
 key_material bytea not null check(octet_length(key_material)=32),
 active boolean not null default true,registered_at timestamptz not null default clock_timestamp());
alter table authz.nexloop_sim_hosts owner to nexloop_owner;
revoke all on authz.nexloop_sim_hosts from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_sim_files;
create function authz.nexloop_sim_host_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' or tg_op='TRUNCATE' then raise exception 'simulation Hosts are deactivated, never deleted' using errcode='42501';end if;
 if new.host_id<>old.host_id or new.key_material<>old.key_material or (new.active and not old.active) then
  raise exception 'a simulation Host key is never changed or reactivated' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_sim_host_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_sim_host_guard() from public;
create trigger nx041_sim_host_guard before update or delete on authz.nexloop_sim_hosts for each row execute function authz.nexloop_sim_host_guard();
create trigger nx041_sim_host_no_truncate before truncate on authz.nexloop_sim_hosts for each statement execute function authz.nexloop_sim_host_guard();

create function authz.nexloop_sim_host_mac(p_host text,p_text text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select encode(extensions.hmac(convert_to(p_text,'UTF8'),h.key_material,'sha256'),'hex') from authz.nexloop_sim_hosts h where h.host_id=p_host and h.active
$$;
alter function authz.nexloop_sim_host_mac(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_sim_host_mac(text,text) from public;
create function authz.nexloop_sim_ts(p timestamptz) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select to_char(p at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')
$$;
alter function authz.nexloop_sim_ts(timestamptz) owner to nexloop_owner;
revoke all on function authz.nexloop_sim_ts(timestamptz) from public;

-- 3. Per-Host confirmations ----------------------------------------------------------------------------------------------------
create table runtime.nexloop_sim_world_file_confirmations (
 sim_world text not null check(coalesce(runtime.nexloop_world_kind(sim_world),'') in ('simulation','shadow')),
 host_id text not null references authz.nexloop_sim_hosts(host_id),
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 outcome text not null check(outcome in ('purged','absent')),seen boolean not null,
 entries integer not null check(entries>=0),listing_digest text not null check(listing_digest~'^[0-9a-f]{64}$'),
 issued_at timestamptz not null,confirmed_at timestamptz not null default clock_timestamp(),
 primary key(sim_world,host_id),check(outcome='purged' or entries=0));
alter table runtime.nexloop_sim_world_file_confirmations owner to nexloop_owner;
alter table runtime.nexloop_sim_world_file_confirmations enable row level security;
alter table runtime.nexloop_sim_world_file_confirmations force row level security;
create policy tenant_boundary on runtime.nexloop_sim_world_file_confirmations to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_sim_world_file_confirmations from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,
 nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_sim_files;
create trigger nx041_sim_file_confirmations_append_only before update or delete on runtime.nexloop_sim_world_file_confirmations
 for each row execute function control.nexloop_nx022_append_only();

-- The S6 entries are replaced by Host-bound ones (new signatures; nexloop_scheduler loses them).
drop function authz.nexloop_sim_world_files_pending(integer);
drop function authz.nexloop_sim_world_files_confirm(text,text);

-- Worlds this Host still has to confirm, each with a purge permit valid for ten minutes.
create function authz.nexloop_sim_world_files_pending(p_host text,p_limit integer) returns table(world text,permit jsonb)
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;w text;v_exp text;
begin
 if session_user is distinct from 'nexloop_sim_files' or not exists(select 1 from authz.nexloop_sim_hosts h where h.host_id=p_host and h.active) then
  raise exception 'sim world files unavailable' using errcode='42501';end if;
 v_exp:=authz.nexloop_sim_ts(clock_timestamp()+interval '10 minutes');
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  for w in select p.sim_world from runtime.nexloop_sim_world_file_purges p where p.tenant_id=t and p.confirmed_at is null
    and not exists(select 1 from runtime.nexloop_sim_world_file_confirmations c where c.sim_world=p.sim_world and c.host_id=p_host)
    order by p.requested_at limit greatest(1,least(coalesce(p_limit,20),200)) loop
   world:=w;
   permit:=jsonb_build_object('host_id',p_host,'world',w,'expires_at',v_exp,
    'mac',authz.nexloop_sim_host_mac(p_host,'nexloop-sim-world-purge-v1:'||p_host||':'||w||':'||v_exp));
   return next;
  end loop;
 end loop;
end $$;

-- A Host's signed deletion proof; the world's files are confirmed when every active Host has confirmed.
create function authz.nexloop_sim_world_files_confirm(p_host text,p_world text,p_proof jsonb) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;v_issued timestamptz;v_text text;
begin
 if session_user is distinct from 'nexloop_sim_files' then raise exception 'sim world files unavailable' using errcode='42501';end if;
 if jsonb_typeof(p_proof) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(p_proof) k)
    is distinct from array['after','entries','host_id','issued_at','listing_digest','mac','outcome','seen','world']
  or p_proof->>'host_id' is distinct from p_host or p_proof->>'world' is distinct from p_world or p_proof->>'after' is distinct from 'absent'
  or coalesce(p_proof->>'outcome','') not in ('purged','absent') or jsonb_typeof(p_proof->'seen') is distinct from 'boolean'
  or jsonb_typeof(p_proof->'entries') is distinct from 'number' or coalesce(p_proof->>'entries','')!~'^[0-9]{1,9}$'
  or coalesce(p_proof->>'listing_digest','')!~'^[0-9a-f]{64}$' or coalesce(p_proof->>'issued_at','')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  or coalesce(p_proof->>'mac','')!~'^[0-9a-f]{64}$' then
  raise exception 'sim world files: proof invalid' using errcode='42501';end if;
 v_issued:=(p_proof->>'issued_at')::timestamptz;
 v_text:='nexloop-sim-world-purge-proof-v1:'||p_host||':'||p_world||':'||(p_proof->>'outcome')||':'||(p_proof->>'seen')||':'||(p_proof->>'entries')
  ||':'||(p_proof->>'listing_digest')||':'||(p_proof->>'issued_at');
 if authz.nexloop_sim_host_mac(p_host,v_text) is distinct from p_proof->>'mac' or v_issued>clock_timestamp()+interval '1 minute'
  or v_issued<clock_timestamp()-interval '1 hour' then
  raise exception 'sim world files: proof invalid' using errcode='42501';end if;
 v_tenant:=control.nexloop_sim_world_tenant(p_world);
 if v_tenant is null then return false;end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from runtime.nexloop_sim_world_file_purges where sim_world=p_world) then return false;end if;
 insert into runtime.nexloop_sim_world_file_confirmations(sim_world,host_id,tenant_id,outcome,seen,entries,listing_digest,issued_at)
  values(p_world,p_host,v_tenant,p_proof->>'outcome',(p_proof->>'seen')::boolean,(p_proof->>'entries')::integer,p_proof->>'listing_digest',v_issued)
  on conflict(sim_world,host_id) do nothing;
 if not exists(select 1 from authz.nexloop_sim_hosts h where h.active
   and not exists(select 1 from runtime.nexloop_sim_world_file_confirmations c where c.sim_world=p_world and c.host_id=h.host_id)) then
  update runtime.nexloop_sim_world_file_purges set confirmed_at=clock_timestamp(),
    outcome=case when exists(select 1 from runtime.nexloop_sim_world_file_confirmations c where c.sim_world=p_world and c.outcome='purged') then 'purged' else 'absent' end
   where sim_world=p_world and confirmed_at is null;
  -- The summary (S2) completes now if the database part is done (deleted).
  update control.nexloop_sim_world_summaries s set completed_at=clock_timestamp()
   where s.world=p_world and s.completed_at is null and exists(select 1 from control.nexloop_sim_worlds w where w.world=p_world and w.status='deleted');
 end if;
 return true;
end $$;
do $$declare f text;begin
 foreach f in array array['authz.nexloop_sim_world_files_pending(text,integer)','authz.nexloop_sim_world_files_confirm(text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
  execute 'grant execute on function '||f||' to nexloop_sim_files';
 end loop;
end $$;

-- A deployment without any registered simulation Host holds no world directory: its queue entry is confirmed at once.
create function runtime.nexloop_sim_world_files_no_host() returns trigger language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if not exists(select 1 from authz.nexloop_sim_hosts where active) then new.outcome:='absent';new.confirmed_at:=clock_timestamp();end if;
 return new;
end $$;
alter function runtime.nexloop_sim_world_files_no_host() owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_files_no_host() from public;
create trigger nx041_sim_world_files_no_host before insert on runtime.nexloop_sim_world_file_purges for each row execute function runtime.nexloop_sim_world_files_no_host();

-- 4. The summary waits for the files -------------------------------------------------------------------------------------------
create function control.nexloop_sim_summary_files() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if new.completed_at is not null and old.completed_at is null and exists(select 1 from runtime.nexloop_sim_world_file_purges p
   where p.sim_world=new.world and p.confirmed_at is null) then
  new.completed_at:=null;
 end if;
 return new;
end $$;
alter function control.nexloop_sim_summary_files() owner to nexloop_owner;
revoke all on function control.nexloop_sim_summary_files() from public;
create trigger nx041_sim_summary_files before update of completed_at on control.nexloop_sim_world_summaries
 for each row execute function control.nexloop_sim_summary_files();

-- Deleted worlds of a tenant whose files are not yet confirmed, with how long they have waited (numbers and world ids only).
create function runtime.nexloop_sim_world_files_overdue(p_tenant text,p_hours integer) returns table(world text,deleted_at timestamptz,hosts_confirmed integer)
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return query select w.world,w.deleted_at,(select count(*)::integer from runtime.nexloop_sim_world_file_confirmations c where c.sim_world=w.world)
  from control.nexloop_sim_worlds w join runtime.nexloop_sim_world_file_purges p on p.sim_world=w.world
  where w.tenant_id=p_tenant and w.status='deleted' and p.confirmed_at is null and w.deleted_at<clock_timestamp()-make_interval(hours=>greatest(p_hours,0))
  order by w.deleted_at;
end $$;
alter function runtime.nexloop_sim_world_files_overdue(text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_files_overdue(text,integer) from public;

-- 5. Wall-clock audit time and no catch-all ---------------------------------------------------------------------------------------
drop trigger nx041_sim_effect_world_time on runtime.nexloop_sim_effect_intents;
drop function runtime.nexloop_sim_effect_world_time();
create or replace function runtime.nexloop_sim_world_time(p_tenant text,p_world text) returns timestamptz
 language plpgsql volatile set search_path=pg_catalog,pg_temp as $$
declare v timestamptz;
begin
 if to_regprocedure('runtime.nexloop_world_now(text,text)') is null then return clock_timestamp();end if;  -- before S5 is integrated
 execute 'select runtime.nexloop_world_now($1,$2)' into v using p_tenant,p_world;
 return v;
end $$;

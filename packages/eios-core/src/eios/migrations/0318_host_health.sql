-- EIOS capability gap E2 + E3 (docs/design/eios-capability-gap.md; docs/implementation/host-health.md): crash loops and the
-- freshness of scheduled runs, observed from outside the services themselves. Temporary number; the dispatcher renumbers.
--
-- A host probe (scripts/deploy/host_health_probe.py, a root systemd timer on each host, standard library plus psycopg) reads
-- container restart counts (docker inspect), service restart counts (systemctl NRestarts) and the last run of each timer's
-- service (systemctl), and records them through authz.nexloop_record_host_observation. Each host has its own login role,
-- nexloop_host_recorder_<host_role> (app_host, data_host): it executes only that function and reads no table, and the host_role
-- of what it records is derived from session_user - never taken from the caller (L6 review C2: one host's credential cannot
-- write another host's ticks, samples or timer runs). Nothing here comes from the observed service, and nothing reaches the
-- runtime, an Agent or a Run.
--
-- 1. Deployment-level tables (no tenant), append-only for the recorder, FORCE RLS, owner only: probe ticks, unit samples and the
--    timer run ledger (one row per observed run; scheduled = the run started within the grace after the timer's last trigger,
--    so a manual rerun never counts as the scheduled round).
-- 2. runtime.nexloop_host_metrics(): per host and name (key '<host_role>:<name>')
--      restarts_1h                        restarts in the last hour: positive restart-count deltas of one instance plus instance changes
--      timer_success_age_seconds          since the last scheduled successful run; fail closed: since the timer was first expected when
--                                         no scheduled success was ever seen
--      timer_scheduled_failures_24h       scheduled runs that failed in the last 24 hours
--      probe_age_seconds (per host_role)  since the host's last probe tick; blind (per host_role) 1 when the last tick saw nothing it expected
--      missing_units                      1 for each unit the host's last tick expected and did not see (L6 review I2), else 0
--    A timer run is scheduled only when the probe says so, it started within a minute of its trigger, and it is the first run
--    recorded for that trigger (L6 review C1: a manual rerun inside the trigger's grace is not the scheduled round).
--    Thresholds are alert rules (deploy/configuration/alert-rules.v12.json), not code.
-- 3. runtime.nexloop_metrics_snapshot is renamed once and wrapped: the wrapper adds the 'host_health' block (the 'host' block is the Agent Host's own samples).
-- Published migrations are not edited.

create role nexloop_host_recorder_app_host login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
create role nexloop_host_recorder_data_host login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema authz to nexloop_host_recorder_app_host,nexloop_host_recorder_data_host;

create table runtime.nexloop_host_probe_ticks (
 tick_id bigint generated always as identity primary key,
 host_role text not null check(host_role~'^[a-z][a-z0-9_]{0,31}$'),
 observed_at timestamptz not null default clock_timestamp(),
 expected_units text[] not null default '{}',expected_timers text[] not null default '{}',
 units_seen integer not null check(units_seen>=0),timers_seen integer not null check(timers_seen>=0),
 blind boolean not null);
create index nexloop_host_probe_ticks_host on runtime.nexloop_host_probe_ticks(host_role,observed_at);
create table runtime.nexloop_host_unit_samples (
 sample_id bigint generated always as identity primary key,tick_id bigint not null,
 host_role text not null,unit_name text not null check(unit_name~'^[a-z0-9][a-z0-9_.-]{0,63}$'),
 kind text not null check(kind in ('container','service')),instance_id text not null check(instance_id~'^[A-Za-z0-9._:-]{1,128}$'),
 restart_count bigint not null check(restart_count>=0),running boolean not null,
 sampled_at timestamptz not null default clock_timestamp());
create index nexloop_host_unit_samples_unit on runtime.nexloop_host_unit_samples(host_role,unit_name,sampled_at);
create index nexloop_host_unit_samples_tick on runtime.nexloop_host_unit_samples(tick_id);
create table runtime.nexloop_host_timer_runs (
 host_role text not null,timer_name text not null check(timer_name~'^[a-z0-9][a-z0-9_.-]{0,63}$'),
 run_started_at timestamptz not null,run_finished_at timestamptz not null,result text not null check(result in ('success','failure')),
 scheduled boolean not null,last_trigger_at timestamptz,recorded_at timestamptz not null default clock_timestamp(),
 primary key(host_role,timer_name,run_started_at),check(run_finished_at>=run_started_at));

do $$declare t text;begin
 foreach t in array array['runtime.nexloop_host_probe_ticks','runtime.nexloop_host_unit_samples','runtime.nexloop_host_timer_runs'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy host_health_owner on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public',t);
 end loop;
end $$;

create function authz.nexloop_record_host_observation(p_entry jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
-- One probe tick: {blind, expected_units[], expected_timers[], units:[{name, kind, instance_id, restart_count, running}],
-- timers:[{name, run_started_at, run_finished_at, result, scheduled, last_trigger_at}]}. Names, ids, counts, flags and times only.
-- The host is the recorder role's (C2); a host_role in the entry is refused.
declare v_host text;u jsonb;r jsonb;n_units integer:=0;n_runs integer:=0;c integer;v_tick bigint;v_scheduled boolean;
 v_started timestamptz;v_trigger timestamptz;
begin
 v_host:=case session_user when 'nexloop_host_recorder_app_host' then 'app_host' when 'nexloop_host_recorder_data_host' then 'data_host' end;
 if v_host is null then raise exception 'host recorder unavailable' using errcode='42501';end if;
 if jsonb_typeof(p_entry) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(p_entry) k where k not in ('blind','expected_units','expected_timers','units','timers'))
  or jsonb_typeof(p_entry->'blind') is distinct from 'boolean'
  or jsonb_typeof(p_entry->'units') is distinct from 'array' or jsonb_typeof(p_entry->'timers') is distinct from 'array'
  or jsonb_typeof(p_entry->'expected_units') is distinct from 'array' or jsonb_typeof(p_entry->'expected_timers') is distinct from 'array'
  or jsonb_array_length(p_entry->'units')>100 or jsonb_array_length(p_entry->'timers')>100
  or jsonb_array_length(p_entry->'expected_units')>100 or jsonb_array_length(p_entry->'expected_timers')>100
  or exists(select 1 from jsonb_array_elements((p_entry->'expected_units')||(p_entry->'expected_timers')) e
     where jsonb_typeof(e) is distinct from 'string' or e#>>'{}'!~'^[a-z0-9][a-z0-9_.-]{0,63}$') then
  raise exception 'host observation invalid' using errcode='22023';end if;
 -- Bounded volume (L6 review S1): at most 10 ticks a minute per host (the probe ticks once a minute).
 if (select count(*) from runtime.nexloop_host_probe_ticks t where t.host_role=v_host and t.observed_at>clock_timestamp()-interval '1 minute')>=10 then
  return jsonb_build_object('recorded',false,'reason','rate_limited');end if;
 insert into runtime.nexloop_host_probe_ticks(host_role,expected_units,expected_timers,units_seen,timers_seen,blind)
  values(v_host,array(select jsonb_array_elements_text(p_entry->'expected_units')),array(select jsonb_array_elements_text(p_entry->'expected_timers')),
         jsonb_array_length(p_entry->'units'),jsonb_array_length(p_entry->'timers'),(p_entry->>'blind')::boolean)
  returning tick_id into v_tick;
 for u in select value from jsonb_array_elements(p_entry->'units') loop
  if jsonb_typeof(u) is distinct from 'object'
   or exists(select 1 from jsonb_object_keys(u) k where k not in ('name','kind','instance_id','restart_count','running'))
   or jsonb_typeof(u->'restart_count') is distinct from 'number' or jsonb_typeof(u->'running') is distinct from 'boolean' then
   raise exception 'host observation invalid' using errcode='22023';end if;
  insert into runtime.nexloop_host_unit_samples(tick_id,host_role,unit_name,kind,instance_id,restart_count,running)
   values(v_tick,v_host,u->>'name',u->>'kind',u->>'instance_id',(u->>'restart_count')::bigint,(u->>'running')::boolean);
  n_units:=n_units+1;
 end loop;
 for r in select value from jsonb_array_elements(p_entry->'timers') loop
  if jsonb_typeof(r) is distinct from 'object'
   or exists(select 1 from jsonb_object_keys(r) k where k not in ('name','run_started_at','run_finished_at','result','scheduled','last_trigger_at'))
   or jsonb_typeof(r->'scheduled') is distinct from 'boolean' then
   raise exception 'host observation invalid' using errcode='22023';end if;
  v_started:=(r->>'run_started_at')::timestamptz;v_trigger:=(r->>'last_trigger_at')::timestamptz;
  -- C1: the scheduled round of a trigger is the first run recorded for it, started within a minute after it; a later run of
  -- the same trigger (a manual rerun inside the grace, seen by a later tick) is not.
  v_scheduled:=(r->>'scheduled')::boolean and v_trigger is not null and v_started>=v_trigger-interval '1 second'
   and v_started<=v_trigger+interval '1 minute'
   and not exists(select 1 from runtime.nexloop_host_timer_runs x where x.host_role=v_host and x.timer_name=r->>'name' and x.last_trigger_at=v_trigger
     and x.run_started_at<>v_started);
  -- The ledger is append-only: a run already recorded (the probe sees the same last run on every tick) is not changed.
  insert into runtime.nexloop_host_timer_runs(host_role,timer_name,run_started_at,run_finished_at,result,scheduled,last_trigger_at)
   values(v_host,r->>'name',v_started,(r->>'run_finished_at')::timestamptz,r->>'result',v_scheduled,v_trigger)
   on conflict(host_role,timer_name,run_started_at) do nothing;
  get diagnostics c=row_count;n_runs:=n_runs+c;
 end loop;
 -- Bounded history: samples and ticks older than 8 days go (the windows are 1 hour and 24 hours); the run ledger keeps 90 days.
 delete from runtime.nexloop_host_unit_samples where host_role=v_host and sampled_at<clock_timestamp()-interval '8 days';
 delete from runtime.nexloop_host_probe_ticks where host_role=v_host and observed_at<clock_timestamp()-interval '8 days'
  and tick_id<>(select min(t.tick_id) from runtime.nexloop_host_probe_ticks t where t.host_role=v_host);  -- the first tick anchors fail-closed ages
 delete from runtime.nexloop_host_timer_runs x where x.host_role=v_host and x.run_finished_at<clock_timestamp()-interval '90 days'
  and x.run_started_at<>(select max(y.run_started_at) from runtime.nexloop_host_timer_runs y where y.host_role=x.host_role and y.timer_name=x.timer_name
   and y.scheduled and y.result='success');  -- the last scheduled success stays (its age is the freshness)
 return jsonb_build_object('recorded',true,'host_role',v_host,'units',n_units,'new_runs',n_runs);
end $$;
alter function authz.nexloop_record_host_observation(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_record_host_observation(jsonb) from public;
grant execute on function authz.nexloop_record_host_observation(jsonb) to nexloop_host_recorder_app_host,nexloop_host_recorder_data_host;

create function runtime.nexloop_host_metrics() returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare now_at timestamptz:=clock_timestamp();v_restarts jsonb;v_age jsonb;v_fail jsonb;v_probe jsonb;v_blind jsonb;v_missing jsonb;
begin
 -- Restarts in the last hour: consecutive samples of one unit (the sample just before the hour is the baseline); a positive
 -- count delta within one instance counts that many, an instance change (container recreated, service re-invoked with a reset
 -- counter) counts one; a negative delta within one instance (counter reset) counts nothing.
 select coalesce(jsonb_object_agg(k,n),'{}'::jsonb) into v_restarts from (
  select s.host_role||':'||s.unit_name k,sum(case when s.prev_instance is null then 0 when s.prev_instance<>s.instance_id then 1
     else greatest(s.restart_count-s.prev_count,0) end)::bigint n
  from (select x.*,lag(x.instance_id) over w prev_instance,lag(x.restart_count) over w prev_count
        from runtime.nexloop_host_unit_samples x
        where x.sampled_at>=now_at-interval '1 hour'
           or x.sample_id=(select y.sample_id from runtime.nexloop_host_unit_samples y where y.host_role=x.host_role and y.unit_name=x.unit_name
                           and y.sampled_at<now_at-interval '1 hour' order by y.sampled_at desc limit 1)
        window w as (partition by x.host_role,x.unit_name order by x.sampled_at,x.sample_id)) s
  where s.sampled_at>=now_at-interval '1 hour'
  group by 1) z;
 -- Timers: every timer the latest tick of its host expects; age since the last scheduled success, else since it was first expected.
 select coalesce(jsonb_object_agg(k,a),'{}'::jsonb),coalesce(jsonb_object_agg(k,f),'{}'::jsonb) into v_age,v_fail from (
  select e.host_role||':'||e.timer_name k,
   round(extract(epoch from now_at-coalesce(
     (select max(r.run_finished_at) from runtime.nexloop_host_timer_runs r where r.host_role=e.host_role and r.timer_name=e.timer_name and r.scheduled and r.result='success'),
     (select min(t.observed_at) from runtime.nexloop_host_probe_ticks t where t.host_role=e.host_role and e.timer_name=any(t.expected_timers)))))::bigint a,
   (select count(*) from runtime.nexloop_host_timer_runs r where r.host_role=e.host_role and r.timer_name=e.timer_name and r.scheduled
     and r.result='failure' and r.run_finished_at>=now_at-interval '24 hours') f
  from (select distinct l.host_role,unnest(l.expected_timers) timer_name
        from (select distinct on (t.host_role) t.host_role,t.expected_timers from runtime.nexloop_host_probe_ticks t order by t.host_role,t.observed_at desc,t.tick_id desc) l) e) z;
 select coalesce(jsonb_object_agg(host_role,round(extract(epoch from now_at-last_at))::bigint),'{}'::jsonb),
        coalesce(jsonb_object_agg(host_role,case when blind then 1 else 0 end),'{}'::jsonb) into v_probe,v_blind
  from (select distinct on (t.host_role) t.host_role,t.observed_at last_at,t.blind from runtime.nexloop_host_probe_ticks t order by t.host_role,t.observed_at desc,t.tick_id desc) l;
 -- I2: every unit the host's last tick expected, 1 when that tick holds no sample of it.
 select coalesce(jsonb_object_agg(l.host_role||':'||e.unit_name,case when exists(select 1 from runtime.nexloop_host_unit_samples s
     where s.tick_id=l.tick_id and s.unit_name=e.unit_name) then 0 else 1 end),'{}'::jsonb) into v_missing
  from (select distinct on (t.host_role) t.host_role,t.tick_id,t.expected_units from runtime.nexloop_host_probe_ticks t order by t.host_role,t.observed_at desc,t.tick_id desc) l
  cross join lateral unnest(l.expected_units) e(unit_name);
 return jsonb_build_object('restarts_1h',v_restarts,'timer_success_age_seconds',v_age,'timer_scheduled_failures_24h',v_fail,
  'probe_age_seconds',v_probe,'blind',v_blind,'missing_units',v_missing);
end $$;
alter function runtime.nexloop_host_metrics() owner to nexloop_owner;
revoke all on function runtime.nexloop_host_metrics() from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_host_health;
revoke all on function runtime.nexloop_metrics_snapshot_before_host_health(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_host_health(p_tenant,p_world)||jsonb_build_object('host_health',runtime.nexloop_host_metrics());
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

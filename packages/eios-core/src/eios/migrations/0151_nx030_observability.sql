-- NX-030 (temporary number 0151): audit metrics and alerts — design docs/implementation/NX-030-design.md, D1–D8 settled.
--
-- * PostgreSQL is the single authority for metrics, alerts and audit; no new external dependency.
-- * runtime.nexloop_metrics_snapshot(tenant, world): one owner-only computation of every metric (no raw text, no names, no
--   property values; codes, counts, ages, ratios). The evaluator, the workbench and the optional loopback export all read it.
-- * D8: runtime.nexloop_dispatch_refusals, written by the transaction that already persists the refusal outcome: the Run path in
--   the queue `finish` of the refused task (authz.nexloop_queue_command, renamed and kept, wrapped to add only this insert), the
--   effect path by the effect worker right after the refused admission (authz.nexloop_record_effect_refusal). Neither changes a
--   dispatch decision; neither touches the dispatch wrappers.
-- * Process samples (guard latency / 2 s timeouts, Agent Host concurrency, connection pools) written once a minute by the
--   processes that own them; deployment-level, no tenant data.
-- * Alert rules: versioned trusted configuration (deploy/configuration/alert-rules.v1.json, D3). The evaluator (service, signed
--   nexloop.alert.evaluate:1) evaluates only world 'real' (D6); state + append-only events, deduplicated per rule and selector.
-- * D4: login role nexloop_metrics that can only execute the export function (no table, no raw text).
-- * D7: the workbench reads metrics, alerts and the human-action audit through a separate function; 0117 is not changed.

-- 1. Dispatch refusals (D8) ------------------------------------------------------------------------------------------------
create table control.nexloop_dispatch_refusal_codes (
 code text primary key check(code~'^NX[A-Z][0-9]{2}$'),reason text not null unique check(reason~'^[a-z_]{1,64}$')
);
insert into control.nexloop_dispatch_refusal_codes values
 ('NXC01','control_paused'),('NXC02','control_revision_stale'),('NXC03','goal_version_stale'),('NXC04','object_revision_stale'),
 ('NXC05','contact_restricted'),('NXC06','taken_over'),('NXB01','budget_exhausted'),('NXB02','budget_consumption_conflict'),
 ('NXB03','budget_unconfigured'),('NXM01','observation_conflict');
alter table control.nexloop_dispatch_refusal_codes owner to nexloop_owner;
create trigger nx030_append_only before update or delete on control.nexloop_dispatch_refusal_codes for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_dispatch_refusal_codes from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

create table runtime.nexloop_dispatch_refusals (
 refusal_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 path text not null check(path in ('run','effect')),code text not null references control.nexloop_dispatch_refusal_codes(code),
 task_id text,intent_id uuid,recorded_at timestamptz not null default clock_timestamp(),
 check((path='run')=(task_id is not null) and (path='effect')=(intent_id is not null))
);
create index nexloop_dispatch_refusals_time on runtime.nexloop_dispatch_refusals(tenant_id,world,recorded_at desc);
create index nexloop_dispatch_refusals_intent on runtime.nexloop_dispatch_refusals(tenant_id,world,intent_id,code,recorded_at desc) where intent_id is not null;

-- 2. Process samples (M08–M10; deployment level) ---------------------------------------------------------------------------
create table runtime.nexloop_process_samples (
 sample_id bigint generated always as identity primary key,kind text not null check(kind in ('guard','host','pool')),
 service text not null check(service~'^[a-z][a-z0-9_-]{0,63}$'),instance text not null check(instance~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 window_start timestamptz not null,window_seconds integer not null check(window_seconds between 1 and 3600),
 sample jsonb not null check(jsonb_typeof(sample)='object'),recorded_at timestamptz not null default clock_timestamp()
);
create index nexloop_process_samples_time on runtime.nexloop_process_samples(kind,recorded_at desc);
-- Hourly rollups (D5: minute samples 7 days, hourly 90 days).
create table runtime.nexloop_process_sample_hours (
 kind text not null,service text not null,hour timestamptz not null,samples integer not null,summary jsonb not null,
 primary key(kind,service,hour)
);

-- 3. Alert rules, state and events ----------------------------------------------------------------------------------------
create table control.nexloop_alert_rules (
 tenant_id text not null,version integer not null check(version between 1 and 100000),rules jsonb not null check(jsonb_typeof(rules)='array'),
 digest text not null check(digest~'^[a-f0-9]{64}$'),recorded_by text not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,version)
);
create table control.nexloop_alert_state (
 tenant_id text not null,world text not null,dedupe_key text not null check(length(dedupe_key) between 1 and 300),
 rule_id text not null,severity text not null check(severity in ('warning','critical')),selector text,
 breached_since timestamptz,firing boolean not null default false,first_fired_at timestamptz,last_fired_at timestamptz,fire_count integer not null default 0,
 last_value numeric,last_evaluated_at timestamptz not null,rules_version integer not null,
 primary key(tenant_id,world,dedupe_key)
);
create table control.nexloop_alert_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,dedupe_key text not null,
 rule_id text not null,severity text not null check(severity in ('warning','critical')),kind text not null check(kind in ('firing','resolved')),
 selector text,value numeric,threshold numeric,rules_version integer not null,recorded_at timestamptz not null default clock_timestamp()
);
create index nexloop_alert_events_time on control.nexloop_alert_events(tenant_id,world,recorded_at desc);

do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_dispatch_refusals','control.nexloop_alert_rules','control.nexloop_alert_state','control.nexloop_alert_events'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
 end loop;
 foreach t in array array['runtime.nexloop_process_samples','runtime.nexloop_process_sample_hours'] loop
  -- Deployment-level operational counters (no tenant column): forced RLS with an owner-only policy, no application privileges.
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy deployment_counters on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;
create trigger nx030_append_only before update or delete on runtime.nexloop_dispatch_refusals for each row execute function control.nexloop_nx022_append_only();
create trigger nx030_append_only before update or delete on control.nexloop_alert_rules for each row execute function control.nexloop_nx022_append_only();
create trigger nx030_append_only before update or delete on control.nexloop_alert_events for each row execute function control.nexloop_nx022_append_only();

-- 4. Run path: the refused task's finish (rename and keep; the wrapper only adds the refusal row) ------------------------------
alter function authz.nexloop_queue_command(text,text,text,text,text) rename to nexloop_queue_command_before_refusals_v0150;
revoke all on function authz.nexloop_queue_command_before_refusals_v0150(text,text,text,text,text) from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker;
create function authz.nexloop_queue_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v jsonb;p jsonb:=p_payload::jsonb;v_code text;v_tenant text:=(p_text::jsonb)->>'tenant_id';
begin
 v:=authz.nexloop_queue_command_before_refusals_v0150(p_digest,p_world,p_text,p_signature,p_payload);
 if p->>'verb'='finish' and jsonb_typeof(p->'result')='object' then
  select code into v_code from control.nexloop_dispatch_refusal_codes where reason=p->'result'->>'code';
  -- Same transaction as the persisted outcome; a replayed finish (no new transition) records nothing again.
  if v_code is not null and not exists(select 1 from runtime.nexloop_dispatch_refusals r where r.tenant_id=v_tenant and r.world=p_world
    and r.path='run' and r.task_id=p->>'task_id') then
   perform set_config('eios.tenant_id',v_tenant,true);
   insert into runtime.nexloop_dispatch_refusals(tenant_id,world,path,code,task_id) values(v_tenant,p_world,'run',v_code,p->>'task_id');
  end if;
 end if;
 return v;
end $$;
alter function authz.nexloop_queue_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_queue_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_queue_command(text,text,text,text,text) to nexloop_api,nexloop_scheduler,nexloop_domain_worker;

-- 5. Effect path: the executor records the refused admission of its own leased intent ---------------------------------------
create function authz.nexloop_record_effect_refusal(p_digest text,p_world text,p_intent uuid,p_code text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;
begin
 if session_user is distinct from 'nexloop_action_worker' or not exists(select 1 from control.nexloop_dispatch_refusal_codes where code=p_code) then
  raise exception 'effect refusal unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from runtime.nexloop_effect_intents i where i.intent_id=p_intent and i.tenant_id=v_tenant and i.world=p_world and i.executor_principal=v_principal) then
  raise exception 'effect refusal unavailable' using errcode='42501';end if;
 -- The executor retries a refused intent on every claim: one row per intent and code within ten minutes.
 if exists(select 1 from runtime.nexloop_dispatch_refusals r where r.tenant_id=v_tenant and r.world=p_world and r.intent_id=p_intent and r.code=p_code
   and r.recorded_at>clock_timestamp()-interval '10 minutes') then return false;end if;
 insert into runtime.nexloop_dispatch_refusals(tenant_id,world,path,code,intent_id) values(v_tenant,p_world,'effect',p_code,p_intent);
 return true;
end $$;
alter function authz.nexloop_record_effect_refusal(text,text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_record_effect_refusal(text,text,uuid,text) from public;
grant execute on function authz.nexloop_record_effect_refusal(text,text,uuid,text) to nexloop_action_worker;

-- 6. Process sample writer (operational counters only; shape-checked, no free text) -----------------------------------------
create function authz.nexloop_record_process_sample(p_kind text,p_service text,p_instance text,p_window_start timestamptz,p_window_seconds integer,p_sample jsonb)
 returns void language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare allowed text[];k text;
begin
 allowed:=case p_kind
  when 'guard' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'host' then array['active_runs','waiting','max_active_runs','admission_timeouts','busy_refusals','runs_started','available']
  when 'pool' then array['pool_size','pool_available','requests_waiting','requests_num','requests_errors','requests_wait_ms','usage_ms','backend_busy','max_size']
  end;
 if allowed is null or session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler')
  or jsonb_typeof(p_sample) is distinct from 'object' or p_window_seconds not between 1 and 3600
  or p_window_start>clock_timestamp()+interval '1 minute' or p_window_start<clock_timestamp()-interval '1 day' then
  raise exception 'process sample invalid' using errcode='22023';end if;
 for k in select jsonb_object_keys(p_sample) loop
  if not k=any(allowed) or jsonb_typeof(p_sample->k) not in ('number','boolean') then raise exception 'process sample invalid' using errcode='22023';end if;
 end loop;
 insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample) values(p_kind,p_service,p_instance,p_window_start,p_window_seconds,p_sample);
end $$;
alter function authz.nexloop_record_process_sample(text,text,text,timestamptz,integer,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_record_process_sample(text,text,text,timestamptz,integer,jsonb) from public;
grant execute on function authz.nexloop_record_process_sample(text,text,text,timestamptz,integer,jsonb) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- Rollup and retention (D5), run by the evaluator each pass; idempotent.
create function runtime.nexloop_observability_maintain() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_rolled integer;v_minutes integer;v_hours integer;
begin
 insert into runtime.nexloop_process_sample_hours(kind,service,hour,samples,summary)
 select g.kind,g.service,g.h,
  (select count(*) from runtime.nexloop_process_samples s where s.kind=g.kind and s.service=g.service and date_trunc('hour',s.window_start)=g.h),
  jsonb_build_object(
   'max',(select coalesce(jsonb_object_agg(k,mx),'{}'::jsonb) from (select k,max((s.sample->>k)::numeric) mx from runtime.nexloop_process_samples s,jsonb_object_keys(s.sample) k
     where s.kind=g.kind and s.service=g.service and date_trunc('hour',s.window_start)=g.h and jsonb_typeof(s.sample->k)='number' group by k) m),
   'sum',(select coalesce(jsonb_object_agg(k,sm),'{}'::jsonb) from (select k,sum((s.sample->>k)::numeric) sm from runtime.nexloop_process_samples s,jsonb_object_keys(s.sample) k
     where s.kind=g.kind and s.service=g.service and date_trunc('hour',s.window_start)=g.h and jsonb_typeof(s.sample->k)='number' group by k) t))
 from (select distinct kind,service,date_trunc('hour',window_start) h from runtime.nexloop_process_samples where window_start<date_trunc('hour',clock_timestamp())) g
 on conflict (kind,service,hour) do update set samples=excluded.samples,summary=excluded.summary;
 get diagnostics v_rolled=row_count;
 delete from runtime.nexloop_process_samples where recorded_at<clock_timestamp()-interval '7 days';get diagnostics v_minutes=row_count;
 delete from runtime.nexloop_process_sample_hours where hour<clock_timestamp()-interval '90 days';get diagnostics v_hours=row_count;
 return jsonb_build_object('rolled',v_rolled,'purged_minutes',v_minutes,'purged_hours',v_hours);
end $$;
alter function runtime.nexloop_observability_maintain() owner to nexloop_owner;
revoke all on function runtime.nexloop_observability_maintain() from public;

-- 7. The snapshot (one definition of every metric) ------------------------------------------------------------------------------------------------
create function runtime.nexloop_process_section(p_kind text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select case when count(*)=0 then jsonb_build_object('status','unavailable') else jsonb_build_object('status','ok','window_seconds',300,'samples',count(*),
  'by_service',(select jsonb_object_agg(service,v) from (select service,jsonb_build_object(
     'sum',(select jsonb_object_agg(k,sm) from (select k,sum((x.sample->>k)::numeric) sm from runtime.nexloop_process_samples x,jsonb_object_keys(x.sample) k
       where x.kind=p_kind and x.service=s2.service and x.recorded_at>clock_timestamp()-interval '5 minutes' and jsonb_typeof(x.sample->k)='number' group by k) a),
     'max',(select jsonb_object_agg(k,mx) from (select k,max((x.sample->>k)::numeric) mx from runtime.nexloop_process_samples x,jsonb_object_keys(x.sample) k
       where x.kind=p_kind and x.service=s2.service and x.recorded_at>clock_timestamp()-interval '5 minutes' and jsonb_typeof(x.sample->k)='number' group by k) b)) v
    from (select distinct service from runtime.nexloop_process_samples where kind=p_kind and recorded_at>clock_timestamp()-interval '5 minutes') s2) z)) end
 from runtime.nexloop_process_samples where kind=p_kind and recorded_at>clock_timestamp()-interval '5 minutes' $$;
alter function runtime.nexloop_process_section(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_process_section(text) from public;

create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;now_at timestamptz:=clock_timestamp();guard jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 guard:=runtime.nexloop_process_section('guard');
 if guard->>'status'='ok' then
  guard:=guard||jsonb_build_object('timeout_rate',(select case when coalesce(sum((s.value->'sum'->>'calls')::numeric),0)=0 then 0
     else round(sum((s.value->'sum'->>'timeouts')::numeric)/sum((s.value->'sum'->>'calls')::numeric),4) end from jsonb_each(guard->'by_service') s),
   'p95_ms_max',(select max((s.value->'max'->>'p95_ms')::numeric) from jsonb_each(guard->'by_service') s));
 end if;
 v:=jsonb_build_object('tenant_id',p_tenant,'world',p_world,'computed_at',now_at,
  -- M01 dispatch refusals by code (D8 table).
  'refusals',jsonb_build_object(
   'last_5m',coalesce((select jsonb_object_agg(code,n) from (select code,count(*) n from runtime.nexloop_dispatch_refusals where tenant_id=p_tenant and world=p_world and recorded_at>now_at-interval '5 minutes' group by code) x),'{}'::jsonb),
   'last_hour',coalesce((select jsonb_object_agg(code,n) from (select code,count(*) n from runtime.nexloop_dispatch_refusals where tenant_id=p_tenant and world=p_world and recorded_at>now_at-interval '1 hour' group by code) x),'{}'::jsonb)),
  -- M02/M03 effect intents.
  'effects',jsonb_build_object(
   'by_state',coalesce((select jsonb_object_agg(state,n) from (select state,count(*) n from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world group by state) x),'{}'::jsonb),
   'unknown',(select jsonb_build_object('count',count(*),'oldest_age_seconds',floor(extract(epoch from now_at-min(created_at)))::bigint,
      'over_24h',count(*) filter (where created_at<now_at-interval '24 hours')) from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world and state='unknown')),
  -- M04 replies.
  'replies',jsonb_build_object(
   'pending',(select jsonb_build_object('count',count(*),'oldest_age_seconds',floor(extract(epoch from now_at-min(created_at)))::bigint)
     from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='reply-due' and status='pending'),
   'escalations_last_hour',coalesce((select jsonb_object_agg(reason,n) from (select reason,count(*) n from control.nexloop_reply_escalations where tenant_id=p_tenant and world=p_world and escalated_at>now_at-interval '1 hour' group by reason) x),'{}'::jsonb),
   'escalations_total',(select count(*) from control.nexloop_reply_escalations where tenant_id=p_tenant and world=p_world)),
  -- M05 commitments.
  'commitments',jsonb_build_object(
   'exceptions',coalesce((select jsonb_object_agg(reason,n) from (select reason,count(*) n from runtime.nexloop_commitment_exceptions where tenant_id=p_tenant and world=p_world group by reason) x),'{}'::jsonb),
   'exceptions_last_hour',coalesce((select jsonb_object_agg(reason,n) from (select reason,count(*) n from runtime.nexloop_commitment_exceptions where tenant_id=p_tenant and world=p_world and raised_at>now_at-interval '1 hour' group by reason) x),'{}'::jsonb)),
  -- M06/M07 queues, work feeds and the extraction feed.
  'queues',coalesce((select jsonb_object_agg(queue,q) from (select queue,jsonb_build_object(
     'pending',count(*) filter (where status='pending'),'retry_wait',count(*) filter (where status='retry_wait'),'running',count(*) filter (where status='running'),
     'dead_lettered',count(*) filter (where status='dead_lettered'),'dead_lettered_last_hour',count(*) filter (where status='dead_lettered' and updated_at>now_at-interval '1 hour'),
     'oldest_pending_seconds',floor(extract(epoch from now_at-min(available_at) filter (where status in ('pending','retry_wait') and available_at<=now_at)))::bigint) q
    from runtime.jobs where tenant_id=p_tenant and world=p_world and queue<>'' group by queue) x),'{}'::jsonb),
  'feeds',coalesce((select jsonb_object_agg(feed,q) from (select feed,jsonb_build_object('pending',count(*) filter (where status='pending'),
     'dead_lettered',count(*) filter (where status='dead_lettered'),'dead_lettered_last_hour',count(*) filter (where status='dead_lettered' and changed_at>now_at-interval '1 hour'),
     'oldest_pending_seconds',floor(extract(epoch from now_at-min(changed_at) filter (where status='pending')))::bigint) q
    from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world group by feed) x),'{}'::jsonb),
  'extraction',(select jsonb_build_object('pending_messages',count(*),'oldest_pending_seconds',floor(extract(epoch from now_at-min(created_at)))::bigint)
    from runtime.nexloop_claim_extraction_feed where tenant_id=p_tenant and world=p_world and task_id is null),
  -- M08–M10 process samples (deployment level, the last five minutes).
  'guard',guard,'host',runtime.nexloop_process_section('host'),'pool',runtime.nexloop_process_section('pool'),
  -- Database-wide backends (pg_stat_database is readable without extra privileges; per-role detail needs pg_read_all_stats).
  'connections',(select jsonb_build_object('total',coalesce(numbackends,0)) from pg_catalog.pg_stat_database where datname=current_database()),
  -- M11 cost (D6: data_mode separated; amounts as text).
  'cost',jsonb_build_object(
   'budgets',coalesce((select jsonb_object_agg(b.budget_kind,jsonb_build_object('limit',b.limit_amount::text,'unit',b.unit,
      'consumed',(select coalesce(sum(u.amount),0)::text from control.nexloop_budget_consumption u where u.tenant_id=b.tenant_id and u.world=b.world and u.budget_kind=b.budget_kind
        and u.recorded_at>=b.period_start and u.recorded_at<b.period_end),
      'ratio',case when b.limit_amount=0 then null else round((select coalesce(sum(u.amount),0) from control.nexloop_budget_consumption u where u.tenant_id=b.tenant_id and u.world=b.world
        and u.budget_kind=b.budget_kind and u.recorded_at>=b.period_start and u.recorded_at<b.period_end)/b.limit_amount,4) end))
     from control.nexloop_budget_limits b where b.tenant_id=p_tenant and b.world=p_world),'{}'::jsonb),
   'last_24h',coalesce((select jsonb_agg(c.v order by c.cost_kind,c.data_mode,c.currency) from (select cost_kind,data_mode,currency,
      jsonb_build_object('cost_kind',cost_kind,'data_mode',data_mode,'currency',currency,'amount',coalesce(sum(amount),0)::text,
       'entries',count(*),'unpriced',count(*) filter (where amount is null)) v
     from runtime.nexloop_cost_entries where tenant_id=p_tenant and world=p_world and occurred_at>now_at-interval '24 hours' group by cost_kind,data_mode,currency) c),'[]'::jsonb)),
  -- M12 backups belong to NX-035: unavailable until its manifest table exists, never zero.
  'backup',case when to_regclass('control.nexloop_backup_runs') is null then jsonb_build_object('status','unavailable') else jsonb_build_object('status','unavailable','reason','not_wired') end,
  -- M13 the evaluator itself.
  'evaluator',(select jsonb_build_object('last_evaluated_at',max(last_evaluated_at),'rules_version',max(rules_version),
     'stale',coalesce(max(last_evaluated_at)<now_at-interval '5 minutes',true)) from control.nexloop_alert_state where tenant_id=p_tenant and world=p_world));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- 8. Alert rules (trusted configuration, D3) --------------------------------------------------------------------------------
create function control.nexloop_configure_alert_rules(p_tenant text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r jsonb;cur integer;ids text[]:='{}';
begin
 if session_user<>'nexloop_configurator' then raise exception 'alert rules are trusted configuration only' using errcode='42501';end if;
 if p_manifest->>'schema_version' is distinct from 'nexloop-alert-rules/1' or jsonb_typeof(p_manifest->'version') is distinct from 'number'
  or jsonb_typeof(p_manifest->'rules') is distinct from 'array' or jsonb_array_length(p_manifest->'rules') not between 1 and 200 then
  raise exception 'alert rules invalid' using errcode='22023';end if;
 for r in select value from jsonb_array_elements(p_manifest->'rules') loop
  if jsonb_typeof(r) is distinct from 'object' or not (r ?& array['rule_id','metric','condition','threshold','for_seconds','severity','purpose'])
   or exists(select 1 from jsonb_object_keys(r) k where k not in ('rule_id','metric','selector','condition','threshold','for_seconds','severity','purpose'))
   or r->>'rule_id'!~'^[a-z][a-z0-9_]{0,63}$' or r->>'metric'!~'^[a-z_0-9]+(\.([a-z_0-9]+|\*))*$' or r->>'condition' not in ('gt','ge')
   or (length(r->>'metric')-length(replace(r->>'metric','*','')))>1 or (strpos(r->>'metric','*')>0 and r ? 'selector')
   or (strpos(r->>'metric','*')>0 and strpos(r->>'metric','.*.')=0)
   or jsonb_typeof(r->'threshold') is distinct from 'number' or jsonb_typeof(r->'for_seconds') is distinct from 'number'
   or (r->>'for_seconds')::integer not between 0 and 86400 or r->>'severity' not in ('warning','critical')
   or (r ? 'selector' and coalesce(r->>'selector','')!~'^(\*|[A-Za-z0-9_.:-]{1,64})$') or length(coalesce(r->>'purpose','')) not between 1 and 500
   or r->>'rule_id'=any(ids) then
   raise exception 'alert rule invalid: %',r->>'rule_id' using errcode='22023';end if;
  ids:=ids||(r->>'rule_id');
 end loop;
 perform set_config('eios.tenant_id',p_tenant,true);
 select max(version) into cur from control.nexloop_alert_rules where tenant_id=p_tenant;
 if cur is not null and (p_manifest->>'version')::integer<=cur then
  if exists(select 1 from control.nexloop_alert_rules where tenant_id=p_tenant and version=(p_manifest->>'version')::integer and rules=p_manifest->'rules') then
   perform set_config('eios.tenant_id',coalesce(prior,''),true);return jsonb_build_object('configured',true,'version',(p_manifest->>'version')::integer,'replay',true);end if;
  raise exception 'alert rules version must increase' using errcode='22023';end if;
 insert into control.nexloop_alert_rules(tenant_id,version,rules,digest,recorded_by)
  values(p_tenant,(p_manifest->>'version')::integer,p_manifest->'rules',encode(sha256(convert_to((p_manifest->'rules')::text,'UTF8')),'hex'),session_user);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('configured',true,'version',(p_manifest->>'version')::integer,'replay',false);
end $$;
alter function control.nexloop_configure_alert_rules(text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_alert_rules(text,jsonb) from public;
grant execute on function control.nexloop_configure_alert_rules(text,jsonb) to nexloop_configurator;

-- 9. Evaluator (service, nexloop.alert.evaluate:1; only world 'real' alerts, D6) ---------------------------------------------
create function authz.nexloop_alert_evaluate(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-alert-evaluate-v1','eios:action:nexloop.alert.evaluate:1',
  array['nexloop_domain_worker']);
 snap jsonb;rs control.nexloop_alert_rules;r jsonb;base jsonb;item record;v_value numeric;v_key text;st control.nexloop_alert_state;
 v_breach boolean;now_at timestamptz:=clock_timestamp();seen text[]:='{}';v_fired integer:=0;v_resolved integer:=0;v_maint jsonb;
begin
 if p_world is distinct from 'real' then return jsonb_build_object('evaluated',false,'reason','alerts_real_world_only');end if;
 v_maint:=runtime.nexloop_observability_maintain();
 perform set_config('eios.tenant_id',t,true);
 select * into rs from control.nexloop_alert_rules where tenant_id=t order by version desc limit 1;
 if rs.tenant_id is null then return jsonb_build_object('evaluated',false,'reason','no_rules','maintenance',v_maint);end if;
 snap:=runtime.nexloop_metrics_snapshot(t,p_world);
 perform set_config('eios.tenant_id',t,true);
 for r in select value from jsonb_array_elements(rs.rules) loop
  -- metric: a dotted path; one '*' segment iterates the children of that object (the child key is the selector).
  base:=snap#>string_to_array(split_part(r->>'metric','.*',1),'.');
  for item in
   select e.key sel,e.value#>string_to_array(substr(r->>'metric',strpos(r->>'metric','.*.')+3),'.') val
    from jsonb_each(case when strpos(r->>'metric','.*.')>0 and jsonb_typeof(base)='object' then base else '{}'::jsonb end) e
    where jsonb_typeof(e.value#>string_to_array(substr(r->>'metric',strpos(r->>'metric','.*.')+3),'.'))='number'
   union all select null::text,base where strpos(r->>'metric','*')=0 and coalesce(r->>'selector','')='' and jsonb_typeof(base)='number'
   union all select e.key,e.value from jsonb_each(case when jsonb_typeof(base)='object' then base else '{}'::jsonb end) e
    where strpos(r->>'metric','*')=0 and r->>'selector'='*' and jsonb_typeof(e.value)='number'
   union all select r->>'selector',base->(r->>'selector') where strpos(r->>'metric','*')=0 and coalesce(r->>'selector','') not in ('','*') and jsonb_typeof(base)='object' and jsonb_typeof(base->(r->>'selector'))='number'
  loop
   v_value:=(item.val#>>'{}')::numeric;v_key:=r->>'rule_id'||coalesce(':'||item.sel,'');seen:=seen||v_key;
   v_breach:=case r->>'condition' when 'gt' then v_value>(r->>'threshold')::numeric else v_value>=(r->>'threshold')::numeric end;
   select * into st from control.nexloop_alert_state where tenant_id=t and world=p_world and dedupe_key=v_key for update;
   if not found then
    insert into control.nexloop_alert_state(tenant_id,world,dedupe_key,rule_id,severity,selector,last_evaluated_at,rules_version)
     values(t,p_world,v_key,r->>'rule_id',r->>'severity',item.sel,now_at,rs.version) returning * into st;
   end if;
   if v_breach then
    st.breached_since:=coalesce(st.breached_since,now_at);
    if not st.firing and st.breached_since<=now_at-make_interval(secs=>(r->>'for_seconds')::integer) then
     st.firing:=true;st.first_fired_at:=coalesce(st.first_fired_at,now_at);st.fire_count:=st.fire_count+1;st.last_fired_at:=now_at;v_fired:=v_fired+1;
     insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
      values(t,p_world,v_key,r->>'rule_id',r->>'severity','firing',item.sel,v_value,(r->>'threshold')::numeric,rs.version);
    elsif st.firing then st.last_fired_at:=now_at;end if;
   else
    if st.firing then
     v_resolved:=v_resolved+1;
     insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
      values(t,p_world,v_key,r->>'rule_id',r->>'severity','resolved',item.sel,v_value,(r->>'threshold')::numeric,rs.version);
    end if;
    st.firing:=false;st.breached_since:=null;
   end if;
   update control.nexloop_alert_state set breached_since=st.breached_since,firing=st.firing,first_fired_at=st.first_fired_at,last_fired_at=st.last_fired_at,
    fire_count=st.fire_count,last_value=v_value,last_evaluated_at=now_at,rules_version=rs.version,severity=r->>'severity'
    where tenant_id=t and world=p_world and dedupe_key=v_key;
  end loop;
 end loop;
 -- A selector or metric that disappeared (for example a queue drained to no rows) resolves its alert.
 for st in select * from control.nexloop_alert_state where tenant_id=t and world=p_world and not (dedupe_key=any(seen)) for update loop
  if st.firing then
   v_resolved:=v_resolved+1;
   insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
    values(t,p_world,st.dedupe_key,st.rule_id,st.severity,'resolved',st.selector,null,null,rs.version);
  end if;
  update control.nexloop_alert_state set firing=false,breached_since=null,last_evaluated_at=now_at,rules_version=rs.version where tenant_id=t and world=p_world and dedupe_key=st.dedupe_key;
 end loop;
 return jsonb_build_object('evaluated',true,'rules_version',rs.version,'fired',v_fired,'resolved',v_resolved,
  'firing',(select count(*) from control.nexloop_alert_state where tenant_id=t and world=p_world and firing),'maintenance',v_maint);
end $$;
alter function authz.nexloop_alert_evaluate(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_alert_evaluate(text,text,text,text,text) from public;
grant execute on function authz.nexloop_alert_evaluate(text,text,text,text,text) to nexloop_domain_worker;

-- 10. Workbench reads (D7: a separate function; 0117 unchanged) --------------------------------------------------------------
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;v_limit integer;
 v_action text:='eios:action:nexloop.workbench.read:1';v_verb text:=c->>'verb';
begin
 if v_verb not in ('metrics','alerts','human_actions') or octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or (v_verb='human_actions' and w->>'role'<>'owner') then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 v_limit:=coalesce((c->>'limit')::integer,100);
 if v_limit not between 1 and 500 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 if v_verb='metrics' then
  return runtime.nexloop_metrics_snapshot(v_tenant,p_world);
 elsif v_verb='alerts' then
  return jsonb_build_object('real_world_only',true,
   'firing',coalesce((select jsonb_agg(jsonb_build_object('dedupe_key',s.dedupe_key,'rule_id',s.rule_id,'severity',s.severity,'selector',s.selector,'value',s.last_value::text,
      'first_fired_at',s.first_fired_at,'last_fired_at',s.last_fired_at,'fire_count',s.fire_count) order by s.severity,s.first_fired_at)
     from control.nexloop_alert_state s where s.tenant_id=v_tenant and s.world=p_world and s.firing),'[]'::jsonb),
   'events',coalesce((select jsonb_agg(x.v order by x.event_id desc) from (select e.event_id,jsonb_build_object('event_id',e.event_id,'rule_id',e.rule_id,'severity',e.severity,
      'kind',e.kind,'selector',e.selector,'value',e.value::text,'threshold',e.threshold::text,'recorded_at',e.recorded_at) v
     from control.nexloop_alert_events e where e.tenant_id=v_tenant and e.world=p_world order by e.event_id desc limit v_limit) x),'[]'::jsonb),
   'evaluator',(select jsonb_build_object('last_evaluated_at',max(last_evaluated_at),'rules_version',max(rules_version),
      'stale',coalesce(max(last_evaluated_at)<clock_timestamp()-interval '5 minutes',true)) from control.nexloop_alert_state where tenant_id=v_tenant and world=p_world));
 end if;
 -- human_actions (owner): who did which governed human Action when, plus ADR-025 reads; IDs, kinds and times only.
 return jsonb_build_object('items',coalesce((select jsonb_agg(x order by (x->>'occurred_at')::timestamptz desc) from (select x from (
   select jsonb_build_object('category','action','action','control.'||e.event_kind,'principal_id',e.principal_id,'target_kind',e.scope_kind,'target_ref',e.scope_ref,
     'intent_id',e.intent_id,'occurred_at',e.recorded_at) x from control.nexloop_control_events e where e.tenant_id=v_tenant and e.world=p_world
   union all select jsonb_build_object('category','action','action','commitment.'||ce.kind,'principal_id',ce.principal_id,'target_kind','commitment','target_ref',ce.subject_ref,
     'intent_id',ce.ref,'occurred_at',ce.recorded_at) from runtime.nexloop_commitment_events ce
    where ce.tenant_id=v_tenant and ce.world=p_world and ce.principal_id is not null and ce.kind in ('cancel_requested','extend_requested','condition_met','marked_communication')
   union all select jsonb_build_object('category','action','action','request.'||h.kind,'principal_id',h.principal_id,'target_kind',h.kind,'target_ref',h.target,
     'intent_id',h.intent_id,'occurred_at',h.requested_at) from runtime.nexloop_human_requests h where h.tenant_id=v_tenant and h.world=p_world
   union all select jsonb_build_object('category','action','action','takeover.start','principal_id',tk.taken_by,'target_kind',tk.scope_kind,'target_ref',tk.scope_ref,
     'intent_id',tk.start_intent,'occurred_at',tk.started_at) from control.nexloop_takeovers tk where tk.tenant_id=v_tenant and tk.world=p_world
   union all select jsonb_build_object('category','action','action','takeover.'||tk.end_reason,'principal_id',coalesce(tk.ended_by,'system'),'target_kind',tk.scope_kind,
     'target_ref',tk.scope_ref,'intent_id',tk.end_intent,'occurred_at',tk.ended_at) from control.nexloop_takeovers tk where tk.tenant_id=v_tenant and tk.world=p_world and tk.ended_at is not null
   union all select jsonb_build_object('category','action','action','message.staff_send','principal_id',sr.staff_principal,'target_kind','message','target_ref',sr.message_id,
     'intent_id',sr.request_intent,'occurred_at',sr.created_at) from runtime.nexloop_staff_replies sr where sr.tenant_id=v_tenant and sr.world=p_world
   union all select jsonb_build_object('category','action','action','review.'||d.decision,'principal_id',d.reviewer_principal,'target_kind','candidate','target_ref',d.candidate_id,
     'intent_id',d.decision_id,'occurred_at',d.decided_at) from ontology.nexloop_review_decisions d where d.tenant_id=v_tenant and d.world=p_world
   union all select jsonb_build_object('category','read','action','read.'||ra.read_purpose,'principal_id',ra.principal_id,'role',ra.role,'target_kind',ra.object_kind,
     'target_ref',ra.target_resource,'intent_id',null,'occurred_at',ra.read_at) from runtime.nexloop_workbench_read_audit ra where ra.tenant_id=v_tenant and ra.world=p_world
  ) u order by (x->>'occurred_at')::timestamptz desc limit v_limit) y(x)),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;

-- 11. D4: read-only metrics role for the loopback export (D1); executes the export only -----------------------------------------
create role nexloop_metrics login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema authz to nexloop_metrics;
create function authz.nexloop_metrics_export() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user is distinct from 'nexloop_metrics' then raise exception 'metrics export unavailable' using errcode='42501';end if;
 return coalesce((select jsonb_agg(runtime.nexloop_metrics_snapshot(t.tenant_id,w.world) order by t.tenant_id,w.world)
  from control.nexloop_tenants t cross join (values ('real'),('test'),('simulation')) w(world) where t.status='active'),'[]'::jsonb);
end $$;
alter function authz.nexloop_metrics_export() owner to nexloop_owner;
revoke all on function authz.nexloop_metrics_export() from public;
grant execute on function authz.nexloop_metrics_export() to nexloop_metrics;

-- NX-043 pooled results (owner decision 2026-10-21 on the D2 / D6 conflict, as recommended): rounds of one strategy comparison are
-- pooled. The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. runtime.nexloop_experiment_pooled_freeze(tenant, experiment ids, as_of, by) (owner only; through nexloop.experiment.report kind
--    pooled): the named experiments must be one comparison - the same tenant, world real, stage online, the same candidate and control
--    strategy versions (strategy_mismatch), the same maturity definition (maturity_mismatch) and primary metric (metric_mismatch). A
--    Consumer allocated in several of them counts once, with its earliest allocation (arm and time). The minimum sample is on the
--    pooled arms. The same anti-differencing as one experiment, per comparison: whole UTC days, a later as_of than the comparison's last
--    pooled report, each arm at least 10 more matured Consumers - adding an experiment to the pool is such a change too.
-- 2. control.nexloop_governed_register_experiment (0290's body): the result carries a notice when the candidate arm is smaller than
--    the minimum sample per group (one round reports nothing; pool rounds). The registration is never refused for it.

create table control.nexloop_experiment_pooled_reports (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 comparison_key text not null check(comparison_key~'^[0-9a-f]{64}$'),
 report_id uuid not null,as_of timestamptz not null,
 experiment_ids text[] not null check(cardinality(experiment_ids) between 1 and 50),
 candidate_matured integer not null check(candidate_matured>=0),control_matured integer not null check(control_matured>=0),
 report jsonb not null check(jsonb_typeof(report)='object'),
 report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 created_by text not null check(char_length(created_by) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,comparison_key,report_id));
alter table control.nexloop_experiment_pooled_reports owner to nexloop_owner;
alter table control.nexloop_experiment_pooled_reports enable row level security;
alter table control.nexloop_experiment_pooled_reports force row level security;
create policy tenant_boundary on control.nexloop_experiment_pooled_reports to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_experiment_pooled_reports from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;
create trigger nx043_frozen before update or delete on control.nexloop_experiment_pooled_reports for each row execute function control.nexloop_experiment_frozen();
create trigger nx043_no_truncate before truncate on control.nexloop_experiment_pooled_reports for each statement execute function control.nexloop_experiment_frozen();

create function runtime.nexloop_experiment_pooled_freeze(p_tenant text,p_experiments text[],p_as_of timestamptz,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_ids text[];v_found integer;v_pair jsonb;v_window integer;v_min integer;v_metric text;v_key text;
 v_id uuid;r control.nexloop_experiment_pooled_reports;c record;v_result jsonb;v_report jsonb;v_digests jsonb;v_left record;
begin
 if p_as_of is null or p_as_of>clock_timestamp() or coalesce(p_by,'')='' or p_experiments is null or cardinality(p_experiments) not between 2 and 50 then
  raise exception 'experiment report: invalid' using errcode='22023';end if;
 if p_as_of<>date_trunc('day',p_as_of at time zone 'UTC') at time zone 'UTC' then raise exception 'experiment report: window_not_on_grid' using errcode='22023';end if;
 v_ids:=array(select distinct e from unnest(p_experiments) e order by 1);
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select count(*) into v_found from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=any(v_ids);
 if v_found<>cardinality(v_ids) then raise exception 'experiment report: not_found' using errcode='22023';end if;
 -- One comparison: world real, stage online, the same candidate and control versions, the same maturity and metric.
 if exists(select 1 from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=any(v_ids)
    and (registration->>'world' is distinct from 'real' or registration->>'stage' is distinct from 'online')) then
  raise exception 'experiment report: not_online_real' using errcode='22023';end if;
 if (select count(distinct jsonb_build_object('candidate',registration->'candidate','control',registration->'control')) from control.nexloop_experiments
    where tenant_id=p_tenant and experiment_id=any(v_ids))<>1 then raise exception 'experiment report: strategy_mismatch' using errcode='22023';end if;
 if (select count(distinct jsonb_build_object('window_days',coalesce((registration->'maturity'->>'window_days')::integer,14),
      'min',coalesce((registration->'maturity'->>'min_samples_per_group')::integer,30))) from control.nexloop_experiments
    where tenant_id=p_tenant and experiment_id=any(v_ids))<>1 then raise exception 'experiment report: maturity_mismatch' using errcode='22023';end if;
 if (select count(distinct registration->'primary_metric') from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=any(v_ids))<>1 then  -- key and direction (L6 1.2)
  raise exception 'experiment report: metric_mismatch' using errcode='22023';end if;
 select jsonb_build_object('candidate',registration->'candidate','control',registration->'control'),coalesce((registration->'maturity'->>'window_days')::integer,14),
   coalesce((registration->'maturity'->>'min_samples_per_group')::integer,30),registration->'primary_metric'->>'key'
  into v_pair,v_window,v_min,v_metric from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=v_ids[1];
 v_key:=encode(sha256(convert_to(v_pair::text,'UTF8')),'hex');
 v_id:=md5(v_key||'|'||p_as_of::text||'|'||array_to_string(v_ids,','))::uuid;
 select * into r from control.nexloop_experiment_pooled_reports where tenant_id=p_tenant and comparison_key=v_key and report_id=v_id;
 if found then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return r.report||jsonb_build_object('report_digest',r.report_digest,'frozen',true,'replayed',true);
 end if;
 -- Each Consumer once: its earliest allocation in the pool (arm and time).
 -- Left out (L6 pooled review 4.1, 2.2): an erased Consumer's tombstone rows, and a Consumer allocated to both arms in the pool.
 with pool as (select a.* from control.nexloop_experiment_allocations a where a.tenant_id=p_tenant and a.experiment_id=any(v_ids) and a.arm is not null),
  both_arms as (select consumer_id from pool group by consumer_id having count(distinct arm)>1),
  earliest as (
   select distinct on (a.consumer_id) a.consumer_id,a.arm,a.allocated_at from pool a
   where a.consumer_id not like 'pseudonym:%' and a.consumer_id not in (select consumer_id from both_arms)
   order by a.consumer_id,a.allocated_at,a.experiment_id),
  matured as (select arm,runtime.nexloop_experiment_paid(p_tenant,'real',consumer_id,allocated_at,v_window) paid,
    runtime.nexloop_experiment_unknown(p_tenant,'real',consumer_id,allocated_at,v_window) unknown
   from earliest where allocated_at+make_interval(days=>v_window)<=p_as_of),
  scored as (select arm,paid and not unknown success,unknown from matured)
 select count(*) filter (where arm='candidate') cn,count(*) filter (where arm='control') kn,
        count(*) filter (where arm='candidate' and success) cs,count(*) filter (where arm='control' and success) ks,
        count(*) filter (where arm='candidate' and unknown) cu,count(*) filter (where arm='control' and unknown) ku into c from scored;
 select exists(select 1 from control.nexloop_experiment_allocations a where a.tenant_id=p_tenant and a.experiment_id=any(v_ids) and a.arm is not null
    and a.consumer_id like 'pseudonym:%' and a.allocated_at+make_interval(days=>v_window)<=p_as_of) erased,
   exists(select 1 from control.nexloop_experiment_allocations a where a.tenant_id=p_tenant and a.experiment_id=any(v_ids) and a.arm is not null
    and a.consumer_id not like 'pseudonym:%' group by a.consumer_id having count(distinct a.arm)>1) crossed into v_left;
 -- The anti-differencing per comparison (any change of the pool, a new experiment included, is a new report).
 select * into r from control.nexloop_experiment_pooled_reports where tenant_id=p_tenant and comparison_key=v_key order by as_of desc,created_at desc limit 1;
 if found then
  if p_as_of<=r.as_of then raise exception 'experiment report: as_of_not_after_last' using errcode='22023';end if;
  if c.cn-r.candidate_matured<runtime.nexloop_experiment_report_min_increase() or c.kn-r.control_matured<runtime.nexloop_experiment_report_min_increase() then
   raise exception 'experiment report: too_few_new_matured' using errcode='22023';end if;
 end if;
 v_result:=runtime.nexloop_experiment_result(v_metric,c.cn,c.kn,c.cs,c.ks,c.cu,c.ku,v_min);
 select jsonb_agg(registration_digest order by experiment_id) into v_digests from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=any(v_ids);
 v_report:=jsonb_build_object(
  'schema_version','1.0','report_id',v_id,'level','pooled','experiment_ids',to_jsonb(v_ids),'comparison',v_pair,'stage','online','world','real',
  'frozen_inputs_digest',encode(sha256(convert_to(v_digests::text,'UTF8')),'hex'),'as_of',p_as_of,
  'primary_metric',jsonb_build_object('key',v_metric),'maturity',jsonb_build_object('window_days',v_window,'min_samples_per_group',v_min),
  'pooling','each Consumer once, at its earliest allocation in the pooled experiments; a Consumer in both arms and erased Consumers left out',
  'left_out',jsonb_build_object('erased',case when v_left.erased then 'some' else 'none' end,'cross_allocated',case when v_left.crossed then 'some' else 'none' end),
  'hard_gates',jsonb_build_array(jsonb_build_object('gate','minimum_sample','result',case when v_result->>'reason'='insufficient_sample' then 'fail' else 'pass' end)),
  'guardrails','not_evaluated','scores',v_result->'scores','difference',case when jsonb_typeof(v_result->'difference')='object' then v_result->'difference' end,
  'unknown_policy','an unknown effect is never a success','conclusion',v_result->>'conclusion','reason',v_result->>'reason',
  'statement',case when v_result->>'conclusion'='reported' then
    'Pooled over these experiments of one strategy comparison, in their design, window and sample, the candidate minus control difference of the primary metric is the estimate above with its 95% interval; not a general causal effect, and no single message is the cause of a payment.'
   else 'No result is reported: '||(v_result->>'reason')||'.' end,
  'data_mode','real','created_at',clock_timestamp());
 insert into control.nexloop_experiment_pooled_reports(tenant_id,comparison_key,report_id,as_of,experiment_ids,candidate_matured,control_matured,report,report_digest,created_by)
  values(p_tenant,v_key,v_id,p_as_of,v_ids,c.cn,c.kn,v_report,encode(sha256(convert_to(v_report::text,'UTF8')),'hex'),p_by) returning * into r;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return r.report||jsonb_build_object('report_digest',r.report_digest,'frozen',true,'replayed',false);
end $$;
alter function runtime.nexloop_experiment_pooled_freeze(text,text[],timestamptz,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_pooled_freeze(text,text[],timestamptz,text) from public;

-- The registration's notice (never a refusal): a candidate arm under the minimum sample reports nothing alone.
create function runtime.nexloop_experiment_notice(p_registration jsonb) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_registration->>'stage'='online' and (p_registration->'sample_size'->>'candidate')::integer
   <coalesce((p_registration->'maturity'->>'min_samples_per_group')::integer,30)
  then 'single_round_below_minimum_sample: one round reports nothing (minimum '||coalesce(p_registration->'maturity'->>'min_samples_per_group','30')
   ||' per group); pool rounds of the same strategy comparison (nexloop.experiment.report kind pooled)' end
$$;
alter function runtime.nexloop_experiment_notice(jsonb) owner to nexloop_owner;

create or replace function control.nexloop_governed_register_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb:=body->'registration';x control.nexloop_experiments;v_code text;v_frozen jsonb;v_digest text;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','registration'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment-request:'||p_tenant||':'||(body->>'request_id'),0));
 select * into x from control.nexloop_experiments where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A retry replays the frozen registration; the same request with another registration is refused.
  begin v_frozen:=runtime.nexloop_experiment_registration_frozen(r);exception when others then v_frozen:=null;end;
  if v_frozen is distinct from x.registration then raise exception 'experiment: request_reused' using errcode='22023';end if;
  return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'registration',x.registration,
   'status',runtime.nexloop_experiment_status(p_tenant,x.experiment_id),'registered',false,'notice',runtime.nexloop_experiment_notice(x.registration));
 end if;
 v_code:=runtime.nexloop_experiment_registration_problem(p_tenant,r);
 if v_code is not null then raise exception 'experiment: %',v_code using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||(r->>'experiment_id'),0));
 -- D3: registered once; a changed experiment is a new experiment.
 if exists(select 1 from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=r->>'experiment_id') then
  raise exception 'experiment: experiment_exists' using errcode='22023';end if;
 v_frozen:=runtime.nexloop_experiment_registration_frozen(r);
 v_digest:=encode(sha256(convert_to(v_frozen::text,'UTF8')),'hex');
 insert into control.nexloop_experiments(tenant_id,experiment_id,registration,registration_digest,request_id,registered_by)
  values(p_tenant,r->>'experiment_id',v_frozen,v_digest,body->>'request_id',p_principal) returning * into x;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:register:'||x.experiment_id||':'||md5(p_tenant),'nexloop.experiment.registered',p_tenant,p_principal,'experiment',p_intent,
   jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'stage',v_frozen->>'stage'),clock_timestamp());
 return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'registration',x.registration,
  'status','registered','registered',true,'notice',runtime.nexloop_experiment_notice(x.registration));
end $$;

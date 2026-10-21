-- NX-043 X6 (design §6, §8, D6): experiment result statistics and the frozen report. The migration number lives only in the file name
-- and the catalog; nothing below repeats it.
-- runtime.nexloop_experiment_report_freeze(tenant, experiment, as_of, by) (owner only) counts the allocated Consumers of each arm (X4)
-- whose maturity window (the frozen registration's maturity.window_days, D6: 14 by default) has passed at as_of, and the primary
-- metric within that window. The primary metric supported here is payment: a verified real commercial payment (record kind payment,
-- status paid / succeeded, disposition applied) of a customer linked to the Consumer, in the experiment's world, occurring in
-- [allocated_at, allocated_at + window). An effect of the Consumer left unknown in the window is counted and never a success (M19).
-- Below the registration's maturity.min_samples_per_group (D6: 30) in either arm the report says insufficient_sample and gives only
-- the matured count per arm: no rate, no difference, nothing that narrows to a person. Otherwise: each arm's rate with a Wilson 95%
-- interval and the candidate - control difference with a Newcombe (Wilson) 95% interval. The wording never claims a cause (§6).
-- The report keeps NX-039's report shape (schema_version, report_id, frozen input digest, hard gates, scores, conclusion, reason,
-- created_at) with the experiment's statistics; it is frozen (append-only) and holds aggregates only.

create table control.nexloop_experiment_reports (
 tenant_id text not null,experiment_id text not null,
 report_id uuid not null,as_of timestamptz not null,
 report jsonb not null check(jsonb_typeof(report)='object'),
 report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 created_by text not null check(char_length(created_by) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,experiment_id,report_id),
 foreign key(tenant_id,experiment_id) references control.nexloop_experiments(tenant_id,experiment_id));
alter table control.nexloop_experiment_reports owner to nexloop_owner;
alter table control.nexloop_experiment_reports enable row level security;
alter table control.nexloop_experiment_reports force row level security;
create policy tenant_boundary on control.nexloop_experiment_reports to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_experiment_reports from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;
create trigger nx043_frozen before update or delete on control.nexloop_experiment_reports for each row execute function control.nexloop_experiment_frozen();
create trigger nx043_no_truncate before truncate on control.nexloop_experiment_reports for each statement execute function control.nexloop_experiment_frozen();

-- The Wilson 95% interval of k successes in n: [low, high].
create function runtime.nexloop_experiment_wilson(p_k bigint,p_n bigint) returns numeric[] language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_n is null or p_n<=0 then null else (
  with v as (select p_k::numeric/p_n p,1.959963984540054::numeric z,p_n::numeric n)
  select array[round(greatest(0,(p+z*z/(2*n)-z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)),6),
               round(least(1,(p+z*z/(2*n)+z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)),6)] from v) end
$$;
alter function runtime.nexloop_experiment_wilson(bigint,bigint) owner to nexloop_owner;

create function runtime.nexloop_experiment_report_freeze(p_tenant text,p_experiment text,p_as_of timestamptz,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);e control.nexloop_experiments;reg jsonb;v_window integer;v_min integer;v_world text;
 v_id uuid;r control.nexloop_experiment_reports;v_report jsonb;c record;v_conclusion text;v_reason text;v_scores jsonb;v_diff jsonb;v_excl jsonb;
 w1 numeric[];w2 numeric[];p1 numeric;p2 numeric;
begin
 if p_as_of is null or p_as_of>clock_timestamp() or coalesce(p_by,'')='' then raise exception 'experiment report: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select * into e from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=p_experiment;
 if not found then raise exception 'experiment report: not_found' using errcode='22023';end if;
 reg:=e.registration;
 v_window:=coalesce((reg->'maturity'->>'window_days')::integer,14);
 v_min:=coalesce((reg->'maturity'->>'min_samples_per_group')::integer,30);
 v_world:=reg->>'world';
 v_id:=md5(p_experiment||'|'||p_as_of::text)::uuid;
 select * into r from control.nexloop_experiment_reports where tenant_id=p_tenant and experiment_id=p_experiment and report_id=v_id;
 if found then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return r.report||jsonb_build_object('report_digest',r.report_digest,'frozen',true,'replayed',true);
 end if;
 -- Per arm: matured Consumers, successes (payment in the window, unless an unknown effect), unknown.
 with rows_ as (
   select a.arm,
    reg->'primary_metric'->>'key'='payment' and exists(select 1 from runtime.nexloop_commercial_events ce join control.nexloop_commercial_customer_links l
      on l.connector_id=ce.connector_id and l.customer_ref=ce.customer_ref
     where ce.tenant_id=p_tenant and ce.world=v_world and l.tenant_id=p_tenant and l.world=v_world and l.consumer_id=a.consumer_id
      and ce.record_kind='payment' and ce.status in ('paid','succeeded') and ce.disposition='applied'
      and ce.occurred_at>=a.allocated_at and ce.occurred_at<a.allocated_at+make_interval(days=>v_window)) paid,
    exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=v_world and i.consumer_id=a.consumer_id and i.state='unknown'
      and i.created_at>=a.allocated_at and i.created_at<a.allocated_at+make_interval(days=>v_window)) unknown
   from control.nexloop_experiment_allocations a
   where a.tenant_id=p_tenant and a.experiment_id=p_experiment and a.arm is not null and a.allocated_at+make_interval(days=>v_window)<=p_as_of),
  scored as (select arm,paid and not unknown success,unknown from rows_)  -- M19: an unknown effect is never a success
 select count(*) filter (where arm='candidate') cn,count(*) filter (where arm='control') kn,
        count(*) filter (where arm='candidate' and success) cs,count(*) filter (where arm='control' and success) ks,
        count(*) filter (where arm='candidate' and unknown) cu,count(*) filter (where arm='control' and unknown) ku into c from scored;
 if reg->'primary_metric'->>'key' is distinct from 'payment' then
  v_conclusion:='not_reported';v_reason:='metric_not_supported';
  v_scores:=jsonb_build_object('candidate',jsonb_build_object('matured',c.cn),'control',jsonb_build_object('matured',c.kn));
 elsif c.cn<v_min or c.kn<v_min then
  -- D6: under the minimum in either arm, no rate and no difference (nothing that narrows to a person).
  v_conclusion:='not_reported';v_reason:='insufficient_sample';
  v_scores:=jsonb_build_object('candidate',jsonb_build_object('matured',c.cn),'control',jsonb_build_object('matured',c.kn));
 else
  w1:=runtime.nexloop_experiment_wilson(c.cs,c.cn);w2:=runtime.nexloop_experiment_wilson(c.ks,c.kn);
  p1:=c.cs::numeric/c.cn;p2:=c.ks::numeric/c.kn;
  v_scores:=jsonb_build_object(
   'candidate',jsonb_build_object('matured',c.cn,'successes',c.cs,'unknown',c.cu,'rate',round(p1,6),'interval',to_jsonb(w1)),
   'control',jsonb_build_object('matured',c.kn,'successes',c.ks,'unknown',c.ku,'rate',round(p2,6),'interval',to_jsonb(w2)));
  v_diff:=jsonb_build_object('estimate',round(p1-p2,6),'method','newcombe-wilson-95',
   'interval',jsonb_build_array(round(p1-p2-sqrt(power(p1-w1[1],2)+power(w2[2]-p2,2)),6),round(p1-p2+sqrt(power(w1[2]-p1,2)+power(p2-w2[1],2)),6)));
  v_conclusion:='reported';v_reason:=null;
 end if;
 select jsonb_object_agg(excluded_reason,n) into v_excl from (select excluded_reason,count(*) n from control.nexloop_experiment_allocations
   where tenant_id=p_tenant and experiment_id=p_experiment and excluded_reason is not null group by excluded_reason) x;
 v_report:=jsonb_build_object(
  'schema_version','1.0','report_id',v_id,'experiment_id',p_experiment,'stage',reg->>'stage','world',v_world,'level','experiment',
  'frozen_inputs_digest',e.registration_digest,'as_of',p_as_of,
  'primary_metric',reg->'primary_metric','maturity',jsonb_build_object('window_days',v_window,'min_samples_per_group',v_min),
  'hard_gates',jsonb_build_array(jsonb_build_object('gate','minimum_sample','result',case when v_reason='insufficient_sample' then 'fail' else 'pass' end)),
  'guardrails','not_evaluated',  -- X5
  'scores',v_scores,'difference',v_diff,
  'exclusions',coalesce(v_excl,'{}'::jsonb),
  'unknown_policy','an unknown effect is never a success',
  'conclusion',v_conclusion,'reason',v_reason,
  'statement',case when v_conclusion='reported' then
    'In this experiment design, window and sample, the candidate minus control difference of the primary metric is the estimate above with its 95% interval; not a general causal effect, and no single message is the cause of a payment.'
   else 'No result is reported: '||v_reason||'.' end,
  'data_mode',case when v_world='real' then 'real' else 'simulation' end,
  'created_at',clock_timestamp());
 insert into control.nexloop_experiment_reports(tenant_id,experiment_id,report_id,as_of,report,report_digest,created_by)
  values(p_tenant,p_experiment,v_id,p_as_of,v_report,encode(sha256(convert_to(v_report::text,'UTF8')),'hex'),p_by) returning * into r;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return r.report||jsonb_build_object('report_digest',r.report_digest,'frozen',true,'replayed',false);
end $$;
alter function runtime.nexloop_experiment_report_freeze(text,text,timestamptz,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_report_freeze(text,text,timestamptz,text) from public;

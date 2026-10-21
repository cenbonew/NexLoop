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
 -- The matured counts per arm (kept for the next report's minimum increase; the report itself shows them only from 30 on).
 candidate_matured integer not null check(candidate_matured>=0),control_matured integer not null check(control_matured>=0),
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

-- A side count as the shadow report and T5 give it: the number at or above the minimum, otherwise none / some (L6 X3 review 4.2).
create function runtime.nexloop_experiment_count(p_n bigint,p_min integer) returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_n>=p_min then to_jsonb(p_n) when p_n>0 then '"some"'::jsonb else '"none"'::jsonb end
$$;
alter function runtime.nexloop_experiment_count(bigint,integer) owner to nexloop_owner;

-- The least increase of matured Consumers per arm between two reports of one experiment (L6 X6 review 2.2).
create function runtime.nexloop_experiment_report_min_increase() returns integer language sql immutable set search_path=pg_catalog,pg_temp as $$ select 10 $$;
alter function runtime.nexloop_experiment_report_min_increase() owner to nexloop_owner;

-- The Wilson 95% interval of k successes in n: [low, high].
create function runtime.nexloop_experiment_wilson(p_k bigint,p_n bigint) returns numeric[] language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_n is null or p_n<=0 then null else (
  with v as (select p_k::numeric/p_n p,1.959963984540054::numeric z,p_n::numeric n)
  select array[round(greatest(0,(p+z*z/(2*n)-z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)),6),
               round(least(1,(p+z*z/(2*n)+z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)),6)] from v) end
$$;
alter function runtime.nexloop_experiment_wilson(bigint,bigint) owner to nexloop_owner;

-- The primary metric of one allocated Consumer (shared by the experiment and the pooled report): a verified payment in the window.
create function runtime.nexloop_experiment_paid(p_tenant text,p_world text,p_consumer text,p_at timestamptz,p_window integer) returns boolean
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select exists(select 1 from runtime.nexloop_commercial_events ce join control.nexloop_commercial_customer_links l on l.connector_id=ce.connector_id and l.customer_ref=ce.customer_ref
  where ce.tenant_id=p_tenant and ce.world=p_world and l.tenant_id=p_tenant and l.world=p_world and l.consumer_id=p_consumer
   and ce.record_kind='payment' and ce.status in ('paid','succeeded') and ce.disposition='applied'
   and ce.occurred_at>=p_at and ce.occurred_at<p_at+make_interval(days=>p_window))
$$;
alter function runtime.nexloop_experiment_paid(text,text,text,timestamptz,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_paid(text,text,text,timestamptz,integer) from public;
-- An effect of the Consumer left unknown in the window (M19: never a success).
create function runtime.nexloop_experiment_unknown(p_tenant text,p_world text,p_consumer text,p_at timestamptz,p_window integer) returns boolean
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer and i.state='unknown'
   and i.created_at>=p_at and i.created_at<p_at+make_interval(days=>p_window))
$$;
alter function runtime.nexloop_experiment_unknown(text,text,text,timestamptz,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_unknown(text,text,text,timestamptz,integer) from public;
-- The statistics of matured / successful / unknown counts per arm (shared): conclusion, reason, scores and difference.
create function runtime.nexloop_experiment_result(p_metric text,cn bigint,kn bigint,cs bigint,ks bigint,cu bigint,ku bigint,v_min integer) returns jsonb
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
declare w1 numeric[];w2 numeric[];p1 numeric;p2 numeric;
begin
 if p_metric is distinct from 'payment' or cn<v_min or kn<v_min then
  -- D6: under the minimum in either arm, no rate and no difference (nothing that narrows to a person).
  return jsonb_build_object('conclusion','not_reported','reason',case when p_metric is distinct from 'payment' then 'metric_not_supported' else 'insufficient_sample' end,
   'scores',jsonb_build_object('candidate',jsonb_build_object('matured',runtime.nexloop_experiment_count(cn,v_min)),
     'control',jsonb_build_object('matured',runtime.nexloop_experiment_count(kn,v_min))),'difference',null);
 end if;
 w1:=runtime.nexloop_experiment_wilson(cs,cn);w2:=runtime.nexloop_experiment_wilson(ks,kn);
 p1:=cs::numeric/cn;p2:=ks::numeric/kn;
 return jsonb_build_object('conclusion','reported','reason',null,
  'scores',jsonb_build_object(
   'candidate',jsonb_build_object('matured',cn,'successes',cs,'unknown',runtime.nexloop_experiment_count(cu,v_min),'rate',round(p1,6),'interval',to_jsonb(w1)),
   'control',jsonb_build_object('matured',kn,'successes',ks,'unknown',runtime.nexloop_experiment_count(ku,v_min),'rate',round(p2,6),'interval',to_jsonb(w2))),
  'difference',jsonb_build_object('estimate',round(p1-p2,6),'method','newcombe-wilson-95',
   'interval',jsonb_build_array(round(p1-p2-sqrt(power(p1-w1[1],2)+power(w2[2]-p2,2)),6),round(p1-p2+sqrt(power(w1[2]-p1,2)+power(p2-w2[1],2)),6))));
end $$;
alter function runtime.nexloop_experiment_result(text,bigint,bigint,bigint,bigint,bigint,bigint,integer) owner to nexloop_owner;

-- Who an experiment report leaves out (L6 pooled review 4.1, 2.2): an erased Consumer's tombstone row (consumer_id pseudonym:..., the
-- NX-029 wiring): neither numerator nor denominator; and a Consumer allocated to the other arm in another experiment of the same
-- comparison (the same candidate and control versions): exposed to both strategies, so in neither result.
create function runtime.nexloop_experiment_leave_out(p_tenant text,p_experiment text,p_consumer text,p_arm text) returns text
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select case when p_consumer like 'pseudonym:%' then 'erased'
  when exists(select 1 from control.nexloop_experiments e join control.nexloop_experiments o on o.tenant_id=e.tenant_id and o.experiment_id<>e.experiment_id
     and o.registration->'candidate'=e.registration->'candidate' and o.registration->'control'=e.registration->'control'
    join control.nexloop_experiment_allocations a on a.tenant_id=o.tenant_id and a.experiment_id=o.experiment_id and a.consumer_id=p_consumer
     and a.arm is not null and a.arm<>p_arm
    where e.tenant_id=p_tenant and e.experiment_id=p_experiment) then 'cross_allocated' end
$$;
alter function runtime.nexloop_experiment_leave_out(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_leave_out(text,text,text,text) from public;

create function runtime.nexloop_experiment_report_freeze(p_tenant text,p_experiment text,p_as_of timestamptz,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);e control.nexloop_experiments;reg jsonb;v_window integer;v_min integer;v_world text;
 v_id uuid;r control.nexloop_experiment_reports;v_report jsonb;c record;v_conclusion text;v_reason text;v_scores jsonb;v_diff jsonb;v_excl jsonb;v_result jsonb;v_left record;
begin
 if p_as_of is null or p_as_of>clock_timestamp() or coalesce(p_by,'')='' then raise exception 'experiment report: invalid' using errcode='22023';end if;
 -- As the shadow report and NX-042 T5: as_of on whole UTC days only, so two reports cannot differ by a few allocations.
 if p_as_of<>date_trunc('day',p_as_of at time zone 'UTC') at time zone 'UTC' then raise exception 'experiment report: window_not_on_grid' using errcode='22023';end if;
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
 -- Per arm: matured Consumers, successes (payment in the window, unless an unknown effect), unknown.
 with allocated as (
   select a.*,runtime.nexloop_experiment_leave_out(p_tenant,p_experiment,a.consumer_id,a.arm) left_out from control.nexloop_experiment_allocations a
   where a.tenant_id=p_tenant and a.experiment_id=p_experiment and a.arm is not null and a.allocated_at+make_interval(days=>v_window)<=p_as_of),
  rows_ as (
   select a.arm,runtime.nexloop_experiment_paid(p_tenant,v_world,a.consumer_id,a.allocated_at,v_window) paid,
    runtime.nexloop_experiment_unknown(p_tenant,v_world,a.consumer_id,a.allocated_at,v_window) unknown
   from allocated a where a.left_out is null),
  scored as (select arm,paid and not unknown success,unknown from rows_)  -- M19: an unknown effect is never a success
 select count(*) filter (where arm='candidate') cn,count(*) filter (where arm='control') kn,
        count(*) filter (where arm='candidate' and success) cs,count(*) filter (where arm='control' and success) ks,
        count(*) filter (where arm='candidate' and unknown) cu,count(*) filter (where arm='control' and unknown) ku into c from scored;
 select count(*) filter (where left_out='erased') erased,count(*) filter (where left_out='cross_allocated') crossed into v_left
  from (select runtime.nexloop_experiment_leave_out(p_tenant,p_experiment,a.consumer_id,a.arm) left_out from control.nexloop_experiment_allocations a
   where a.tenant_id=p_tenant and a.experiment_id=p_experiment and a.arm is not null and a.allocated_at+make_interval(days=>v_window)<=p_as_of) x;
 v_result:=runtime.nexloop_experiment_result(reg->'primary_metric'->>'key',c.cn,c.kn,c.cs,c.ks,c.cu,c.ku,v_min);
 v_conclusion:=v_result->>'conclusion';v_reason:=v_result->>'reason';v_scores:=v_result->'scores';
 v_diff:=case when jsonb_typeof(v_result->'difference')='object' then v_result->'difference' end;
 select jsonb_object_agg(excluded_reason,runtime.nexloop_experiment_count(n,v_min)) into v_excl from (select excluded_reason,count(*) n from control.nexloop_experiment_allocations
   where tenant_id=p_tenant and experiment_id=p_experiment and excluded_reason is not null group by excluded_reason) x;
 v_report:=jsonb_build_object(
  'schema_version','1.0','report_id',v_id,'experiment_id',p_experiment,'stage',reg->>'stage','world',v_world,'level','experiment',
  'frozen_inputs_digest',e.registration_digest,'as_of',p_as_of,
  'primary_metric',reg->'primary_metric','maturity',jsonb_build_object('window_days',v_window,'min_samples_per_group',v_min),
  'hard_gates',jsonb_build_array(jsonb_build_object('gate','minimum_sample','result',case when v_reason='insufficient_sample' then 'fail' else 'pass' end)),
  'guardrails','not_evaluated',  -- X5
  'scores',v_scores,'difference',v_diff,
  'exclusions',coalesce(v_excl,'{}'::jsonb),
  -- Left out of numerator and denominator (L6 pooled review 4.1, 2.2): only none / some.
  'left_out',jsonb_build_object('erased',case when v_left.erased>0 then 'some' else 'none' end,
   'cross_allocated',case when v_left.crossed>0 then 'some' else 'none' end),
  'unknown_policy','an unknown effect is never a success',
  'conclusion',v_conclusion,'reason',v_reason,
  'statement',case when v_conclusion='reported' then
    'In this experiment design, window and sample, the candidate minus control difference of the primary metric is the estimate above with its 95% interval; not a general causal effect, and no single message is the cause of a payment.'
   else 'No result is reported: '||v_reason||'.' end,
  'data_mode',case when v_world='real' then 'real' else 'simulation' end,
  'created_at',clock_timestamp());
 -- L6 X6 review 2.2: a new report comes after the last one and only once each arm has matured at least
 -- runtime.nexloop_experiment_report_min_increase() more Consumers - two reports never differ by one person.
 select * into r from control.nexloop_experiment_reports where tenant_id=p_tenant and experiment_id=p_experiment order by as_of desc limit 1;
 if found then
  if p_as_of<=r.as_of then raise exception 'experiment report: as_of_not_after_last' using errcode='22023';end if;
  if c.cn-r.candidate_matured<runtime.nexloop_experiment_report_min_increase() or c.kn-r.control_matured<runtime.nexloop_experiment_report_min_increase() then
   raise exception 'experiment report: too_few_new_matured' using errcode='22023';end if;
 end if;
 insert into control.nexloop_experiment_reports(tenant_id,experiment_id,report_id,as_of,report,report_digest,candidate_matured,control_matured,created_by)
  values(p_tenant,p_experiment,v_id,p_as_of,v_report,encode(sha256(convert_to(v_report::text,'UTF8')),'hex'),c.cn,c.kn,p_by) returning * into r;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return r.report||jsonb_build_object('report_digest',r.report_digest,'frozen',true,'replayed',false);
end $$;
alter function runtime.nexloop_experiment_report_freeze(text,text,timestamptz,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_report_freeze(text,text,timestamptz,text) from public;

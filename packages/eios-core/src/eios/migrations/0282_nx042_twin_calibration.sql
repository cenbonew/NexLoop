-- NX-042 T5 (design §3.2, §3.3, D2, D3, D4, D7): calibration runs and frozen calibration reports of a consumer twin version. The
-- migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. control.nexloop_twin_calibration_reports: one frozen report per run (contract nexloop-twin-calibration-report/1): the twin version
--    and its definition digest, the calibration windows (1 to 3, dispatcher 2026-10-21; each [split_at, split_at + span) with its
--    reference window the span before it, its aggregate input digest, groups, exclusions and the cells it contributed), the digest of
--    the whole input (T4, real only, groups of at least 30 Consumers), per behaviour, window and group the twin's interval, the observed
--    rate and Consumers, whether the interval covered it and its Brier score, the coverage, the conclusion and, when there is no
--    verdict, why. Append only (no update, delete or truncate). report_ref = twin-calibration:<digest>.
-- 2. Calibration (D3). A cell is (behaviour, window, group): one twin behaviour with real aggregate data - reply_within_24h (replied
--    within 24 hours of an agent contact) or refuse_contact (a real contact restriction after it) - in one window and one output group
--    of that window (the four UTC dayparts when all four reach 30 Consumers, else `all` alone, else none). A window gives B x {0, 1, 4}
--    cells for B mapped behaviours. Coverage = cells whose observed rate lies in the interval / cells.
--    The threshold is 16 cells (L6 nx042-t5-fix-verify 1.1, dispatcher 2026-10-21; docs/implementation/NX-042-T5-implementation.md):
--    reachable with two full windows of a two-behaviour twin, and of the thresholds two windows can reach the one with the lowest worst
--    misjudgement of an 80%-coverage twin (34.2%). passed: at least 16 cells and |coverage - nominal| <= 0.10 (80% intervals: 70%-90%);
--    failed: at least 16 cells otherwise; insufficient_sample: fewer than 16 cells; not_calibratable: a twin behaviour without real
--    aggregate data (e.g. payment_intent; D4: an uncalibrated quantity is never passed as calibrated), or a twin with a single mapped
--    behaviour (at most 4 cells a window, 12 over three windows: it can never reach 16).
--    Up to 3 windows may be combined; they must not overlap, and a window that overlaps one used by an earlier report of the same version
--    is refused (window_used: no recalibrating on the same data until it passes). Brier score per cell and Consumer-weighted overall, with
--    the interval midpoint as the forecast (computed from counts: r(1-p)^2 + (1-r)p^2); reported, not a verdict.
-- 3. Independence (§3.2): every calibration window starts at or after the version's publication (its parameters cannot have seen it)
--    and has ended; a window excludes the Consumers contacted from its reference window or from the first window's start, whichever is
--    earlier (T4), so a Consumer counts in one window of a report at most; grouped by Consumer, never per message.
-- 4. Events (T1 table, append-only): passed appends `passed`, failed appends `failed`; insufficient_sample and not_calibratable append
--    nothing. A trigger lets a calibration event in only when it names a report of this version whose conclusion is its verdict and whose
--    digest matches the version's definition. The report carries the attempt number (reports with a verdict for this version, this one
--    included) and the verdicts of the recent attempts. runtime.nexloop_consumer_twin_calibration (T1, replaced) is calibrated only when the
--    latest verdict passed and none of the last 3 verdicts failed (K = 3): a version that failed must pass three times in a row.
-- 5. Small counts (L6 3.2/3.3): a window's exclusion counts below 30 are written as "<30", never as the number.
-- 6. Governed owner Action consumer_twin.calibrate / calibrate_consumer_twin (D7: owner only, from real), idempotent per request.

create table control.nexloop_twin_calibration_reports (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 twin_id text not null,version integer not null,
 twin_digest text not null check(twin_digest~'^[0-9a-f]{64}$'),
 windows jsonb not null check(jsonb_typeof(windows)='array' and jsonb_array_length(windows) between 1 and 3),
 input_digest text not null check(input_digest~'^[0-9a-f]{64}$'),
 coverage numeric check(coverage between 0 and 1),
 conclusion text not null check(conclusion in ('passed','failed','insufficient_sample','not_calibratable')),
 report jsonb not null check(jsonb_typeof(report)='object'),
 requested_by text not null check(char_length(requested_by) between 1 and 200),
 request_id text not null check(char_length(request_id) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,report_digest),unique(tenant_id,request_id),
 foreign key(tenant_id,twin_id,version) references control.nexloop_consumer_twins(tenant_id,twin_id,version));
create index nexloop_twin_calibration_reports_version on control.nexloop_twin_calibration_reports(tenant_id,twin_id,version,created_at);
alter table control.nexloop_twin_calibration_reports owner to nexloop_owner;
alter table control.nexloop_twin_calibration_reports enable row level security;
alter table control.nexloop_twin_calibration_reports force row level security;
create policy tenant_boundary on control.nexloop_twin_calibration_reports to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_twin_calibration_reports from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;
create function control.nexloop_twin_calibration_report_frozen() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception 'a twin calibration report is frozen' using errcode='42501';
end $$;
alter function control.nexloop_twin_calibration_report_frozen() owner to nexloop_owner;
revoke all on function control.nexloop_twin_calibration_report_frozen() from public;
create trigger nx042_calibration_report_frozen before update or delete on control.nexloop_twin_calibration_reports
 for each row execute function control.nexloop_twin_calibration_report_frozen();
create trigger nx042_calibration_report_no_truncate before truncate on control.nexloop_twin_calibration_reports
 for each statement execute function control.nexloop_twin_calibration_report_frozen();

-- 4. A calibration event only from a report of this version with that conclusion (and the version's own definition digest).
create function control.nexloop_twin_calibration_event_check() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);ok boolean;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select exists(select 1 from control.nexloop_twin_calibration_reports r join control.nexloop_consumer_twins t
   on t.tenant_id=r.tenant_id and t.twin_id=r.twin_id and t.version=r.version
  where r.tenant_id=new.tenant_id and 'twin-calibration:'||r.report_digest=new.report_ref and r.twin_id=new.twin_id and r.version=new.version
   and r.conclusion=new.verdict and r.twin_digest=t.definition_digest) into ok;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 if not ok then raise exception 'consumer twin: calibration event needs a matching report' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_twin_calibration_event_check() owner to nexloop_owner;
revoke all on function control.nexloop_twin_calibration_event_check() from public;
create trigger nx042_calibration_event_report before insert on control.nexloop_consumer_twin_calibrations
 for each row execute function control.nexloop_twin_calibration_event_check();

-- 2. The calibration of one version against the aggregate inputs of its windows (pure: no write). Behaviour -> aggregate count.
--    p_windows: [{"window": n, "aggregates": <T4 output>}, ...].
create function runtime.nexloop_twin_calibration_evaluate(p_definition jsonb,p_windows jsonb) returns jsonb
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
declare v_map constant jsonb:='{"reply_within_24h":"replied_24h","refuse_contact":"refused"}';c_min constant integer:=16;
 b record;w jsonb;g jsonb;v_groups jsonb;cells jsonb:='[]';v_unmapped text[]:='{}';v_single boolean;n_cells integer;n_cov integer;v_cov numeric;v_nominal numeric;
 v_brier numeric;v_weight numeric;v_conc text;v_why text;r numeric;p numeric;
begin
 for b in select key from jsonb_each(p_definition->'parameters'->'behaviors') order by key loop
  if not v_map ? b.key then v_unmapped:=v_unmapped||b.key;end if;
 end loop;
 v_single:=(select count(*) from jsonb_object_keys(p_definition->'parameters'->'behaviors') k where v_map ? k)=1;
 for w in select x from jsonb_array_elements(p_windows) x order by (x->>'window')::integer loop
  v_groups:=coalesce((select jsonb_agg(x) from jsonb_array_elements(w->'aggregates'->'groups') x where x->>'group'<>'all'),'[]'::jsonb);
  if jsonb_array_length(v_groups)=0 then
   v_groups:=coalesce((select jsonb_agg(x) from jsonb_array_elements(w->'aggregates'->'groups') x where x->>'group'='all'),'[]'::jsonb);end if;
  for b in select key,value from jsonb_each(p_definition->'parameters'->'behaviors') where v_map ? key order by key loop
   for g in select x from jsonb_array_elements(v_groups) x order by x->>'group' loop
    r:=(g->>(v_map->>b.key))::numeric/(g->>'consumers')::numeric;
    p:=((b.value->>'low')::numeric+(b.value->>'high')::numeric)/2;
    cells:=cells||jsonb_build_array(jsonb_build_object('behavior',b.key,'window',(w->>'window')::integer,'group',g->>'group','consumers',(g->>'consumers')::integer,
     'observed_rate',round(r,4),'low',(b.value->>'low')::numeric,'high',(b.value->>'high')::numeric,'nominal_coverage',(b.value->>'coverage')::numeric,
     'covered',r>=(b.value->>'low')::numeric and r<=(b.value->>'high')::numeric,'brier',round(r*(1-p)^2+(1-r)*p^2,6)));
   end loop;
  end loop;
 end loop;
 n_cells:=jsonb_array_length(cells);
 n_cov:=(select count(*) from jsonb_array_elements(cells) c where (c->>'covered')::boolean);
 v_cov:=case when n_cells>0 then round(n_cov::numeric/n_cells,4) end;
 v_nominal:=(select round(avg((c->>'nominal_coverage')::numeric),4) from jsonb_array_elements(cells) c);
 v_weight:=(select sum((c->>'consumers')::numeric) from jsonb_array_elements(cells) c);
 v_brier:=case when v_weight>0 then round((select sum((c->>'brier')::numeric*(c->>'consumers')::numeric) from jsonb_array_elements(cells) c)/v_weight,6) end;
 if cardinality(v_unmapped)>0 then v_conc:='not_calibratable';v_why:='behaviours without real aggregate data stay uncalibrated: '||array_to_string(v_unmapped,', ');
 elsif v_single then v_conc:='not_calibratable';
  v_why:='a single calibratable behaviour gives at most 4 cells a window (12 over three windows), below the 16 cells for a verdict';
 elsif n_cells<c_min then v_conc:='insufficient_sample';
  v_why:=case when n_cells=0 then 'fewer than 30 Consumers in every calibration window'
   else 'fewer than 16 calibration cells (two full windows give 16; dayparts below 30 Consumers are suppressed)' end;
 elsif abs(v_cov-v_nominal)<=0.10 then v_conc:='passed';
 else v_conc:='failed';v_why:=case when v_cov>v_nominal then 'intervals too wide (over-coverage)' else 'intervals miss the observed rates (under-coverage)' end;
 end if;
 return jsonb_build_object('cells',cells,'cell_count',n_cells,'covered_cells',n_cov,'coverage',v_cov,'nominal_coverage',v_nominal,
  'coverage_band',case when v_nominal is not null then jsonb_build_array(v_nominal-0.10,v_nominal+0.10) end,'min_cells',c_min,
  'brier',v_brier,'uncalibrated_behaviors',to_jsonb(v_unmapped),'conclusion',v_conc,'reason',v_why);
end $$;
alter function runtime.nexloop_twin_calibration_evaluate(jsonb,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_twin_calibration_evaluate(jsonb,jsonb) from public;

-- A count below the minimum group (30) is written as "<30" (L6 3.2/3.3: no small number frozen into a report).
create function runtime.nexloop_twin_small_count(p integer) returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p is null then null when p<30 then to_jsonb('<30'::text) else to_jsonb(p) end
$$;
alter function runtime.nexloop_twin_small_count(integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_twin_small_count(integer) from public;

-- 6. The governed owner Action ----------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_calibrate_consumer_twin(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare t control.nexloop_consumer_twins;rep control.nexloop_twin_calibration_reports;x jsonb;v_split timestamptz;v_span integer;v_prev_end timestamptz;
 v_windows jsonb:='[]';v_inputs jsonb:='[]';v_norm jsonb;v_agg jsonb;v_eval jsonb;v_report jsonb;v_digest text;v_status text;n integer:=0;
 v_first timestamptz;v_attempt integer;v_recent jsonb;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','twin_id','version','windows'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'twin_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(body->'version') is distinct from 'number'
  or (body->>'version')!~'^[0-9]{1,9}$' or (body->>'version')::integer<1
  or jsonb_typeof(body->'windows') is distinct from 'array' or jsonb_array_length(body->'windows') not between 1 and 3
  or exists(select 1 from jsonb_array_elements(body->'windows') w where jsonb_typeof(w)<>'object'
   or (select array_agg(k order by k) from jsonb_object_keys(w) k) is distinct from array['span_days','split_at']
   or coalesce(w->>'split_at','')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}$' or jsonb_typeof(w->'span_days')<>'number' or (w->>'span_days') not in ('28','56','84')) then
  raise exception 'consumer twin: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'consumer twin: owner_world_only' using errcode='22023';end if;
 -- The windows in time order (dates checked; the same windows in another order are the same request).
 begin
  v_norm:=(select jsonb_agg(jsonb_build_object('split_at',(w->>'split_at')::date,'span_days',(w->>'span_days')::integer) order by (w->>'split_at')::date)
   from jsonb_array_elements(body->'windows') w);
 exception when others then raise exception 'consumer twin: invalid' using errcode='22023';end;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-calibration-request:'||p_tenant||':'||(body->>'request_id'),0));
 select * into rep from control.nexloop_twin_calibration_reports where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (rep.twin_id,rep.version,rep.windows) is distinct from (body->>'twin_id',(body->>'version')::integer,v_norm) then
   raise exception 'consumer twin: request_reused' using errcode='22023';end if;
  return jsonb_build_object('report_ref','twin-calibration:'||rep.report_digest,'twin_id',rep.twin_id,'version',rep.version,'conclusion',rep.conclusion,
   'coverage',rep.coverage,'calibration',runtime.nexloop_consumer_twin_calibration(p_tenant,rep.twin_id,rep.version),'created',false);
 end if;
 select * into t from control.nexloop_consumer_twins where tenant_id=p_tenant and twin_id=body->>'twin_id' and version=(body->>'version')::integer for share;
 if not found then raise exception 'consumer twin: not_found' using errcode='22023';end if;
 v_status:=runtime.nexloop_consumer_twin_status(p_tenant,t.twin_id,t.version);
 if v_status is distinct from 'published' then raise exception 'consumer twin: not_published' using errcode='22023';end if;
 -- One calibration of a version at a time; a window overlapping one an earlier report of this version used is refused (any conclusion:
 -- no recalibrating on the same data until it passes).
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-calibration:'||p_tenant||':'||t.twin_id||':'||t.version,0));
 if exists(select 1 from control.nexloop_twin_calibration_reports r cross join jsonb_array_elements(r.windows) u cross join jsonb_array_elements(v_norm) w
   where r.tenant_id=p_tenant and r.twin_id=t.twin_id and r.version=t.version
     and (w->>'split_at')::date<(u->>'split_at')::date+(u->>'span_days')::integer and (u->>'split_at')::date<(w->>'split_at')::date+(w->>'span_days')::integer) then
  raise exception 'consumer twin: window_used' using errcode='22023';end if;
 v_first:=(v_norm->0->>'split_at')::date::timestamptz;
 for x in select value from jsonb_array_elements(v_norm) loop
  n:=n+1;v_split:=(x->>'split_at')::date::timestamptz;v_span:=(x->>'span_days')::integer;
  -- Independence: every window starts at or after publication (the parameters cannot have seen it) and has ended; no overlap.
  if v_split<t.published_at then raise exception 'consumer twin: window_before_publication' using errcode='22023';end if;
  if v_split+make_interval(days=>v_span)>clock_timestamp() then raise exception 'consumer twin: window_open' using errcode='22023';end if;
  if v_prev_end is not null and v_split<v_prev_end then raise exception 'consumer twin: windows_overlap' using errcode='22023';end if;
  v_prev_end:=v_split+make_interval(days=>v_span);
  -- Excluded: Consumers contacted from its reference window or from the first window's start, whichever is earlier (L6 1.2).
  v_agg:=runtime.nexloop_twin_calibration_aggregates(p_tenant,v_split,v_split+make_interval(days=>v_span),least(v_split-make_interval(days=>v_span),v_first));
  perform set_config('eios.tenant_id',p_tenant,true);
  v_inputs:=v_inputs||jsonb_build_array(jsonb_build_object('window',n,'aggregates',v_agg));
  v_windows:=v_windows||jsonb_build_array(jsonb_build_object('window',n,'split_at',runtime.nexloop_commercial_ts(v_split),'span_days',v_span,
   'reference',jsonb_build_array(runtime.nexloop_commercial_ts(v_split-make_interval(days=>v_span)),runtime.nexloop_commercial_ts(v_split)),
   'calibration',jsonb_build_array(runtime.nexloop_commercial_ts(v_split),runtime.nexloop_commercial_ts(v_split+make_interval(days=>v_span))),
   'reference_consumers_excluded',runtime.nexloop_twin_small_count(coalesce((v_agg->'excluded'->>'in_reference_window')::integer,0)),
   'input_digest',encode(sha256(convert_to(v_agg::text,'UTF8')),'hex'),
   'groups',(select coalesce(jsonb_agg(jsonb_build_object('group',g->>'group','consumers',(g->>'consumers')::integer)),'[]') from jsonb_array_elements(v_agg->'groups') g),
   'suppressed',coalesce(v_agg->'suppressed','[]'),
   'excluded',(select coalesce(jsonb_object_agg(e.key,runtime.nexloop_twin_small_count(e.value::text::integer)),'{}') from jsonb_each(coalesce(v_agg->'excluded','{}')) e)));
 end loop;
 v_eval:=runtime.nexloop_twin_calibration_evaluate(t.definition,v_inputs);
 -- Each window's contribution in cells.
 v_windows:=(select jsonb_agg(w||jsonb_build_object('cells',(select count(*) from jsonb_array_elements(v_eval->'cells') c where (c->>'window')::integer=(w->>'window')::integer))
   order by (w->>'window')::integer) from jsonb_array_elements(v_windows) w);
 -- Attempts: reports of this version with a verdict, this one included when it has one; the last 3 verdicts, newest first (K = 3).
 v_attempt:=(select count(*) from control.nexloop_twin_calibration_reports r where r.tenant_id=p_tenant and r.twin_id=t.twin_id and r.version=t.version
   and r.conclusion in ('passed','failed'))+case when v_eval->>'conclusion' in ('passed','failed') then 1 else 0 end;
 v_recent:=(select coalesce(jsonb_agg(a.c order by a.at desc,a.o desc),'[]') from (select * from (
    select v_eval->>'conclusion' c,clock_timestamp() at,1 o where v_eval->>'conclusion' in ('passed','failed')
    union all select r.conclusion,r.created_at,0 from control.nexloop_twin_calibration_reports r where r.tenant_id=p_tenant and r.twin_id=t.twin_id
      and r.version=t.version and r.conclusion in ('passed','failed')) u order by u.at desc,u.o desc limit 3) a);
 v_report:=jsonb_build_object('schema','nexloop-twin-calibration-report/1','twin_id',t.twin_id,'version',t.version,'twin_digest',t.definition_digest,
   'world','real','grouping','consumer','windows',v_windows,
   'input',jsonb_build_object('source','authz.nexloop_twin_calibration_aggregates','min_group_consumers',30,
     'input_digest',encode(sha256(convert_to(v_inputs::text,'UTF8')),'hex')),
   'metrics',v_eval-'conclusion'-'reason','conclusion',v_eval->>'conclusion','reason',v_eval->'reason',
   'attempts',jsonb_build_object('attempt',v_attempt,'recent_verdicts',v_recent,'k',3),
   'requested_by',p_principal,'request_id',body->>'request_id','created_at',runtime.nexloop_commercial_ts(clock_timestamp()));
 v_digest:=encode(sha256(convert_to(v_report::text,'UTF8')),'hex');
 insert into control.nexloop_twin_calibration_reports(tenant_id,report_digest,twin_id,version,twin_digest,windows,input_digest,coverage,conclusion,report,
   requested_by,request_id)
  values(p_tenant,v_digest,t.twin_id,t.version,t.definition_digest,v_norm,v_report->'input'->>'input_digest',(v_eval->>'coverage')::numeric,
   v_eval->>'conclusion',v_report,p_principal,body->>'request_id') returning * into rep;
 if rep.conclusion in ('passed','failed') then
  insert into control.nexloop_consumer_twin_calibrations(tenant_id,twin_id,version,report_ref,verdict,recorded_by,request_id)
   values(p_tenant,t.twin_id,t.version,'twin-calibration:'||v_digest,rep.conclusion,p_principal,body->>'request_id');
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('consumer-twin:calibrate:'||v_digest,'nexloop.consumer_twin.calibrated',p_tenant,p_principal,'consumer_twin',p_intent,
   jsonb_build_object('twin_id',t.twin_id,'version',t.version,'report_ref','twin-calibration:'||v_digest,'conclusion',rep.conclusion,'coverage',rep.coverage,
    'windows',jsonb_array_length(v_norm)),clock_timestamp());
 return jsonb_build_object('report_ref','twin-calibration:'||v_digest,'twin_id',t.twin_id,'version',t.version,'conclusion',rep.conclusion,'coverage',rep.coverage,
  'calibration',runtime.nexloop_consumer_twin_calibration(p_tenant,t.twin_id,t.version),'created',true);
end $$;
alter function control.nexloop_governed_calibrate_consumer_twin(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_calibrate_consumer_twin(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('consumer_twin.calibrate','calibrate_consumer_twin','human','control.nexloop_governed_calibrate_consumer_twin(text,text,text,text,text,jsonb)','NX-042');

-- 4. Calibrated (T1 body, replaced): the latest verdict passed and none of the last 3 verdicts failed (K = 3; L6 1.3).
create or replace function runtime.nexloop_consumer_twin_calibration(p_tenant text,p_twin text,p_version integer) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;v_recent_failed boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select c.verdict into v from control.nexloop_consumer_twin_calibrations c where c.tenant_id=p_tenant and c.twin_id=p_twin and c.version=p_version
  order by c.event_id desc limit 1;
 select bool_or(x.verdict='failed') into v_recent_failed from (select c.verdict from control.nexloop_consumer_twin_calibrations c
  where c.tenant_id=p_tenant and c.twin_id=p_twin and c.version=p_version order by c.event_id desc limit 3) x;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return case when v='passed' and not coalesce(v_recent_failed,false) then 'calibrated' else 'uncalibrated' end;
end $$;

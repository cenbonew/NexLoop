-- NX-030 UI part (temporary number 0150): owner-only alert silencing, the workbench alert/audit read with silences, and the
-- NX-031 D6 per-Run tool time sum.
--
-- * nexloop.alert.silence:1 (capability alert.silence, operation silence_alert): a governed human Action through the 0124
--   registry (one row, no change to the entry). Owner only: the role manifest grants it to owner alone, and the handler also
--   requires the caller to be a current workbench owner. A silence names a rule of the current rule version (and optionally one
--   selector), lasts at most 7 days, has a reason, and is append-only. Silenced alerts are still evaluated and recorded; the
--   workbench only marks them (design §3). The evaluator's own staleness is not a rule and cannot be silenced.
-- * authz.nexloop_workbench_observe_read: the 0131 function body with the silences added (create or replace, same signature).
-- * D6: runtime.nexloop_run_tool_timings — the runtime guard adds each tool request's elapsed time (effects submit/find,
--   outcomes record) under its Run once the response is sent; the Runtime Dispatcher writes the sum into the task's terminal
--   result (runtime.jobs.result.tool_timing: calls, total_ms, max_ms). Numbers only.

-- 1. Alert silences --------------------------------------------------------------------------------------------------------
create table control.nexloop_alert_silences (
 silence_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 rule_id text not null check(rule_id~'^[a-z][a-z0-9_]{0,63}$'),selector text check(selector is null or selector~'^[A-Za-z0-9_.:-]{1,64}$'),
 until timestamptz not null,reason text not null check(length(reason) between 1 and 500),principal_id text not null,intent_id text not null,
 created_at timestamptz not null default clock_timestamp(),unique(tenant_id,world,intent_id)
);
alter table control.nexloop_alert_silences owner to nexloop_owner;
alter table control.nexloop_alert_silences enable row level security;
alter table control.nexloop_alert_silences force row level security;
create policy tenant_boundary on control.nexloop_alert_silences to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger nx030_append_only before update or delete on control.nexloop_alert_silences for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_alert_silences from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

create function control.nexloop_governed_alert_silence(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_until timestamptz;v_rules jsonb;
begin
 if body->>'operation' is distinct from 'silence_alert' or coalesce(body->>'rule_id','')!~'^[a-z][a-z0-9_]{0,63}$'
  or (jsonb_typeof(body->'selector') not in ('null','string')) or (jsonb_typeof(body->'selector')='string' and body->>'selector'!~'^[A-Za-z0-9_.:-]{1,64}$')
  or length(coalesce(body->>'reason','')) not between 1 and 500 or jsonb_typeof(body->'until') is distinct from 'string' then
  raise exception 'alert silence invalid' using errcode='22023';end if;
 -- Owner only (D2): the grant is the owner role's; the member's current role is checked again here.
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'alert silence is owner only' using errcode='42501';end if;
 v_until:=(body->>'until')::timestamptz;
 if v_until<=clock_timestamp() or v_until>clock_timestamp()+interval '7 days' then raise exception 'silence must end within 7 days' using errcode='22023';end if;
 select rules into v_rules from control.nexloop_alert_rules where tenant_id=p_tenant order by version desc limit 1;
 if v_rules is null or not exists(select 1 from jsonb_array_elements(v_rules) r where r->>'rule_id'=body->>'rule_id') then
  raise exception 'no such alert rule' using errcode='22023';end if;
 insert into control.nexloop_alert_silences(tenant_id,world,rule_id,selector,until,reason,principal_id,intent_id)
  values(p_tenant,p_world,body->>'rule_id',body->>'selector',v_until,body->>'reason',p_principal,p_intent);
 return jsonb_build_object('silenced',true,'rule_id',body->>'rule_id','selector',body->'selector','until',v_until);
end $$;
alter function control.nexloop_governed_alert_silence(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_alert_silence(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('alert.silence','silence_alert','human','control.nexloop_governed_alert_silence(text,text,text,text,text,jsonb)','NX-030');

-- 2. D6 per-Run tool time --------------------------------------------------------------------------------------------------
create table runtime.nexloop_run_tool_timings (
 tenant_id text not null,world text not null,run_id uuid not null,calls integer not null default 0 check(calls>=0),
 total_ms numeric not null default 0 check(total_ms>=0),max_ms numeric not null default 0 check(max_ms>=0),
 updated_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,run_id)
);
alter table runtime.nexloop_run_tool_timings owner to nexloop_owner;
alter table runtime.nexloop_run_tool_timings enable row level security;
alter table runtime.nexloop_run_tool_timings force row level security;
create policy tenant_boundary on runtime.nexloop_run_tool_timings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_run_tool_timings from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

-- The guard's own service credential; only a Run its tenant issued; numbers only.
create function authz.nexloop_record_run_tool_timing(p_digest text,p_world text,p_run uuid,p_elapsed_ms numeric) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user not in ('nexloop_domain_worker','nexloop_scheduler') or p_elapsed_ms is null or p_elapsed_ms<0 or p_elapsed_ms>600000 then
  raise exception 'tool timing unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 if not exists(select 1 from authz.nexloop_run_credentials r join authz.nexloop_service_credentials s on s.token_digest=r.source_digest
   where r.run_id=p_run and r.world=p_world and s.tenant_id=v_tenant) then raise exception 'tool timing unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 insert into runtime.nexloop_run_tool_timings(tenant_id,world,run_id,calls,total_ms,max_ms) values(v_tenant,p_world,p_run,1,p_elapsed_ms,p_elapsed_ms)
  on conflict(tenant_id,world,run_id) do update set calls=runtime.nexloop_run_tool_timings.calls+1,total_ms=runtime.nexloop_run_tool_timings.total_ms+excluded.total_ms,
   max_ms=greatest(runtime.nexloop_run_tool_timings.max_ms,excluded.max_ms),updated_at=clock_timestamp();
end $$;
create function authz.nexloop_run_tool_timing(p_digest text,p_world text,p_run uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;t runtime.nexloop_run_tool_timings;
begin
 if session_user not in ('nexloop_domain_worker','nexloop_scheduler') then raise exception 'tool timing unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into t from runtime.nexloop_run_tool_timings where tenant_id=v_tenant and world=p_world and run_id=p_run;
 return jsonb_build_object('calls',coalesce(t.calls,0),'total_ms',coalesce(round(t.total_ms,1),0),'max_ms',coalesce(round(t.max_ms,1),0));
end $$;
alter function authz.nexloop_record_run_tool_timing(text,text,uuid,numeric) owner to nexloop_owner;
alter function authz.nexloop_run_tool_timing(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_record_run_tool_timing(text,text,uuid,numeric),authz.nexloop_run_tool_timing(text,text,uuid) from public;
grant execute on function authz.nexloop_record_run_tool_timing(text,text,uuid,numeric),authz.nexloop_run_tool_timing(text,text,uuid) to nexloop_domain_worker,nexloop_scheduler;

-- 3. Workbench observe read (0131 body + silences) and maintenance (+ D6 retention) ------------------------------------------
create or replace function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
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
      'first_fired_at',s.first_fired_at,'last_fired_at',s.last_fired_at,'fire_count',s.fire_count,
      'silenced_until',(select max(si.until) from control.nexloop_alert_silences si where si.tenant_id=s.tenant_id and si.world=s.world and si.rule_id=s.rule_id
        and (si.selector is null or si.selector is not distinct from s.selector) and si.until>clock_timestamp())) order by s.severity,s.first_fired_at)
     from control.nexloop_alert_state s where s.tenant_id=v_tenant and s.world=p_world and s.firing),'[]'::jsonb),
   'events',coalesce((select jsonb_agg(x.v order by x.event_id desc) from (select e.event_id,jsonb_build_object('event_id',e.event_id,'rule_id',e.rule_id,'severity',e.severity,
      'kind',e.kind,'selector',e.selector,'value',e.value::text,'threshold',e.threshold::text,'recorded_at',e.recorded_at) v
     from control.nexloop_alert_events e where e.tenant_id=v_tenant and e.world=p_world order by e.event_id desc limit v_limit) x),'[]'::jsonb),
   'silences',coalesce((select jsonb_agg(jsonb_build_object('rule_id',si.rule_id,'selector',si.selector,'until',si.until,'principal_id',si.principal_id,
      'created_at',si.created_at) order by si.until desc) from control.nexloop_alert_silences si where si.tenant_id=v_tenant and si.world=p_world and si.until>clock_timestamp()),'[]'::jsonb),
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
   union all select jsonb_build_object('category','action','action','alert.silence','principal_id',si.principal_id,'target_kind','alert_rule','target_ref',si.rule_id,
     'intent_id',si.intent_id,'occurred_at',si.created_at) from control.nexloop_alert_silences si where si.tenant_id=v_tenant and si.world=p_world
   union all select jsonb_build_object('category','read','action','read.'||ra.read_purpose,'principal_id',ra.principal_id,'role',ra.role,'target_kind',ra.object_kind,
     'target_ref',ra.target_resource,'intent_id',null,'occurred_at',ra.read_at) from runtime.nexloop_workbench_read_audit ra where ra.tenant_id=v_tenant and ra.world=p_world
  ) u order by (x->>'occurred_at')::timestamptz desc limit v_limit) y(x)),'[]'::jsonb));
end $$;

create or replace function runtime.nexloop_observability_maintain() returns jsonb
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
 -- D6 per-Run tool timings: operational counters of finished Runs, kept as long as the minute samples (D5).
 delete from runtime.nexloop_run_tool_timings where updated_at<clock_timestamp()-interval '7 days';
 return jsonb_build_object('rolled',v_rolled,'purged_minutes',v_minutes,'purged_hours',v_hours);
end $$;

-- NX-029 completion (temporary number 0142): the remaining expiry classes of the design table (§3), the expiry of the
-- deletion ledger and tombstones, and the value copies of an erased Claim. Owner decisions D2, D3, D6; NX-035 D4;
-- dispatcher ruling 2026-10-11. Executed by the retention keeper through its existing sweep port; holds apply first.
--
-- 1. Settings v4 (retention.v1.json): plan_closed 365, effect_text 90 (follows message_text), job_records 365,
--    model_request_manifest 365, read_audit 365, governance_audit 365; the keeper also runs 'records' (ledger and
--    tombstones, settings.records). See docs/implementation/NX-029-implementation.md §11 for each class.
-- 2. records - minimum retention. A backup is restorable for at most backup_max_retention_days (B) after it started.
--    After restoring a backup started at T_b (deletion watermark W_b taken at its start, NX-035 §5) at T_r <= T_b + B,
--    replay needs every ledger entry newer than W_b, i.e. recorded at or after T_b: at most T_r - T_b <= B days old. So
--    the ledger (and the tombstones that prove each deletion) must be kept strictly longer than B: the settings check
--    (0135) refuses any version with B >= min(deletion_ledger_days, tombstone_days), which makes the shortest allowed
--    value B + 1 days (36 with B = 35). The default is 365 (D6, deletion credentials one year). The sweep never deletes
--    while a restore replay is open (runtime.nexloop_erasure_readiness), so a replay in progress keeps its entries.
--    NX-035's off-database restore journal (not yet implemented) exports ledger entries within minutes; when it lands,
--    this sweep must also stay below its exported sequence (recorded here as an NX-035 integration point).
-- 3. A Claim whose value becomes erased (Consumer erasure, or claim_superseded after a year) also loses the value copies
--    kept by NX-020: its mutation proposals (proposal, apply arguments, receipt) and its match decisions and recall.
-- 4. The NX-023 model request guard admits a registered erasure for deletion (body replaced in place). Read audit rows
--    leave folded into runtime.nexloop_read_audit_daily (day, purpose, object kind, count; no principal, no target).
-- Published migrations are not edited.

-- 1. Settings v4 ----------------------------------------------------------------------------------------------------------
create or replace function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation','run_files','commitment_closed','claim_superseded',
   'plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit')
   or jsonb_typeof(v->'days') is distinct from 'number' or (v->>'days')!~'^[0-9]{1,5}$' or (v->>'days')::integer not between 1 and 36500
   or (jsonb_typeof(v->'source')<>'null' and (new.definition->'classes'->(v->>'source') is null
     or (v->>'days')::integer>(new.definition->'classes'->(v->>'source')->>'days')::integer)) then
   raise exception 'retention class % invalid',k using errcode='22023';end if;
 end loop;
 if (new.definition->'sweep'->>'batch')::integer not between 1 and 1000 or (new.definition->'sweep'->>'max_passes')::integer not between 1 and 100
  or (new.definition->'sweep'->>'interval_seconds')::integer not between 60 and 86400 then
  raise exception 'retention sweep settings invalid' using errcode='22023';end if;
 return new;
end $$;
insert into control.nexloop_retention_settings(version,definition,definition_digest,published_by)
 values(4,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"claim_superseded":{"days":365,"source":null},"commercial_receipt":{"days":90,"source":null},"commitment_closed":{"days":365,"source":null},"effect_text":{"days":90,"source":"message_text"},"governance_audit":{"days":365,"source":null},"job_input":{"days":30,"source":"message_text"},"job_records":{"days":365,"source":null},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"model_request_manifest":{"days":365,"source":null},"plan_closed":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"},"read_audit":{"days":365,"source":null},"run_files":{"days":30,"source":null}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused. v3 (NX-029 final, AT-026): three expiry classes the keeper now executes - run_files: a Pi Run directory 30 days after its Run is terminal (queued for the Agent Host runs/purge, deleted only when the Host confirms; ADR-005, 06:92); commitment_closed: a Commitment 365 days after it was closed (fulfilled, cancelled or breached), object and ledger deleted (D4); claim_superseded: the structured value and remaining text of a superseded Claim 365 days after it was superseded, ids and the correction chain kept (D2). v4 (NX-029 completion): the remaining expiry classes of the design table - plan_closed: a plan 365 days after its last status became closed or invalidated, all versions, steps, events, Runs, outcomes and its unshared strategies deleted (D3); effect_text: the frozen request text of a terminal effect intent 90 days after it was created, states, receipts and digests kept (follows message_text; ADR-021); job_records: 365 days after a task became terminal its events, inbox and outbox rows and old idempotency records are deleted, the task row too unless a Run binding still refers to it (then its payloads are erased); model_request_manifest: model request rows and their prompt rows 365 days after the request (D3; cost entries keep their own financial retention); read_audit: read audit rows 365 days after the read, folded into per-day counts (D6); governance_audit: audit events, superseded control events (the latest per scope stays), human requests, export requests and completed deletion requests whose ledger entries are gone, 365 days after the event (D6). records: ledger entries and tombstones leave after deletion_ledger_days / tombstone_days, never while a restore replay is open; both must exceed backup_max_retention_days.","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":4}',
  '7ce0b377ab73e02e6001def0c0e88f78d320aff3ca1243b86e9d108a41e15974','migration:0142');

-- The keeper runs every configured class, then financial (NX-027) and records (0129 body plus 'records').
create or replace function runtime.nexloop_retention_classes() returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select array(select k from jsonb_object_keys(runtime.nexloop_retention_settings_current()->'classes') k order by k)||array['financial','records']
$$;

-- 3. Value copies of an erased Claim ----------------------------------------------------------------------------------------
create function ontology.nexloop_claim_value_erased() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if new.value ? 'erased' and not (old.value ? 'erased') then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  update ontology.nexloop_mutation_proposals set proposal='{"erased":true}'::jsonb,
    apply_args=case when apply_args is null then null else '{"erased":true}'::jsonb end,receipt=case when receipt is null then null else '{"erased":true}'::jsonb end
   where tenant_id=new.tenant_id and world=new.world and claim_id=new.claim_id and not (proposal ? 'erased');
  update ontology.nexloop_claim_matches set decision='{"erased":true}'::jsonb,recall='[]'::jsonb
   where tenant_id=new.tenant_id and world=new.world and claim_id=new.claim_id and not (decision ? 'erased');
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
 end if;
 return null;
end $$;
alter function ontology.nexloop_claim_value_erased() owner to nexloop_owner;
revoke all on function ontology.nexloop_claim_value_erased() from public;
create trigger nx029_claim_value_erased after update of value on ontology.nexloop_claims for each row execute function ontology.nexloop_claim_value_erased();

-- 4. Model request guard admits a registered erasure (0089 body, one branch) ---------------------------------------------
create or replace function runtime.nexloop_model_request_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_previous integer;
begin
 if tg_op='DELETE' and runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_model_requests','DELETE',to_jsonb(old),null) then return old;end if;
 if tg_op='DELETE' then raise exception 'model requests are append-only' using errcode='22023';end if;
 if tg_op='INSERT' then
  if new.result_status is not null then raise exception 'model request result must be recorded after the request' using errcode='22023';end if;
  perform pg_advisory_xact_lock(hashtextextended(new.tenant_id||':'||new.world||':model-request:'||new.run_id::text,23));
  select max(call_sequence) into v_previous from runtime.nexloop_model_requests where tenant_id=new.tenant_id and world=new.world and run_id=new.run_id;
  if new.call_sequence is distinct from coalesce(v_previous,0)+1 then raise exception 'model request call_sequence must be dense' using errcode='22023';end if;
  return new;
 end if;
 if old.result_status is not null or new.result_status is null
  or (new.tenant_id,new.world,new.run_id,new.call_sequence,new.context_id,new.model_provider,new.model_id,new.tool_manifest_digest,new.request_digest,
      new.prompt_artifact_id,new.input_token_budget,new.output_token_budget,new.settings_digest,new.requested_at)
   is distinct from (old.tenant_id,old.world,old.run_id,old.call_sequence,old.context_id,old.model_provider,old.model_id,old.tool_manifest_digest,old.request_digest,
      old.prompt_artifact_id,old.input_token_budget,old.output_token_budget,old.settings_digest,old.requested_at) then
  raise exception 'model request is immutable; its result is recorded once' using errcode='22023';end if;
 return new;
end $$;

create table runtime.nexloop_read_audit_daily (
 tenant_id text not null,world text not null,day date not null,read_purpose text not null,object_kind text not null,reads bigint not null check(reads>=0),
 primary key(tenant_id,world,day,read_purpose,object_kind)
);
alter table runtime.nexloop_read_audit_daily owner to nexloop_owner;
alter table runtime.nexloop_read_audit_daily enable row level security;
alter table runtime.nexloop_read_audit_daily force row level security;
create policy tenant_boundary on runtime.nexloop_read_audit_daily to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_read_audit_daily from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- 1-2. The classes (one bounded batch per call; 'more' while any part filled its batch) -----------------------------------
create function runtime.nexloop_retention_sweep_records(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz;n integer:=0;m integer:=0;o integer:=0;q integer:=0;u integer:=0;x record;v_counts jsonb:='{}'::jsonb;
 v_terminal text[]:=array['succeeded','failed','dead_lettered','cancelled'];v_ids bigint[];r jsonb:=runtime.nexloop_retention_settings_current()->'records';
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_class<>'records' then
  v_cut:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));
  if v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 end if;
 if p_class='plan_closed' then
  -- D3: a plan whose current status (its last event) is closed or invalidated, a year after that event; every version.
  perform runtime.nexloop_erasure_open('runtime.nexloop_plans',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_events',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_steps',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_runs',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_outcomes',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_strategies',null,true,p_ref);
  for x in select l.plan_id from (select distinct on (e.plan_id) e.plan_id,e.status,e.recorded_at from runtime.nexloop_plan_events e
     where e.tenant_id=p_tenant and e.world=p_world order by e.plan_id,e.event_id desc) l
    where l.status in ('closed','invalidated') and l.recorded_at<v_cut order by l.plan_id limit p_batch loop
   delete from runtime.nexloop_plan_outcomes where tenant_id=p_tenant and world=p_world and plan_id=x.plan_id;
   delete from runtime.nexloop_plan_runs where tenant_id=p_tenant and world=p_world and plan_id=x.plan_id;
   delete from runtime.nexloop_plan_steps where tenant_id=p_tenant and world=p_world and plan_id=x.plan_id;
   delete from runtime.nexloop_plan_events where tenant_id=p_tenant and world=p_world and plan_id=x.plan_id;
   -- Strategies this plan referred to and no remaining plan does.
   delete from runtime.nexloop_strategies s where s.tenant_id=p_tenant and s.world=p_world
    and 'strategy:'||s.strategy_id||'@'||s.version in (select p.strategy_ref from runtime.nexloop_plans p where p.tenant_id=p_tenant and p.world=p_world
      and p.plan_id=x.plan_id and p.strategy_ref is not null)
    and not exists(select 1 from runtime.nexloop_plans p where p.tenant_id=p_tenant and p.world=p_world and p.plan_id<>x.plan_id
      and p.strategy_ref='strategy:'||s.strategy_id||'@'||s.version);
   get diagnostics m=row_count;
   delete from runtime.nexloop_plans where tenant_id=p_tenant and world=p_world and plan_id=x.plan_id;
   delete from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='plan-reevaluate' and item_key='plan:'||x.plan_id;
   perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,'plan_closed','plan',x.plan_id::text,'deleted',jsonb_build_object('strategies',m),p_by);
   n:=n+1;o:=o+m;
  end loop;
  v_counts:=jsonb_build_object('plans',n,'strategies',o);
 elsif p_class='effect_text' then
  -- The text sent or requested, 90 days after the intent; never an intent still in flight.
  with due as (select intent_id from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world and state in ('fulfilled','failed','confirmed')
    and created_at<v_cut and not (frozen_request ? 'erased') order by created_at limit p_batch for update skip locked)
  update runtime.nexloop_effect_intents i set frozen_request='{"erased":true}'::jsonb from due where i.intent_id=due.intent_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('intents',n);
 elsif p_class='job_records' then
  for x in select j.job_id,exists(select 1 from authz.nexloop_runtime_run_bindings b where b.tenant_id=j.tenant_id and b.task_id=j.job_id) bound
    from runtime.jobs j where j.tenant_id=p_tenant and j.world=p_world and j.status=any(v_terminal) and j.updated_at<v_cut
     and (not exists(select 1 from authz.nexloop_runtime_run_bindings b where b.tenant_id=j.tenant_id and b.task_id=j.job_id)
      or j.normalized_input is distinct from '{"erased":true}'::jsonb)
    order by j.updated_at,j.job_id limit p_batch for update of j skip locked loop
   delete from runtime.job_events where tenant_id=p_tenant and job_id=x.job_id;
   delete from runtime.nexloop_inbox where tenant_id=p_tenant and job_id=x.job_id;
   delete from runtime.nexloop_outbox where tenant_id=p_tenant and job_id=x.job_id;
   if x.bound then
    -- The Run binding (authz registry) still names the task: keep the row, erase its payloads.
    update runtime.jobs set normalized_input='{"erased":true}'::jsonb,plan_snapshot='{"erased":true}'::jsonb,
     result=case when result is null then null else '{"erased":true}'::jsonb end,error=case when error is null then null else '{"erased":true}'::jsonb end
     where tenant_id=p_tenant and job_id=x.job_id;
    m:=m+1;
   else
    delete from runtime.jobs where tenant_id=p_tenant and job_id=x.job_id;
    n:=n+1;
   end if;
  end loop;
  with due as (select operation,idempotency_key from runtime.idempotency_records where tenant_id=p_tenant and created_at<v_cut
    order by created_at limit p_batch for update skip locked)
  delete from runtime.idempotency_records r using due where r.tenant_id=p_tenant and r.operation=due.operation and r.idempotency_key=due.idempotency_key;
  get diagnostics o=row_count;
  v_counts:=jsonb_build_object('jobs_deleted',n,'jobs_redacted',m,'idempotency_records',o);n:=greatest(n+m,o);
 elsif p_class='model_request_manifest' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_model_requests',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',null,true,p_ref);
  for x in select run_id,call_sequence from runtime.nexloop_model_requests where tenant_id=p_tenant and world=p_world and requested_at<v_cut
    order by requested_at limit p_batch for update skip locked loop
   delete from runtime.nexloop_model_request_prompts where tenant_id=p_tenant and world=p_world and run_id=x.run_id and call_sequence=x.call_sequence;
   delete from runtime.nexloop_model_requests where tenant_id=p_tenant and world=p_world and run_id=x.run_id and call_sequence=x.call_sequence;
   n:=n+1;
  end loop;
  v_counts:=jsonb_build_object('model_requests',n);
 elsif p_class='read_audit' then
  -- D6: rows leave; only per-day counts by purpose and object kind stay (no principal, session or target).
  perform runtime.nexloop_erasure_open('runtime.nexloop_workbench_read_audit',null,true,p_ref);
  v_ids:=array(select audit_id from runtime.nexloop_workbench_read_audit where tenant_id=p_tenant and world=p_world and read_at<v_cut
   order by audit_id limit p_batch for update skip locked);
  insert into runtime.nexloop_read_audit_daily(tenant_id,world,day,read_purpose,object_kind,reads)
   select p_tenant,p_world,(read_at at time zone 'UTC')::date,read_purpose,object_kind,count(*) from runtime.nexloop_workbench_read_audit
    where tenant_id=p_tenant and world=p_world and audit_id=any(v_ids) group by 3,4,5
   on conflict(tenant_id,world,day,read_purpose,object_kind) do update set reads=runtime.nexloop_read_audit_daily.reads+excluded.reads;
  delete from runtime.nexloop_workbench_read_audit where tenant_id=p_tenant and world=p_world and audit_id=any(v_ids);
  get diagnostics n=row_count;v_counts:=jsonb_build_object('reads',n);
 elsif p_class='governance_audit' then
  perform runtime.nexloop_erasure_open('control.nexloop_control_events',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_human_requests',null,true,p_ref);
  perform runtime.nexloop_erasure_open('control.nexloop_consumer_erasures',null,true,p_ref);
  perform runtime.nexloop_erasure_open('control.nexloop_data_exports',null,true,p_ref);
  with due as (select event_id from runtime.audit_events where tenant_id=p_tenant and created_at<v_cut order by created_at limit p_batch for update skip locked)
  delete from runtime.audit_events a using due where a.event_id=due.event_id;
  get diagnostics n=row_count;
  -- A control event leaves only when a newer one of the same scope exists (the latest stays as the scope's last word).
  with due as (select e.revision from control.nexloop_control_events e where e.tenant_id=p_tenant and e.world=p_world and e.recorded_at<v_cut
    and exists(select 1 from control.nexloop_control_events f where f.tenant_id=e.tenant_id and f.world=e.world and f.scope_kind=e.scope_kind
     and f.scope_ref=e.scope_ref and f.revision>e.revision) order by e.revision limit p_batch for update skip locked)
  delete from control.nexloop_control_events e using due where e.tenant_id=p_tenant and e.world=p_world and e.revision=due.revision;
  get diagnostics m=row_count;
  with due as (select intent_id from runtime.nexloop_human_requests where tenant_id=p_tenant and world=p_world and requested_at<v_cut
    order by requested_at limit p_batch for update skip locked)
  delete from runtime.nexloop_human_requests h using due where h.tenant_id=p_tenant and h.world=p_world and h.intent_id=due.intent_id;
  get diagnostics o=row_count;
  -- A deletion request is the credential of its deletion: it stays while its ledger entry does (records).
  with due as (select request_id from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=p_world
    and e.state in ('completed','completed_with_holds') and e.completed_at<v_cut
    and not exists(select 1 from control.nexloop_deletion_ledger l where l.tenant_id=e.tenant_id and l.world=e.world and l.request_ref=e.request_id)
    order by e.completed_at limit p_batch for update skip locked)
  delete from control.nexloop_consumer_erasures e using due where e.tenant_id=p_tenant and e.world=p_world and e.request_id=due.request_id;
  get diagnostics q=row_count;
  with due as (select export_id from control.nexloop_data_exports where tenant_id=p_tenant and world=p_world and expires_at<v_cut
    order by expires_at limit p_batch for update skip locked)
  delete from control.nexloop_data_exports d using due where d.tenant_id=p_tenant and d.world=p_world and d.export_id=due.export_id;
  get diagnostics u=row_count;
  v_counts:=jsonb_build_object('audit_events',n,'control_events',m,'human_requests',o,'erasure_requests',q,'exports',u);n:=greatest(n,m,o,q,u);
 elsif p_class='records' then
  -- Never while a restore replay is open; ledger and tombstones each after their own days (both > backup retention).
  if not (runtime.nexloop_erasure_readiness()->>'ready')::boolean then
   return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts',jsonb_build_object('replay_open',true));
  end if;
  perform runtime.nexloop_erasure_open('control.nexloop_deletion_ledger',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_erasure_tombstones',null,true,p_ref);
  with due as (select seq from control.nexloop_deletion_ledger where tenant_id=p_tenant and world=p_world
    and recorded_at<clock_timestamp()-make_interval(days=>(r->>'deletion_ledger_days')::integer) order by seq limit p_batch for update skip locked)
  delete from control.nexloop_deletion_ledger l using due where l.seq=due.seq;
  get diagnostics n=row_count;
  with due as (select tombstone_id from runtime.nexloop_erasure_tombstones where tenant_id=p_tenant and world=p_world
    and executed_at<clock_timestamp()-make_interval(days=>(r->>'tombstone_days')::integer) order by executed_at limit p_batch for update skip locked)
  delete from runtime.nexloop_erasure_tombstones t using due where t.tenant_id=p_tenant and t.world=p_world and t.tombstone_id=due.tombstone_id;
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  return jsonb_build_object('item_class',p_class,'processed',n+m,'more',greatest(n,m)>=p_batch,'counts',jsonb_build_object('ledger',n,'tombstones',m));
 else
  raise exception 'retention class unavailable' using errcode='22023';
 end if;
 perform runtime.nexloop_erasure_close();
 if p_class not in ('plan_closed') and exists(select 1 from jsonb_each_text(v_counts) c where c.value::bigint>0) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,
   case when p_class='effect_text' then 'redacted' else 'deleted' end,v_counts,p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n,'more',n>=p_batch,'counts',v_counts,'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep_records(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_records(text,text,text,integer,text,text) from public;
-- The 0132/0141 hold wrapper dispatches them (body replaced in place; holds checked first as before).
create or replace function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
 end if;
 if p_class in ('plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','records') then
  return runtime.nexloop_retention_sweep_records(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class in ('run_files','commitment_closed','claim_superseded') then
  return runtime.nexloop_retention_sweep_expiry(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 return runtime.nexloop_retention_sweep_before_holds_v0142(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
end $$;

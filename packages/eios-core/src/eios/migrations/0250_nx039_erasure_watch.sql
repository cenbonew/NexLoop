-- NX-039 V5 "erasure propagation and the watch window" (design docs/implementation/NX-039-design.md §8.4, §9, §11 V5; owner D39-9,
-- D39-10, D39-11, D39-12 decided 2026-10-20). Temporary number; the dispatcher renumbers at merge.
--
-- 1. Case references (D39-11). From here on a recorded evaluation report carries no case or access id: authz.nexloop_evo_evaluation_conclude
--    (0248 body, replaced in place) stores the report with each case id (swaps, `case:` evidence) and access id (`access:` evidence)
--    replaced by a random pseudonym, and keeps the pseudonym -> id (and its Run) in runtime.nexloop_evo_case_refs, a table that may
--    be deleted. The report digest is over the stored, pseudonymous report; a replay is recognised by the digest of what was
--    submitted (runtime.nexloop_evo_report_submissions). Erasing the references makes the pseudonyms unlinkable; the report itself
--    stays (append only), with digests and counts.
-- 2. Erasure (NX-029; §9): when a Consumer's erasure purges (insert or state -> purging / completed), for the Runs of that Consumer:
--    their case references are deleted; every holdout that holds one of them is invalidated (an opening answers holdout_exhausted)
--    and the Run ids are removed from it; evaluating candidates with such a case end incomplete/missing_data (E4 already ends
--    proposed and accepted ones) and their L1 sessions close. A tombstone records counts only. Published release items (V4) follow
--    the same rule through the release path: runtime.nexloop_evo_release_erasure is the hook V4 replaces (here it reports 0).
-- 3. Retention class evolution_records (D39-12; settings v6, retention.v1.json): reports, their review links and case references
--    365 days after they were recorded; the reports of the current semantic release and the one before it are kept
--    (runtime.nexloop_evo_retained_reports, which V4's release path replaces; here it keeps nothing extra).
-- 4. The watch window (D39-10; §8.4): runtime.nexloop_evo_watch_start(tenant, world, release_ref, published_at) opens a 7-day watch
--    (V4's publish calls it). runtime.nexloop_evo_release_watch compares, from the E3 records, the miss rate and the ambiguity rate of
--    the week before publication with the time since; a rise of more than 2 points of either, with at least 20 accesses on each
--    side, is `regressing` with the suggestion `rollback` - never an automatic rollback. The metrics snapshot gains the block
--    'evolution' (release_watch.active / .regressing / .releases); alert rules v7 warn on it.
-- Published migrations are not edited.

-- 1. Case references ---------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_evo_case_refs (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),pseudonym uuid not null,
 kind text not null check(kind in ('case','access')),original_id text not null check(char_length(original_id) between 1 and 160),run_id uuid,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,report_digest,pseudonym));
create index nexloop_evo_case_refs_run on runtime.nexloop_evo_case_refs(tenant_id,world,run_id);
create table runtime.nexloop_evo_report_submissions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),report_id uuid not null,
 submitted_digest text not null check(submitted_digest~'^[0-9a-f]{64}$'),report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 primary key(tenant_id,world,report_id));
create table runtime.nexloop_evo_holdout_invalidations (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),holdout_digest text not null,
 reason text not null check(reason in ('missing_data')),request_ref text not null check(char_length(request_ref) between 1 and 200),
 invalidated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,holdout_digest));
do $$declare t text;begin
 foreach t in array array['nexloop_evo_case_refs','nexloop_evo_report_submissions','nexloop_evo_holdout_invalidations'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator',t);
 end loop;
end $$;

-- The frozen evaluation rows (0237 / 0241 bodies, replaced in place): still never changed, except by the two owner paths of this
-- migration under their transaction-local flags - retention deletes expired reports and links; erasure removes a Run id from a holdout.
create or replace function runtime.nexloop_evo_frozen_rows() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and current_user='nexloop_owner' and current_setting('nexloop.evo_retention',true)='on'
  and tg_table_name in ('nexloop_evo_reports','nexloop_evo_review_links') then return old;end if;
 -- Integration with V4 (D39-12): the semantic_release sweep deletes an expired report under its own flag.
 if tg_op='DELETE' and tg_table_name='nexloop_evo_reports' and current_user='nexloop_owner' and current_setting('nexloop.semantic_release_purge',true)='on' then
  return old;end if;
 raise exception 'evaluation bundles, reports and review links are frozen' using errcode='42501';
end $$;
create or replace function runtime.nexloop_evo_holdout_frozen() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and tg_table_name='nexloop_evo_holdouts' and current_user='nexloop_owner' and current_setting('nexloop.evo_erasure',true)='on' then
  if new.tenant_id=old.tenant_id and new.world=old.world and new.holdout_digest=old.holdout_digest and new.registered_at=old.registered_at
   and new.case_ids<@old.case_ids and cardinality(new.case_ids)>=1 then return new;end if;
 end if;
 raise exception 'holdouts and their uses are frozen' using errcode='42501';
end $$;

-- 2. Erasure -----------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_evo_release_erasure(p_tenant text,p_world text,p_consumer text,p_ref text,p_by text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select 0 $$;
alter function runtime.nexloop_evo_release_erasure(text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_release_erasure(text,text,text,text,text) from public;

-- The Runs of a Consumer: the erasure's own list (role bindings, runtime bindings) and, besides, the Runs a message of the
-- Consumer's conversations was routed to (message Runs carry no runtime binding to a Consumer).
create function runtime.nexloop_evo_consumer_runs(p_tenant text,p_world text,p_consumer text) returns setof uuid
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select r from runtime.nexloop_consumer_runs(p_tenant,p_world,p_consumer) r
 union
 select mr.run_id from runtime.nexloop_message_routes mr join runtime.nexloop_conversation_messages m on m.tenant_id=mr.tenant_id and m.world=mr.world
   and m.message_id=mr.message_id join runtime.nexloop_conversations c on c.tenant_id=m.tenant_id and c.world=m.world and c.conversation_id=m.conversation_id
  where mr.tenant_id=p_tenant and mr.world=p_world and c.consumer_id=p_consumer and mr.run_id is not null
$$;
alter function runtime.nexloop_evo_consumer_runs(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_consumer_runs(text,text,text) from public;

create function runtime.nexloop_evo_erase_evaluations(p_tenant text,p_world text,p_consumer text,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare runs text[];n_refs integer:=0;n_holdouts integer:=0;n_candidates integer:=0;n_sessions integer:=0;n_release integer;h record;x record;s record;
begin
 if p_world<>'real' then return '{}'::jsonb;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 runs:=array(select r::text from runtime.nexloop_evo_consumer_runs(p_tenant,p_world,p_consumer) r);
 if cardinality(runs)=0 then return '{}'::jsonb;end if;
 delete from runtime.nexloop_evo_case_refs where tenant_id=p_tenant and world=p_world and (run_id::text=any(runs) or original_id=any(runs));
 get diagnostics n_refs=row_count;
 perform set_config('nexloop.evo_erasure','on',true);
 for h in select holdout_digest,case_ids from runtime.nexloop_evo_holdouts where tenant_id=p_tenant and world=p_world and case_ids&&runs loop
  insert into runtime.nexloop_evo_holdout_invalidations(tenant_id,world,holdout_digest,reason,request_ref) values(p_tenant,p_world,h.holdout_digest,'missing_data',p_ref)
   on conflict do nothing;
  if cardinality(array(select c from unnest(h.case_ids) c where not c=any(runs)))>=1 then
   update runtime.nexloop_evo_holdouts set case_ids=array(select c from unnest(h.case_ids) c where not c=any(runs) order by c)
    where tenant_id=p_tenant and world=p_world and holdout_digest=h.holdout_digest;
  end if;
  n_holdouts:=n_holdouts+1;
 end loop;
 perform set_config('nexloop.evo_erasure','',true);
 for x in select candidate_id from runtime.nexloop_evo_candidates where tenant_id=p_tenant and world=p_world and status='evaluating'
   and (design_case_ids&&runs or validation_case_ids&&runs) loop
  if runtime.nexloop_evo_candidate_incomplete(p_tenant,x.candidate_id,'missing_data','invalidated',jsonb_build_object('request',p_ref),p_by) then
   n_candidates:=n_candidates+1;end if;
 end loop;
 for s in select l.session_id from runtime.nexloop_evo_l1_sessions l join runtime.nexloop_evo_candidates c on c.tenant_id=l.tenant_id and c.world=l.world
   and c.candidate_id=l.candidate_id where l.tenant_id=p_tenant and l.world=p_world and l.status='open' and c.status<>'evaluating' loop
  perform runtime.nexloop_evo_l1_cleanup(p_tenant,s.session_id,'candidate_ended');n_sessions:=n_sessions+1;
 end loop;
 n_release:=runtime.nexloop_evo_release_erasure(p_tenant,p_world,p_consumer,p_ref,p_by);
 if n_refs+n_holdouts+n_candidates+n_sessions+n_release>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',p_ref,'evolution_records','consumer_runs',p_consumer,'deleted',
   jsonb_build_object('case_refs',n_refs,'holdouts',n_holdouts,'candidates',n_candidates,'l1_sessions',n_sessions,'release_items',n_release),p_by);
 end if;
 return jsonb_build_object('case_refs',n_refs,'holdouts',n_holdouts,'candidates',n_candidates,'l1_sessions',n_sessions,'release_items',n_release);
end $$;
alter function runtime.nexloop_evo_erase_evaluations(text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_erase_evaluations(text,text,text,text,text) from public;

create function control.nexloop_evo_evaluation_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.state in ('purging','completed') and (tg_op='INSERT' or old.state is distinct from new.state) then
  perform runtime.nexloop_evo_erase_evaluations(new.tenant_id,new.world,new.consumer_id,'erasure:'||new.request_id,'service:retention_keeper');
 end if;
 return null;
end $$;
alter function control.nexloop_evo_evaluation_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_evo_evaluation_erasure() from public;
create trigger nx039_evo_evaluation_erasure after insert or update of state on control.nexloop_consumer_erasures
 for each row execute function control.nexloop_evo_evaluation_erasure();

-- 3. Retention class evolution_records ----------------------------------------------------------------------------------------------
create or replace function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation','run_files','commitment_closed','claim_superseded',
   'plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','semantic_access','semantic_release','evolution_records')
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
 values(7,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"claim_superseded":{"days":365,"source":null},"commercial_receipt":{"days":90,"source":null},"commitment_closed":{"days":365,"source":null},"effect_text":{"days":90,"source":"message_text"},"evolution_records":{"days":365,"source":null},"governance_audit":{"days":365,"source":null},"job_input":{"days":30,"source":"message_text"},"job_records":{"days":365,"source":null},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"model_request_manifest":{"days":365,"source":null},"plan_closed":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"},"read_audit":{"days":365,"source":null},"run_files":{"days":30,"source":null},"semantic_access":{"days":90,"source":null},"semantic_release":{"days":365,"source":null}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused. v3 (NX-029 final, AT-026): three expiry classes the keeper now executes - run_files: a Pi Run directory 30 days after its Run is terminal (queued for the Agent Host runs/purge, deleted only when the Host confirms; ADR-005, 06:92); commitment_closed: a Commitment 365 days after it was closed (fulfilled, cancelled or breached), object and ledger deleted (D4); claim_superseded: the structured value and remaining text of a superseded Claim 365 days after it was superseded, ids and the correction chain kept (D2). v4 (NX-029 completion): the remaining expiry classes of the design table - plan_closed: a plan 365 days after its last status became closed or invalidated, all versions, steps, events, Runs, outcomes and its unshared strategies deleted (D3); effect_text: the frozen request text of a terminal effect intent 90 days after it was created, states, receipts and digests kept (follows message_text; ADR-021); job_records: 365 days after a task became terminal its events, inbox and outbox rows and old idempotency records are deleted, the task row too unless a Run binding still refers to it (then its payloads are erased); model_request_manifest: model request rows and their prompt rows 365 days after the request (D3; cost entries keep their own financial retention); read_audit: read audit rows 365 days after the read, folded into per-day counts (D6); governance_audit: audit events, superseded control events (the latest per scope stays), human requests, export requests and completed deletion requests whose ledger entries are gone, 365 days after the event (D6). records: ledger entries and tombstones leave after deletion_ledger_days / tombstone_days, never while a restore replay is open; both must exceed backup_max_retention_days. v5 (NX-038 E3, owner D2 2026-10-12): semantic_access - the append-only per-access records of the Context Engine''s semantic browse/resolve (term hashes, refs and counts, no text) are deleted 90 days after they were recorded; a Consumer erasure deletes the records of that Consumer''s Runs when purging begins. v6 (NX-039 V4, owner D39-12 2026-10-20): semantic_release - semantic release records (with their items and rollback reports) and evaluation reports are deleted 365 days after they were recorded; the current release version and the one before it are always kept, and so is a report that a kept release or a review link still names. v7 (NX-039 V5, owner D39-12 2026-10-20): evolution_records - evaluation reports with their review links and case references 365 days after they were recorded; the reports of the current semantic release and the one before it are kept (the release publication path names them). (v6 on its own line; v6 here is semantic_release.)","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":7}','a121cd41658ce1340bdf8540832ceb0aa8eae9df9d62f5b983b45c990a128553','migration:nx039-v5');

-- The reports V4's release path keeps (current release and the one before it); none until it replaces this.
create function runtime.nexloop_evo_retained_reports(p_tenant text,p_world text) returns setof text
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select null::text where false $$;
alter function runtime.nexloop_evo_retained_reports(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_retained_reports(text,text) from public;

create function runtime.nexloop_retention_sweep_evolution(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz;digests text[];n_links integer;n_refs integer;n_reports integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v_cut:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));
 if p_class<>'evolution_records' or v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 digests:=array(select r.report_digest from runtime.nexloop_evo_reports r where r.tenant_id=p_tenant and r.world=p_world and r.recorded_at<v_cut
   and r.report_digest not in (select x from runtime.nexloop_evo_retained_reports(p_tenant,p_world) x where x is not null)
   order by r.recorded_at limit p_batch);
 perform set_config('nexloop.evo_retention','on',true);
 delete from runtime.nexloop_evo_review_links where tenant_id=p_tenant and world=p_world and report_digest=any(digests);
 get diagnostics n_links=row_count;
 delete from runtime.nexloop_evo_case_refs where tenant_id=p_tenant and world=p_world and report_digest=any(digests);
 get diagnostics n_refs=row_count;
 delete from runtime.nexloop_evo_report_submissions where tenant_id=p_tenant and world=p_world and report_digest=any(digests);
 delete from runtime.nexloop_evo_reports where tenant_id=p_tenant and world=p_world and report_digest=any(digests);
 get diagnostics n_reports=row_count;
 perform set_config('nexloop.evo_retention','',true);
 if n_reports>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'deleted',
   jsonb_build_object('reports',n_reports,'review_links',n_links,'case_refs',n_refs),p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n_reports,'more',n_reports>=p_batch,
  'counts',jsonb_build_object('reports',n_reports,'review_links',n_links,'case_refs',n_refs),'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep_evolution(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_evolution(text,text,text,integer,text,text) from public;

-- The hold wrapper (0234 body, replaced in place) dispatches the new class; holds are checked first as before.
create or replace function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
 end if;
 if p_class='evolution_records' then
  return runtime.nexloop_retention_sweep_evolution(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class='semantic_release' then  -- V4 (integration)
  return runtime.nexloop_retention_sweep_semantic_release(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class='semantic_access' then
  return runtime.nexloop_retention_sweep_semantic_access(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class in ('plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','records') then
  return runtime.nexloop_retention_sweep_records(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class in ('run_files','commitment_closed','claim_superseded') then
  return runtime.nexloop_retention_sweep_expiry(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 return runtime.nexloop_retention_sweep_before_holds_v0142(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
end $$;

-- 4. The watch window -------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_evo_release_watches (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 release_ref text not null check(release_ref~'^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}$'),
 published_at timestamptz not null,watch_until timestamptz not null,started_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,release_ref),check(watch_until=published_at+interval '7 days'));
alter table runtime.nexloop_evo_release_watches owner to nexloop_owner;
alter table runtime.nexloop_evo_release_watches enable row level security;
alter table runtime.nexloop_evo_release_watches force row level security;
create policy tenant_boundary on runtime.nexloop_evo_release_watches to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_evo_release_watches from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator;

-- Called by the release path (V4) in the publishing transaction; no role holds it.
create function runtime.nexloop_evo_watch_start(p_tenant text,p_world text,p_release text,p_published timestamptz) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_evo_release_watches(tenant_id,world,release_ref,published_at,watch_until)
  values(p_tenant,p_world,p_release,p_published,p_published+interval '7 days') on conflict do nothing;
end $$;
alter function runtime.nexloop_evo_watch_start(text,text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_watch_start(text,text,text,timestamptz) from public;

create function runtime.nexloop_evo_release_watch(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w record;b record;a record;items jsonb:='[]'::jsonb;n_active integer:=0;n_reg integer:=0;v_reg boolean;v_judged boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for w in select release_ref,published_at,watch_until from runtime.nexloop_evo_release_watches
   where tenant_id=p_tenant and world=p_world and watch_until>clock_timestamp() order by published_at loop
  n_active:=n_active+1;
  select count(*) n,avg(case when result_count=0 or unresolved_count>0 then 1.0 else 0.0 end) miss,avg(case when ambiguity then 1.0 else 0.0 end) amb into b
   from runtime.nexloop_semantic_accesses where tenant_id=p_tenant and world=p_world and kind='access'
    and recorded_at>=w.published_at-interval '7 days' and recorded_at<w.published_at;
  select count(*) n,avg(case when result_count=0 or unresolved_count>0 then 1.0 else 0.0 end) miss,avg(case when ambiguity then 1.0 else 0.0 end) amb into a
   from runtime.nexloop_semantic_accesses where tenant_id=p_tenant and world=p_world and kind='access' and recorded_at>=w.published_at;
  v_judged:=b.n>=20 and a.n>=20;
  v_reg:=v_judged and (a.miss-b.miss>0.02 or a.amb-b.amb>0.02);
  if v_reg then n_reg:=n_reg+1;end if;
  items:=items||jsonb_build_array(jsonb_build_object('release_ref',w.release_ref,'published_at',w.published_at,'watch_until',w.watch_until,
   'accesses_before',b.n,'accesses_after',a.n,'miss_rate_before',round(coalesce(b.miss,0),4),'miss_rate_after',round(coalesce(a.miss,0),4),
   'ambiguity_rate_before',round(coalesce(b.amb,0),4),'ambiguity_rate_after',round(coalesce(a.amb,0),4),'judged',v_judged,'regressing',v_reg,
   'suggestion',case when v_reg then 'rollback' end));
 end loop;
 return jsonb_build_object('active',n_active,'regressing',n_reg,'releases',items);
end $$;
alter function runtime.nexloop_evo_release_watch(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_release_watch(text,text) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_evolution;
revoke all on function runtime.nexloop_metrics_snapshot_before_evolution(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_evolution(p_tenant,p_world)||jsonb_build_object('evolution',
  jsonb_build_object('release_watch',runtime.nexloop_evo_release_watch(p_tenant,p_world)));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- 1. Conclude (0248 body, replaced in place): pseudonymous storage and replay by the submitted digest ---------------------------
create or replace function authz.nexloop_evo_evaluation_conclude(p_digest text,p_report jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';r jsonb:=p_report;
 bd runtime.nexloop_evo_bundles;old runtime.nexloop_evo_reports;x runtime.nexloop_evo_candidates;v_digest text;v_submitted text:=encode(sha256(convert_to(p_report::text,'UTF8')),'hex');v_map jsonb:='{}'::jsonb;v_id text;stored jsonb;sub runtime.nexloop_evo_report_submissions;v_failed text[];v_to text;v_end text;
begin
 if jsonb_typeof(r) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(r) k) is distinct from
   array['bundle_digest','candidate_id','change_class','conclusion','cost','created_at','elapsed_ms','evaluator_version','failed_gates','hard_gates',
         'hard_gates_version','level','reason','report_id','reproduced','schema_version','scores','swaps']
  or r->>'schema_version' is distinct from '1.0' or coalesce(r->>'report_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(r->>'candidate_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or jsonb_typeof(r->'hard_gates') is distinct from 'array'
  or jsonb_typeof(r->'failed_gates') is distinct from 'array' or jsonb_typeof(r->'reproduced') is distinct from 'boolean'
  or r->>'conclusion' not in ('accept','reject','incomplete') or r->>'conclusion' is null or octet_length(r::text)>131072
  or r->>'change_class' not in ('patch','minor','major','refused') or r->>'change_class' is null
  or exists(select 1 from jsonb_array_elements(r->'hard_gates') h where jsonb_typeof(h)<>'object' or h->>'result' not in ('pass','fail') or h->>'result' is null) then
  raise exception 'evaluation report invalid' using errcode='22023';end if;
 select * into old from runtime.nexloop_evo_reports where tenant_id=v_tenant and world='real' and report_id=(r->>'report_id')::uuid;
 if found then
  select * into sub from runtime.nexloop_evo_report_submissions where tenant_id=v_tenant and world='real' and report_id=old.report_id;
  if coalesce(sub.submitted_digest,encode(sha256(convert_to(old.report::text,'UTF8')),'hex'))<>v_submitted then
   raise exception 'evaluation report id reused' using errcode='22023';end if;
  select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=old.candidate_id;
  return jsonb_build_object('concluded',true,'replayed',true,'report_digest',old.report_digest,'status',x.status);
 end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=(r->>'candidate_id')::uuid for update;
 if not found or x.status<>'evaluating' then raise exception 'evaluation conclude: the candidate is not evaluating' using errcode='22023';end if;
 select * into bd from runtime.nexloop_evo_bundles where tenant_id=v_tenant and world='real' and candidate_id=x.candidate_id;
 if not found or bd.bundle_digest is distinct from r->>'bundle_digest' then raise exception 'evaluation report names no frozen bundle' using errcode='22023';end if;
 if not (bd.bundle->'levels') ? (r->>'level') or r->>'evaluator_version' is distinct from bd.bundle->>'evaluator_version'
  or r->>'hard_gates_version' is distinct from bd.bundle->>'hard_gates_version' then
  raise exception 'evaluation report differs from its frozen bundle' using errcode='22023';end if;
 if array(select h->>'gate' from jsonb_array_elements(r->'hard_gates') h order by h->>'gate')
   is distinct from array(select e#>>'{}' from jsonb_array_elements(bd.bundle->'hard_gates') e order by 1) then
  raise exception 'evaluation report must answer every frozen hard gate' using errcode='22023';end if;
 v_failed:=array(select h->>'gate' from jsonb_array_elements(r->'hard_gates') h where h->>'result'='fail' order by 1);
 if v_failed is distinct from array(select e#>>'{}' from jsonb_array_elements(r->'failed_gates') e order by 1) then
  raise exception 'evaluation report lists other failed gates than it answers' using errcode='22023';end if;
 -- A hard gate is separate from the score (M22); a breaking change is always closed (§7.2); an incomplete one names why.
 if r->>'conclusion'='accept' and (cardinality(v_failed)>0 or r->>'change_class' not in ('patch','minor') or r->'reason'<>'null'::jsonb
   or not (r->>'reproduced')::boolean) then
  raise exception 'evaluation report accepts what it may not' using errcode='22023';end if;
 if r->>'conclusion'='reject' and cardinality(v_failed)>0 and r->>'reason' is distinct from 'hard_gate_failed' then
  raise exception 'evaluation report rejects a failed gate under another reason' using errcode='22023';end if;
 if r->>'conclusion'='reject' and cardinality(v_failed)=0 and r->>'change_class' in ('major','refused') and r->>'reason' is distinct from 'breaking_change' then
  raise exception 'evaluation report rejects a breaking change under another reason' using errcode='22023';end if;
 if r->>'conclusion'='reject' and r->>'reason' not in ('hard_gate_failed','breaking_change','not_better','inferior','validation_leak') then
  raise exception 'evaluation report reason invalid' using errcode='22023';end if;
 if r->>'conclusion'='incomplete' and r->>'reason' not in ('missing_data','budget_exhausted','not_reproducible','evaluator_timeout') then
  raise exception 'evaluation report reason invalid' using errcode='22023';end if;
 -- NX-039 V5 (D39-11): the stored report names no case or access: each id becomes a random pseudonym, the mapping goes to the
 -- deletable case references.
 for v_id in select distinct q.id from (select s->>'case_id' id from jsonb_array_elements(r->'swaps') s
   union select substring(e#>>'{}' from '^(?:case|access):(.+)$') from jsonb_array_elements(r->'hard_gates') g,jsonb_array_elements(g->'evidence_ids') e) q
   where q.id is not null loop
  v_map:=v_map||jsonb_build_object(v_id,gen_random_uuid()::text);
 end loop;
 stored:=jsonb_set(jsonb_set(r,'{swaps}',coalesce((select jsonb_agg(jsonb_build_object('case_id',v_map->>(s.v->>'case_id'),'swapped',s.v->'swapped') order by s.ord)
   from jsonb_array_elements(r->'swaps') with ordinality s(v,ord)),'[]'::jsonb)),
  '{hard_gates}',(select jsonb_agg(jsonb_set(g.v,'{evidence_ids}',coalesce((select jsonb_agg(case when e.v#>>'{}' ~ '^(case|access):.+$'
     then to_jsonb(split_part(e.v#>>'{}',':',1)||':'||(v_map->>substring(e.v#>>'{}' from '^(?:case|access):(.+)$'))) else e.v end order by e.ord)
    from jsonb_array_elements(g.v->'evidence_ids') with ordinality e(v,ord)),'[]'::jsonb)) order by g.ord)
   from jsonb_array_elements(r->'hard_gates') with ordinality g(v,ord)));
 v_digest:=encode(sha256(convert_to(stored::text,'UTF8')),'hex');
 insert into runtime.nexloop_evo_reports(tenant_id,world,report_id,candidate_id,bundle_digest,level,conclusion,report,report_digest)
  values(v_tenant,'real',(r->>'report_id')::uuid,x.candidate_id,bd.bundle_digest,r->>'level',r->>'conclusion',stored,v_digest);
 insert into runtime.nexloop_evo_report_submissions(tenant_id,world,report_id,submitted_digest,report_digest)
  values(v_tenant,'real',(r->>'report_id')::uuid,v_submitted,v_digest);
 insert into runtime.nexloop_evo_case_refs(tenant_id,world,report_digest,pseudonym,kind,original_id,run_id)
  select v_tenant,'real',v_digest,(m.value#>>'{}')::uuid,
   case when exists(select 1 from jsonb_array_elements(r->'swaps') s where s->>'case_id'=m.key) or m.key=any(x.design_case_ids||x.validation_case_ids) then 'case' else 'access' end,
   m.key,coalesce(case when m.key~'^[0-9a-f-]{36}$' and m.key=any(x.design_case_ids||x.validation_case_ids) then m.key::uuid end,
    (select sa.run_id from runtime.nexloop_semantic_accesses sa where sa.tenant_id=v_tenant and sa.world='real' and sa.access_id::text=m.key))
  from jsonb_each(v_map) m;
 v_to:=case r->>'conclusion' when 'accept' then 'accepted_for_release' when 'reject' then 'rejected' else 'incomplete' end;
 v_end:=case when v_to<>'incomplete' then null when r->>'reason'='not_reproducible' then 'evaluator_error' else r->>'reason' end;
 update runtime.nexloop_evo_candidates set status=v_to,end_reason=v_end,updated_at=clock_timestamp()
  where tenant_id=v_tenant and world='real' and candidate_id=x.candidate_id;
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,x.candidate_id,'evaluation_concluded','evaluating',v_to,r->>'reason',
  jsonb_build_object('report_digest',v_digest,'level',r->>'level','conclusion',r->>'conclusion','change_class',r->>'change_class',
   'failed_gates',to_jsonb(v_failed)),'service:evaluator');
 return jsonb_build_object('concluded',true,'replayed',false,'report_digest',v_digest,'status',v_to);
end $$;
alter function authz.nexloop_evo_evaluation_conclude(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_evaluation_conclude(text,jsonb) from public;
grant execute on function authz.nexloop_evo_evaluation_conclude(text,jsonb) to nexloop_evo_evaluator;

alter function authz.nexloop_evo_evaluation_conclude(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_evaluation_conclude(text,jsonb) from public;
grant execute on function authz.nexloop_evo_evaluation_conclude(text,jsonb) to nexloop_evo_evaluator;

alter function authz.nexloop_evo_evaluation_conclude(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_evaluation_conclude(text,jsonb) from public;
grant execute on function authz.nexloop_evo_evaluation_conclude(text,jsonb) to nexloop_evo_evaluator;

-- 5. Integration with V4 (semantic release, migration before this one): the three hooks V4 replaces --------------------------------------
-- An erasure release: the current version without the items a Consumer being erased contributed (§9, D17); a new, higher version.
alter table ontology.nexloop_semantic_releases drop constraint nexloop_semantic_releases_reason_check;
alter table ontology.nexloop_semantic_releases add constraint nexloop_semantic_releases_reason_check check(reason in ('publish','rollback','erasure'));
create or replace function runtime.nexloop_evo_release_erasure(p_tenant text,p_world text,p_consumer text,p_ref text,p_by text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur ontology.nexloop_semantic_release_pointers;now_r ontology.nexloop_semantic_releases;v_items jsonb;v_kept jsonb;n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from ontology.nexloop_semantic_release_pointers where tenant_id=p_tenant and world=p_world for update;
 if not found then return 0;end if;
 select * into now_r from ontology.nexloop_semantic_releases where tenant_id=p_tenant and world=p_world and release_id=cur.release_id;
 v_items:=runtime.nexloop_semantic_release_items_of(p_tenant,p_world,cur.release_id);
 v_kept:=coalesce((select jsonb_agg(i order by ord) from jsonb_array_elements(v_items) with ordinality e(i,ord) where not (i->'source_consumer_ids') ? p_consumer),'[]'::jsonb);
 n:=jsonb_array_length(v_items)-jsonb_array_length(v_kept);
 if n=0 then return 0;end if;
 perform runtime.nexloop_semantic_release_write(p_tenant,p_world,array[now_r.major,now_r.minor,now_r.patch+1],'erasure',null,null,null,null,
  jsonb_build_object('removed_items',n,'from_version',cur.version),'erasure '||left(p_ref,480),p_by,p_ref,v_kept);
 return n;
end $$;
-- Reports kept by evolution_records retention: those the current release and the one before it name.
create or replace function runtime.nexloop_evo_retained_reports(p_tenant text,p_world text) returns setof text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur ontology.nexloop_semantic_releases;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select r.* into cur from ontology.nexloop_semantic_release_pointers p join ontology.nexloop_semantic_releases r
   on r.tenant_id=p.tenant_id and r.world=p.world and r.release_id=p.release_id where p.tenant_id=p_tenant and p.world=p_world;
 if not found then return;end if;
 return query select x.report_digest from (select r.report_digest from ontology.nexloop_semantic_releases r where r.tenant_id=p_tenant and r.world=p_world
   and (r.major,r.minor,r.patch)<=(cur.major,cur.minor,cur.patch) order by r.major desc,r.minor desc,r.patch desc limit 2) x where x.report_digest is not null;
end $$;
-- A publication opens the 7-day watch (D39-10): V4's writer (body copied, the call marked) starts it for every publish.
create or replace function runtime.nexloop_semantic_release_write(p_tenant text,p_world text,p_version int[],p_reason text,p_candidate uuid,p_report text,p_class text,
  p_target text,p_report_json jsonb,p_owner_reason text,p_principal text,p_intent text,p_items jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_id uuid:=gen_random_uuid();v_version text:=p_version[1]||'.'||p_version[2]||'.'||p_version[3];it jsonb;k integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into ontology.nexloop_semantic_releases(tenant_id,world,release_id,version,major,minor,patch,reason,candidate_id,report_digest,change_class,rolled_back_to,
   rollback_report,owner_reason,published_by,request_intent)
  values(p_tenant,p_world,v_id,v_version,p_version[1],p_version[2],p_version[3],p_reason,p_candidate,p_report,p_class,p_target,coalesce(p_report_json,'{}'::jsonb),
   p_owner_reason,p_principal,p_intent);
 for it in select value from jsonb_array_elements(p_items) loop
  k:=k+1;
  insert into ontology.nexloop_semantic_release_items(tenant_id,world,release_id,item_no,kind,ref,body,source_candidate_id,source_consumer_ids)
   values(p_tenant,p_world,v_id,k,it->>'kind',it->>'ref',it->>'body',(it->>'source_candidate_id')::uuid,
    array(select e#>>'{}' from jsonb_array_elements(coalesce(it->'source_consumer_ids','[]'::jsonb)) e));
 end loop;
 insert into ontology.nexloop_semantic_release_pointers(tenant_id,world,release_id,version) values(p_tenant,p_world,v_id,v_version)
  on conflict(tenant_id,world) do update set release_id=excluded.release_id,version=excluded.version,updated_at=clock_timestamp();
 perform runtime.nexloop_semantic_release_reindex(p_tenant,p_world);
 if p_reason='publish' then  -- Integration with V5: the watch window (D39-10)
  perform runtime.nexloop_evo_watch_start(p_tenant,p_world,'release:'||v_version,clock_timestamp());
 end if;
 return jsonb_build_object('release_id',v_id,'version',v_version,'items',k);
end $$;

-- AT-048 (S6 stage acceptance finding, scheduler 2026-10-21): the evaluation opening checks the Consumers of the cases itself. The
-- migration number lives only in the file name and the catalog; nothing below repeats it.
-- Until now check 3 (leakage_detected) compared only the writer's case_groups and near-duplicate term sets, so a writer grouping by
-- Run could put one Consumer's Runs on both sides undetected. runtime.nexloop_evo_run_consumer derives a case Run's Consumer as the
-- trace reader does (0233: message route, fallback binding or role binding); authz.nexloop_evo_evaluation_open is 0241's body with
-- one more condition in check 3: a Consumer with cases on both sides is leakage_detected (runtime.nexloop_evo_consumer_leak: each
-- case's Consumer once, then one join - linear in the cases, indexed lookups; L6 review 4). Everything else is unchanged.
-- Not re-checked: a candidate already evaluating before this migration replays its opening without checks (L6 review 1.1); the
-- deployment's read-only post-check lists such candidates (docs/implementation/S6-stage-acceptance.md).

create function runtime.nexloop_evo_run_consumer(p_tenant text,p_world text,p_run text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;v_run uuid;v_message text;v_conversation text;
begin
 -- Case ids are Run uuids: compared as uuid, the unique run_id indexes answer (0047, 0110, 0063); anything else has no Consumer.
 if coalesce(p_run,'')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then return null;end if;
 v_run:=p_run::uuid;
 -- The conversation tables are tenant-bounded: the caller's tenant for the lookup, the prior setting restored after it.
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 -- Step by step from the Run (each a unique-index lookup, whatever the statistics): its message route or fallback binding, the
 -- message's conversation (index 0134 on message_id), the conversation's Consumer; else its role binding.
 select mr.message_id into v_message from runtime.nexloop_message_routes mr where mr.run_id=v_run and mr.tenant_id=p_tenant and mr.world=p_world;
 if v_message is null then
  select fb.message_id into v_message from runtime.nexloop_fallback_context_bindings fb where fb.run_id=v_run and fb.tenant_id=p_tenant and fb.world=p_world;
 end if;
 if v_message is not null then
  select m.conversation_id into v_conversation from runtime.nexloop_conversation_messages m where m.tenant_id=p_tenant and m.world=p_world and m.message_id=v_message limit 1;
  select cv.consumer_id into v from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world and cv.conversation_id=v_conversation;
 end if;
 if v is null then
  select rb.consumer_id into v from authz.nexloop_role_run_bindings rb where rb.run_id=v_run and rb.tenant_id=p_tenant and rb.world=p_world;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_evo_run_consumer(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_run_consumer(text,text,text) from public;

-- One Consumer on both sides (L6 review 4: linear, not per pair): each case's Consumer is derived once, then the two sides' sets meet
-- in one hash join. At most 512 cases (256 a side, 0235): at most 512 indexed lookups.
create function runtime.nexloop_evo_consumer_leak(p_tenant text,p_design text[],p_validation text[]) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 with cases as (select 'design' side,c from unnest(p_design) c union all select 'validation',c from unnest(p_validation) c),
  consumers as materialized (select side,runtime.nexloop_evo_run_consumer(p_tenant,'real',c) consumer from cases)
 select exists(select 1 from (select distinct consumer from consumers where side='design' and consumer is not null) d
   join (select distinct consumer from consumers where side='validation' and consumer is not null) v using(consumer))
$$;
alter function runtime.nexloop_evo_consumer_leak(text,text[],text[]) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_consumer_leak(text,text[],text[]) from public;

create or replace function authz.nexloop_evo_evaluation_open(p_digest text,p_candidate uuid,p_bundle_digest text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';v_principal text:=who->>'principal_id';
 x runtime.nexloop_evo_candidates;bd runtime.nexloop_evo_bundles;d runtime.nexloop_evo_diagnoses;p runtime.nexloop_evo_patches;
 lim control.nexloop_budget_limits%rowtype;v_holdout text;v_uses integer;used runtime.nexloop_evo_holdout_uses;b jsonb;v_c numeric;v_m numeric;
 refuse text;
begin
 if coalesce(p_bundle_digest,'')!~'^[0-9a-f]{64}$' or p_candidate is null then raise exception 'evaluation open invalid' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate for update;
 if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
 select * into bd from runtime.nexloop_evo_bundles where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 -- Replay: this candidate was opened with this bundle.
 if x.status='evaluating' and found and bd.bundle_digest=p_bundle_digest then
  select * into used from runtime.nexloop_evo_holdout_uses where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
  if found then
   return jsonb_build_object('opened',true,'replayed',true,'candidate_id',p_candidate,'status','evaluating','holdout_digest',used.holdout_digest,'holdout_use',used.use_number);
  end if;
 end if;
 b:=bd.bundle;
 select * into p from runtime.nexloop_evo_patches where tenant_id=v_tenant and world='real' and patch_id=x.patch_id;
 -- 1. bundle_mismatch: the frozen bundle exists, is the one named, and still says what the candidate row says.
 if bd.candidate_id is null or bd.bundle_digest<>p_bundle_digest or encode(sha256(convert_to(b::text,'UTF8')),'hex')<>bd.bundle_digest
  or b->>'parent_revision' is distinct from x.parent_revision or b->>'candidate_revision' is distinct from x.candidate_revision
  or b->>'input_snapshot_digest' is distinct from x.input_snapshot_digest or b->>'evaluator_version' is distinct from x.evaluator_version
  or b->>'model_profile' is distinct from x.model_profile or b->>'patch_digest' is distinct from p.body_digest
  or b->'design_case_ids' is distinct from to_jsonb(x.design_case_ids) or b->'validation_case_ids' is distinct from to_jsonb(x.validation_case_ids)
  or b->'case_groups' is distinct from x.case_groups then
  refuse:='bundle_mismatch';
 end if;
 -- 2. candidate_not_ready: proposed, its diagnosis not invalidated, no source Consumer erasing.
 if refuse is null then
  select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=x.diagnosis_id;
  if x.status<>'proposed' or d.status='incomplete'
   or exists(select 1 from runtime.nexloop_evo_patch_sources s where s.tenant_id=v_tenant and s.world='real' and s.patch_id=x.patch_id
     and runtime.nexloop_consumer_erasing(v_tenant,'real',s.consumer_id)) then
   refuse:='candidate_not_ready';
  end if;
 end if;
 -- 3. leakage_detected (AT-048): every case has a group; no group on both sides; no two cases across the sides asked exactly the
 --    same set of terms (E3 records: a near duplicate is the same cluster).
 if refuse is null then
  if exists(select 1 from unnest(x.design_case_ids||x.validation_case_ids) c where not (x.case_groups ? c))
   or exists(select 1 from unnest(x.design_case_ids) dc join unnest(x.validation_case_ids) vc on x.case_groups->>dc=x.case_groups->>vc)
   -- AT-048 (S6 stage finding): the Consumers of the cases, derived here - one Consumer on both sides leaks whatever the writer grouped.
   or runtime.nexloop_evo_consumer_leak(v_tenant,x.design_case_ids,x.validation_case_ids)
   or exists(
    with sets as (select s.run_id::text case_id,array_agg(distinct h order by h) terms from runtime.nexloop_semantic_accesses s,unnest(s.term_hashes) h
      where s.tenant_id=v_tenant and s.world='real' and s.kind='access' and s.run_id::text=any(x.design_case_ids||x.validation_case_ids) group by s.run_id)
    select 1 from sets a join sets v on a.terms=v.terms where a.case_id=any(x.design_case_ids) and v.case_id=any(x.validation_case_ids)) then
   refuse:='leakage_detected';
  end if;
 end if;
 -- 4. holdout_exhausted (D39-3): at most 5 candidates decided per registered validation set.
 if refuse is null then
  v_holdout:=runtime.nexloop_evo_holdout_digest(x.validation_case_ids);
  select count(*) into v_uses from runtime.nexloop_evo_holdout_uses where tenant_id=v_tenant and world='real' and holdout_digest=v_holdout;
  if v_uses>=runtime.nexloop_evo_holdout_limit() then refuse:='holdout_exhausted';end if;
 end if;
 -- 5. dimension_closed; 6. dimension_stopped.
 if refuse is null and not exists(select 1 from control.nexloop_evo_open_dimensions o where o.dimension=x.dimension) then refuse:='dimension_closed';end if;
 if refuse is null and exists(select 1 from control.nexloop_evo_dimension_stops s where s.tenant_id=v_tenant and s.world='real' and s.dimension=x.dimension and s.stopped) then
  refuse:='dimension_stopped';
 end if;
 -- 7. budget_exhausted: the candidate cap and the tenant's month (L0 has no model call, but nothing opens on a spent budget).
 if refuse is null then
  select coalesce(sum(amount),0) into v_c from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
   and source_ref='evolution-candidate:'||x.candidate_id;
  select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world='real' and budget_kind='evolution';
  if found then
   select coalesce(sum(amount),0) into v_m from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
    and recorded_at>=lim.period_start and recorded_at<lim.period_end;
  end if;
  if v_c>=x.cost_limit or (lim.tenant_id is not null and (clock_timestamp()<lim.period_start or clock_timestamp()>=lim.period_end or v_m>=lim.limit_amount)) then
   refuse:='budget_exhausted';
  end if;
 end if;
 if refuse is not null then
  return jsonb_build_object('opened',false,'candidate_id',p_candidate,'reason',refuse,'status',x.status);
 end if;
 insert into runtime.nexloop_evo_holdouts(tenant_id,world,holdout_digest,case_ids)
  values(v_tenant,'real',v_holdout,array(select distinct c from unnest(x.validation_case_ids) c order by c)) on conflict do nothing;
 insert into runtime.nexloop_evo_holdout_uses(tenant_id,world,holdout_digest,candidate_id,use_number,bundle_digest,evaluator_principal)
  values(v_tenant,'real',v_holdout,p_candidate,v_uses+1,bd.bundle_digest,v_principal);
 update runtime.nexloop_evo_candidates set status='evaluating',updated_at=clock_timestamp() where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,p_candidate,'evaluation_opened','proposed','evaluating',null,
  jsonb_build_object('bundle_digest',bd.bundle_digest,'holdout_digest',v_holdout,'holdout_use',v_uses+1),'service:evaluator');
 return jsonb_build_object('opened',true,'replayed',false,'candidate_id',p_candidate,'status','evaluating','holdout_digest',v_holdout,'holdout_use',v_uses+1);
end $$;

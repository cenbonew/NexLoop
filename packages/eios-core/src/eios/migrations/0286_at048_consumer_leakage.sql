-- AT-048 (S6 stage acceptance finding, scheduler 2026-10-21): the evaluation opening checks the Consumers of the cases itself. The
-- migration number lives only in the file name and the catalog; nothing below repeats it.
-- Until now check 3 (leakage_detected) compared only the writer's case_groups and near-duplicate term sets, so a writer grouping by
-- Run could put one Consumer's Runs on both sides undetected. runtime.nexloop_evo_run_consumer derives a case Run's Consumer as the
-- trace reader does (0233: message route, fallback binding or role binding); authz.nexloop_evo_evaluation_open is 0241's body with
-- one more condition in check 3: a Consumer with cases on both sides is leakage_detected. Everything else is unchanged.

create function runtime.nexloop_evo_run_consumer(p_tenant text,p_world text,p_run text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;
begin
 -- The conversation tables are tenant-bounded: the caller's tenant for the lookup, the prior setting restored after it.
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=coalesce(
  (select cv.consumer_id from runtime.nexloop_message_routes mr join runtime.nexloop_conversation_messages m on m.tenant_id=mr.tenant_id and m.world=mr.world
     and m.message_id=mr.message_id join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
    where mr.tenant_id=p_tenant and mr.world=p_world and mr.run_id::text=p_run limit 1),
  (select cv.consumer_id from runtime.nexloop_fallback_context_bindings fb join runtime.nexloop_conversation_messages m on m.tenant_id=fb.tenant_id and m.world=fb.world
     and m.message_id=fb.message_id join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
    where fb.tenant_id=p_tenant and fb.world=p_world and fb.run_id::text=p_run limit 1),
  (select rb.consumer_id from authz.nexloop_role_run_bindings rb where rb.tenant_id=p_tenant and rb.world=p_world and rb.run_id::text=p_run limit 1));
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_evo_run_consumer(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_run_consumer(text,text,text) from public;

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
   or exists(select 1 from unnest(x.design_case_ids) dc join unnest(x.validation_case_ids) vc
     on runtime.nexloop_evo_run_consumer(v_tenant,'real',dc)=runtime.nexloop_evo_run_consumer(v_tenant,'real',vc))
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

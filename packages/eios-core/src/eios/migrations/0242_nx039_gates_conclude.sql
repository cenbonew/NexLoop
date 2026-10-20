-- NX-039 V2 (design docs/implementation/NX-039-design.md §3.4, §4.1, §5.1, §7.2; owner D39-1, D39-2, D39-5, D39-7 decided 2026-10-20).
-- Temporary number (the dispatcher renumbers at merge). Why a migration in V2: the gate list a bundle freezes and the evaluator's
-- report path are database functions; neither can change without one.
--
-- 1. The frozen gate list (0236 runtime.nexloop_evo_hard_gates, body replaced in place) becomes version nx039-gates-1: G1-G8 plus
--    G9 (index_migration_impact, not applicable to semantic content). Bundles frozen before keep the list they froze; the evaluator
--    answers whatever version a bundle names (nexloop_eios.evolution.evaluation.GATE_VERSIONS).
-- 2. The evaluator writes the report and concludes the evaluation (§3.4): authz.nexloop_evo_evaluation_conclude checks the report
--    against its frozen bundle (every frozen gate answered, the gate version, any failed gate is a rejection with reason
--    hard_gate_failed, a major or refused change class is a rejection with reason breaking_change, an acceptance has no failed gate
--    and a patch or minor class), records it in the E5 report table and moves the candidate evaluating -> accepted_for_release |
--    rejected | incomplete in the same transaction.
-- 3. D39-1: the writer no longer records reports (EXECUTE on authz.nexloop_evo_report_record revoked): whoever proposes never
--    evaluates. The function itself is kept unchanged.
-- 4. An incomplete evaluation that could not reproduce its own report ends `incomplete/evaluator_error` (the candidate check gains the
--    value; the report reason is not_reproducible).
-- Published migrations are not edited.

-- 1. The gate list ----------------------------------------------------------------------------------------------------------------
create or replace function runtime.nexloop_evo_hard_gates() returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select '{"version":"nx039-gates-1","gates":["references_valid","structure_valid","zero_overreach","zero_cross_tenant","zero_fact_pollution","no_forbidden_regression","semantic_regression","cost_performance","semantic_conflict","index_migration_impact"],"metrics":["coverage","ambiguity_rate","regressions"],"non_inferiority":{"primary_metric":"coverage","margin":0.01,"ties_accept":false}}'::jsonb
$$;

-- 4. End reason --------------------------------------------------------------------------------------------------------------------
alter table runtime.nexloop_evo_candidates drop constraint nexloop_evo_candidates_end_reason_check;
alter table runtime.nexloop_evo_candidates add constraint nexloop_evo_candidates_end_reason_check
 check(end_reason in ('budget_exhausted','missing_data','user_interrupted','external_block','evaluator_error'));
alter table runtime.nexloop_evo_candidate_events drop constraint nexloop_evo_candidate_events_event_kind_check;
alter table runtime.nexloop_evo_candidate_events add constraint nexloop_evo_candidate_events_event_kind_check check(event_kind in
 ('diagnosis_opened','diagnosis_closed','proposed','status_changed','budget_reserved','invalidated','bundle_frozen','report_recorded','review_linked',
  'evaluation_opened','evaluation_concluded'));

-- 3. Only the evaluator records reports -------------------------------------------------------------------------------------------
revoke execute on function authz.nexloop_evo_report_record(text,jsonb) from nexloop_evo_writer;

-- 2. Conclude ---------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_evo_evaluation_conclude(p_digest text,p_report jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';r jsonb:=p_report;
 bd runtime.nexloop_evo_bundles;old runtime.nexloop_evo_reports;x runtime.nexloop_evo_candidates;v_digest text;v_failed text[];v_to text;v_end text;
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
  if old.report<>r then raise exception 'evaluation report id reused' using errcode='22023';end if;
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
 if r->>'conclusion'='incomplete' and r->>'reason' not in ('missing_data','budget_exhausted','not_reproducible') then
  raise exception 'evaluation report reason invalid' using errcode='22023';end if;
 v_digest:=encode(sha256(convert_to(r::text,'UTF8')),'hex');
 insert into runtime.nexloop_evo_reports(tenant_id,world,report_id,candidate_id,bundle_digest,level,conclusion,report,report_digest)
  values(v_tenant,'real',(r->>'report_id')::uuid,x.candidate_id,bd.bundle_digest,r->>'level',r->>'conclusion',r,v_digest);
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

-- NX-038 E5 isolation and hand-over (docs/implementation/NX-038-design.md §12.3, §12.4, §12.5, §12.7 E5; owner D4, D16, D18, D19).
-- Temporary number; the dispatcher renumbers at merge (no other migration number is referenced below).
--
-- 1. Evaluation credentials (D18): trusted configuration registers a service credential issued for world `test` only as the
--    dedicated evaluation credential of its tenant; a credential with any other world is refused at registration.
-- 2. The overlay (§12.3, AT-023): authz.nexloop_evo_overlay is how the Context Engine's semantic read loads a candidate patch as an
--    overlay. It refuses world `real` at its entry (the second line of defence; the adapter refuses first), and otherwise needs a
--    registered evaluation credential, world `test`, a candidate of the same tenant in status `evaluating` and the patch digest
--    the caller names. It returns refs and hashes only and writes nothing; active definitions and aliases are never touched.
-- 3. The evaluation bundle (§12.4): authz.nexloop_evo_bundle_freeze builds the bundle in SQL from the stored candidate (ids,
--    digests, versions, case groups), the fixed hard-gate list, the D7 non-inferiority rule and the server-computed budget
--    remaining; the caller chooses only the levels and the anonymization seed. Once frozen it never changes (a changed condition
--    is a new candidate, 08 §5). Only a `proposed` candidate is frozen; NX-038 still writes no status after `evaluating`.
-- 4. The evaluation report (§12.4): authz.nexloop_evo_report_record stores a report against the frozen bundle digest (the level
--    must be one of the bundle's, every hard gate of the bundle answered, `accept` only with every gate passed) and writes its
--    digest to the candidate event log. Recording a report changes no candidate status.
-- 5. Review links (D4): runtime.nexloop_evo_review_links ties an S3 review decision to an evolution candidate and the digest of
--    one of its recorded reports; review-decision is not changed.
-- Ids, digests, codes and numbers only: no message text, Claim value, prompt or patch text. Published migrations are not edited.

-- 0. The event log knows the new kinds -------------------------------------------------------------------------------------------
alter table runtime.nexloop_evo_candidate_events drop constraint nexloop_evo_candidate_events_event_kind_check;
alter table runtime.nexloop_evo_candidate_events add constraint nexloop_evo_candidate_events_event_kind_check check(event_kind in
 ('diagnosis_opened','diagnosis_closed','proposed','status_changed','budget_reserved','invalidated','bundle_frozen','report_recorded','review_linked'));

-- 1. Evaluation credentials (D18) ------------------------------------------------------------------------------------------------
create table authz.nexloop_evo_evaluation_credentials (
 token_digest text primary key references authz.nexloop_service_credentials(token_digest),
 tenant_id text not null,registered_by text not null check(char_length(registered_by) between 1 and 200),
 registered_at timestamptz not null default clock_timestamp());
alter table authz.nexloop_evo_evaluation_credentials owner to nexloop_owner;
revoke all on authz.nexloop_evo_evaluation_credentials from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer;

create function authz.nexloop_evo_evaluation_credential_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op<>'INSERT' then raise exception 'evaluation credentials are registered, never changed' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_service_credentials c where c.token_digest=new.token_digest and c.tenant_id=new.tenant_id and c.worlds=array['test']) then
  raise exception 'an evaluation credential is issued for world test only' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_evo_evaluation_credential_check() owner to nexloop_owner;
revoke all on function authz.nexloop_evo_evaluation_credential_check() from public;
create trigger nx038_evaluation_credential before insert or update on authz.nexloop_evo_evaluation_credentials
 for each row execute function authz.nexloop_evo_evaluation_credential_check();

-- 2. The overlay (§12.3) -------------------------------------------------------------------------------------------------------
create function authz.nexloop_evo_overlay(p_digest text,p_world text,p_candidate uuid,p_patch_digest text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;x runtime.nexloop_evo_candidates;p runtime.nexloop_evo_patches;
begin
 -- AT-023 second line: a real-world session never reads a candidate, whatever the code passes.
 if p_world is distinct from 'test' then raise exception 'evaluation overlay refused outside world test' using errcode='42501';end if;
 if session_user not in ('nexloop_api','nexloop_domain_worker') or coalesce(p_patch_digest,'')!~'^[0-9a-f]{64}$' then
  raise exception 'evaluation overlay unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,'test');v_tenant:=ident->'binding'->>'tenant_id';
 if v_tenant is null or not exists(select 1 from authz.nexloop_evo_evaluation_credentials e where e.token_digest=p_digest and e.tenant_id=v_tenant) then
  raise exception 'evaluation overlay unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if not found or x.status<>'evaluating' then raise exception 'evaluation overlay unavailable' using errcode='42501';end if;
 select * into p from runtime.nexloop_evo_patches where tenant_id=v_tenant and world='real' and patch_id=x.patch_id;
 if not found or p.body_digest<>p_patch_digest then raise exception 'evaluation overlay unavailable' using errcode='42501';end if;
 return jsonb_build_object('candidate_id',x.candidate_id,'patch_digest',p.body_digest,'change_kind',p.body->'change_kind','target_refs',p.body->'target_refs',
  'term_hashes',p.body->'term_hashes');
end $$;
alter function authz.nexloop_evo_overlay(text,text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_overlay(text,text,uuid,text) from public;
grant execute on function authz.nexloop_evo_overlay(text,text,uuid,text) to nexloop_api,nexloop_domain_worker;

-- 3. Bundles and reports ---------------------------------------------------------------------------------------------------------
create table runtime.nexloop_evo_bundles (
 tenant_id text not null,world text not null check(world='real'),candidate_id uuid not null,
 bundle jsonb not null check(jsonb_typeof(bundle)='object' and octet_length(bundle::text)<=131072),bundle_digest text not null check(bundle_digest~'^[0-9a-f]{64}$'),
 frozen_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,candidate_id),unique(tenant_id,world,bundle_digest),
 foreign key(tenant_id,world,candidate_id) references runtime.nexloop_evo_candidates(tenant_id,world,candidate_id));

create table runtime.nexloop_evo_reports (
 tenant_id text not null,world text not null check(world='real'),report_id uuid not null,candidate_id uuid not null,
 bundle_digest text not null,level text not null check(level in ('L0','L1')),conclusion text not null check(conclusion in ('accept','reject','incomplete')),
 report jsonb not null check(jsonb_typeof(report)='object' and octet_length(report::text)<=131072),report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,report_id),unique(tenant_id,world,report_digest),
 foreign key(tenant_id,world,candidate_id) references runtime.nexloop_evo_bundles(tenant_id,world,candidate_id));

-- D4: the review decision (S3) and the evolution candidate it decided, with the report digest it relied on.
create table runtime.nexloop_evo_review_links (
 tenant_id text not null,world text not null check(world='real'),decision_id text not null,candidate_id uuid not null,
 report_digest text not null,gate_result text not null check(gate_result in ('pass','fail')),
 linked_by text not null check(char_length(linked_by) between 1 and 200),linked_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,decision_id,candidate_id),
 foreign key(tenant_id,world,decision_id) references ontology.nexloop_review_decisions(tenant_id,world,decision_id),
 foreign key(tenant_id,world,report_digest) references runtime.nexloop_evo_reports(tenant_id,world,report_digest));

create function runtime.nexloop_evo_frozen_rows() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception 'evaluation bundles, reports and review links are frozen' using errcode='42501';
end $$;
alter function runtime.nexloop_evo_frozen_rows() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_frozen_rows() from public;

do $$declare t text;begin
 foreach t in array array['nexloop_evo_bundles','nexloop_evo_reports','nexloop_evo_review_links'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer',t);
  execute format('create trigger nx038_frozen before update or delete on runtime.%I for each row execute function runtime.nexloop_evo_frozen_rows()',t);
  execute format('create trigger nx038_no_truncate before truncate on runtime.%I for each statement execute function runtime.nexloop_evo_frozen_rows()',t);
 end loop;
end $$;

-- The frozen conditions (§7, D7): one place to change, a new version string when they change.
create function runtime.nexloop_evo_hard_gates() returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select '{"version":"nx038-hard-gates-1","gates":["references_valid","structure_valid","zero_overreach","zero_cross_tenant","zero_fact_pollution","no_forbidden_regression","index_migration_impact"],"metrics":["coverage","ambiguity_rate","regressions"],"non_inferiority":{"primary_metric":"coverage","margin":0.01,"ties_accept":false}}'::jsonb
$$;
alter function runtime.nexloop_evo_hard_gates() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_hard_gates() from public;

create function authz.nexloop_evo_bundle_freeze(p_digest text,p_candidate uuid,p_levels text[],p_seed text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);x runtime.nexloop_evo_candidates;d runtime.nexloop_evo_diagnoses;p runtime.nexloop_evo_patches;
 old runtime.nexloop_evo_bundles;lim control.nexloop_budget_limits%rowtype;g jsonb:=runtime.nexloop_evo_hard_gates();b jsonb;v_d numeric;v_c numeric;v_m numeric;
 v_digest text;
begin
 if p_levels is null or not (p_levels=array['L0'] or p_levels=array['L0','L1']) or coalesce(p_seed,'')!~'^[0-9a-f]{32}$' then
  raise exception 'evaluation bundle invalid' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate for update;
 if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
 select * into old from runtime.nexloop_evo_bundles where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if found then
  if old.bundle->'levels'<>to_jsonb(p_levels) or old.bundle->>'anonymize_seed'<>p_seed then
   raise exception 'evaluation bundle frozen: a changed condition is a new candidate' using errcode='22023';end if;
  return jsonb_build_object('frozen',true,'replayed',true,'bundle',old.bundle,'bundle_digest',old.bundle_digest);
 end if;
 if x.status<>'proposed' then raise exception 'evolution candidate %: only a proposed candidate is frozen',x.status using errcode='22023';end if;
 select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=x.diagnosis_id;
 select * into p from runtime.nexloop_evo_patches where tenant_id=v_tenant and world='real' and patch_id=x.patch_id;
 if not found then raise exception 'evolution patch missing' using errcode='22023';end if;
 select coalesce(sum(amount),0) into v_d from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
  and source_ref='evolution-diagnosis:'||d.diagnosis_id;
 select coalesce(sum(amount),0) into v_c from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
  and source_ref='evolution-candidate:'||x.candidate_id;
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world='real' and budget_kind='evolution';
 if found then
  select coalesce(sum(amount),0) into v_m from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
   and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 end if;
 b:=jsonb_build_object('schema_version','1.0','candidate_id',x.candidate_id,'parent_revision',x.parent_revision,'candidate_revision',x.candidate_revision,
  'patch_digest',p.body_digest,'input_snapshot_digest',x.input_snapshot_digest,'design_case_ids',to_jsonb(x.design_case_ids),
  'validation_case_ids',to_jsonb(x.validation_case_ids),'case_groups',x.case_groups,'hard_gates_version',g->'version','hard_gates',g->'gates',
  'metrics',g->'metrics','non_inferiority',g->'non_inferiority','levels',to_jsonb(p_levels),
  'budget_remaining',jsonb_build_object('unit',x.unit,'rounds',x.maximum_rounds,'diagnosis',greatest(d.cost_limit-v_d,0)::text,
    'candidate',greatest(x.cost_limit-v_c,0)::text,'tenant_month',case when lim.tenant_id is null then null else greatest(lim.limit_amount-v_m,0)::text end),
  'evaluator_version',x.evaluator_version,'model_profile',x.model_profile,'anonymize_seed',p_seed,
  'frozen_at',to_char(clock_timestamp() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
 v_digest:=encode(sha256(convert_to(b::text,'UTF8')),'hex');
 insert into runtime.nexloop_evo_bundles(tenant_id,world,candidate_id,bundle,bundle_digest) values(v_tenant,'real',x.candidate_id,b,v_digest);
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,x.candidate_id,'bundle_frozen',null,null,null,
  jsonb_build_object('bundle_digest',v_digest,'levels',to_jsonb(p_levels)),'service:evolution');
 return jsonb_build_object('frozen',true,'replayed',false,'bundle',b,'bundle_digest',v_digest);
end $$;
alter function authz.nexloop_evo_bundle_freeze(text,uuid,text[],text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_bundle_freeze(text,uuid,text[],text) from public;
grant execute on function authz.nexloop_evo_bundle_freeze(text,uuid,text[],text) to nexloop_evo_writer;

create function authz.nexloop_evo_report_record(p_digest text,p_report jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);r jsonb:=p_report;bd runtime.nexloop_evo_bundles;old runtime.nexloop_evo_reports;x runtime.nexloop_evo_candidates;
 v_digest text;v_gates text[];
begin
 if jsonb_typeof(r) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(r) k) is distinct from
   array['bundle_digest','candidate_id','conclusion','cost','created_at','elapsed_ms','evaluator_version','hard_gates','level','reason','report_id','schema_version','scores','swaps']
  or r->>'schema_version' is distinct from '1.0' or coalesce(r->>'report_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(r->>'candidate_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or jsonb_typeof(r->'hard_gates') is distinct from 'array'
  or r->>'conclusion' not in ('accept','reject','incomplete') or r->>'conclusion' is null or octet_length(r::text)>131072
  or exists(select 1 from jsonb_array_elements(r->'hard_gates') h where jsonb_typeof(h)<>'object' or h->>'result' not in ('pass','fail') or h->>'result' is null) then
  raise exception 'evaluation report invalid' using errcode='22023';end if;
 select * into old from runtime.nexloop_evo_reports where tenant_id=v_tenant and world='real' and report_id=(r->>'report_id')::uuid;
 if found then
  if old.report<>r then raise exception 'evaluation report id reused' using errcode='22023';end if;
  return jsonb_build_object('recorded',true,'replayed',true,'report_digest',old.report_digest);
 end if;
 select * into bd from runtime.nexloop_evo_bundles where tenant_id=v_tenant and world='real' and candidate_id=(r->>'candidate_id')::uuid;
 if not found or bd.bundle_digest is distinct from r->>'bundle_digest' then raise exception 'evaluation report names no frozen bundle' using errcode='22023';end if;
 if not (bd.bundle->'levels') ? (r->>'level') or r->>'evaluator_version' is distinct from bd.bundle->>'evaluator_version' then
  raise exception 'evaluation report differs from its frozen bundle' using errcode='22023';end if;
 v_gates:=array(select h->>'gate' from jsonb_array_elements(r->'hard_gates') h order by h->>'gate');
 if v_gates is distinct from array(select e#>>'{}' from jsonb_array_elements(bd.bundle->'hard_gates') e order by 1) then
  raise exception 'evaluation report must answer every frozen hard gate' using errcode='22023';end if;
 -- A hard gate is separate from the score: any failed gate is never an acceptance (M22).
 if r->>'conclusion'='accept' and exists(select 1 from jsonb_array_elements(r->'hard_gates') h where h->>'result'<>'pass') then
  raise exception 'evaluation report accepts with a failed hard gate' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=bd.candidate_id;
 v_digest:=encode(sha256(convert_to(r::text,'UTF8')),'hex');
 insert into runtime.nexloop_evo_reports(tenant_id,world,report_id,candidate_id,bundle_digest,level,conclusion,report,report_digest)
  values(v_tenant,'real',(r->>'report_id')::uuid,bd.candidate_id,bd.bundle_digest,r->>'level',r->>'conclusion',r,v_digest);
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,x.candidate_id,'report_recorded',null,null,null,
  jsonb_build_object('report_digest',v_digest,'level',r->>'level','conclusion',r->>'conclusion'),'service:evolution');
 return jsonb_build_object('recorded',true,'replayed',false,'report_digest',v_digest);
end $$;
alter function authz.nexloop_evo_report_record(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_report_record(text,jsonb) from public;
grant execute on function authz.nexloop_evo_report_record(text,jsonb) to nexloop_evo_writer;

-- 5. Review links (D4) -----------------------------------------------------------------------------------------------------------
create function authz.nexloop_evo_review_link(p_digest text,p_decision text,p_candidate uuid,p_report_digest text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);rp runtime.nexloop_evo_reports;x runtime.nexloop_evo_candidates;v_result text;
begin
 select * into rp from runtime.nexloop_evo_reports where tenant_id=v_tenant and world='real' and report_digest=p_report_digest and candidate_id=p_candidate;
 if not found or not exists(select 1 from ontology.nexloop_review_decisions where tenant_id=v_tenant and world='real' and decision_id=p_decision) then
  raise exception 'review link unavailable' using errcode='22023';end if;
 v_result:=case when rp.conclusion='accept' then 'pass' else 'fail' end;
 insert into runtime.nexloop_evo_review_links(tenant_id,world,decision_id,candidate_id,report_digest,gate_result,linked_by)
  values(v_tenant,'real',p_decision,p_candidate,p_report_digest,v_result,'service:evolution') on conflict do nothing;
 if not found then
  if not exists(select 1 from runtime.nexloop_evo_review_links where tenant_id=v_tenant and world='real' and decision_id=p_decision and candidate_id=p_candidate
    and report_digest=p_report_digest) then raise exception 'review link differs' using errcode='22023';end if;
  return jsonb_build_object('linked',true,'replayed',true,'gate_result',v_result);
 end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,x.candidate_id,'review_linked',null,null,null,
  jsonb_build_object('decision_id',p_decision,'report_digest',p_report_digest,'gate_result',v_result),'service:evolution');
 return jsonb_build_object('linked',true,'replayed',false,'gate_result',v_result);
end $$;
alter function authz.nexloop_evo_review_link(text,text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_review_link(text,text,uuid,text) from public;
grant execute on function authz.nexloop_evo_review_link(text,text,uuid,text) to nexloop_evo_writer;

-- NX-040 F4 "gate registry, permit check and the watch window" (docs/implementation/NX-040-design.md §4.6, §6, §7, §11 F4; owner D21-D27
-- decided 2026-10-20; NX-039 design §12). The migration number lives only in the file name and the catalog; nothing below repeats it.
-- The opening of the ontology_schema evaluation dimension (D21) is a migration of its own, the next one.
--
-- 1. Gate registry: runtime.nexloop_evo_hard_gates_for(dimension) gives an ontology_schema bundle the version nx040-gates-1 (G1-G8 of
--    nx039-gates-1 unchanged, G9 index_migration_impact enabled, G10 migration_bounded, G11 reflow_budget, G12 tool_context_compatible,
--    G13 authority_unchanged); every other dimension keeps runtime.nexloop_evo_hard_gates(). authz.nexloop_evo_bundle_freeze (0237 body,
--    replaced in place) freezes the dimension's list. The evaluator answers it with nexloop_eios.evolution.schema_gates.
-- 2. Release permits (39 design §12.4): runtime.nexloop_evo_release_permits. The evaluator issues one for an ontology_schema candidate
--    accepted_for_release whose latest report accepts a minor change under nx040-gates-1 (authz.nexloop_evo_permit_issue, 7 days). The
--    Schema publication (F3, 0254 body replaced in place) uses it once: permit_missing, permit_used, permit_expired, permit_mismatch
--    (another report, class or gate version). A permit grants no authority (AT-024); the proposal's permit reference is the permit.
-- 3. Reflow budget (G11, D26): authz.nexloop_evo_reflow_reserve counts the expected reflow cost of an evaluating ontology_schema
--    candidate as evolution of the candidate (candidate cap, tenant month); a spent tier ends it incomplete/budget_exhausted.
-- 4. Migration limit (D25): a publication moves at most 10 000 objects; above that it is refused (migration_over_limit) unless the
--    reviewer asks for a batched one: the first 10 000 move with it, the rest through authz.nexloop_schema_migrate_batch (the same
--    reviewer entry, 10 000 per call) - objects of the previous version stay valid meanwhile (additive changes only).
-- 5. Watch window (D27): every Schema publication (publish or hide) starts a 7-day watch. runtime.nexloop_schema_watch compares the
--    claim-extraction failure rate and the Context volume (budget_report used) of the week before with the time since: a failure rate
--    more than 2 points higher (at least 20 extraction tasks a side) or a Context volume above 105% (at least 20 packs a side) is
--    regressing with the suggestion `hide` (the next version) - never an automatic rollback. The metrics snapshot gains
--    evolution.schema_watch; alert rules v9 warn on it.

-- 1. Gate registry --------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_evo_hard_gates_for(p_dimension text) returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_dimension='ontology_schema' then
  '{"version":"nx040-gates-1","gates":["references_valid","structure_valid","zero_overreach","zero_cross_tenant","zero_fact_pollution","no_forbidden_regression","semantic_regression","cost_performance","semantic_conflict","index_migration_impact","migration_bounded","reflow_budget","tool_context_compatible","authority_unchanged"],"metrics":["tool_regression","context_volume","reflow_cost"],"non_inferiority":{"primary_metric":"tool_regression","margin":0.01,"ties_accept":true}}'::jsonb
  else runtime.nexloop_evo_hard_gates() end
$$;
alter function runtime.nexloop_evo_hard_gates_for(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_hard_gates_for(text) from public;

-- 2. Release permits ------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_evo_release_permits (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),permit_id uuid not null,
 candidate_id uuid not null,report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 change_class text not null check(change_class in ('patch','minor')),gates_version text not null check(gates_version~'^[A-Za-z0-9._:@+-]{1,128}$'),
 issued_by text not null,issued_at timestamptz not null default clock_timestamp(),expires_at timestamptz not null,
 used_at timestamptz,used_by text check(used_by~'^publication:[0-9a-f-]{36}$'),
 primary key(tenant_id,world,permit_id),
 foreign key(tenant_id,world,candidate_id) references runtime.nexloop_evo_candidates(tenant_id,world,candidate_id),
 check((used_at is null)=(used_by is null)),check(expires_at=issued_at+interval '7 days'));
create unique index nexloop_evo_release_permits_open on runtime.nexloop_evo_release_permits(tenant_id,world,candidate_id) where used_at is null;
create table ontology.nexloop_schema_migration_batches (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),publication_id uuid not null,
 type_name text not null,from_version integer not null,to_version integer not null check(to_version=from_version+1),
 remaining integer not null check(remaining>=0),batches integer not null default 1 check(batches>=1),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,publication_id),
 foreign key(tenant_id,world,publication_id) references ontology.nexloop_schema_publications(tenant_id,world,publication_id) deferrable initially deferred);
create table runtime.nexloop_schema_watches (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),publication_id uuid not null,
 type_name text not null,published_at timestamptz not null,watch_until timestamptz not null,
 primary key(tenant_id,world,publication_id),check(watch_until=published_at+interval '7 days'),
 foreign key(tenant_id,world,publication_id) references ontology.nexloop_schema_publications(tenant_id,world,publication_id) deferrable initially deferred);
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_evo_release_permits','ontology.nexloop_schema_migration_batches','runtime.nexloop_schema_watches'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';execute 'alter table '||t||' force row level security';
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute 'revoke all on '||t||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator';
 end loop;
end $$;
-- A permit is issued once and only ever marked used, once.
create function runtime.nexloop_evo_permit_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'release permits are never deleted' using errcode='42501';end if;
 if (to_jsonb(new)-'used_at'-'used_by') is distinct from (to_jsonb(old)-'used_at'-'used_by') or old.used_at is not null or new.used_at is null then
  raise exception 'a release permit is used once and never changed' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_evo_permit_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_permit_guard() from public;
create trigger nx040_permit_guard before update or delete on runtime.nexloop_evo_release_permits for each row execute function runtime.nexloop_evo_permit_guard();

create function authz.nexloop_evo_permit_issue(p_digest text,p_candidate uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';x runtime.nexloop_evo_candidates;rp runtime.nexloop_evo_reports;
 bd runtime.nexloop_evo_bundles;pm runtime.nexloop_evo_release_permits;v_now timestamptz:=clock_timestamp();
begin
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate for update;
 if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
 select * into pm from runtime.nexloop_evo_release_permits where tenant_id=v_tenant and world='real' and candidate_id=p_candidate and used_at is null;
 if found and pm.expires_at>clock_timestamp() then
  return jsonb_build_object('issued',true,'replayed',true,'permit_id',pm.permit_id,'expires_at',pm.expires_at,'report_digest',pm.report_digest);end if;
 select * into rp from runtime.nexloop_evo_reports where tenant_id=v_tenant and world='real' and candidate_id=p_candidate order by recorded_at desc limit 1;
 select * into bd from runtime.nexloop_evo_bundles where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if x.dimension<>'ontology_schema' or x.status<>'accepted_for_release' or rp.report_digest is null or rp.conclusion<>'accept'
  or coalesce(rp.report->>'change_class','')<>'minor' or bd.bundle->>'hard_gates_version' is distinct from 'nx040-gates-1' then
  raise exception 'release permit unavailable: the candidate has no accepted minor ontology_schema report under nx040-gates-1' using errcode='22023';end if;
 if pm.permit_id is not null then  -- an expired, unused one is spent so that a new one can be issued
  update runtime.nexloop_evo_release_permits set used_at=clock_timestamp(),used_by='publication:00000000-0000-0000-0000-000000000000'
   where tenant_id=v_tenant and world='real' and permit_id=pm.permit_id;
 end if;
 insert into runtime.nexloop_evo_release_permits(tenant_id,world,permit_id,candidate_id,report_digest,change_class,gates_version,issued_by,issued_at,expires_at)
  values(v_tenant,'real',gen_random_uuid(),p_candidate,rp.report_digest,'minor','nx040-gates-1','service:evaluator',v_now,v_now+interval '7 days')
  returning * into pm;
 return jsonb_build_object('issued',true,'replayed',false,'permit_id',pm.permit_id,'expires_at',pm.expires_at,'report_digest',pm.report_digest);
end $$;
alter function authz.nexloop_evo_permit_issue(text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_permit_issue(text,uuid) from public;
grant execute on function authz.nexloop_evo_permit_issue(text,uuid) to nexloop_evo_evaluator;

-- 3. Reflow budget (G11, D26) ----------------------------------------------------------------------------------------------------------
create function authz.nexloop_evo_reflow_reserve(p_digest text,p_candidate uuid,p_consumption_id text,p_amount text,p_unit text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';x runtime.nexloop_evo_candidates;
 lim control.nexloop_budget_limits%rowtype;old control.nexloop_budget_consumption%rowtype;v_amount numeric;v_source text:='evolution-candidate:'||p_candidate;
 v_used numeric;v_month numeric;v_tier text;
begin
 if coalesce(p_consumption_id,'')!~'^reflow:[A-Za-z0-9._:-]{1,120}$' then raise exception 'reflow reservation invalid' using errcode='22023';end if;
 v_amount:=control.nexloop_nx022_decimal(p_amount);
 if v_amount<=0 then raise exception 'budget consumption must be positive' using errcode='22023';end if;
 select * into old from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution' and consumption_id=p_consumption_id;
 if found then
  if old.amount<>v_amount or old.unit<>p_unit or old.source_ref<>v_source then raise exception 'budget consumption id reused' using errcode='NXB02';end if;
  return jsonb_build_object('reserved',true,'replayed',true);
 end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate for update;
 if not found or x.dimension<>'ontology_schema' or x.status<>'evaluating' then raise exception 'reflow reservation: not an evaluating ontology_schema candidate' using errcode='22023';end if;
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world='real' and budget_kind='evolution' for update;
 if not found then raise exception 'budget evolution not configured' using errcode='NXB03';end if;
 if p_unit is distinct from lim.unit then raise exception 'budget unit mismatch' using errcode='22023';end if;
 select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution' and source_ref=v_source;
 if v_used+v_amount>x.cost_limit then v_tier:='candidate';
 elsif clock_timestamp()<lim.period_start or clock_timestamp()>=lim.period_end then v_tier:='tenant_period';
 else
  select coalesce(sum(amount),0) into v_month from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
   and recorded_at>=lim.period_start and recorded_at<lim.period_end;
  if v_month+v_amount>lim.limit_amount then v_tier:='tenant_month';end if;
 end if;
 if v_tier is not null then
  perform runtime.nexloop_evo_candidate_incomplete(v_tenant,p_candidate,'budget_exhausted','status_changed',jsonb_build_object('tier',v_tier,'reflow',true),'service:evaluator');
  return jsonb_build_object('reserved',false,'reason','budget_exhausted','tier',v_tier);
 end if;
 insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
  values(v_tenant,'real','evolution',p_consumption_id,v_amount,p_unit,v_source,'service:evaluator');
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,p_candidate,'budget_reserved',null,null,null,
  jsonb_build_object('scope','candidate','reflow',true,'amount',v_amount::text,'unit',p_unit),'service:evaluator');
 return jsonb_build_object('reserved',true,'replayed',false,'candidate_remaining',(x.cost_limit-v_used-v_amount)::text);
end $$;
alter function authz.nexloop_evo_reflow_reserve(text,uuid,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_reflow_reserve(text,uuid,text,text,text) from public;
grant execute on function authz.nexloop_evo_reflow_reserve(text,uuid,text,text,text) to nexloop_evo_evaluator;

-- 5. Watch window ---------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_schema_watch_start(p_tenant text,p_world text,p_publication uuid,p_type text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n timestamptz:=clock_timestamp();
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_schema_watches(tenant_id,world,publication_id,type_name,published_at,watch_until) values(p_tenant,p_world,p_publication,p_type,n,n+interval '7 days')
  on conflict do nothing;
end $$;
alter function runtime.nexloop_schema_watch_start(text,text,uuid,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_schema_watch_start(text,text,uuid,text) from public;

create function runtime.nexloop_schema_watch(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w record;eb record;ea record;cb record;ca record;items jsonb:='[]'::jsonb;n_active integer:=0;n_reg integer:=0;v_ext boolean;v_ctx boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for w in select publication_id,type_name,published_at,watch_until from runtime.nexloop_schema_watches
   where tenant_id=p_tenant and world=p_world and watch_until>clock_timestamp() order by published_at loop
  n_active:=n_active+1;
  select count(*) n,avg(case when status in ('failed','dead_lettered') then 1.0 else 0.0 end) rate into eb from runtime.jobs
   where tenant_id=p_tenant and world=p_world and queue='claim-extraction' and status in ('succeeded','failed','dead_lettered')
    and updated_at>=w.published_at-interval '7 days' and updated_at<w.published_at;
  select count(*) n,avg(case when status in ('failed','dead_lettered') then 1.0 else 0.0 end) rate into ea from runtime.jobs
   where tenant_id=p_tenant and world=p_world and queue='claim-extraction' and status in ('succeeded','failed','dead_lettered') and updated_at>=w.published_at;
  select count(*) n,avg((budget_report->>'used')::numeric) used into cb from runtime.nexloop_context_packs
   where tenant_id=p_tenant and world=p_world and created_at>=w.published_at-interval '7 days' and created_at<w.published_at and jsonb_typeof(budget_report->'used')='number';
  select count(*) n,avg((budget_report->>'used')::numeric) used into ca from runtime.nexloop_context_packs
   where tenant_id=p_tenant and world=p_world and created_at>=w.published_at and jsonb_typeof(budget_report->'used')='number';
  v_ext:=eb.n>=20 and ea.n>=20 and ea.rate-eb.rate>0.02;
  v_ctx:=cb.n>=20 and ca.n>=20 and cb.used>0 and ca.used>cb.used*1.05;
  if v_ext or v_ctx then n_reg:=n_reg+1;end if;
  items:=items||jsonb_build_array(jsonb_build_object('publication_id',w.publication_id,'type_name',w.type_name,'published_at',w.published_at,'watch_until',w.watch_until,
   'extraction_tasks_before',eb.n,'extraction_tasks_after',ea.n,'extraction_failure_rate_before',round(coalesce(eb.rate,0),4),'extraction_failure_rate_after',round(coalesce(ea.rate,0),4),
   'context_packs_before',cb.n,'context_packs_after',ca.n,'context_used_before',round(coalesce(cb.used,0),1),'context_used_after',round(coalesce(ca.used,0),1),
   'extraction_regressing',v_ext,'context_over_limit',v_ctx,'regressing',v_ext or v_ctx,'suggestion',case when v_ext or v_ctx then 'hide' end));
 end loop;
 return jsonb_build_object('active',n_active,'regressing',n_reg,'publications',items);
end $$;
alter function runtime.nexloop_schema_watch(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_schema_watch(text,text) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_schema_watch;
revoke all on function runtime.nexloop_metrics_snapshot_before_schema_watch(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_schema_watch(p_tenant,p_world);
begin
 return jsonb_set(s,'{evolution}',coalesce(s->'evolution','{}'::jsonb)||jsonb_build_object('schema_watch',runtime.nexloop_schema_watch(p_tenant,p_world)));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- 1. The frozen gate list per dimension (0237 body, replaced in place) -------------------------------------------------------------
create or replace function authz.nexloop_evo_bundle_freeze(p_digest text,p_candidate uuid,p_levels text[],p_seed text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);x runtime.nexloop_evo_candidates;d runtime.nexloop_evo_diagnoses;p runtime.nexloop_evo_patches;
 old runtime.nexloop_evo_bundles;lim control.nexloop_budget_limits%rowtype;g jsonb;b jsonb;v_d numeric;v_c numeric;v_m numeric;
 v_digest text;
begin
 if p_levels is null or not (p_levels=array['L0'] or p_levels=array['L0','L1']) or coalesce(p_seed,'')!~'^[0-9a-f]{32}$' then
  raise exception 'evaluation bundle invalid' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate for update;
 if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
 g:=runtime.nexloop_evo_hard_gates_for(x.dimension);  -- NX-040 F4: nx040-gates-1 for ontology_schema
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


-- 4. Migration limit in the gates (0254 body, replaced in place) ------------------------------------------------------------------------
create or replace function ontology.nexloop_schema_publication_gates(p_tenant text,r ontology.nexloop_schema_proposals,p_kind text,p_pub jsonb,p_hide text)
 returns text[] language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare f text[]:='{}';old jsonb;oldv integer;new jsonb:=p_pub->'schema';np jsonb;expected jsonb;prop jsonb;a jsonb;pred jsonb;i integer;
 restricted text[]:=ontology.nexloop_schema_restricted_names(p_tenant);v_count integer;
begin
 select d.version,d.definition into oldv,old from ontology.object_type_versions d where d.tenant_id=p_tenant and d.type_name=r.type_name order by d.version desc limit 1;
 if jsonb_typeof(new) is distinct from 'object' or new->>'type_name' is distinct from r.type_name then return array['schema_not_the_proposal'];end if;
 if p_kind='publish' and coalesce(oldv,0)<>r.parent_schema_version then return array['parent_moved'];end if;
 if r.operation='add_object_type' and p_kind='publish' then
  if oldv is not null then return array['parent_moved'];end if;
  if jsonb_array_length(coalesce(r.change->'primary_key','[]'::jsonb))>0 or jsonb_array_length(coalesce(new->'primary_key','[]'::jsonb))>0 then
   f:=f||'breaking_primary_key'::text;end if;
  if (new->>'only_edit_via_actions')::boolean is not true then f:=f||'edit_mode_open'::text;end if;
  if jsonb_array_length(coalesce(p_pub->'actions','[]'::jsonb))<>0 then f:=f||'authority_content'::text;end if;
  if exists(select 1 from jsonb_array_elements(coalesce(r.change->'properties','[]'::jsonb)) e where e->>'property_name'=any(restricted)) then
   f:=f||'property_security_bypass'::text;end if;
  if exists(select 1 from jsonb_array_elements(coalesce(r.change->'properties','[]'::jsonb)) e where e->>'value_type' not in ('string','integer','number','boolean','datetime')) then
   f:=f||'value_type_not_allowed'::text;end if;
  expected:=jsonb_build_object('type_name',r.type_name,'version',1,'only_edit_via_actions',true,'primary_key','[]'::jsonb,
   'properties',(select coalesce(jsonb_agg(jsonb_build_object('property_name',e->>'property_name','value_type',e->>'value_type','required',false,'nullable',true)
     order by n),'[]'::jsonb) from jsonb_array_elements(coalesce(r.change->'properties','[]'::jsonb)) with ordinality q(e,n)));
  if jsonb_build_object('type_name',new->>'type_name','version',(new->>'version')::integer,'only_edit_via_actions',new->'only_edit_via_actions','primary_key',coalesce(new->'primary_key','[]'::jsonb),
     'properties',(select coalesce(jsonb_agg(jsonb_build_object('property_name',e->>'property_name','value_type',e->>'value_type','required',coalesce((e->>'required')::boolean,false),
       'nullable',coalesce((e->>'nullable')::boolean,false)) order by n),'[]'::jsonb) from jsonb_array_elements(coalesce(new->'properties','[]'::jsonb)) with ordinality q(e,n)))
    is distinct from expected or exists(select 1 from jsonb_object_keys(new) k where k in ('relations','actions')) then
   f:=f||'schema_not_the_proposal'::text;end if;
  return f;
 end if;
 if oldv is null then return array['parent_moved'];end if;
 if (new->>'version')::integer is distinct from oldv+1 then f:=f||'schema_not_the_proposal'::text;end if;
 if p_kind='hide' then
  if r.operation<>'add_optional_property' or r.status<>'published' or not exists(select 1 from jsonb_array_elements(old->'properties') e
    where e->>'property_name'=p_hide and p_hide=r.change->>'property_name') then return array['not_published_property'];end if;
  expected:=old||jsonb_build_object('version',oldv+1,'properties',(select jsonb_agg(case when e->>'property_name'=p_hide then e||'{"visibility":"hidden"}'::jsonb else e end order by n)
   from jsonb_array_elements(old->'properties') with ordinality q(e,n)));
  if new is distinct from expected then f:=f||'schema_not_the_proposal'::text;end if;
 elsif r.operation='add_optional_property' then
  if r.change->>'value_type' not in ('string','integer','number','boolean','datetime') then f:=f||'value_type_not_allowed'::text;end if;
  if exists(select 1 from jsonb_array_elements(old->'properties') e where e->>'property_name'=r.change->>'property_name') then f:=f||'property_exists'::text;end if;
  if r.change->>'property_name'=any(restricted) then f:=f||'property_security_bypass'::text;end if;
  np:=jsonb_build_object('property_name',r.change->>'property_name','value_type',r.change->>'value_type','required',false,'nullable',true,'default_value',null,
   'description','','display_name','','render_hint','plain','visibility','normal','type_descriptor',null);
  expected:=old||jsonb_build_object('version',oldv+1,'properties',(old->'properties')||jsonb_build_array(np));
  if new is distinct from expected then f:=f||'schema_not_the_proposal'::text;end if;
 elsif r.operation='add_vocabulary_value' then
  select e into prop from jsonb_array_elements(old->'properties') e where e->>'property_name'=r.change->>'property_name';
  if prop is null or jsonb_typeof(prop->'type_descriptor'->'enum') is distinct from 'array' or jsonb_array_length(prop->'type_descriptor'->'enum')=0 then
   f:=f||'not_closed_vocabulary'::text;
  elsif prop->'type_descriptor'->'enum' @> jsonb_build_array(r.change->'value') then f:=f||'vocabulary_value_exists'::text;
  else
   expected:=old||jsonb_build_object('version',oldv+1,'properties',(select jsonb_agg(case when e->>'property_name'=r.change->>'property_name'
     then jsonb_set(e,'{type_descriptor,enum}',(e->'type_descriptor'->'enum')||jsonb_build_array(r.change->'value')) else e end order by n)
     from jsonb_array_elements(old->'properties') with ordinality q(e,n)));
   if new is distinct from expected then f:=f||'schema_not_the_proposal'::text;end if;
  end if;
 end if;
 select count(*) into v_count from ontology.objects o where o.tenant_id=p_tenant and o.world='real' and o.type_name=r.type_name and o.schema_version=oldv;
 -- D25: above 10 000 objects the publication is refused unless it is a batched one (then the objects move 10 000 per call).
 if v_count>10000 and coalesce((p_pub->>'batch')::boolean,false) is not true then f:=f||'migration_over_limit'::text;end if;
 -- Successor Actions (§5.3): exactly the active Actions bound to the old version, each changed only in its version and the bound
 -- Schema reference (0082's rule, with F1's codes).
 if jsonb_array_length(coalesce(p_pub->'actions','[]'::jsonb))<>(select count(*) from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.active
   and exists(select 1 from jsonb_array_elements(d.definition->'object_types') x where x->>'stable_name'=r.type_name and (x->>'version')::integer=oldv)) then
  f:=f||'successor_not_one_to_one'::text;
 end if;
 for a in select value from jsonb_array_elements(coalesce(p_pub->'actions','[]'::jsonb)) loop
  pred:=null;
  select jsonb_build_object('definition',d.definition,'capability',d.capability) into pred from control.nexloop_action_definitions d
   where d.tenant_id=p_tenant and d.world='real' and d.active and d.definition->>'stable_name'=a->'definition'->>'stable_name'
    and (d.definition->>'version')::integer=(a->'definition'->>'version')::integer-1
    and exists(select 1 from jsonb_array_elements(d.definition->'object_types') x where x->>'stable_name'=r.type_name and (x->>'version')::integer=oldv);
  if pred is null then f:=f||'authority_content'::text;continue;end if;
  if a->'capability' is distinct from pred->'capability'
   or exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.resource_id='eios:action:'||(a->'definition'->>'stable_name')||':'||(a->'definition'->>'version'))
   or ((a->'definition')-'version'-'object_types'-'governance'-'contract_digest'-'previous_version'-'created_at')
      is distinct from ((pred->'definition')-'version'-'object_types'-'governance'-'contract_digest'-'previous_version'-'created_at')
   or ((a->'definition'->'governance')-'change_scope') is distinct from ((pred->'definition'->'governance')-'change_scope')
   or ((a->'definition'->'governance'->'change_scope')-'object_types') is distinct from ((pred->'definition'->'governance'->'change_scope')-'object_types') then
   f:=f||'tool_signature_changed'::text;
  end if;
  for i in 0..greatest(jsonb_array_length(a->'definition'->'object_types'),jsonb_array_length(pred->'definition'->'object_types'))-1 loop
   if ((a->'definition'->'object_types'->i)-'version'-'schema_digest') is distinct from ((pred->'definition'->'object_types'->i)-'version'-'schema_digest')
    or (pred->'definition'->'object_types'->i->>'stable_name'<>r.type_name and a->'definition'->'object_types'->i is distinct from pred->'definition'->'object_types'->i)
    or (pred->'definition'->'object_types'->i->>'stable_name'=r.type_name and (a->'definition'->'object_types'->i->>'version')::integer<>oldv+1) then
    f:=f||'tool_signature_changed'::text;exit;
   end if;
  end loop;
 end loop;
 return array(select distinct x from unnest(f) x order by 1);
end $$;


-- 2/4/5. The publication (0254 body, replaced in place): permit check and use, batched migration, the watch -------------------------------
create or replace function authz.nexloop_schema_proposal_publish(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;t text;principal text;r ontology.nexloop_schema_proposals;
 prior ontology.nexloop_schema_publications;gates text[];pub jsonb:=c->'publication';v_kind text:=c->>'kind';oldv integer;fp_before text;
 perm_before text;perm_after text;pm runtime.nexloop_evo_release_permits;v_latest text;v_permit text;v_left integer:=0;successors text[]:='{}';act jsonb;n integer:=0;v_report text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>2097152
  or a->>'protocol' is distinct from 'nexloop-schema-publication-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'schema publication unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-schema-publication-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'schema publication unavailable' using errcode='42501';end if;
 -- D22: a person, never a service, a Run or an Agent.
 if not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'schema publication requires a human session' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'human' or ident->'binding'->>'tenant_id' is distinct from a->>'tenant_id'
  or ident->'binding'->>'subject_principal_id' is distinct from a->>'principal_id' then
  raise exception 'schema publication requires a human session' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 t:=a->>'tenant_id';principal:=a->>'principal_id';
 perform set_config('eios.tenant_id',t,true);
 if coalesce(c->>'publication_id','')!~'^[0-9a-f-]{36}$' or coalesce(c->>'proposal_id','')!~'^[0-9a-f-]{36}$' or v_kind not in ('publish','hide') or v_kind is null
  or char_length(coalesce(c->>'rationale',''))not between 1 and 2000 or jsonb_typeof(pub) is distinct from 'object'
  or jsonb_typeof(c->'compatibility_report') is distinct from 'object' then
  raise exception 'schema publication invalid' using errcode='22023';end if;
 select * into prior from ontology.nexloop_schema_publications where tenant_id=t and world=p_world and publication_id=(c->>'publication_id')::uuid;
 if found then
  if prior.proposal_id<>(c->>'proposal_id')::uuid or prior.kind<>v_kind then raise exception 'schema publication id reused' using errcode='40001';end if;
  return jsonb_build_object('published',true,'replayed',true,'publication_id',prior.publication_id,'version_after',prior.version_after,
   'permission_digest',prior.permission_digest_after);
 end if;
 select * into r from ontology.nexloop_schema_proposals where tenant_id=t and world=p_world and proposal_id=(c->>'proposal_id')::uuid for update;
 if not found then raise exception 'schema proposal unknown' using errcode='22023';end if;
 if (v_kind='publish' and r.status<>'frozen') or (v_kind='hide' and r.status<>'published') then
  raise exception 'schema proposal % cannot be %ed',r.status,v_kind using errcode='22023';end if;
 -- The backend's F1 verdict must allow it; SQL then re-derives the version itself.
 if (c->'compatibility_report'->>'allowed')::boolean is not true or c->'compatibility_report'->>'op' is distinct from
   (case when v_kind='hide' then 'hide_property' else r.operation end) or jsonb_array_length(coalesce(c->'compatibility_report'->'codes','[]'::jsonb))>0 then
  return jsonb_build_object('published',false,'codes',coalesce(c->'compatibility_report'->'codes','["breaking_change"]'::jsonb),'source','compatibility');
 end if;
 v_report:=encode(sha256(convert_to(runtime.nexloop_canonical_json(c->'compatibility_report'),'UTF8')),'hex');
 -- NX-040 F4 (39 design §12.4): a publication uses the candidate's release permit once - present, unexpired, unused, and naming the
 -- candidate's latest accepted report, a minor change class and the nx040 gates. A hide (rollback) is the person's own decision.
 if v_kind='publish' then
  select * into pm from runtime.nexloop_evo_release_permits where tenant_id=t and world='real' and candidate_id=r.candidate_id order by issued_at desc limit 1 for update;
  select rp.report_digest into v_latest from runtime.nexloop_evo_reports rp where rp.tenant_id=t and rp.world='real' and rp.candidate_id=r.candidate_id
   order by rp.recorded_at desc limit 1;
  v_permit:=case when pm.permit_id is null then 'permit_missing' when pm.used_at is not null then 'permit_used' when pm.expires_at<=clock_timestamp() then 'permit_expired'
   when pm.report_digest is distinct from v_latest or pm.change_class<>'minor' or pm.gates_version<>'nx040-gates-1' then 'permit_mismatch' end;
  if v_permit is not null then return jsonb_build_object('published',false,'codes',jsonb_build_array(v_permit),'source','permit');end if;
 end if;
 gates:=ontology.nexloop_schema_publication_gates(t,r,v_kind,pub,c->>'property_name');
 if cardinality(gates)>0 then
  if 'parent_moved'=any(gates) and r.status='frozen' then
   update ontology.nexloop_schema_proposals set status='withdrawn',withdraw_reason='parent_moved',finished_at=clock_timestamp()
    where tenant_id=t and world=p_world and proposal_id=r.proposal_id;
   insert into ontology.nexloop_schema_proposal_events(tenant_id,world,proposal_id,from_status,to_status,reason,recorded_by)
    values(t,p_world,r.proposal_id,'frozen','withdrawn','parent_moved','human:'||principal);
  end if;
  return jsonb_build_object('published',false,'codes',to_jsonb(gates),'source','gates');
 end if;
 select max(version) into oldv from ontology.object_type_versions where tenant_id=t and type_name=r.type_name;
 fp_before:=authz.nexloop_tenant_authority_fingerprint(t);
 perm_before:=authz.nexloop_schema_permission_digest(t,'{}');
 insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(t,r.type_name,(pub->'schema'->>'version')::integer,pub->'schema');
 for act in select value from jsonb_array_elements(coalesce(pub->'actions','[]'::jsonb)) loop
  insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
   values(t,'real','eios:action:'||(act->'definition'->>'stable_name')||':'||(act->'definition'->>'version'),act->'definition',act->'capability',true);
  successors:=successors||('eios:action:'||(act->'definition'->>'stable_name')||':'||(act->'definition'->>'version'));
 end loop;
 if oldv is not null then
  -- Additive: existing objects stay valid and only move to the published version (values untouched).
  -- D25: at most 10 000 objects in the publishing transaction; a batched publication leaves the rest to authz.nexloop_schema_migrate_batch.
  update ontology.objects o set schema_version=oldv+1,updated_at=clock_timestamp() from (select type_name,object_id from ontology.objects where tenant_id=t and world='real'
    and type_name=r.type_name and schema_version=oldv order by object_id limit 10000) q
   where o.tenant_id=t and o.world='real' and o.type_name=q.type_name and o.object_id=q.object_id;
  get diagnostics n=row_count;
  select count(*) into v_left from ontology.objects where tenant_id=t and world='real' and type_name=r.type_name and schema_version=oldv;
 end if;
 if authz.nexloop_tenant_authority_fingerprint(t) is distinct from fp_before then
  raise exception 'publication must not change authority' using errcode='42501';end if;
 perm_after:=authz.nexloop_schema_permission_digest(t,successors);
 if perm_after is distinct from perm_before then raise exception 'publication must not change any permission' using errcode='42501';end if;
 insert into ontology.nexloop_schema_publications(tenant_id,world,publication_id,proposal_id,kind,type_name,version_before,version_after,successor_actions,
   objects_migrated,permission_digest_before,permission_digest_after,compatibility_report_digest,published_by,rationale)
  values(t,p_world,(c->>'publication_id')::uuid,r.proposal_id,v_kind,r.type_name,coalesce(oldv,0),(pub->'schema'->>'version')::integer,successors,n,
   perm_before,perm_after,v_report,'human:'||principal,c->>'rationale');
 if v_left>0 then
  insert into ontology.nexloop_schema_migration_batches(tenant_id,world,publication_id,type_name,from_version,to_version,remaining)
   values(t,p_world,(c->>'publication_id')::uuid,r.type_name,oldv,oldv+1,v_left);
 end if;
 if v_kind='publish' then
  update runtime.nexloop_evo_release_permits set used_at=clock_timestamp(),used_by='publication:'||(c->>'publication_id')
   where tenant_id=t and world='real' and permit_id=pm.permit_id;
  perform runtime.nexloop_schema_proposal_mark_published(t,r.proposal_id,'permit:'||pm.permit_id,'human:'||principal);
 end if;
 perform runtime.nexloop_schema_watch_start(t,p_world,(c->>'publication_id')::uuid,r.type_name);
 return jsonb_build_object('published',true,'replayed',false,'publication_id',c->>'publication_id','version_after',(pub->'schema'->>'version')::integer,
  'successor_actions',to_jsonb(successors),'objects_migrated',n,'objects_remaining',v_left,'permit',case when v_kind='publish' then 'permit:'||pm.permit_id end,
  'permission_digest',perm_after);
end $$;


-- 4. Batched migration: the next 10 000 objects of a batched publication, by the same reviewer entry (signed claims as for publish).
create function authz.nexloop_schema_migrate_batch(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;t text;principal text;b ontology.nexloop_schema_migration_batches;n integer;v_left integer;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>2097152
  or a->>'protocol' is distinct from 'nexloop-schema-publication-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'schema migration unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-schema-publication-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'schema migration unavailable' using errcode='42501';end if;
 -- D22: a person, never a service, a Run or an Agent.
 if not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'schema migration requires a human session' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'human' or ident->'binding'->>'tenant_id' is distinct from a->>'tenant_id'
  or ident->'binding'->>'subject_principal_id' is distinct from a->>'principal_id' then
  raise exception 'schema migration requires a human session' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 t:=a->>'tenant_id';principal:=a->>'principal_id';
 perform set_config('eios.tenant_id',t,true);
 if coalesce(c->>'publication_id','')!~'^[0-9a-f-]{36}$' then raise exception 'schema migration invalid' using errcode='22023';end if;
 select * into b from ontology.nexloop_schema_migration_batches where tenant_id=t and world=p_world and publication_id=(c->>'publication_id')::uuid for update;
 if not found then raise exception 'schema migration unknown' using errcode='22023';end if;
 if b.remaining=0 then return jsonb_build_object('migrated',0,'remaining',0,'batches',b.batches);end if;
 update ontology.objects o set schema_version=b.to_version,updated_at=clock_timestamp() from (select type_name,object_id from ontology.objects where tenant_id=t and world='real'
   and type_name=b.type_name and schema_version=b.from_version order by object_id limit 10000) q
  where o.tenant_id=t and o.world='real' and o.type_name=q.type_name and o.object_id=q.object_id;
 get diagnostics n=row_count;
 select count(*) into v_left from ontology.objects where tenant_id=t and world='real' and type_name=b.type_name and schema_version=b.from_version;
 update ontology.nexloop_schema_migration_batches set remaining=v_left,batches=batches+1,updated_at=clock_timestamp()
  where tenant_id=t and world=p_world and publication_id=b.publication_id;
 return jsonb_build_object('migrated',n,'remaining',v_left,'batches',b.batches+1,'migrated_by','human:'||principal);
end $$;
alter function authz.nexloop_schema_migrate_batch(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_schema_migrate_batch(text,text,text,text,text) from public;
grant execute on function authz.nexloop_schema_migrate_batch(text,text,text,text,text) to nexloop_api;

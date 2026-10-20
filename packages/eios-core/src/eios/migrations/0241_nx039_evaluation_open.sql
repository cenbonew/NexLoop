-- NX-039 V1 (design docs/implementation/NX-039-design.md §2.2, §3.4, §4.2, §4.3, §5.1, §11 V1, §12.1; owner D39-1, D39-2, D39-3
-- decided 2026-10-20). Temporary number; the dispatcher renumbers at merge.
--
-- 1. A login role nexloop_evo_evaluator (D39-1) executes the evaluation functions below and nothing else. Identity: an active
--    service credential without a Run context whose principal is registered for this tenant as an evaluator; the tenant comes
--    from the credential, world real only.
-- 2. One purpose per service principal: authz.nexloop_evo_principals has one row per principal (writer or evaluator), so a principal
--    is never registered for both. The writer entry (runtime.nexloop_evo_writer_tenant, 0235 body, replaced in place) now also
--    refuses a principal registered as an evaluator, so the evaluator can never write candidates, bundles or reports.
-- 3. The open dimensions (§12.1: an auditable row, not a code constant; semantic_content only, D19, D39-6) and the owner's
--    per-dimension stop (§8.5; the governed stop Action is V4, this table is what the opening checks).
-- 4. Holdouts (§4.2, §4.3, D39-3): a validation set is registered once by the digest of its case ids (append only); every opened
--    evaluation records one use; a holdout decides at most 5 candidates.
-- 5. authz.nexloop_evo_evaluation_open: the seven checks of §2.2 in order, each with its own reason code (bundle_mismatch,
--    candidate_not_ready, leakage_detected, holdout_exhausted, dimension_closed, dimension_stopped, budget_exhausted). A refusal
--    changes nothing and is returned, not raised; an opening turns the candidate proposed -> evaluating, records the holdout use
--    and a status event in the same transaction. Replay of an opened candidate with the same bundle returns the same answer.
-- Reports keep the E5 table and contract (evolution-evaluation-report); the evaluator's report path is V2.
-- Published migrations are not edited.

-- 1. The evaluator role --------------------------------------------------------------------------------------------------------
do $$begin
 if not exists(select 1 from pg_roles where rolname='nexloop_evo_evaluator') then
  create role nexloop_evo_evaluator login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
end $$;
grant usage on schema authz to nexloop_evo_evaluator;

-- 2. One purpose per principal ----------------------------------------------------------------------------------------------------
create table authz.nexloop_evo_principals (
 principal_id text primary key check(char_length(principal_id) between 1 and 200),
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 purpose text not null check(purpose in ('writer','evaluator')),
 registered_by text not null check(char_length(registered_by) between 1 and 200),registered_at timestamptz not null default clock_timestamp());
alter table authz.nexloop_evo_principals owner to nexloop_owner;
revoke all on authz.nexloop_evo_principals from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator;
create function authz.nexloop_evo_principals_fixed() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception 'an evolution principal is registered once for one purpose, never changed' using errcode='42501';
end $$;
alter function authz.nexloop_evo_principals_fixed() owner to nexloop_owner;
revoke all on function authz.nexloop_evo_principals_fixed() from public;
create trigger nx039_principal_fixed before update or delete on authz.nexloop_evo_principals for each row execute function authz.nexloop_evo_principals_fixed();
create trigger nx039_principal_no_truncate before truncate on authz.nexloop_evo_principals for each statement execute function authz.nexloop_evo_principals_fixed();

-- The writer entry: 0235 body, plus the refusal of an evaluator principal.
create or replace function runtime.nexloop_evo_writer_tenant(p_digest text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;
begin
 if session_user is distinct from 'nexloop_evo_writer' then raise exception 'evolution write unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,'real');
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest) or ident->'binding'->>'tenant_id' is null then
  raise exception 'evolution write unavailable' using errcode='42501';end if;
 -- NX-039 D39-1: the principal that evaluates never writes what it evaluates.
 if exists(select 1 from authz.nexloop_evo_principals p where p.principal_id=ident->'binding'->>'subject_principal_id' and p.purpose='evaluator') then
  raise exception 'evolution write unavailable: an evaluator principal' using errcode='42501';end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 return ident->'binding'->>'tenant_id';
end $$;

create function runtime.nexloop_evo_evaluator_identity(p_digest text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_principal text;v_tenant text;
begin
 if session_user is distinct from 'nexloop_evo_evaluator' then raise exception 'evolution evaluation unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,'real');
 v_principal:=ident->'binding'->>'subject_principal_id';v_tenant:=ident->'binding'->>'tenant_id';
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest) or v_tenant is null
  or not exists(select 1 from authz.nexloop_evo_principals p where p.principal_id=v_principal and p.tenant_id=v_tenant and p.purpose='evaluator') then
  raise exception 'evolution evaluation unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return jsonb_build_object('tenant_id',v_tenant,'principal_id',v_principal);
end $$;
alter function runtime.nexloop_evo_evaluator_identity(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_evaluator_identity(text) from public;

-- 3. Open dimensions and the owner's stop --------------------------------------------------------------------------------------
create table control.nexloop_evo_open_dimensions (
 dimension text primary key check(dimension in ('semantic_content','semantic_tool','ontology_schema')),
 opened_by text not null check(char_length(opened_by) between 1 and 200),opened_at timestamptz not null default clock_timestamp(),
 decision text not null check(char_length(decision) between 1 and 500));
insert into control.nexloop_evo_open_dimensions(dimension,opened_by,decision)
 values('semantic_content','migration:nx039-v1','NX-038 D19 and NX-039 D39-6 (owner 2026-10-20): the first batch evaluates semantic_content only');
create table control.nexloop_evo_dimension_stops (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 dimension text not null check(dimension in ('semantic_content','semantic_tool','ontology_schema')),
 stopped boolean not null,revision bigint not null check(revision>=1),changed_by text not null check(char_length(changed_by) between 1 and 200),
 changed_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,dimension));
do $$declare t text;begin
 foreach t in array array['nexloop_evo_open_dimensions','nexloop_evo_dimension_stops'] loop
  execute format('alter table control.%I owner to nexloop_owner',t);
  execute format('revoke all on control.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator',t);
 end loop;
end $$;
alter table control.nexloop_evo_dimension_stops enable row level security;
alter table control.nexloop_evo_dimension_stops force row level security;
create policy tenant_boundary on control.nexloop_evo_dimension_stops to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));

-- 4. Holdouts ---------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_evo_holdout_limit() returns integer language sql immutable set search_path=pg_catalog,pg_temp as $$ select 5 $$;
alter function runtime.nexloop_evo_holdout_limit() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_holdout_limit() from public;
create table runtime.nexloop_evo_holdouts (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 holdout_digest text not null check(holdout_digest~'^[0-9a-f]{64}$'),
 case_ids text[] not null check(cardinality(case_ids) between 1 and 256),
 registered_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,holdout_digest));
create table runtime.nexloop_evo_holdout_uses (
 tenant_id text not null,world text not null check(world='real'),holdout_digest text not null,candidate_id uuid not null,
 use_number integer not null check(use_number between 1 and 5),bundle_digest text not null check(bundle_digest~'^[0-9a-f]{64}$'),
 evaluator_principal text not null,used_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,holdout_digest,candidate_id),unique(tenant_id,world,holdout_digest,use_number),
 foreign key(tenant_id,world,holdout_digest) references runtime.nexloop_evo_holdouts(tenant_id,world,holdout_digest),
 foreign key(tenant_id,world,candidate_id) references runtime.nexloop_evo_bundles(tenant_id,world,candidate_id));
create function runtime.nexloop_evo_holdout_frozen() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception 'holdouts and their uses are frozen' using errcode='42501';
end $$;
alter function runtime.nexloop_evo_holdout_frozen() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_holdout_frozen() from public;
do $$declare t text;begin
 foreach t in array array['nexloop_evo_holdouts','nexloop_evo_holdout_uses'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator',t);
  execute format('create trigger nx039_frozen before update or delete on runtime.%I for each row execute function runtime.nexloop_evo_holdout_frozen()',t);
  execute format('create trigger nx039_no_truncate before truncate on runtime.%I for each statement execute function runtime.nexloop_evo_holdout_frozen()',t);
 end loop;
end $$;

create function runtime.nexloop_evo_holdout_digest(p_case_ids text[]) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(array_to_string(array(select distinct x from unnest(p_case_ids) x order by x),','),'UTF8')),'hex')
$$;
alter function runtime.nexloop_evo_holdout_digest(text[]) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_holdout_digest(text[]) from public;

-- The event log knows the opening.
alter table runtime.nexloop_evo_candidate_events drop constraint nexloop_evo_candidate_events_event_kind_check;
alter table runtime.nexloop_evo_candidate_events add constraint nexloop_evo_candidate_events_event_kind_check check(event_kind in
 ('diagnosis_opened','diagnosis_closed','proposed','status_changed','budget_reserved','invalidated','bundle_frozen','report_recorded','review_linked',
  'evaluation_opened'));

-- 5. Opening an evaluation (§2.2, §3.4) ----------------------------------------------------------------------------------------------
create function authz.nexloop_evo_evaluation_open(p_digest text,p_candidate uuid,p_bundle_digest text) returns jsonb
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
alter function authz.nexloop_evo_evaluation_open(text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_evaluation_open(text,uuid,text) from public;
grant execute on function authz.nexloop_evo_evaluation_open(text,uuid,text) to nexloop_evo_evaluator;

-- NX-038 E4 attribution and candidates (docs/implementation/NX-038-design.md §3.1, §3.2, §11.3, §12.2, §12.5, §12.7 E4;
-- owner D15-D20 2026-10-20). Temporary number; the dispatcher renumbers at merge.
--
-- 1. Budget kind `evolution` (D15): the 0068 check is widened; the owner sets it through the existing governed Budget.set. An
--    evolution consumption always names an evolution source and nothing else may (a check on the consumption table), so an
--    evolution model call is counted against `evolution` only and never against `model`.
-- 2. A login role nexloop_evo_writer (§12.5) executes the evolution functions below and nothing else. Identity: an active service
--    credential without a Run context; the tenant comes from that credential, world real only.
-- 3. Diagnoses (frozen inputs: snapshot digest, Run ids <= 2000, Consumer ids, versions; wall clock 30 minutes, cost <= 20 of the
--    evolution unit, D20), candidates (the evolution-candidate contract; status starts `proposed`; the first batch opens only the
--    semantic_content dimension, D19; at most 3 rounds and 10 of the unit each, D20; AT-048: design and validation cases never
--    share a Consumer or session cluster), patches (ids, hashes and refs only - never text - with the Consumers they came from,
--    D17) and an append-only event log. Nothing here writes a definition, an alias, an authorization fact, a business object, a
--    Claim or a Commitment, and there is no publication or activation entry (ADR-007, D18): NX-038 writes no state after
--    `evaluating` except `incomplete` on budget exhaustion or erasure.
-- 4. Three-tier budget (§12.2, D20): authz.nexloop_evo_reserve checks the diagnosis or candidate cap, the diagnosis deadline and the
--    tenant's monthly `evolution` limit before each model call; whichever tier is spent turns its diagnosis or candidate
--    `incomplete/budget_exhausted` and nothing is reserved.
-- 5. Erasure (§12.5, D17): when a Consumer's erasure purges, the patches that name it as a source are deleted and their candidates
--    become `incomplete/missing_data` (also `accepted_for_release` ones, not yet published); diagnoses whose frozen snapshot holds
--    that Consumer become `incomplete/missing_data` with their open candidates. A tombstone records counts only.
-- Published migrations are not edited.

-- 1. Budget kind evolution (D15) -------------------------------------------------------------------------------------------
alter table control.nexloop_budget_limits drop constraint nexloop_budget_limits_budget_kind_check;
alter table control.nexloop_budget_limits add constraint nexloop_budget_limits_budget_kind_check check(budget_kind in ('model','incentive','evolution'));
alter table control.nexloop_budget_limits add constraint nexloop_budget_limits_evolution_currency check(budget_kind<>'evolution' or unit ~ '^[A-Z]{3}$');
alter table control.nexloop_budget_consumption add constraint nexloop_budget_consumption_evolution_source
 check((budget_kind='evolution')=(source_ref ~ '^evolution-(diagnosis|candidate):[0-9a-f-]{36}$'));

-- 2. The writer role ---------------------------------------------------------------------------------------------------------
do $$begin
 if not exists(select 1 from pg_roles where rolname='nexloop_evo_writer') then
  create role nexloop_evo_writer login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
end $$;
grant usage on schema authz to nexloop_evo_writer;

-- 3. Records -----------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_evo_diagnoses (
 tenant_id text not null,world text not null check(world='real'),diagnosis_id uuid not null,
 input_snapshot_digest text not null check(input_snapshot_digest~'^[0-9a-f]{64}$'),
 parent_revision text not null check(parent_revision~'^[A-Za-z0-9._:@+-]{1,128}$'),
 evaluator_version text not null check(evaluator_version~'^[A-Za-z0-9._:@+-]{1,128}$'),
 model_profile text not null check(model_profile~'^[A-Za-z0-9._:@+-]{1,128}$'),
 run_ids uuid[] not null check(cardinality(run_ids) between 1 and 2000),
 consumer_ids text[] not null check(cardinality(consumer_ids)<=2000 and array_to_string(consumer_ids,',')~'^([a-f0-9]{64}(,[a-f0-9]{64})*)?$'),
 attribution jsonb not null check(jsonb_typeof(attribution)='object'),
 cost_limit numeric not null check(cost_limit>0 and cost_limit<=20),unit text not null check(unit~'^[A-Z]{3}$'),
 opened_at timestamptz not null default clock_timestamp(),deadline timestamptz not null,
 status text not null check(status in ('open','closed','incomplete')),
 end_reason text check(end_reason in ('budget_exhausted','missing_data','user_interrupted','external_block')),
 closed_at timestamptz,
 primary key(tenant_id,world,diagnosis_id),
 check((status='incomplete')=(end_reason is not null)),check((status='open')=(closed_at is null)),
 check(deadline>opened_at and deadline<=opened_at+interval '30 minutes'));

create table runtime.nexloop_evo_candidates (
 tenant_id text not null,world text not null check(world='real'),candidate_id uuid not null,diagnosis_id uuid not null,
 category text not null check(category in ('retrieval_miss','ambiguity_unresolved')),
 dimension text not null check(dimension='semantic_content'),  -- D19: the first batch opens only this dimension
 parent_revision text not null,candidate_revision text not null check(candidate_revision~'^[A-Za-z0-9._:@+-]{1,128}$'),
 primary_hypothesis text not null check(char_length(primary_hypothesis) between 10 and 4000),
 patch_id uuid not null,input_snapshot_digest text not null check(input_snapshot_digest~'^[0-9a-f]{64}$'),
 design_case_ids text[] not null check(cardinality(design_case_ids) between 1 and 256),
 validation_case_ids text[] not null check(cardinality(validation_case_ids) between 1 and 256),
 case_groups jsonb not null check(jsonb_typeof(case_groups)='object'),regroups integer not null default 0 check(regroups between 0 and 512),
 evaluator_version text not null,model_profile text not null,
 maximum_rounds integer not null check(maximum_rounds between 1 and 3),
 allowed_change_scope text[] not null check(cardinality(allowed_change_scope) between 1 and 256),
 status text not null check(status in ('proposed','evaluating','rejected','accepted_for_release','incomplete')),
 end_reason text check(end_reason in ('budget_exhausted','missing_data','user_interrupted','external_block')),
 cost_limit numeric not null check(cost_limit>0 and cost_limit<=10),unit text not null check(unit~'^[A-Z]{3}$'),
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,candidate_id),
 foreign key(tenant_id,world,diagnosis_id) references runtime.nexloop_evo_diagnoses(tenant_id,world,diagnosis_id),
 check((status='incomplete')=(end_reason is not null)),
 check(not (design_case_ids && validation_case_ids)));

create table runtime.nexloop_evo_patches (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),patch_id uuid not null,candidate_id uuid not null,
 body jsonb not null check(jsonb_typeof(body)='object' and octet_length(body::text)<=65536),body_digest text not null check(body_digest~'^[0-9a-f]{64}$'),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,patch_id),unique(tenant_id,world,candidate_id));

create table runtime.nexloop_evo_patch_sources (
 tenant_id text not null,world text not null,patch_id uuid not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 primary key(tenant_id,world,patch_id,consumer_id),
 foreign key(tenant_id,world,patch_id) references runtime.nexloop_evo_patches(tenant_id,world,patch_id) on delete cascade);
create index nexloop_evo_patch_sources_consumer on runtime.nexloop_evo_patch_sources(tenant_id,world,consumer_id);

create table runtime.nexloop_evo_candidate_events (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,event_id bigint generated always as identity,
 diagnosis_id uuid not null,candidate_id uuid,
 event_kind text not null check(event_kind in ('diagnosis_opened','diagnosis_closed','proposed','status_changed','budget_reserved','invalidated')),
 from_status text,to_status text,reason text check(reason is null or reason~'^[a-z_]{1,64}$'),
 detail jsonb not null default '{}'::jsonb check(jsonb_typeof(detail)='object' and octet_length(detail::text)<=4096),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,event_id));
create index nexloop_evo_candidate_events_candidate on runtime.nexloop_evo_candidate_events(tenant_id,world,candidate_id,event_id);

do $$declare t text;begin
 foreach t in array array['nexloop_evo_diagnoses','nexloop_evo_candidates','nexloop_evo_patches','nexloop_evo_patch_sources','nexloop_evo_candidate_events'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer',t);
 end loop;
end $$;
alter table runtime.nexloop_evo_diagnoses add constraint nexloop_evo_diagnoses_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id);

create function runtime.nexloop_evo_events_append_only() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception 'evolution candidate events are append-only' using errcode='42501';
end $$;
alter function runtime.nexloop_evo_events_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_events_append_only() from public;
create trigger nx038_append_only before update or delete on runtime.nexloop_evo_candidate_events for each row execute function runtime.nexloop_evo_events_append_only();
create trigger nx038_no_truncate before truncate on runtime.nexloop_evo_candidate_events for each statement execute function runtime.nexloop_evo_events_append_only();

-- 4. Internal helpers ----------------------------------------------------------------------------------------------------------
-- The caller: session role nexloop_evo_writer and an active service credential without a Run context (never a browser token).
create function runtime.nexloop_evo_writer_tenant(p_digest text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;
begin
 if session_user is distinct from 'nexloop_evo_writer' then raise exception 'evolution write unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,'real');
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest) or ident->'binding'->>'tenant_id' is null then
  raise exception 'evolution write unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 return ident->'binding'->>'tenant_id';
end $$;
alter function runtime.nexloop_evo_writer_tenant(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_writer_tenant(text) from public;

create function runtime.nexloop_evo_event(p_tenant text,p_diagnosis uuid,p_candidate uuid,p_kind text,p_from text,p_to text,p_reason text,p_detail jsonb,p_by text)
 returns void language sql security definer set search_path=pg_catalog,pg_temp as $$
 insert into runtime.nexloop_evo_candidate_events(tenant_id,world,diagnosis_id,candidate_id,event_kind,from_status,to_status,reason,detail,recorded_by)
  values(p_tenant,'real',p_diagnosis,p_candidate,p_kind,p_from,p_to,p_reason,coalesce(p_detail,'{}'::jsonb),p_by);
$$;
alter function runtime.nexloop_evo_event(text,uuid,uuid,text,text,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_event(text,uuid,uuid,text,text,text,text,jsonb,text) from public;

-- Ends a candidate `incomplete`. From `proposed` for any legitimate reason; from `evaluating` or `accepted_for_release` only for
-- missing_data (an erasure, D17) or budget_exhausted. Terminal candidates are left as they are.
create function runtime.nexloop_evo_candidate_incomplete(p_tenant text,p_candidate uuid,p_reason text,p_kind text,p_detail jsonb,p_by text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c runtime.nexloop_evo_candidates;
begin
 select * into c from runtime.nexloop_evo_candidates where tenant_id=p_tenant and world='real' and candidate_id=p_candidate for update;
 if not found or not (c.status='proposed' or (c.status in ('evaluating','accepted_for_release') and p_reason in ('missing_data','budget_exhausted'))) then
  return false;end if;
 update runtime.nexloop_evo_candidates set status='incomplete',end_reason=p_reason,updated_at=clock_timestamp()
  where tenant_id=p_tenant and world='real' and candidate_id=p_candidate;
 perform runtime.nexloop_evo_event(p_tenant,c.diagnosis_id,p_candidate,p_kind,c.status,'incomplete',p_reason,p_detail,p_by);
 return true;
end $$;
alter function runtime.nexloop_evo_candidate_incomplete(text,uuid,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_candidate_incomplete(text,uuid,text,text,jsonb,text) from public;

create function runtime.nexloop_evo_diagnosis_incomplete(p_tenant text,p_diagnosis uuid,p_reason text,p_kind text,p_detail jsonb,p_by text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 update runtime.nexloop_evo_diagnoses set status='incomplete',end_reason=p_reason,closed_at=clock_timestamp()
  where tenant_id=p_tenant and world='real' and diagnosis_id=p_diagnosis and status='open';
 if not found then return false;end if;
 perform runtime.nexloop_evo_event(p_tenant,p_diagnosis,null,p_kind,'open','incomplete',p_reason,p_detail,p_by);
 return true;
end $$;
alter function runtime.nexloop_evo_diagnosis_incomplete(text,uuid,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_diagnosis_incomplete(text,uuid,text,text,jsonb,text) from public;

create function runtime.nexloop_evo_candidate_wire(c runtime.nexloop_evo_candidates) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('schema_version','1.0','candidate_id',c.candidate_id,'tenant_id',c.tenant_id,'parent_revision',c.parent_revision,
  'candidate_revision',c.candidate_revision,'dimension',c.dimension,'primary_hypothesis',c.primary_hypothesis,
  'patch_artifact_ref','evolution-patch:'||c.patch_id,'input_snapshot_digest',c.input_snapshot_digest,
  'design_case_ids',to_jsonb(c.design_case_ids),'validation_case_ids',to_jsonb(c.validation_case_ids),'evaluator_version',c.evaluator_version,
  'model_profile',c.model_profile,'maximum_rounds',c.maximum_rounds,'allowed_change_scope',to_jsonb(c.allowed_change_scope),'status',c.status,
  'publication_permit_ref',null,'created_at',to_char(c.created_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
$$;
alter function runtime.nexloop_evo_candidate_wire(runtime.nexloop_evo_candidates) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_candidate_wire(runtime.nexloop_evo_candidates) from public;

-- 5. Diagnosis: open (freeze) and close ---------------------------------------------------------------------------------------
create function authz.nexloop_evo_diagnosis_open(p_digest text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);b jsonb:=p_body;d runtime.nexloop_evo_diagnoses;v_runs uuid[];v_consumers text[];
 v_unit text;v_limit numeric;v_known integer;v_now timestamptz;
begin
 if jsonb_typeof(b) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(b) k)
   is distinct from array['attribution','consumer_ids','diagnosis_id','evaluator_version','input_snapshot_digest','model_profile','parent_revision','run_ids']
  or coalesce(b->>'diagnosis_id','')!~'^[0-9a-f-]{36}$' or jsonb_typeof(b->'run_ids') is distinct from 'array' or jsonb_typeof(b->'consumer_ids') is distinct from 'array'
  or jsonb_typeof(b->'attribution') is distinct from 'object'
  or exists(select 1 from jsonb_array_elements(b->'run_ids') r where coalesce(r#>>'{}','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  or exists(select 1 from jsonb_each(b->'attribution') a where jsonb_typeof(a.value)<>'number' or a.key!~'^[a-z_]{1,64}$') then
  raise exception 'evolution diagnosis invalid' using errcode='22023';end if;
 v_runs:=array(select distinct (r#>>'{}')::uuid from jsonb_array_elements(b->'run_ids') r);
 v_consumers:=array(select distinct c#>>'{}' from jsonb_array_elements(b->'consumer_ids') c order by 1);
 select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=(b->>'diagnosis_id')::uuid;
 if found then
  if d.input_snapshot_digest<>b->>'input_snapshot_digest' then raise exception 'evolution diagnosis id reused' using errcode='22023';end if;
  return jsonb_build_object('diagnosis_id',d.diagnosis_id,'status',d.status,'deadline',d.deadline,'replayed',true);
 end if;
 -- Every Run is one this tenant's service credentials issued (the E2 trace function's own rule), world real.
 select count(*) into v_known from unnest(v_runs) x(run_id) where exists(select 1 from authz.nexloop_run_credentials r
   join authz.nexloop_service_credentials sc on sc.token_digest=r.source_digest and sc.tenant_id=v_tenant where r.run_id=x.run_id and r.world='real');
 if v_known<>cardinality(v_runs) then raise exception 'evolution diagnosis names a Run of another tenant' using errcode='42501';end if;
 -- §2.4: the snapshot holds no erasing or erased Consumer.
 if exists(select 1 from unnest(v_consumers) c where runtime.nexloop_consumer_erasing(v_tenant,'real',c)) then
  raise exception 'evolution diagnosis names an erased Consumer' using errcode='22023';end if;
 select unit,limit_amount into v_unit,v_limit from control.nexloop_budget_limits where tenant_id=v_tenant and world='real' and budget_kind='evolution';
 if not found then raise exception 'budget evolution not configured' using errcode='NXB03';end if;
 v_now:=clock_timestamp();  -- one reading for both ends of the 30-minute window (the check compares them)
 insert into runtime.nexloop_evo_diagnoses(tenant_id,world,diagnosis_id,input_snapshot_digest,parent_revision,evaluator_version,model_profile,run_ids,consumer_ids,
   attribution,cost_limit,unit,opened_at,deadline,status)
  values(v_tenant,'real',(b->>'diagnosis_id')::uuid,b->>'input_snapshot_digest',b->>'parent_revision',b->>'evaluator_version',b->>'model_profile',v_runs,v_consumers,
   b->'attribution',20,v_unit,v_now,v_now+interval '30 minutes','open') returning * into d;
 perform runtime.nexloop_evo_event(v_tenant,d.diagnosis_id,null,'diagnosis_opened',null,'open',null,
  jsonb_build_object('runs',cardinality(v_runs),'consumers',cardinality(v_consumers),'attribution',d.attribution),'service:evolution');
 return jsonb_build_object('diagnosis_id',d.diagnosis_id,'status',d.status,'deadline',d.deadline,'replayed',false);
end $$;
alter function authz.nexloop_evo_diagnosis_open(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_diagnosis_open(text,jsonb) from public;
grant execute on function authz.nexloop_evo_diagnosis_open(text,jsonb) to nexloop_evo_writer;

create function authz.nexloop_evo_diagnosis_close(p_digest text,p_diagnosis uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);d runtime.nexloop_evo_diagnoses;
begin
 update runtime.nexloop_evo_diagnoses set status='closed',closed_at=clock_timestamp()
  where tenant_id=v_tenant and world='real' and diagnosis_id=p_diagnosis and status='open' returning * into d;
 if found then
  perform runtime.nexloop_evo_event(v_tenant,p_diagnosis,null,'diagnosis_closed','open','closed',null,
   jsonb_build_object('candidates',(select count(*) from runtime.nexloop_evo_candidates c where c.tenant_id=v_tenant and c.world='real' and c.diagnosis_id=p_diagnosis)),
   'service:evolution');
 else
  select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=p_diagnosis;
  if not found then raise exception 'evolution diagnosis unknown' using errcode='22023';end if;
 end if;
 return jsonb_build_object('diagnosis_id',d.diagnosis_id,'status',d.status,'end_reason',d.end_reason);
end $$;
alter function authz.nexloop_evo_diagnosis_close(text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_diagnosis_close(text,uuid) from public;
grant execute on function authz.nexloop_evo_diagnosis_close(text,uuid) to nexloop_evo_writer;

-- 6. Candidate: propose, read, stop --------------------------------------------------------------------------------------------
-- AT-024: a scope names only object types, properties or vocabulary values; never an Action, an authorization fact, a grant or a
-- tool. AT-048: no cluster on both sides. AT-023: only the candidate tables are written.
create function authz.nexloop_evo_candidate_propose(p_digest text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);b jsonb:=p_body;c jsonb;p jsonb;d runtime.nexloop_evo_diagnoses;x runtime.nexloop_evo_candidates;
 v_design text[];v_validation text[];v_scope text[];v_targets text[];v_terms text[];v_patch_runs text[];v_sources text[];v_runs text[];v_patch uuid;
 v_cost numeric;
begin
 c:=b->'candidate';p:=b->'patch';
 if jsonb_typeof(b) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(b) k)
   is distinct from array['candidate','case_groups','category','diagnosis_id','patch','regroups','source_consumer_ids']
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object' or jsonb_typeof(b->'case_groups') is distinct from 'object'
  or jsonb_typeof(b->'source_consumer_ids') is distinct from 'array' or jsonb_typeof(b->'regroups') is distinct from 'number'
  or coalesce(b->>'diagnosis_id','')!~'^[0-9a-f-]{36}$' or coalesce(c->>'candidate_id','')!~'^[0-9a-f-]{36}$'
  or c->>'schema_version' is distinct from '1.0' or c->'publication_permit_ref' is distinct from 'null'::jsonb or c->>'status' is distinct from 'proposed'
  or jsonb_typeof(c->'maximum_rounds') is distinct from 'number' or jsonb_typeof(c->'design_case_ids') is distinct from 'array'
  or jsonb_typeof(c->'validation_case_ids') is distinct from 'array' or jsonb_typeof(c->'allowed_change_scope') is distinct from 'array' then
  raise exception 'evolution candidate invalid' using errcode='22023';end if;
 if c->>'dimension' is distinct from 'semantic_content' then raise exception 'evolution dimension % not open',c->>'dimension' using errcode='22023';end if;  -- D19
 if (c->>'maximum_rounds')::numeric not in (1,2,3) then raise exception 'evolution candidate round budget over 3' using errcode='22023';end if;  -- D20
 select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=(b->>'diagnosis_id')::uuid for update;
 if not found then raise exception 'evolution diagnosis unknown' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=(c->>'candidate_id')::uuid;
 if found then
  if x.diagnosis_id<>d.diagnosis_id then raise exception 'evolution candidate id reused' using errcode='22023';end if;
  return jsonb_build_object('proposed',true,'replayed',true,'candidate',runtime.nexloop_evo_candidate_wire(x));
 end if;
 if d.status<>'open' then raise exception 'evolution diagnosis %',d.status using errcode='22023';end if;
 if clock_timestamp()>=d.deadline then  -- §12.2: the diagnosis wall clock is spent
  perform runtime.nexloop_evo_diagnosis_incomplete(v_tenant,d.diagnosis_id,'budget_exhausted','status_changed',jsonb_build_object('tier','diagnosis_time'),'service:evolution');
  return jsonb_build_object('proposed',false,'reason','budget_exhausted','tier','diagnosis_time');
 end if;
 if c->>'tenant_id' is distinct from v_tenant or c->>'input_snapshot_digest' is distinct from d.input_snapshot_digest
  or c->>'parent_revision' is distinct from d.parent_revision or c->>'evaluator_version' is distinct from d.evaluator_version
  or c->>'model_profile' is distinct from d.model_profile then
  raise exception 'evolution candidate differs from its frozen diagnosis' using errcode='22023';end if;
 v_design:=array(select e#>>'{}' from jsonb_array_elements(c->'design_case_ids') e);
 v_validation:=array(select e#>>'{}' from jsonb_array_elements(c->'validation_case_ids') e);
 v_scope:=array(select e#>>'{}' from jsonb_array_elements(c->'allowed_change_scope') e);
 v_runs:=array(select r::text from unnest(d.run_ids) r);
 if cardinality(v_design) not between 1 and 256 or cardinality(v_validation) not between 1 and 256 or cardinality(v_scope) not between 1 and 256
  or (select count(distinct e) from unnest(v_design||v_validation) e)<>cardinality(v_design)+cardinality(v_validation)
  or not (v_design||v_validation)<@v_runs then
  raise exception 'evolution candidate cases are not the diagnosis Runs' using errcode='22023';end if;
 if exists(select 1 from unnest(v_scope) s where s!~'^(eios:object_type:|eios:property:|nexloop:vocabulary:)[^[:cntrl:][:space:]]+$' or char_length(s)>512) then
  raise exception 'evolution change scope not allowed' using errcode='42501';end if;  -- AT-024
 -- AT-048: every case has a cluster, and no cluster is on both sides.
 if (select array_agg(k order by k) from jsonb_object_keys(b->'case_groups') k) is distinct from (select array_agg(e order by e) from unnest(v_design||v_validation) e)
  or exists(select 1 from jsonb_each(b->'case_groups') g where jsonb_typeof(g.value)<>'string' or g.value#>>'{}'!~'^[A-Za-z0-9:._-]{1,190}$') then
  raise exception 'evolution case groups invalid' using errcode='22023';end if;
 if exists(select 1 from unnest(v_design) a join unnest(v_validation) v on (b->'case_groups'->>a)=(b->'case_groups'->>v)) then
  raise exception 'evolution validation leak: a cluster is on both sides' using errcode='22023';end if;
 -- The patch: ids, hashes and refs only; no free text, no Action, grant or tool.
 if (select array_agg(k order by k) from jsonb_object_keys(p) k) is distinct from array['change_kind','run_ids','target_refs','term_hashes']
  or p->>'change_kind' not in ('alias','description') or p->>'change_kind' is null
  or jsonb_typeof(p->'target_refs') is distinct from 'array' or jsonb_typeof(p->'term_hashes') is distinct from 'array' or jsonb_typeof(p->'run_ids') is distinct from 'array' then
  raise exception 'evolution patch invalid' using errcode='22023';end if;
 v_targets:=array(select e#>>'{}' from jsonb_array_elements(p->'target_refs') e);
 v_terms:=array(select e#>>'{}' from jsonb_array_elements(p->'term_hashes') e);
 v_patch_runs:=array(select e#>>'{}' from jsonb_array_elements(p->'run_ids') e);
 if cardinality(v_targets)<1 or not v_targets<@v_scope or cardinality(v_terms)>16 or array_to_string(v_terms,',')!~'^([0-9a-f]{64}(,[0-9a-f]{64})*)?$'
  or cardinality(v_patch_runs)<1 or not v_patch_runs<@v_runs then
  raise exception 'evolution patch invalid' using errcode='22023';end if;
 v_sources:=array(select distinct e#>>'{}' from jsonb_array_elements(b->'source_consumer_ids') e order by 1);
 if not v_sources<@d.consumer_ids then raise exception 'evolution patch source is not in the diagnosis' using errcode='22023';end if;
 if exists(select 1 from unnest(v_sources) s where runtime.nexloop_consumer_erasing(v_tenant,'real',s)) then
  raise exception 'evolution patch source erased' using errcode='22023';end if;
 v_patch:=gen_random_uuid();
 v_cost:=least(10,d.cost_limit);
 insert into runtime.nexloop_evo_patches(tenant_id,world,patch_id,candidate_id,body,body_digest)
  values(v_tenant,'real',v_patch,(c->>'candidate_id')::uuid,p,encode(sha256(convert_to(p::text,'UTF8')),'hex'));
 insert into runtime.nexloop_evo_patch_sources(tenant_id,world,patch_id,consumer_id) select v_tenant,'real',v_patch,s from unnest(v_sources) s;
 insert into runtime.nexloop_evo_candidates(tenant_id,world,candidate_id,diagnosis_id,category,dimension,parent_revision,candidate_revision,primary_hypothesis,patch_id,
   input_snapshot_digest,design_case_ids,validation_case_ids,case_groups,regroups,evaluator_version,model_profile,maximum_rounds,allowed_change_scope,status,cost_limit,unit)
  values(v_tenant,'real',(c->>'candidate_id')::uuid,d.diagnosis_id,b->>'category','semantic_content',d.parent_revision,c->>'candidate_revision',c->>'primary_hypothesis',v_patch,
   d.input_snapshot_digest,v_design,v_validation,b->'case_groups',(b->>'regroups')::integer,d.evaluator_version,d.model_profile,(c->>'maximum_rounds')::integer,v_scope,
   'proposed',v_cost,d.unit) returning * into x;
 perform runtime.nexloop_evo_event(v_tenant,d.diagnosis_id,x.candidate_id,'proposed',null,'proposed',null,
  jsonb_build_object('category',x.category,'design',cardinality(v_design),'validation',cardinality(v_validation),'regroups',x.regroups,'sources',cardinality(v_sources)),
  'service:evolution');
 return jsonb_build_object('proposed',true,'replayed',false,'candidate',runtime.nexloop_evo_candidate_wire(x));
end $$;
alter function authz.nexloop_evo_candidate_propose(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_candidate_propose(text,jsonb) from public;
grant execute on function authz.nexloop_evo_candidate_propose(text,jsonb) to nexloop_evo_writer;

create function authz.nexloop_evo_candidate_read(p_digest text,p_candidate uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);x runtime.nexloop_evo_candidates;
begin
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if not found then return null;end if;
 return jsonb_build_object('candidate',runtime.nexloop_evo_candidate_wire(x),'diagnosis_id',x.diagnosis_id,'category',x.category,'end_reason',x.end_reason,
  'case_groups',x.case_groups,'regroups',x.regroups,'cost_limit',x.cost_limit::text,'unit',x.unit,
  'patch_present',exists(select 1 from runtime.nexloop_evo_patches p where p.tenant_id=v_tenant and p.world='real' and p.patch_id=x.patch_id));
end $$;
alter function authz.nexloop_evo_candidate_read(text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_candidate_read(text,uuid) from public;
grant execute on function authz.nexloop_evo_candidate_read(text,uuid) to nexloop_evo_writer;

create function authz.nexloop_evo_candidate_stop(p_digest text,p_candidate uuid,p_reason text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);x runtime.nexloop_evo_candidates;
begin
 if p_reason not in ('user_interrupted','missing_data','external_block','budget_exhausted') or p_reason is null then
  raise exception 'evolution stop reason invalid' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
 if x.status<>'proposed' then raise exception 'evolution candidate %: NX-038 stops only proposed candidates',x.status using errcode='22023';end if;
 perform runtime.nexloop_evo_candidate_incomplete(v_tenant,p_candidate,p_reason,'status_changed','{}'::jsonb,'service:evolution');
 return jsonb_build_object('candidate_id',p_candidate,'status','incomplete','end_reason',p_reason);
end $$;
alter function authz.nexloop_evo_candidate_stop(text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_candidate_stop(text,uuid,text) from public;
grant execute on function authz.nexloop_evo_candidate_stop(text,uuid,text) to nexloop_evo_writer;

-- 7. Three-tier budget (§12.2, D20) --------------------------------------------------------------------------------------------
-- p_scope 'diagnosis' or 'candidate'. Checked in order: the record is still open; the diagnosis deadline; the diagnosis or
-- candidate cap; the tenant's monthly `evolution` limit (its row lock serializes concurrent reservations, 0068). A spent tier
-- ends the diagnosis or candidate `incomplete/budget_exhausted` and returns reserved=false; nothing is consumed. The same
-- consumption id replays its result.
create function authz.nexloop_evo_reserve(p_digest text,p_scope text,p_id uuid,p_consumption_id text,p_amount text,p_unit text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);d runtime.nexloop_evo_diagnoses;x runtime.nexloop_evo_candidates;
 lim control.nexloop_budget_limits%rowtype;old control.nexloop_budget_consumption%rowtype;v_amount numeric;v_source text;v_cap numeric;v_used numeric;v_month numeric;
 v_tier text;
begin
 if p_scope not in ('diagnosis','candidate') or p_scope is null or coalesce(p_consumption_id,'')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
  raise exception 'evolution reservation invalid' using errcode='22023';end if;
 v_amount:=control.nexloop_nx022_decimal(p_amount);
 if v_amount<=0 then raise exception 'budget consumption must be positive' using errcode='22023';end if;
 v_source:='evolution-'||p_scope||':'||p_id;
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world='real' and budget_kind='evolution' for update;
 if not found then raise exception 'budget evolution not configured' using errcode='NXB03';end if;
 select * into old from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution' and consumption_id=p_consumption_id;
 if found then
  if old.amount<>v_amount or old.unit<>p_unit or old.source_ref<>v_source then raise exception 'budget consumption id reused' using errcode='NXB02';end if;
  return jsonb_build_object('reserved',true,'replayed',true,'consumption_id',p_consumption_id);
 end if;
 if p_unit is distinct from lim.unit then raise exception 'budget unit mismatch' using errcode='22023';end if;
 if p_scope='candidate' then
  select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_id for update;
  if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
  if x.status not in ('proposed','evaluating') then raise exception 'evolution candidate %',x.status using errcode='NXB01';end if;
  v_cap:=x.cost_limit;
 else
  select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=p_id for update;
  if not found then raise exception 'evolution diagnosis unknown' using errcode='22023';end if;
  if d.status<>'open' then raise exception 'evolution diagnosis %',d.status using errcode='NXB01';end if;
  v_cap:=d.cost_limit;
  if clock_timestamp()>=d.deadline then v_tier:='diagnosis_time';end if;
 end if;
 if v_tier is null then
  select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution' and source_ref=v_source;
  if v_used+v_amount>v_cap then v_tier:=p_scope;end if;
 end if;
 if v_tier is null then
  if clock_timestamp()<lim.period_start or clock_timestamp()>=lim.period_end then v_tier:='tenant_period';
  else
   select coalesce(sum(amount),0) into v_month from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution'
    and recorded_at>=lim.period_start and recorded_at<lim.period_end;
   if v_month+v_amount>lim.limit_amount then v_tier:='tenant_month';end if;
  end if;
 end if;
 if v_tier is not null then
  if p_scope='candidate' then
   perform runtime.nexloop_evo_candidate_incomplete(v_tenant,p_id,'budget_exhausted','status_changed',jsonb_build_object('tier',v_tier),'service:evolution');
  else
   perform runtime.nexloop_evo_diagnosis_incomplete(v_tenant,p_id,'budget_exhausted','status_changed',jsonb_build_object('tier',v_tier),'service:evolution');
  end if;
  return jsonb_build_object('reserved',false,'reason','budget_exhausted','tier',v_tier);
 end if;
 insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
  values(v_tenant,'real','evolution',p_consumption_id,v_amount,p_unit,v_source,'service:evolution');
 perform runtime.nexloop_evo_event(v_tenant,coalesce(d.diagnosis_id,x.diagnosis_id),x.candidate_id,'budget_reserved',null,null,null,
  jsonb_build_object('scope',p_scope,'amount',v_amount::text,'unit',p_unit),'service:evolution');
 return jsonb_build_object('reserved',true,'replayed',false,'consumption_id',p_consumption_id,'scope_remaining',(v_cap-v_used-v_amount)::text,
  'tenant_remaining',(lim.limit_amount-v_month-v_amount)::text);
end $$;
alter function authz.nexloop_evo_reserve(text,text,uuid,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_reserve(text,text,uuid,text,text,text) from public;
grant execute on function authz.nexloop_evo_reserve(text,text,uuid,text,text,text) to nexloop_evo_writer;

-- 8. Erasure (§12.5, D17) ------------------------------------------------------------------------------------------------------
create function runtime.nexloop_evo_invalidate_consumer(p_tenant text,p_world text,p_consumer text,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_patches integer:=0;v_candidates integer:=0;v_diagnoses integer:=0;r record;
begin
 if p_world<>'real' then return '{}'::jsonb;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Patches that came from the Consumer: deleted; their candidates end incomplete/missing_data.
 for r in select c.candidate_id,p.patch_id from runtime.nexloop_evo_patch_sources s
   join runtime.nexloop_evo_patches p on p.tenant_id=s.tenant_id and p.world=s.world and p.patch_id=s.patch_id
   join runtime.nexloop_evo_candidates c on c.tenant_id=p.tenant_id and c.world=p.world and c.candidate_id=p.candidate_id
   where s.tenant_id=p_tenant and s.world='real' and s.consumer_id=p_consumer loop
  delete from runtime.nexloop_evo_patches where tenant_id=p_tenant and world='real' and patch_id=r.patch_id;
  v_patches:=v_patches+1;
  if runtime.nexloop_evo_candidate_incomplete(p_tenant,r.candidate_id,'missing_data','invalidated',jsonb_build_object('cause','patch_source_erased'),p_by) then
   v_candidates:=v_candidates+1;end if;
 end loop;
 -- Frozen snapshots that hold the Consumer: the diagnosis and its open candidates end incomplete/missing_data.
 for r in select diagnosis_id from runtime.nexloop_evo_diagnoses where tenant_id=p_tenant and world='real' and p_consumer=any(consumer_ids) loop
  if runtime.nexloop_evo_diagnosis_incomplete(p_tenant,r.diagnosis_id,'missing_data','invalidated',jsonb_build_object('cause','snapshot_subject_erased'),p_by) then
   v_diagnoses:=v_diagnoses+1;end if;
  v_candidates:=v_candidates+(select count(*) from runtime.nexloop_evo_candidates c where c.tenant_id=p_tenant and c.world='real' and c.diagnosis_id=r.diagnosis_id
   and runtime.nexloop_evo_candidate_incomplete(p_tenant,c.candidate_id,'missing_data','invalidated',jsonb_build_object('cause','snapshot_subject_erased'),p_by));
 end loop;
 if v_patches+v_candidates+v_diagnoses>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',p_ref,'evolution_candidate','consumer',p_consumer,'deleted',
   jsonb_build_object('patches',v_patches,'candidates_incomplete',v_candidates,'diagnoses_incomplete',v_diagnoses),p_by);
 end if;
 return jsonb_build_object('patches',v_patches,'candidates_incomplete',v_candidates,'diagnoses_incomplete',v_diagnoses);
end $$;
alter function runtime.nexloop_evo_invalidate_consumer(text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_invalidate_consumer(text,text,text,text,text) from public;

create function control.nexloop_evo_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.state in ('purging','completed') and (tg_op='INSERT' or old.state is distinct from new.state) then
  perform runtime.nexloop_evo_invalidate_consumer(new.tenant_id,new.world,new.consumer_id,'erasure:'||new.request_id,'service:retention_keeper');
 end if;
 return null;
end $$;
alter function control.nexloop_evo_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_evo_erasure() from public;
create trigger nx038_evo_erasure after insert or update of state on control.nexloop_consumer_erasures
 for each row execute function control.nexloop_evo_erasure();

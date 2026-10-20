-- NX-039 V3 "L1 test world" (design docs/implementation/NX-039-design.md §3.1, §3.3, §3.4, §5.2, §5.3, §6.1, §11 V3; owner D39-2, D39-4,
-- D39-6, NX-038 D15, D16, D18, D20). Temporary number; the dispatcher renumbers at merge.
--
-- 1. L1 sessions. The evaluator opens one session per evaluating candidate whose frozen bundle allows L1, with a registered
--    evaluation credential (NX-038 E5: issued for world test alone) that holds no Action grant at all (D18: no effect authority, so
--    neither real dispatch nor an effect credential is reachable; the real-dispatch switch is closed for any world but real).
--    Only semantic_content is opened (D39-6, the open-dimension row). The session's deadline is 30 minutes after the evaluation was
--    opened, L0 and L1 together (D39-4).
-- 2. The definition snapshot. EIOS object type definitions are per tenant, not per world, so the snapshot copies what the test-world
--    Run reads of the candidate's target refs: each ref's current schema version and the hashes of its published aliases (real
--    world), plus the candidate's overlay term hashes (source overlay). The copy lives in runtime.nexloop_evo_l1_definitions, keyed
--    by session; closing the session deletes it (no owner or role may delete it otherwise) and records the counts. A sweep closes the
--    sessions past their deadline. Nothing here writes a definition, an alias, a business object or a real-world record.
-- 3. Budget (D15, D20): authz.nexloop_evo_l1_reserve counts each L1 model run against the candidate's cap and the tenant's monthly
--    `evolution` limit (budget kind evolution, source evolution-candidate:<id>); a spent tier ends the candidate
--    incomplete/budget_exhausted, a passed deadline incomplete/evaluator_timeout; nothing is reserved then.
-- 4. Conclude (0242 body, replaced in place) also accepts `incomplete` with reason evaluator_timeout (end reason evaluator_timeout).
-- Published migrations are not edited.

alter table runtime.nexloop_evo_candidates drop constraint nexloop_evo_candidates_end_reason_check;
alter table runtime.nexloop_evo_candidates add constraint nexloop_evo_candidates_end_reason_check
 check(end_reason in ('budget_exhausted','missing_data','user_interrupted','external_block','evaluator_error','evaluator_timeout'));
alter table runtime.nexloop_evo_candidate_events drop constraint nexloop_evo_candidate_events_event_kind_check;
alter table runtime.nexloop_evo_candidate_events add constraint nexloop_evo_candidate_events_event_kind_check check(event_kind in
 ('diagnosis_opened','diagnosis_closed','proposed','status_changed','budget_reserved','invalidated','bundle_frozen','report_recorded','review_linked',
  'evaluation_opened','evaluation_concluded','l1_opened','l1_closed'));

-- 1-2. Sessions and the definition snapshot -------------------------------------------------------------------------------------
create table runtime.nexloop_evo_l1_sessions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),session_id uuid not null,
 candidate_id uuid not null,credential_digest text not null check(credential_digest~'^[0-9a-f]{64}$'),
 opened_at timestamptz not null default clock_timestamp(),deadline timestamptz not null,
 status text not null check(status in ('open','closed')),close_reason text check(close_reason in ('done','timeout','candidate_ended','swept')),
 copied integer not null default 0 check(copied>=0),removed integer,closed_at timestamptz,
 primary key(tenant_id,world,session_id),unique(tenant_id,world,candidate_id),
 foreign key(tenant_id,world,candidate_id) references runtime.nexloop_evo_bundles(tenant_id,world,candidate_id),
 check((status='closed')=(closed_at is not null)),check(deadline<=opened_at+interval '30 minutes'));
create table runtime.nexloop_evo_l1_definitions (
 tenant_id text not null,world text not null check(world='real'),session_id uuid not null,ref text not null check(char_length(ref) between 1 and 512),
 source text not null check(source in ('parent','overlay')),schema_version integer,
 alias_hashes text[] not null default '{}' check(array_to_string(alias_hashes,',')~'^([0-9a-f]{64}(,[0-9a-f]{64})*)?$'),
 primary key(tenant_id,world,session_id,ref,source),
 foreign key(tenant_id,world,session_id) references runtime.nexloop_evo_l1_sessions(tenant_id,world,session_id));
create function runtime.nexloop_evo_l1_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and current_user='nexloop_owner' and current_setting('nexloop.evo_l1_cleanup',true)='on' then return old;end if;
 raise exception 'L1 definition snapshots leave only when their session closes' using errcode='42501';
end $$;
alter function runtime.nexloop_evo_l1_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_l1_guard() from public;
create trigger nx039_l1_guard before update or delete on runtime.nexloop_evo_l1_definitions for each row execute function runtime.nexloop_evo_l1_guard();
create trigger nx039_l1_no_truncate before truncate on runtime.nexloop_evo_l1_definitions for each statement execute function runtime.nexloop_evo_l1_guard();
do $$declare t text;begin
 foreach t in array array['nexloop_evo_l1_sessions','nexloop_evo_l1_definitions'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator',t);
 end loop;
end $$;

-- The type a definition ref names (eios:object_type:T, eios:property:T/p, nexloop:vocabulary:T/p/v).
create function runtime.nexloop_evo_ref_type(p_ref text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_ref ~ '^eios:object_type:[A-Za-z][A-Za-z0-9_]{0,63}$' then substring(p_ref from 18)
  when p_ref ~ '^eios:property:[A-Za-z][A-Za-z0-9_]{0,63}/' then split_part(substring(p_ref from 15),'/',1)
  when p_ref ~ '^nexloop:vocabulary:[A-Za-z][A-Za-z0-9_]{0,63}/' then split_part(substring(p_ref from 20),'/',1) end
$$;
alter function runtime.nexloop_evo_ref_type(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_ref_type(text) from public;

create function runtime.nexloop_evo_l1_cleanup(p_tenant text,p_session uuid,p_reason text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n integer;s runtime.nexloop_evo_l1_sessions;x runtime.nexloop_evo_candidates;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into s from runtime.nexloop_evo_l1_sessions where tenant_id=p_tenant and world='real' and session_id=p_session for update;
 if not found then return null;end if;
 if s.status='closed' then return s.removed;end if;
 perform set_config('nexloop.evo_l1_cleanup','on',true);
 delete from runtime.nexloop_evo_l1_definitions where tenant_id=p_tenant and world='real' and session_id=p_session;
 get diagnostics n=row_count;
 perform set_config('nexloop.evo_l1_cleanup','',true);
 update runtime.nexloop_evo_l1_sessions set status='closed',close_reason=p_reason,removed=n,closed_at=clock_timestamp()
  where tenant_id=p_tenant and world='real' and session_id=p_session;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=p_tenant and world='real' and candidate_id=s.candidate_id;
 perform runtime.nexloop_evo_event(p_tenant,x.diagnosis_id,s.candidate_id,'l1_closed',null,null,null,
  jsonb_build_object('session_id',p_session,'reason',p_reason,'removed',n),'service:evaluator');
 return n;
end $$;
alter function runtime.nexloop_evo_l1_cleanup(text,uuid,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_l1_cleanup(text,uuid,text) from public;

-- A passed deadline: the candidate ends incomplete/evaluator_timeout (if still evaluating) and the session closes.
create function runtime.nexloop_evo_l1_timeout(p_tenant text,p_session uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s runtime.nexloop_evo_l1_sessions;x runtime.nexloop_evo_candidates;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into s from runtime.nexloop_evo_l1_sessions where tenant_id=p_tenant and world='real' and session_id=p_session;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=p_tenant and world='real' and candidate_id=s.candidate_id for update;
 if x.status='evaluating' then
  update runtime.nexloop_evo_candidates set status='incomplete',end_reason='evaluator_timeout',updated_at=clock_timestamp()
   where tenant_id=p_tenant and world='real' and candidate_id=x.candidate_id;
  perform runtime.nexloop_evo_event(p_tenant,x.diagnosis_id,x.candidate_id,'status_changed','evaluating','incomplete','evaluator_timeout',
   jsonb_build_object('session_id',p_session,'deadline',s.deadline),'service:evaluator');
 end if;
 perform runtime.nexloop_evo_l1_cleanup(p_tenant,p_session,'timeout');
end $$;
alter function runtime.nexloop_evo_l1_timeout(text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_l1_timeout(text,uuid) from public;

create function authz.nexloop_evo_l1_open(p_digest text,p_candidate uuid,p_credential text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';x runtime.nexloop_evo_candidates;
 bd runtime.nexloop_evo_bundles;p runtime.nexloop_evo_patches;s runtime.nexloop_evo_l1_sessions;cred authz.nexloop_service_credentials;
 v_opened timestamptz;v_session uuid;v_ref text;n integer:=0;
begin
 if coalesce(p_credential,'')!~'^[0-9a-f]{64}$' or p_candidate is null then raise exception 'L1 open invalid' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=p_candidate for update;
 if not found then raise exception 'evolution candidate unknown' using errcode='22023';end if;
 select * into s from runtime.nexloop_evo_l1_sessions where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if found then
  if s.credential_digest<>p_credential then raise exception 'L1 session exists with another credential' using errcode='22023';end if;
  return jsonb_build_object('opened',s.status='open','replayed',true,'session_id',s.session_id,'deadline',s.deadline,'copied',s.copied,'status',s.status);
 end if;
 if x.status<>'evaluating' then raise exception 'L1 open: the candidate is not evaluating' using errcode='22023';end if;
 -- D39-6: semantic_content only (the audited open-dimension row).
 if x.dimension<>'semantic_content' or not exists(select 1 from control.nexloop_evo_open_dimensions o where o.dimension=x.dimension) then
  raise exception 'L1 open: dimension closed' using errcode='42501';end if;
 select * into bd from runtime.nexloop_evo_bundles where tenant_id=v_tenant and world='real' and candidate_id=p_candidate;
 if not found or not (bd.bundle->'levels') ? 'L1' then raise exception 'L1 open: the frozen bundle does not allow L1' using errcode='22023';end if;
 -- D18: a registered evaluation credential of this tenant, issued for world test alone, holding no Action grant.
 select c.* into cred from authz.nexloop_service_credentials c join authz.nexloop_evo_evaluation_credentials e on e.token_digest=c.token_digest
  where c.token_digest=p_credential and e.tenant_id=v_tenant and c.tenant_id=v_tenant;
 if not found or cred.worlds is distinct from array['test'] then raise exception 'L1 open: not a registered test-only evaluation credential' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_authority_facts f where f.tenant_id=v_tenant and f.fact_kind='grants'
   and f.entity_key && array[cred.binding->>'principal_id',cred.binding->>'subject_principal_id'] and f.payload::text like '%eios:action:%') then
  raise exception 'L1 open: the evaluation credential holds an Action grant' using errcode='42501';end if;
 select min(e.recorded_at) into v_opened from runtime.nexloop_evo_candidate_events e where e.tenant_id=v_tenant and e.world='real'
  and e.candidate_id=p_candidate and e.event_kind='evaluation_opened';
 if v_opened is null or clock_timestamp()>=v_opened+interval '30 minutes' then
  raise exception 'L1 open: the evaluation is past its 30-minute deadline' using errcode='22023';end if;
 v_session:=gen_random_uuid();
 insert into runtime.nexloop_evo_l1_sessions(tenant_id,world,session_id,candidate_id,credential_digest,status,deadline)
  values(v_tenant,'real',v_session,p_candidate,p_credential,'open',least(v_opened+interval '30 minutes',clock_timestamp()+interval '30 minutes'));
 select * into p from runtime.nexloop_evo_patches where tenant_id=v_tenant and world='real' and patch_id=x.patch_id;
 for v_ref in select distinct value#>>'{}' from jsonb_array_elements(coalesce(p.body->'target_refs','[]'::jsonb)) loop
  insert into runtime.nexloop_evo_l1_definitions(tenant_id,world,session_id,ref,source,schema_version,alias_hashes)
   values(v_tenant,'real',v_session,v_ref,'parent',
    (select max(v.version) from ontology.object_type_versions v where v.tenant_id=v_tenant and v.type_name=runtime.nexloop_evo_ref_type(v_ref)),
    array(select distinct encode(sha256(convert_to(a.alias_text,'UTF8')),'hex') from ontology.nexloop_definition_aliases a
      where a.tenant_id=v_tenant and a.world='real' and a.canonical_ref=v_ref order by 1));
  insert into runtime.nexloop_evo_l1_definitions(tenant_id,world,session_id,ref,source,schema_version,alias_hashes)
   values(v_tenant,'real',v_session,v_ref,'overlay',null,
    array(select distinct h#>>'{}' from jsonb_array_elements(coalesce(p.body->'term_hashes','[]'::jsonb)) h order by 1));
  n:=n+2;
 end loop;
 update runtime.nexloop_evo_l1_sessions set copied=n where tenant_id=v_tenant and world='real' and session_id=v_session returning * into s;
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,p_candidate,'l1_opened',null,null,null,
  jsonb_build_object('session_id',v_session,'copied',n,'deadline',s.deadline),'service:evaluator');
 return jsonb_build_object('opened',true,'replayed',false,'session_id',v_session,'deadline',s.deadline,'copied',n,'status','open');
end $$;
alter function authz.nexloop_evo_l1_open(text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_l1_open(text,uuid,text) from public;
grant execute on function authz.nexloop_evo_l1_open(text,uuid,text) to nexloop_evo_evaluator;

-- The snapshot the test-world run reads (refs, versions, hashes only).
create function authz.nexloop_evo_l1_definitions(p_digest text,p_session uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';
begin
 if not exists(select 1 from runtime.nexloop_evo_l1_sessions where tenant_id=v_tenant and world='real' and session_id=p_session and status='open') then
  return null;end if;
 return coalesce((select jsonb_agg(jsonb_build_object('ref',d.ref,'source',d.source,'schema_version',d.schema_version,'alias_hashes',to_jsonb(d.alias_hashes))
   order by d.ref,d.source) from runtime.nexloop_evo_l1_definitions d where d.tenant_id=v_tenant and d.world='real' and d.session_id=p_session),'[]'::jsonb);
end $$;
alter function authz.nexloop_evo_l1_definitions(text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_l1_definitions(text,uuid) from public;
grant execute on function authz.nexloop_evo_l1_definitions(text,uuid) to nexloop_evo_evaluator;

-- 3. Budget and wall clock --------------------------------------------------------------------------------------------------------
create function authz.nexloop_evo_l1_reserve(p_digest text,p_session uuid,p_consumption_id text,p_amount text,p_unit text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';s runtime.nexloop_evo_l1_sessions;
 x runtime.nexloop_evo_candidates;lim control.nexloop_budget_limits%rowtype;old control.nexloop_budget_consumption%rowtype;
 v_amount numeric;v_source text;v_used numeric;v_month numeric;v_tier text;
begin
 if coalesce(p_consumption_id,'')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then raise exception 'L1 reservation invalid' using errcode='22023';end if;
 v_amount:=control.nexloop_nx022_decimal(p_amount);
 if v_amount<=0 then raise exception 'budget consumption must be positive' using errcode='22023';end if;
 select * into s from runtime.nexloop_evo_l1_sessions where tenant_id=v_tenant and world='real' and session_id=p_session for update;
 if not found then raise exception 'L1 session unknown' using errcode='22023';end if;
 v_source:='evolution-candidate:'||s.candidate_id;
 select * into old from control.nexloop_budget_consumption where tenant_id=v_tenant and world='real' and budget_kind='evolution' and consumption_id=p_consumption_id;
 if found then
  if old.amount<>v_amount or old.unit<>p_unit or old.source_ref<>v_source then raise exception 'budget consumption id reused' using errcode='NXB02';end if;
  return jsonb_build_object('reserved',true,'replayed',true,'consumption_id',p_consumption_id);
 end if;
 if s.status<>'open' then return jsonb_build_object('reserved',false,'reason','session_closed');end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=s.candidate_id for update;
 if x.status<>'evaluating' then
  perform runtime.nexloop_evo_l1_cleanup(v_tenant,p_session,'candidate_ended');
  return jsonb_build_object('reserved',false,'reason','candidate_ended','status',x.status);end if;
 -- D39-4: L0 and L1 together within 30 minutes of the opening.
 if clock_timestamp()>=s.deadline then
  perform runtime.nexloop_evo_l1_timeout(v_tenant,p_session);
  return jsonb_build_object('reserved',false,'reason','evaluator_timeout');end if;
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
  perform runtime.nexloop_evo_candidate_incomplete(v_tenant,s.candidate_id,'budget_exhausted','status_changed',jsonb_build_object('tier',v_tier,'level','L1'),'service:evaluator');
  perform runtime.nexloop_evo_l1_cleanup(v_tenant,p_session,'candidate_ended');
  return jsonb_build_object('reserved',false,'reason','budget_exhausted','tier',v_tier);
 end if;
 insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
  values(v_tenant,'real','evolution',p_consumption_id,v_amount,p_unit,v_source,'service:evaluator');
 perform runtime.nexloop_evo_event(v_tenant,x.diagnosis_id,x.candidate_id,'budget_reserved',null,null,null,
  jsonb_build_object('scope','candidate','level','L1','amount',v_amount::text,'unit',p_unit),'service:evaluator');
 return jsonb_build_object('reserved',true,'replayed',false,'consumption_id',p_consumption_id,'candidate_remaining',(x.cost_limit-v_used-v_amount)::text);
end $$;
alter function authz.nexloop_evo_l1_reserve(text,uuid,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_l1_reserve(text,uuid,text,text,text) from public;
grant execute on function authz.nexloop_evo_l1_reserve(text,uuid,text,text,text) to nexloop_evo_evaluator;

create function authz.nexloop_evo_l1_close(p_digest text,p_session uuid,p_reason text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';s runtime.nexloop_evo_l1_sessions;n integer;
begin
 if p_reason not in ('done','timeout') or p_reason is null then raise exception 'L1 close invalid' using errcode='22023';end if;
 select * into s from runtime.nexloop_evo_l1_sessions where tenant_id=v_tenant and world='real' and session_id=p_session;
 if not found then raise exception 'L1 session unknown' using errcode='22023';end if;
 -- A timeout is the database's call: only a passed deadline ends the candidate as evaluator_timeout.
 if p_reason='timeout' and s.status='open' then
  if clock_timestamp()<s.deadline then raise exception 'L1 close: the deadline has not passed' using errcode='22023';end if;
  perform runtime.nexloop_evo_l1_timeout(v_tenant,p_session);
 else
  perform runtime.nexloop_evo_l1_cleanup(v_tenant,p_session,p_reason);
 end if;
 select * into s from runtime.nexloop_evo_l1_sessions where tenant_id=v_tenant and world='real' and session_id=p_session;
 return jsonb_build_object('session_id',p_session,'status',s.status,'close_reason',s.close_reason,'removed',s.removed,
  'remaining',(select count(*) from runtime.nexloop_evo_l1_definitions d where d.tenant_id=v_tenant and d.world='real' and d.session_id=p_session));
end $$;
alter function authz.nexloop_evo_l1_close(text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_l1_close(text,uuid,text) from public;
grant execute on function authz.nexloop_evo_l1_close(text,uuid,text) to nexloop_evo_evaluator;

-- Sessions past their deadline (a crashed evaluator): closed, their candidates incomplete/evaluator_timeout, snapshots removed.
create function authz.nexloop_evo_l1_sweep(p_digest text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare who jsonb:=runtime.nexloop_evo_evaluator_identity(p_digest);v_tenant text:=who->>'tenant_id';r record;n integer:=0;
begin
 for r in select session_id from runtime.nexloop_evo_l1_sessions where tenant_id=v_tenant and world='real' and status='open' and deadline<=clock_timestamp()
   order by deadline limit 100 loop
  perform runtime.nexloop_evo_l1_timeout(v_tenant,r.session_id);n:=n+1;
 end loop;
 return jsonb_build_object('swept',n);
end $$;
alter function authz.nexloop_evo_l1_sweep(text) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_l1_sweep(text) from public;
grant execute on function authz.nexloop_evo_l1_sweep(text) to nexloop_evo_evaluator;

-- 4. Conclude (0242 body; the only change: incomplete may also be evaluator_timeout) -------------------------------------------------
create or replace function authz.nexloop_evo_evaluation_conclude(p_digest text,p_report jsonb) returns jsonb
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
 if r->>'conclusion'='incomplete' and r->>'reason' not in ('missing_data','budget_exhausted','not_reproducible','evaluator_timeout') then
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

alter function authz.nexloop_evo_evaluation_conclude(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_evo_evaluation_conclude(text,jsonb) from public;
grant execute on function authz.nexloop_evo_evaluation_conclude(text,jsonb) to nexloop_evo_evaluator;

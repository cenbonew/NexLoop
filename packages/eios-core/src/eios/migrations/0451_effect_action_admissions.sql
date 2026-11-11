-- Option B M1 (ADR-046 §3; docs/planning-drafts/b-effect-action-follows-schema-version-20261111.md §2.1, §2.2, §3.2, §4 row 2a; L6
-- conditional PASS B2/B5/B6, owner 2026-11-11 B1, H1 (a), H2). Temporary number; the dispatcher renumbers. Additive only: nothing
-- reads the new table or column yet except the functions below, A' (0446/0449) stays, no authority is granted or written.
-- 1. control.nexloop_effect_action_admissions (B2): the tenant-level admission of an effect Action successor :N for one Consumer
--    version. The baseline :1 stays admitted by the global allow-list (control.nexloop_effect_actions, 0178), so the table holds
--    successors only (version>1) and a tenant created later needs no seed. Append-only: a retirement sets retired_at once.
-- 2. control.nexloop_effect_contexts.action_resource: the exact effect Action resource of a context; existing rows and, until the
--    bind command selects it (M2), every new row take the allow-list resource of the context's slot (a trigger; not null in M4).
-- 3. authz.nexloop_effect_action_version: the effect Action version for a Consumer version - the one admitted candidate (allow-list
--    :1 or an active admission) whose active definition's scope holds Consumer:v exactly; none or more than one is refused, never the
--    highest. Called by the bind from M2 on.
-- 4. control.nexloop_admit_effect_action_successor / control.nexloop_retire_effect_action_admission: trusted configuration only. The
--    admission asks authz.nexloop_effect_successor_admissible (0449, N1: the single judgement shared with A', the gate G1 and the
--    smoke S3) and writes the admission row only when it holds; any refusal writes nothing.

-- 1. Admissions ---------------------------------------------------------------------------------------------------------------
create table control.nexloop_effect_action_admissions (
 admission_id uuid primary key default gen_random_uuid(),
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 stable_name text not null references control.nexloop_effect_actions(stable_name),
 version integer not null check(version between 2 and 1000),
 consumer_version integer not null check(consumer_version>1),
 resource_id text generated always as ('eios:action:'||stable_name||':'||version) stored,
 predecessor_resource text not null check(predecessor_resource ~ '^eios:action:[a-z0-9_.]+:[1-9][0-9]*$'),
 publication_id uuid not null,
 admitted_by text not null check(admitted_by ~ '^human:[^[:space:]]{1,200}$'),
 judgement jsonb not null check(jsonb_typeof(judgement)='object' and judgement->>'admissible'='true'),
 admitted_at timestamptz not null default clock_timestamp(),
 retired_at timestamptz,retired_by text check(retired_by ~ '^human:[^[:space:]]{1,200}$'),
 retired_reason text check(char_length(retired_reason) between 1 and 500),
 check((retired_at is null)=(retired_by is null) and (retired_at is null)=(retired_reason is null))
);
-- Each Consumer version admits one effect Action version, and each version is admitted once (while active).
create unique index effect_action_admissions_consumer_version on control.nexloop_effect_action_admissions(tenant_id,world,stable_name,consumer_version) where retired_at is null;
create unique index effect_action_admissions_version on control.nexloop_effect_action_admissions(tenant_id,world,stable_name,version) where retired_at is null;
alter table control.nexloop_effect_action_admissions owner to nexloop_owner;
alter table control.nexloop_effect_action_admissions enable row level security;
alter table control.nexloop_effect_action_admissions force row level security;
create policy effect_action_admissions_tenant on control.nexloop_effect_action_admissions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_effect_action_admissions from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_runtime,
 nexloop_identity,nexloop_metrics,nexloop_configurator,nexloop_evo_writer;

create function control.nexloop_effect_action_admissions_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op<>'UPDATE' then raise exception 'effect Action admissions are append-only' using errcode='55000';end if;
 -- A retirement only: once, nothing else changes.
 if old.retired_at is not null or new.retired_at is null
  -- resource_id is generated (null in NEW before the row is stored) and follows the compared columns.
  or (to_jsonb(new)-'retired_at'-'retired_by'-'retired_reason'-'resource_id') is distinct from (to_jsonb(old)-'retired_at'-'retired_by'-'retired_reason'-'resource_id') then
  raise exception 'effect Action admissions are append-only' using errcode='55000';end if;
 return new;
end $$;
alter function control.nexloop_effect_action_admissions_append_only() owner to nexloop_owner;
revoke all on function control.nexloop_effect_action_admissions_append_only() from public;
create trigger effect_action_admissions_append_only before update or delete on control.nexloop_effect_action_admissions
 for each row execute function control.nexloop_effect_action_admissions_append_only();
create trigger effect_action_admissions_no_truncate before truncate on control.nexloop_effect_action_admissions
 for each statement execute function control.nexloop_effect_action_admissions_append_only();
-- NX-041 outbound guard: configuration of which effect Action version a Consumer version uses; ids, versions, the publication id and
-- a human label - no personal data, reaches nothing outside.
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('control.nexloop_effect_action_admissions',false,'tenant configuration of the admitted effect Action successor per Consumer version (option B); written by trusted configuration only, no external effect');

-- 2. The context's exact effect Action resource ----------------------------------------------------------------------------------
alter table control.nexloop_effect_contexts add column action_resource text;
update control.nexloop_effect_contexts c set action_resource=ea.resource_id from control.nexloop_effect_actions ea where ea.slot_identity=c.slot_identity;
create function control.nexloop_effect_context_action_resource() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='INSERT' and new.action_resource is null then
  select ea.resource_id into new.action_resource from control.nexloop_effect_actions ea where ea.slot_identity=new.slot_identity;
 elsif tg_op='UPDATE' and new.action_resource is distinct from old.action_resource then
  raise exception 'effect context identity immutable' using errcode='22023';
 end if;
 return new;
end $$;
alter function control.nexloop_effect_context_action_resource() owner to nexloop_owner;
revoke all on function control.nexloop_effect_context_action_resource() from public;
create trigger effect_context_action_resource before insert or update on control.nexloop_effect_contexts
 for each row execute function control.nexloop_effect_context_action_resource();
-- Nullable until M4: fixtures that write contexts with triggers off (session_replication_role=replica) leave it null; the bind writes
-- it explicitly from M2 on and the A' removal (M4) sets not null after asserting every unexpired context carries it.
alter table control.nexloop_effect_contexts
 add constraint effect_context_action_resource check(action_resource ~ '^eios:action:[a-z0-9_.]+:[1-9][0-9]*$');

-- 3. The effect Action version for a Consumer version ----------------------------------------------------------------------------
create function authz.nexloop_effect_action_version(p_tenant text,p_world text,p_slot text,p_consumer_version integer) returns integer
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n integer;v integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with candidates as (
  select ea.stable_name,ea.version from control.nexloop_effect_actions ea where ea.slot_identity=p_slot
  union all
  select a.stable_name,a.version from control.nexloop_effect_action_admissions a join control.nexloop_effect_actions ea on ea.stable_name=a.stable_name
   where ea.slot_identity=p_slot and a.tenant_id=p_tenant and a.world=p_world and a.retired_at is null)
 select count(*),min(c.version) into n,v from candidates c join control.nexloop_action_definitions d
   on d.tenant_id=p_tenant and d.world=p_world and d.active and d.resource_id='eios:action:'||c.stable_name||':'||c.version
  where exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') r
   where r->>'tenant_id'=p_tenant and r->>'stable_name'='Consumer' and (r->>'version')::integer=p_consumer_version);
 if n<>1 then raise exception 'effect Action version unavailable' using errcode='42501';end if;
 return v;
end $$;
alter function authz.nexloop_effect_action_version(text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_action_version(text,text,text,integer) from public;

-- 4. Admission and retirement (trusted configuration) ----------------------------------------------------------------------------
-- p_publication: the human F3 publication of Consumer v whose successor_actions name :p_version. The predecessor is the admitted
-- version of the highest Consumer version below v (the allow-list :1 for v1). Returns {admitted, changed, code, ...}.
create function control.nexloop_admit_effect_action_successor(p_tenant text,p_publication uuid,p_stable_name text,p_version integer,p_actor text,p_dry_run boolean default false)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ea control.nexloop_effect_actions;p ontology.nexloop_schema_publications;pred_resource text;b integer;ex control.nexloop_action_definitions;
 d control.nexloop_action_definitions;tb jsonb;tv jsonb;r jsonb;cur control.nexloop_effect_action_admissions;res text;
begin
 if session_user is distinct from 'nexloop_configurator' then raise exception 'trusted configuration only' using errcode='42501';end if;
 if coalesce(p_actor,'') !~ '^human:[^[:space:]]{1,200}$' or p_tenant is null or p_publication is null or p_version is null then
  raise exception 'invalid effect Action admission' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into ea from control.nexloop_effect_actions where stable_name=p_stable_name;
 if ea.stable_name is null then return jsonb_build_object('admitted',false,'changed',false,'code','not_effect_action');end if;
 res:='eios:action:'||p_stable_name||':'||p_version;
 select * into p from ontology.nexloop_schema_publications where tenant_id=p_tenant and world='real' and publication_id=p_publication;
 if p.publication_id is null then return jsonb_build_object('admitted',false,'changed',false,'code','no_publication_record');end if;
 if p.kind<>'publish' or p.type_name<>'Consumer' then return jsonb_build_object('admitted',false,'changed',false,'code','publication_not_consumer');end if;
 if not res=any(p.successor_actions) then return jsonb_build_object('admitted',false,'changed',false,'code','successor_not_in_publication');end if;
 -- Idempotent: the same admission again is unchanged; another version for this Consumer version is refused.
 select * into cur from control.nexloop_effect_action_admissions where tenant_id=p_tenant and world='real' and stable_name=p_stable_name
  and consumer_version=p.version_after and retired_at is null;
 if cur.admission_id is not null then
  if cur.version=p_version then
   return jsonb_build_object('admitted',true,'changed',false,'code',null,'resource_id',res,'consumer_version',p.version_after,'admission_id',cur.admission_id);end if;
  return jsonb_build_object('admitted',false,'changed',false,'code','consumer_version_admitted','admitted_resource',cur.resource_id);
 end if;
 -- The predecessor: the admission of the highest lower Consumer version, else the allow-list :1.
 select a.resource_id,a.consumer_version into pred_resource,b from control.nexloop_effect_action_admissions a where a.tenant_id=p_tenant and a.world='real'
  and a.stable_name=p_stable_name and a.retired_at is null and a.consumer_version<p.version_after order by a.consumer_version desc limit 1;
 pred_resource:=coalesce(pred_resource,ea.resource_id);
 select * into ex from control.nexloop_action_definitions where tenant_id=p_tenant and world='real' and resource_id=pred_resource and active;
 if ex.resource_id is null then return jsonb_build_object('admitted',false,'changed',false,'code','predecessor_missing','predecessor',pred_resource);end if;
 select min((x->>'version')::integer) into b from jsonb_array_elements(ex.definition->'governance'->'change_scope'->'object_types') x
  where x->>'tenant_id'=p_tenant and x->>'stable_name'='Consumer';
 select definition into tb from ontology.object_type_versions where tenant_id=p_tenant and type_name='Consumer' and version=b;
 select definition into tv from ontology.object_type_versions where tenant_id=p_tenant and type_name='Consumer' and version=p.version_after;
 select * into d from control.nexloop_action_definitions where tenant_id=p_tenant and world='real' and resource_id=res;
 r:=authz.nexloop_effect_successor_admissible(p_tenant,'Consumer',
  jsonb_build_object('resource_id',ex.resource_id,'capability',ex.capability,'definition',ex.definition),
  case when d.resource_id is null then null else jsonb_build_object('resource_id',d.resource_id,'capability',d.capability,'definition',d.definition,'active',d.active) end,
  tb,tv,p.version_after,
  jsonb_build_object('published_by',p.published_by,'permission_digest_before',p.permission_digest_before,'permission_digest_after',p.permission_digest_after));
 if (r->>'admissible')::boolean is not true then
  return jsonb_build_object('admitted',false,'changed',false,'code',r->>'code','resource_id',res,'predecessor',pred_resource);end if;
 if p_dry_run then
  return jsonb_build_object('admitted',false,'changed',false,'code',null,'would_admit',true,'resource_id',res,'predecessor',pred_resource,'consumer_version',p.version_after);end if;
 insert into control.nexloop_effect_action_admissions(tenant_id,world,stable_name,version,consumer_version,predecessor_resource,publication_id,admitted_by,judgement)
  values(p_tenant,'real',p_stable_name,p_version,p.version_after,pred_resource,p.publication_id,p_actor,r) returning * into cur;
 return jsonb_build_object('admitted',true,'changed',true,'code',null,'resource_id',res,'predecessor',pred_resource,'consumer_version',p.version_after,
  'admission_id',cur.admission_id);
end $$;
alter function control.nexloop_admit_effect_action_successor(text,uuid,text,integer,text,boolean) owner to nexloop_owner;
revoke all on function control.nexloop_admit_effect_action_successor(text,uuid,text,integer,text,boolean) from public;
grant execute on function control.nexloop_admit_effect_action_successor(text,uuid,text,integer,text,boolean) to nexloop_configurator;

create function control.nexloop_retire_effect_action_admission(p_tenant text,p_stable_name text,p_version integer,p_actor text,p_reason text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_effect_action_admissions;
begin
 if session_user is distinct from 'nexloop_configurator' then raise exception 'trusted configuration only' using errcode='42501';end if;
 if coalesce(p_actor,'') !~ '^human:[^[:space:]]{1,200}$' or char_length(coalesce(p_reason,'')) not between 1 and 500 then
  raise exception 'invalid effect Action retirement' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_effect_action_admissions set retired_at=clock_timestamp(),retired_by=p_actor,retired_reason=p_reason
  where tenant_id=p_tenant and world='real' and stable_name=p_stable_name and version=p_version and retired_at is null returning * into cur;
 return jsonb_build_object('retired',cur.admission_id is not null,'admission_id',cur.admission_id);
end $$;
alter function control.nexloop_retire_effect_action_admission(text,text,integer,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_retire_effect_action_admission(text,text,integer,text,text) from public;
grant execute on function control.nexloop_retire_effect_action_admission(text,text,integer,text,text) to nexloop_configurator;
